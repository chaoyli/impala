# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
#
# The base class that should be used for almost all Impala tests

import grp
import logging
import os
import pprint
import pwd
import pytest
import re
import shutil
import subprocess
import tempfile
import time
from functools import wraps
from getpass import getuser
from random import choice
from subprocess import check_call

from tests.common.base_test_suite import BaseTestSuite
from tests.common.errors import Timeout
from tests.common.impala_connection import create_connection
from tests.common.impala_service import ImpaladService
from tests.common.test_dimensions import (
    ALL_BATCH_SIZES,
    ALL_CLUSTER_SIZES,
    ALL_DISABLE_CODEGEN_OPTIONS,
    ALL_NODES_ONLY,
    TableFormatInfo,
    create_exec_option_dimension,
    get_dataset_from_workload,
    load_table_info_dimension)
from tests.common.test_result_verifier import (
    apply_error_match_filter,
    try_compile_regex,
    verify_raw_results,
    verify_runtime_profile)
from tests.common.test_vector import ImpalaTestDimension
from tests.performance.query import Query
from tests.performance.query_exec_functions import execute_using_jdbc
from tests.performance.query_executor import JdbcQueryExecConfig
from tests.util.filesystem_utils import (
    IS_S3,
    IS_ABFS,
    IS_ADLS,
    S3_BUCKET_NAME,
    ADLS_STORE_NAME,
    FILESYSTEM_PREFIX)

from tests.util.hdfs_util import (
  HdfsConfig,
  get_hdfs_client,
  get_hdfs_client_from_conf,
  NAMENODE)
from tests.util.s3_util import S3Client
from tests.util.abfs_util import ABFSClient
from tests.util.test_file_parser import (
  QueryTestSectionReader,
  parse_query_test_file,
  write_test_file)
from tests.util.thrift_util import create_transport

# Imports required for Hive Metastore Client
from hive_metastore import ThriftHiveMetastore
from thrift.protocol import TBinaryProtocol

# Initializing the logger before conditional imports, since we will need it
# for them.
LOG = logging.getLogger('impala_test_suite')

# The ADLS python client isn't downloaded when ADLS isn't the target FS, so do a
# conditional import.
if IS_ADLS:
  try:
    from tests.util.adls_util import ADLSClient
  except ImportError:
    LOG.error("Need the ADLSClient for testing with ADLS")

IMPALAD_HOST_PORT_LIST = pytest.config.option.impalad.split(',')
assert len(IMPALAD_HOST_PORT_LIST) > 0, 'Must specify at least 1 impalad to target'
IMPALAD = IMPALAD_HOST_PORT_LIST[0]
IMPALAD_HS2_HOST_PORT =\
    IMPALAD.split(':')[0] + ":" + pytest.config.option.impalad_hs2_port
HIVE_HS2_HOST_PORT = pytest.config.option.hive_server2
WORKLOAD_DIR = os.environ['IMPALA_WORKLOAD_DIR']
HDFS_CONF = HdfsConfig(pytest.config.option.minicluster_xml_conf)
TARGET_FILESYSTEM = os.getenv("TARGET_FILESYSTEM") or "hdfs"
IMPALA_HOME = os.getenv("IMPALA_HOME")
EE_TEST_LOGS_DIR = os.getenv("IMPALA_EE_TEST_LOGS_DIR")
# Match any SET statement. Assume that query options' names
# only contain alphabets, underscores and digits after position 1.
# The statement may include SQL line comments starting with --, which we need to
# strip out. The test file parser already strips out comments starting with #.
COMMENT_LINES_REGEX = r'(?:\s*--.*\n)*'
SET_PATTERN = re.compile(
    COMMENT_LINES_REGEX + r'\s*set\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*=*', re.I)

# Base class for Impala tests. All impala test cases should inherit from this class
class ImpalaTestSuite(BaseTestSuite):
  @classmethod
  def add_test_dimensions(cls):
    """
    A hook for adding additional dimensions.

    By default load the table_info and exec_option dimensions, but if a test wants to
    add more dimensions or different dimensions they can override this function.
    """
    super(ImpalaTestSuite, cls).add_test_dimensions()
    cls.ImpalaTestMatrix.add_dimension(
        cls.create_table_info_dimension(cls.exploration_strategy()))
    cls.ImpalaTestMatrix.add_dimension(cls.__create_exec_option_dimension())
    # Execute tests through Beeswax by default. Individual tests that have been converted
    # to work with the HS2 client can add HS2 in addition to or instead of beeswax.
    cls.ImpalaTestMatrix.add_dimension(ImpalaTestDimension('protocol', 'beeswax'))

  @classmethod
  def setup_class(cls):
    """Setup section that runs before each test suite"""
    cls.hive_client, cls.client, cls.hs2_client = [None, None, None]
    # Create a Hive Metastore Client (used for executing some test SETUP steps
    metastore_host, metastore_port = pytest.config.option.metastore_server.split(':')
    trans_type = 'buffered'
    if pytest.config.option.use_kerberos:
      trans_type = 'kerberos'
    cls.hive_transport = create_transport(
        host=metastore_host,
        port=metastore_port,
        service=pytest.config.option.hive_service_name,
        transport_type=trans_type)
    protocol = TBinaryProtocol.TBinaryProtocol(cls.hive_transport)
    cls.hive_client = ThriftHiveMetastore.Client(protocol)
    cls.hive_transport.open()

    # Create a connection to Impala, self.client is Beeswax so that existing tests that
    # assume beeswax do not need modification (yet).
    cls.client = cls.create_impala_client(protocol='beeswax')
    try:
      cls.hs2_client = cls.create_impala_client(protocol='hs2')
    except Exception, e:
      # HS2 connection can fail for benign reasons, e.g. running with unsupported auth.
      LOG.info("HS2 connection setup failed, continuing...: {0}", e)


    # Default query options are populated on demand.
    cls.default_query_options = {}

    cls.impalad_test_service = cls.create_impala_service()
    cls.hdfs_client = cls.create_hdfs_client()
    cls.filesystem_client = cls.hdfs_client
    if IS_S3:
      cls.filesystem_client = S3Client(S3_BUCKET_NAME)
    elif IS_ABFS:
      cls.filesystem_client = ABFSClient()
    elif IS_ADLS:
      cls.filesystem_client = ADLSClient(ADLS_STORE_NAME)

    # Override the shell history path so that commands run by any tests
    # don't write any history into the developer's file.
    os.environ['IMPALA_HISTFILE'] = '/dev/null'

  @classmethod
  def teardown_class(cls):
    """Setup section that runs after each test suite"""
    # Cleanup the Impala and Hive Metastore client connections
    if cls.hive_transport:
      cls.hive_transport.close()
    if cls.client:
      cls.client.close()
    if cls.hs2_client:
      cls.hs2_client.close()

  @classmethod
  def create_impala_client(cls, host_port=None, protocol='beeswax'):
    if host_port is None:
      host_port = cls.__get_default_host_port(protocol)
    client = create_connection(host_port=host_port,
        use_kerberos=pytest.config.option.use_kerberos, protocol=protocol)
    client.connect()
    return client

  @classmethod
  def __get_default_host_port(cls, protocol):
    if protocol == 'beeswax':
      return IMPALAD
    else:
      assert protocol == 'hs2'
      return IMPALAD_HS2_HOST_PORT

  @classmethod
  def __get_cluster_host_ports(cls, protocol):
    """Return a list of host/port combinations for all impalads in the cluster."""
    if protocol == 'beeswax':
      return IMPALAD_HOST_PORT_LIST
    else:
      assert protocol == 'hs2'
      # TODO: support running tests against multiple coordinators for HS2. It should work,
      # we just need to update all test runners to pass in all host/port combinations for
      # the cluster and then handle it here.
      raise NotImplementedError(
          "Not yet implemented: only one HS2 host/port can be configured")

  @classmethod
  def create_impala_service(cls, host_port=IMPALAD, webserver_port=25000):
    host, port = host_port.split(':')
    return ImpaladService(host, beeswax_port=port, webserver_port=webserver_port)

  @classmethod
  def create_hdfs_client(cls):
    if pytest.config.option.namenode_http_address is None:
      hdfs_client = get_hdfs_client_from_conf(HDFS_CONF)
    else:
      host, port = pytest.config.option.namenode_http_address.split(":")
      hdfs_client =  get_hdfs_client(host, port)
    return hdfs_client

  @classmethod
  def all_db_names(cls):
    results = cls.client.execute("show databases").data
    # Extract first column - database name
    return [row.split("\t")[0] for row in results]

  @classmethod
  def cleanup_db(cls, db_name, sync_ddl=1):
    cls.client.execute("use default")
    cls.client.set_configuration({'sync_ddl': sync_ddl})
    cls.client.execute("drop database if exists `" + db_name + "` cascade")

  def __restore_query_options(self, query_options_changed, impalad_client):
    """
    Restore the list of modified query options to their default values.
    """
    # Populate the default query option if it's empty.
    if not self.default_query_options:
      query_options = impalad_client.get_default_configuration()
      for key, value in query_options.iteritems():
        self.default_query_options[key.upper()] = value
    # Restore all the changed query options.
    for query_option in query_options_changed:
      query_option = query_option.upper()
      if not query_option in self.default_query_options:
        continue
      default_val = self.default_query_options[query_option]
      query_str = 'SET ' + query_option + '="' + default_val + '"'
      try:
        impalad_client.execute(query_str)
      except Exception as e:
        LOG.info('Unexpected exception when executing ' + query_str + ' : ' + str(e))

  def get_impala_partition_info(self, table_name, *include_fields):
    """
    Find information about partitions of a table, as returned by a SHOW PARTITION
    statement. Return a list that contains one tuple for each partition.

    If 'include_fields' is not specified, the tuples will contain all the fields returned
    by SHOW PARTITION. Otherwise, return only those fields whose names are listed in
    'include_fields'. Field names are compared case-insensitively.
    """
    exec_result = self.client.execute('show partitions %s' % table_name)
    fieldSchemas = exec_result.schema.fieldSchemas
    fields_dict = {}
    for idx, fs in enumerate(fieldSchemas):
      fields_dict[fs.name.lower()] = idx

    rows = exec_result.get_data().split('\n')
    rows.pop()
    fields_idx = []
    for fn in include_fields:
      fn = fn.lower()
      assert fn in fields_dict, 'Invalid field: %s' % fn
      fields_idx.append(fields_dict[fn])

    result = []
    for row in rows:
      fields = row.split('\t')
      if not fields_idx:
        result_fields = fields
      else:
        result_fields = []
        for i in fields_idx:
          result_fields.append(fields[i])
      result.append(tuple(result_fields))
    return result

  def __verify_exceptions(self, expected_strs, actual_str, use_db):
    """
    Verifies that at least one of the strings in 'expected_str' is either:
    * A row_regex: line that matches the actual exception string 'actual_str'
    * A substring of the actual exception string 'actual_str'.
    """
    actual_str = actual_str.replace('\n', '')
    for expected_str in expected_strs:
      # In error messages, some paths are always qualified and some are not.
      # So, allow both $NAMENODE and $FILESYSTEM_PREFIX to be used in CATCH.
      expected_str = expected_str.strip() \
          .replace('$FILESYSTEM_PREFIX', FILESYSTEM_PREFIX) \
          .replace('$NAMENODE', NAMENODE) \
          .replace('$IMPALA_HOME', IMPALA_HOME)
      if use_db: expected_str = expected_str.replace('$DATABASE', use_db)
      # Strip newlines so we can split error message into multiple lines
      expected_str = expected_str.replace('\n', '')
      expected_regex = try_compile_regex(expected_str)
      if expected_regex:
        if expected_regex.match(actual_str): return
      else:
        # Not a regex - check if expected substring is present in actual.
        if expected_str in actual_str: return
    assert False, 'Unexpected exception string. Expected: %s\nNot found in actual: %s' % \
      (expected_str, actual_str)

  def __verify_results_and_errors(self, vector, test_section, result, use_db):
    """Verifies that both results and error sections are as expected. Rewrites both
      by replacing $NAMENODE, $DATABASE and $IMPALA_HOME with their actual values, and
      optionally rewriting filenames with __HDFS_FILENAME__, to ensure that expected and
      actual values are easily compared.
    """
    replace_filenames_with_placeholder = True
    for section_name in ('RESULTS', 'DBAPI_RESULTS', 'ERRORS'):
      if section_name in test_section:
        if "$NAMENODE" in test_section[section_name]:
          replace_filenames_with_placeholder = False
        test_section[section_name] = test_section[section_name] \
                                     .replace('$NAMENODE', NAMENODE) \
                                     .replace('$IMPALA_HOME', IMPALA_HOME) \
                                     .replace('$USER', getuser())
        if use_db:
          test_section[section_name] = test_section[section_name].replace('$DATABASE', use_db)
    result_section, type_section = 'RESULTS', 'TYPES'
    if vector.get_value('protocol') == 'hs2':
      if 'DBAPI_RESULTS' in test_section:
        assert 'RESULTS' in test_section,\
            "Base RESULTS section must always be included alongside DBAPI_RESULTS"
        # In some cases Impyla (the HS2 dbapi client) is expected to return different
        # results, so use the dbapi-specific section if present.
        result_section = 'DBAPI_RESULTS'
      if 'HS2_TYPES' in test_section:
        assert 'TYPES' in test_section,\
            "Base TYPES section must always be included alongside HS2_TYPES"
        # In some cases HS2 types are expected differ from Beeswax types (e.g. see
        # IMPALA-914), so use the HS2-specific section if present.
        type_section = 'HS2_TYPES'
    verify_raw_results(test_section, result, vector.get_value('table_format').file_format,
                       result_section, type_section, pytest.config.option.update_results,
                       replace_filenames_with_placeholder)


  def run_test_case(self, test_file_name, vector, use_db=None, multiple_impalad=False,
      encoding=None, test_file_vars=None):
    """
    Runs the queries in the specified test based on the vector values

    Runs the query using targeting the file format/compression specified in the test
    vector and the exec options specified in the test vector. If multiple_impalad=True
    a connection to a random impalad will be chosen to execute each test section.
    Otherwise, the default impalad client will be used. If 'protocol' (either 'hs2' or
    'beeswax') is set in the vector, a client for that protocol is used. Otherwise we
    use the default: beeswax.

    Additionally, the encoding for all test data can be specified using the 'encoding'
    parameter. This is useful when data is ingested in a different encoding (ex.
    latin). If not set, the default system encoding will be used.
    If a dict 'test_file_vars' is provided, then all keys will be replaced with their
    values in queries before they are executed. Callers need to avoid using reserved key
    names, see 'reserved_keywords' below.
    """
    table_format_info = vector.get_value('table_format')
    exec_options = vector.get_value('exec_option')
    protocol = vector.get_value('protocol')

    # Resolve the current user's primary group name.
    group_id = pwd.getpwnam(getuser()).pw_gid
    group_name = grp.getgrgid(group_id).gr_name

    target_impalad_clients = list()
    if multiple_impalad:
      target_impalad_clients =\
          [ImpalaTestSuite.create_impala_client(host_port, protocol=protocol)
           for host_port in self.__get_cluster_host_ports(protocol)]
    else:
      if protocol == 'beeswax':
        target_impalad_clients = [self.client]
      else:
        assert protocol == 'hs2'
        target_impalad_clients = [self.hs2_client]

    # Change the database to reflect the file_format, compression codec etc, or the
    # user specified database for all targeted impalad.
    for impalad_client in target_impalad_clients:
      ImpalaTestSuite.change_database(impalad_client,
          table_format_info, use_db, pytest.config.option.scale_factor)
      impalad_client.set_configuration(exec_options)

    sections = self.load_query_test_file(self.get_workload(), test_file_name,
        encoding=encoding)
    for test_section in sections:
      if 'SHELL' in test_section:
        assert len(test_section) == 1, \
          "SHELL test sections can't contain other sections"
        cmd = test_section['SHELL']\
          .replace('$FILESYSTEM_PREFIX', FILESYSTEM_PREFIX)\
          .replace('$IMPALA_HOME', IMPALA_HOME)
        if use_db: cmd = cmd.replace('$DATABASE', use_db)
        LOG.info("Shell command: " + cmd)
        check_call(cmd, shell=True)
        continue

      if 'QUERY' not in test_section:
        assert 0, 'Error in test file %s. Test cases require a -- QUERY section.\n%s' %\
            (test_file_name, pprint.pformat(test_section))

      if 'SETUP' in test_section:
        self.execute_test_case_setup(test_section['SETUP'], table_format_info)

      # TODO: support running query tests against different scale factors
      query = QueryTestSectionReader.build_query(test_section['QUERY']
          .replace('$GROUP_NAME', group_name)
          .replace('$IMPALA_HOME', IMPALA_HOME)
          .replace('$FILESYSTEM_PREFIX', FILESYSTEM_PREFIX)
          .replace('$SECONDARY_FILESYSTEM', os.getenv("SECONDARY_FILESYSTEM") or str())
          .replace('$USER', getuser()))
      if use_db: query = query.replace('$DATABASE', use_db)

      reserved_keywords = ["$DATABASE", "$FILESYSTEM_PREFIX", "$GROUP_NAME",
                           "$IMPALA_HOME", "$NAMENODE", "$QUERY", "$SECONDARY_FILESYSTEM",
                           "$USER"]

      if test_file_vars:
        for key, value in test_file_vars.iteritems():
          if key in reserved_keywords:
            raise RuntimeError("Key {0} is reserved".format(key))
          query = query.replace(key, value)

      if 'QUERY_NAME' in test_section:
        LOG.info('Query Name: \n%s\n' % test_section['QUERY_NAME'])

      # Support running multiple queries within the same test section, only verifying the
      # result of the final query. The main use case is to allow for 'USE database'
      # statements before a query executes, but it is not limited to that.
      # TODO: consider supporting result verification of all queries in the future
      result = None
      target_impalad_client = choice(target_impalad_clients)
      query_options_changed = []
      try:
        user = None
        if 'USER' in test_section:
          # Create a new client so the session will use the new username.
          user = test_section['USER'].strip()
          target_impalad_client = self.create_impala_client(protocol=protocol)
        for query in query.split(';'):
          set_pattern_match = SET_PATTERN.match(query)
          if set_pattern_match != None:
            query_options_changed.append(set_pattern_match.groups()[0])
            assert set_pattern_match.groups()[0] not in vector.get_value("exec_option"), \
                "%s cannot be set in  the '.test' file since it is in the test vector. " \
                "Consider deepcopy()-ing the vector and removing this option in the " \
                "python test." % set_pattern_match.groups()[0]
          result = self.__execute_query(target_impalad_client, query, user=user)
      except Exception as e:
        if 'CATCH' in test_section:
          self.__verify_exceptions(test_section['CATCH'], str(e), use_db)
          continue
        raise
      finally:
        if len(query_options_changed) > 0:
          self.__restore_query_options(query_options_changed, target_impalad_client)

      if 'CATCH' in test_section and '__NO_ERROR__' not in test_section['CATCH']:
        expected_str = " or ".join(test_section['CATCH']).strip() \
          .replace('$FILESYSTEM_PREFIX', FILESYSTEM_PREFIX) \
          .replace('$NAMENODE', NAMENODE) \
          .replace('$IMPALA_HOME', IMPALA_HOME)
        assert False, "Expected exception: %s" % expected_str

      assert result is not None
      assert result.success

      # Decode the results read back if the data is stored with a specific encoding.
      if encoding: result.data = [row.decode(encoding) for row in result.data]
      # Replace $NAMENODE in the expected results with the actual namenode URI.
      if 'RESULTS' in test_section:
        # Combining 'RESULTS' with 'DML_RESULTS" is currently unsupported because
        # __verify_results_and_errors calls verify_raw_results which always checks
        # ERRORS, TYPES, LABELS, etc. which doesn't make sense if there are two
        # different result sets to consider (IMPALA-4471).
        assert 'DML_RESULTS' not in test_section
        self.__verify_results_and_errors(vector, test_section, result, use_db)
      else:
        # TODO: Can't validate errors without expected results for now.
        assert 'ERRORS' not in test_section,\
          "'ERRORS' sections must have accompanying 'RESULTS' sections"
      # If --update_results, then replace references to the namenode URI with $NAMENODE.
      if pytest.config.option.update_results and 'RESULTS' in test_section:
        test_section['RESULTS'] = test_section['RESULTS'] \
            .replace(NAMENODE, '$NAMENODE') \
            .replace('$IMPALA_HOME', IMPALA_HOME)
      rt_profile_info = None
      if 'RUNTIME_PROFILE_%s' % table_format_info.file_format in test_section:
        # If this table format has a RUNTIME_PROFILE section specifically for it, evaluate
        # that section and ignore any general RUNTIME_PROFILE sections.
        rt_profile_info = 'RUNTIME_PROFILE_%s' % table_format_info.file_format
      elif 'RUNTIME_PROFILE' in test_section:
        rt_profile_info = 'RUNTIME_PROFILE'

      if rt_profile_info is not None:
        rt_profile = verify_runtime_profile(test_section[rt_profile_info],
                               result.runtime_profile,
                               update_section=pytest.config.option.update_results)
        if pytest.config.option.update_results:
          test_section[rt_profile_info] = "".join(rt_profile)

      if 'DML_RESULTS' in test_section:
        assert 'ERRORS' not in test_section
        # The limit is specified to ensure the queries aren't unbounded. We shouldn't have
        # test files that are checking the contents of tables larger than that anyways.
        dml_results_query = "select * from %s limit 1000" % \
            test_section['DML_RESULTS_TABLE']
        dml_result = self.__execute_query(target_impalad_client, dml_results_query)
        verify_raw_results(test_section, dml_result,
            vector.get_value('table_format').file_format, result_section='DML_RESULTS',
            update_section=pytest.config.option.update_results)
    if pytest.config.option.update_results:
      output_file = os.path.join(EE_TEST_LOGS_DIR,
                                 test_file_name.replace('/','_') + ".test")
      write_test_file(output_file, sections, encoding=encoding)

  def execute_test_case_setup(self, setup_section, table_format):
    """
    Executes a test case 'SETUP' section

    The test case 'SETUP' section is mainly used for insert tests. These tests need to
    have some actions performed before each test case to ensure the target tables are
    empty. The current supported setup actions:
    RESET <table name> - Drop and recreate the table
    DROP PARTITIONS <table name> - Drop all partitions from the table
    """
    setup_section = QueryTestSectionReader.build_query(setup_section)
    for row in setup_section.split('\n'):
      row = row.lstrip()
      if row.startswith('RESET'):
        db_name, table_name = QueryTestSectionReader.get_table_name_components(\
          table_format, row.split('RESET')[1])
        self.__reset_table(db_name, table_name)
        self.client.execute("invalidate metadata " + db_name + "." + table_name)
      elif row.startswith('DROP PARTITIONS'):
        db_name, table_name = QueryTestSectionReader.get_table_name_components(\
          table_format, row.split('DROP PARTITIONS')[1])
        self.__drop_partitions(db_name, table_name)
        self.client.execute("invalidate metadata " + db_name + "." + table_name)
      else:
        assert False, 'Unsupported setup command: %s' % row

  @classmethod
  def change_database(cls, impala_client, table_format=None,
      db_name=None, scale_factor=None):
    if db_name == None:
      assert table_format != None
      db_name = QueryTestSectionReader.get_db_name(table_format,
          scale_factor if scale_factor else '')
    query = 'use %s' % db_name
    # Clear the exec_options before executing a USE statement.
    # The USE statement should not fail for negative exec_option tests.
    impala_client.clear_configuration()
    impala_client.execute(query)

  def execute_wrapper(function):
    """
    Issues a use database query before executing queries.

    Database names are dependent on the input format for table, which the table names
    remaining the same. A use database is issued before query execution. As such,
    database names need to be build pre execution, this method wraps around the different
    execute methods and provides a common interface to issue the proper use command.
    """
    @wraps(function)
    def wrapper(*args, **kwargs):
      table_format = None
      if kwargs.get('table_format'):
        table_format = kwargs.get('table_format')
        del kwargs['table_format']
      if kwargs.get('vector'):
        table_format = kwargs.get('vector').get_value('table_format')
        del kwargs['vector']
        # self is the implicit first argument
      if table_format is not None:
        args[0].change_database(args[0].client, table_format)
      return function(*args, **kwargs)
    return wrapper

  @classmethod
  @execute_wrapper
  def execute_query_expect_success(cls, impalad_client, query, query_options=None,
      user=None):
    """Executes a query and asserts if the query fails"""
    result = cls.__execute_query(impalad_client, query, query_options, user)
    assert result.success
    return result

  @execute_wrapper
  def execute_query_expect_failure(self, impalad_client, query, query_options=None,
      user=None):
    """Executes a query and asserts if the query succeeds"""
    result = None
    try:
      result = self.__execute_query(impalad_client, query, query_options, user)
    except Exception, e:
      return e

    assert not result.success, "No failure encountered for query %s" % query
    return result

  @execute_wrapper
  def execute_query_unchecked(self, impalad_client, query, query_options=None, user=None):
    return self.__execute_query(impalad_client, query, query_options, user)

  @execute_wrapper
  def execute_query(self, query, query_options=None):
    return self.__execute_query(self.client, query, query_options)

  def execute_query_using_client(self, client, query, vector):
    self.change_database(client, vector.get_value('table_format'))
    return client.execute(query)

  def execute_query_async_using_client(self, client, query, vector):
    self.change_database(client, vector.get_value('table_format'))
    return client.execute_async(query)

  def close_query_using_client(self, client, query):
    return client.close_query(query)

  @execute_wrapper
  def execute_query_async(self, query, query_options=None):
    self.client.set_configuration(query_options)
    return self.client.execute_async(query)

  @execute_wrapper
  def close_query(self, query):
    return self.client.close_query(query)

  @execute_wrapper
  def execute_scalar(self, query, query_options=None):
    result = self.__execute_query(self.client, query, query_options)
    assert len(result.data) <= 1, 'Multiple values returned from scalar'
    return result.data[0] if len(result.data) == 1 else None

  def exec_and_compare_hive_and_impala_hs2(self, stmt, compare = lambda x, y: x == y):
    """Compare Hive and Impala results when executing the same statment over HS2"""
    # execute_using_jdbc expects a Query object. Convert the query string into a Query
    # object
    query = Query()
    query.query_str = stmt
    # Run the statement targeting Hive
    exec_opts = JdbcQueryExecConfig(impalad=HIVE_HS2_HOST_PORT, transport='SASL')
    hive_results = execute_using_jdbc(query, exec_opts).data

    # Run the statement targeting Impala
    exec_opts = JdbcQueryExecConfig(impalad=IMPALAD_HS2_HOST_PORT, transport='NOSASL')
    impala_results = execute_using_jdbc(query, exec_opts).data

    # Compare the results
    assert (impala_results is not None) and (hive_results is not None)
    assert compare(impala_results, hive_results)

  def load_query_test_file(self, workload, file_name, valid_section_names=None,
      encoding=None):
    """
    Loads/Reads the specified query test file. Accepts the given section names as valid.
    Uses a default list of valid section names if valid_section_names is None.
    """
    test_file_path = os.path.join(WORKLOAD_DIR, workload, 'queries', file_name + '.test')
    if not os.path.isfile(test_file_path):
      assert False, 'Test file not found: %s' % file_name
    return parse_query_test_file(test_file_path, valid_section_names, encoding=encoding)

  def __drop_partitions(self, db_name, table_name):
    """Drops all partitions in the given table"""
    for partition in self.hive_client.get_partition_names(db_name, table_name, 0):
      assert self.hive_client.drop_partition_by_name(db_name, table_name, \
          partition, True), 'Could not drop partition: %s' % partition

  @classmethod
  def __execute_query(cls, impalad_client, query, query_options=None, user=None):
    """Executes the given query against the specified Impalad"""
    if query_options is not None: impalad_client.set_configuration(query_options)
    return impalad_client.execute(query, user=user)

  def __reset_table(self, db_name, table_name):
    """Resets a table (drops and recreates the table)"""
    table = self.hive_client.get_table(db_name, table_name)
    assert table is not None
    self.hive_client.drop_table(db_name, table_name, True)
    self.hive_client.create_table(table)

  def clone_table(self, src_tbl, dst_tbl, recover_partitions, vector):
    src_loc = self._get_table_location(src_tbl, vector)
    self.client.execute("create external table {0} like {1} location '{2}'"\
        .format(dst_tbl, src_tbl, src_loc))
    if recover_partitions:
      self.client.execute("alter table {0} recover partitions".format(dst_tbl))

  def appx_equals(self, a, b, diff_perc):
    """Returns True if 'a' and 'b' are within 'diff_perc' percent of each other,
    False otherwise. 'diff_perc' must be a float in [0,1]."""
    if a == b: return True # Avoid division by 0
    assert abs(a - b) / float(max(a,b)) <= diff_perc

  def _get_table_location(self, table_name, vector):
    """ Returns the HDFS location of the table """
    result = self.execute_query_using_client(self.client,
        "describe formatted %s" % table_name, vector)
    for row in result.data:
      if 'Location:' in row:
        return row.split('\t')[1]
    # This should never happen.
    assert 0, 'Unable to get location for table: ' + table_name

  def run_stmt_in_hive(self, stmt, username=getuser()):
    """
    Run a statement in Hive, returning stdout if successful and throwing
    RuntimeError(stderr) if not.
    """
    # When HiveServer2 is configured to use "local" mode (i.e., MR jobs are run
    # in-process rather than on YARN), Hadoop's LocalDistributedCacheManager has a
    # race, wherein it tires to localize jars into
    # /tmp/hadoop-$USER/mapred/local/<millis>. Two simultaneous Hive queries
    # against HS2 can conflict here. Weirdly LocalJobRunner handles a similar issue
    # (with the staging directory) by appending a random number. To overcome this,
    # in the case that HS2 is on the local machine (which we conflate with also
    # running MR jobs locally), we move the temporary directory into a unique
    # directory via configuration. This workaround can be removed when
    # https://issues.apache.org/jira/browse/MAPREDUCE-6441 is resolved.
    # A similar workaround is used in bin/load-data.py.
    tmpdir = None
    beeline_opts = []
    if pytest.config.option.hive_server2.startswith("localhost:"):
      tmpdir = tempfile.mkdtemp(prefix="impala-tests-")
      beeline_opts += ['--hiveconf', 'mapreduce.cluster.local.dir={0}'.format(tmpdir)]
    try:
      # Remove HADOOP_CLASSPATH from environment. Beeline doesn't need it,
      # and doing so avoids Hadoop 3's classpath de-duplication code from
      # placing $HADOOP_CONF_DIR too late in the classpath to get the right
      # log4j configuration file picked up. Some log4j configuration files
      # in Hadoop's jars send logging to stdout, confusing Impala's test
      # framework.
      env = os.environ.copy()
      env.pop("HADOOP_CLASSPATH", None)
      call = subprocess.Popen(
          ['beeline',
           '--outputformat=csv2',
           '-u', 'jdbc:hive2://' + pytest.config.option.hive_server2,
           '-n', username,
           '-e', stmt] + beeline_opts,
          stdout=subprocess.PIPE,
          stderr=subprocess.PIPE,
          # Beeline in Hive 2.1 will read from stdin even when "-e"
          # is specified; explicitly make sure there's nothing to
          # read to avoid hanging, especially when running interactively
          # with py.test.
          stdin=file("/dev/null"),
          env=env)
      (stdout, stderr) = call.communicate()
      call.wait()
      if call.returncode != 0:
        raise RuntimeError(stderr)
      return stdout
    finally:
      if tmpdir is not None: shutil.rmtree(tmpdir)

  def hive_partition_names(self, table_name):
    """Find the names of the partitions of a table, as Hive sees them.

    The return format is a list of strings. Each string represents a partition
    value of a given column in a format like 'column1=7/column2=8'.
    """
    return self.run_stmt_in_hive(
        'show partitions %s' % table_name).split('\n')[1:-1]

  @classmethod
  def create_table_info_dimension(cls, exploration_strategy):
    # If the user has specified a specific set of table formats to run against, then
    # use those. Otherwise, load from the workload test vectors.
    if pytest.config.option.table_formats:
      table_formats = list()
      for tf in pytest.config.option.table_formats.split(','):
        dataset = get_dataset_from_workload(cls.get_workload())
        table_formats.append(TableFormatInfo.create_from_string(dataset, tf))
      tf_dimensions = ImpalaTestDimension('table_format', *table_formats)
    else:
      tf_dimensions = load_table_info_dimension(cls.get_workload(), exploration_strategy)
    # If 'skip_hbase' is specified or the filesystem is isilon, s3 or local, we don't
    # need the hbase dimension.
    if pytest.config.option.skip_hbase or TARGET_FILESYSTEM.lower() \
        in ['s3', 'isilon', 'local', 'abfs', 'adls']:
      for tf_dimension in tf_dimensions:
        if tf_dimension.value.file_format == "hbase":
          tf_dimensions.remove(tf_dimension)
          break
    return tf_dimensions

  @classmethod
  def __create_exec_option_dimension(cls):
    cluster_sizes = ALL_CLUSTER_SIZES
    disable_codegen_options = ALL_DISABLE_CODEGEN_OPTIONS
    batch_sizes = ALL_BATCH_SIZES
    exec_single_node_option = [0]
    if cls.exploration_strategy() == 'core':
      disable_codegen_options = [False]
      cluster_sizes = ALL_NODES_ONLY
    return create_exec_option_dimension(cluster_sizes, disable_codegen_options,
                                        batch_sizes,
                                        exec_single_node_option=exec_single_node_option,
                                        disable_codegen_rows_threshold_options=[0])

  @classmethod
  def exploration_strategy(cls):
    default_strategy = pytest.config.option.exploration_strategy
    if pytest.config.option.workload_exploration_strategy:
      workload_strategies = pytest.config.option.workload_exploration_strategy.split(',')
      for workload_strategy in workload_strategies:
        workload_strategy = workload_strategy.split(':')
        if len(workload_strategy) != 2:
          raise ValueError, 'Invalid workload:strategy format: %s' % workload_strategy
        if cls.get_workload() == workload_strategy[0]:
          return workload_strategy[1]
    return default_strategy

  def wait_for_state(self, handle, expected_state, timeout):
    """Waits for the given 'query_handle' to reach the 'expected_state'. If it does not
    reach the given state within 'timeout' seconds, the method throws an AssertionError.
    """
    start_time = time.time()
    actual_state = self.client.get_state(handle)
    while actual_state != expected_state and time.time() - start_time < timeout:
      actual_state = self.client.get_state(handle)
      time.sleep(0.5)
    if actual_state != expected_state:
      raise Timeout("query '%s' did not reach expected state '%s', last known state '%s'"
                    % (handle.get_handle().id, expected_state, actual_state))

  def assert_impalad_log_contains(self, level, line_regex, expected_count=1):
    """
    Convenience wrapper around assert_log_contains for impalad logs.
    """
    self.assert_log_contains("impalad", level, line_regex, expected_count)

  def assert_catalogd_log_contains(self, level, line_regex, expected_count=1):
    """
    Convenience wrapper around assert_log_contains for catalogd logs.
    """
    self.assert_log_contains("catalogd", level, line_regex, expected_count)

  def assert_log_contains(self, daemon, level, line_regex, expected_count=1):
    """
    Assert that the daemon log with specified level (e.g. ERROR, WARNING, INFO) contains
    expected_count lines with a substring matching the regex. When expected_count is -1,
    at least one match is expected.
    When using this method to check log files of running processes, the caller should
    make sure that log buffering has been disabled, for example by adding
    '-logbuflevel=-1' to the daemon startup options.
    """
    pattern = re.compile(line_regex)
    found = 0
    if hasattr(self, "impala_log_dir"):
      log_dir = self.impala_log_dir
    else:
      log_dir = EE_TEST_LOGS_DIR
    log_file_path = os.path.join(log_dir, daemon + "." + level)
    # Resolve symlinks to make finding the file easier.
    log_file_path = os.path.realpath(log_file_path)
    with open(log_file_path) as log_file:
      for line in log_file:
        if pattern.search(line):
          found += 1
    if expected_count == -1:
      assert found > 0, "Expected at least one line in file %s matching regex '%s'"\
        ", but found none." % (log_file_path, line_regex)
    else:
      assert found == expected_count, "Expected %d lines in file %s matching regex '%s'"\
        ", but found %d lines. Last line was: \n%s" %\
        (expected_count, log_file_path, line_regex, found, line)

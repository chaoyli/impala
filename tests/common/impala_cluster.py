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
# Basic object model of an Impala cluster (set of Impala processes).

import json
import logging
import os
import psutil
import socket
import sys
import time
from getpass import getuser
from random import choice
from signal import SIGKILL
from subprocess import check_call
from time import sleep

if sys.version_info >= (2, 7):
  # We use some functions in the docker code that don't exist in Python 2.6.
  from subprocess import check_output

from tests.common.impala_service import (
    CatalogdService,
    ImpaladService,
    StateStoredService)
from tests.util.shell_util import exec_process, exec_process_async

LOG = logging.getLogger('impala_cluster')
LOG.setLevel(level=logging.DEBUG)

IMPALA_HOME = os.environ['IMPALA_HOME']
CATALOGD_PATH = os.path.join(IMPALA_HOME, 'bin/start-catalogd.sh')
IMPALAD_PATH = os.path.join(IMPALA_HOME, 'bin/start-impalad.sh -build_type=latest')

DEFAULT_BEESWAX_PORT = 21000
DEFAULT_HS2_PORT = 21050
DEFAULT_BE_PORT = 22000
DEFAULT_KRPC_PORT = 27000
DEFAULT_CATALOG_SERVICE_PORT = 26000
DEFAULT_STATE_STORE_SUBSCRIBER_PORT = 23000
DEFAULT_IMPALAD_WEBSERVER_PORT = 25000
DEFAULT_STATESTORED_WEBSERVER_PORT = 25010
DEFAULT_CATALOGD_WEBSERVER_PORT = 25020

# Timeout to use when waiting for a cluster to start up. Set quite high to avoid test
# flakiness.
CLUSTER_WAIT_TIMEOUT_IN_SECONDS = 240


# Represents a set of Impala processes.
# Handles two cases:
# * The traditional minicluster with many processes running as the current user on
#   the local system. In this case various settings are detected based on command
#   line options(beeswax_port, webserver_port, etc)
# * The docker minicluster with one container per process connected to a user-defined
#   bridge network.
class ImpalaCluster(object):
  def __init__(self, docker_network=None):
    self.docker_network = docker_network
    self.refresh()

  def refresh(self):
    """ Re-loads the impalad/statestored/catalogd processes if they exist.

    Helpful to confirm that processes have been killed.
    """
    if self.docker_network is None:
      self.__impalads, self.__statestoreds, self.__catalogd =\
          self.__build_impala_process_lists()
    else:
      self.__impalads, self.__statestoreds, self.__catalogd =\
          self.__find_docker_containers()
    LOG.info("Found %d impalad/%d statestored/%d catalogd process(es)" %
        (len(self.__impalads), len(self.__statestoreds), 1 if self.__catalogd else 0))

  @property
  def statestored(self):
    """
    Returns the statestore process

    Note: Currently we expectly a single statestore process, in the future this might
    change in which case this should return the "active" statestore.
    """
    # If no statestored process exists, return None.
    return self.__statestoreds[0] if len(self.__statestoreds) > 0 else None

  @property
  def impalads(self):
    """Returns a list of the known impalad processes"""
    return self.__impalads

  @property
  def catalogd(self):
    """Returns the catalogd process, or None if no catalogd process was found"""
    return self.__catalogd

  def get_first_impalad(self):
    return self.impalads[0]

  def get_any_impalad(self):
    """Selects a random impalad from the list of known processes"""
    return choice(self.impalads)

  def get_different_impalad(self, other_impalad):
    """Selects an impalad that is different from the given impalad"""
    if len(self.impalads) <= 1:
      assert 0, "Only %d impalads available to choose from" % len(self.impalads)
    LOG.info("other_impalad: " + str(other_impalad))
    LOG.info("Cluster: " + str(len(self.impalads)))
    LOG.info("Cluster: " + str(self.impalads))
    return choice([impalad for impalad in self.impalads if impalad != other_impalad])

  def num_responsive_coordinators(self):
    """Find the number of impalad coordinators that can evaluate a test query."""
    n = 0
    for impalad in self.impalads:
      try:
        client = impalad.service.create_beeswax_client()
        result = client.execute("select 1")
        assert result.success
        ++n
      except Exception as e: print e
      finally:
        client.close()
    return n

  def wait_until_ready(self, expected_num_impalads=1, expected_num_ready_impalads=None):
    """Waits for this 'cluster' to be ready to submit queries.

      A cluster is deemed "ready" if:
        - expected_num_impalads impala processes are up (or, if not specified, at least
          one impalad is up).
        - expected_num_ready_impalads backends are registered with the statestore.
          expected_num_ready_impalads defaults to expected_num_impalads.
        - All impalads knows about all other ready impalads.
        - Each coordinator impalad's catalog cache is ready.
      This information is retrieved by querying the statestore debug webpage
      and each individual impalad's metrics webpage.
    """
    self.wait_for_num_impalads(expected_num_impalads)

    # TODO: fix this for coordinator-only nodes as well.
    if expected_num_ready_impalads is None:
      expected_num_ready_impalads = len(self.impalads)

    for impalad in self.impalads:
      impalad.service.wait_for_num_known_live_backends(expected_num_ready_impalads,
          timeout=CLUSTER_WAIT_TIMEOUT_IN_SECONDS, interval=2)
      if (impalad._get_arg_value("is_coordinator", default="true") == "true" and
         impalad._get_arg_value("stress_catalog_init_delay_ms", default=0) == 0):
        impalad.wait_for_catalog()

  def wait_for_num_impalads(self, num_impalads, retries=10):
    """Checks that at least 'num_impalads' impalad processes are running, along with
    the statestored and catalogd.

    Refresh until the number running impalad processes reaches the expected
    number based on num_impalads, or the retry limit is hit. Failing this, raise a
    RuntimeError.
    """
    for i in range(retries):
      if len(self.impalads) < num_impalads or not self.statestored or not self.catalogd:
        sleep(1)
        self.refresh()
    msg = ""
    if len(self.impalads) < num_impalads:
      msg += "Expected {expected_num} impalad(s), only {actual_num} found\n".format(
          expected_num=num_impalads, actual_num=len(self.impalads))
    if not self.statestored:
      msg += "statestored failed to start.\n"
    if not self.catalogd:
      msg += "catalogd failed to start.\n"
    if msg:
      raise RuntimeError(msg)

  def __build_impala_process_lists(self):
    """
    Gets all the running Impala procs (with start arguments) on the machine.

    Note: This currently only works for the local case. To support running in a cluster
    environment this would need to enumerate each machine in the cluster.
    """
    impalads = list()
    statestored = list()
    catalogd = None
    for process in find_user_processes(['impalad', 'catalogd', 'statestored']):
      # IMPALA-6889: When a process shuts down and becomes a zombie its cmdline becomes
      # empty for a brief moment, before it gets reaped by its parent (see man proc). We
      # copy the cmdline to prevent it from changing between the following checks and
      # the construction of the *Process objects.
      cmdline = process.cmdline
      if len(cmdline) == 0:
        continue
      if process.name == 'impalad':
        impalads.append(ImpaladProcess(cmdline))
      elif process.name == 'statestored':
        statestored.append(StateStoreProcess(cmdline))
      elif process.name == 'catalogd':
        catalogd = CatalogdProcess(cmdline)

    # If the operating system PIDs wrap around during startup of the local minicluster,
    # the order of the impalads is incorrect. We order them by their HS2 port, so that
    # get_first_impalad() always returns the first one. We need to use a port that is
    # exposed and mapped to a host port for the containerised cluster.
    impalads.sort(key=lambda i: i.service.hs2_port)
    return impalads, statestored, catalogd

  def __find_docker_containers(self):
    """
    Gets all the running Impala containers on self.docker_network.
    """
    impalads = []
    statestoreds = []
    catalogd = None
    output = check_output(["docker", "network", "inspect", self.docker_network])
    # Only one network should be present in the top level array.
    for container_id in json.loads(output)[0]["Containers"]:
      container_info = self._get_container_info(container_id)
      if container_info["State"]["Status"] != "running":
        # Skip over stopped containers.
        continue
      args = container_info["Args"]
      executable = os.path.basename(args[0])
      port_map = {}
      for k, v in container_info["NetworkSettings"]["Ports"].iteritems():
        # Key looks like "25000/tcp"..
        port = int(k.split("/")[0])
        # Value looks like { "HostPort": "25002", "HostIp": "" }.
        host_port = int(v[0]["HostPort"])
        port_map[port] = host_port
      if executable == 'impalad':
        impalads.append(ImpaladProcess(args, container_id=container_id,
                                       port_map=port_map))
      elif executable == 'statestored':
        statestoreds.append(StateStoreProcess(args, container_id=container_id,
                                              port_map=port_map))
      elif executable == 'catalogd':
        assert catalogd is None
        catalogd = CatalogdProcess(args, container_id=container_id,
                                   port_map=port_map)
    impalads.sort(key=lambda i: i.service.be_port)
    return impalads, statestoreds, catalogd

  def _get_container_info(self, container_id):
    """Get the output of "docker container inspect" as a python data structure."""
    containers = json.loads(
        check_output(["docker", "container", "inspect", container_id]))
    # Only one container should be present in the top level array.
    assert len(containers) == 1, json.dumps(containers, indent=4)
    return containers[0]


# Represents a process running on a machine and common actions that can be performed
# on a process such as restarting or killing. The process may be the main process in
# a Docker container, if the cluster is containerised (in this case container_id must
# be provided). Note that containerised processes are really just processes running
# on the local system with some additional virtualisation, so some operations are
# the same for both containerised and non-containerised cases.
#
# For containerised processes, 'port_map' should be provided to map from the container's
# ports to ports on the host. Methods from this class always return the host port.
class Process(object):
  def __init__(self, cmd, container_id=None, port_map=None):
    assert cmd is not None and len(cmd) >= 1,\
        'Process object must be created with valid command line argument list'
    assert container_id is None or port_map is not None,\
        "Must provide port_map for containerised process"
    self.cmd = cmd
    self.container_id = container_id
    self.port_map = port_map

  def get_pid(self):
    """Gets the PID of the process. Returns None if the PID cannot be determined"""
    LOG.info("Attempting to find PID for %s" % ' '.join(self.cmd))
    return self.__get_pid()

  def __get_pid(self):
    if self.container_id is not None:
      container_info = self._get_container_info(self.container_id)
      if container_info["State"]["Status"] != "running":
        return None
      return container_info["State"]["Status"]["Pid"]

    # In non-containerised case, search for process based on matching command lines.
    for pid in psutil.get_pid_list():
      try:
        process = psutil.Process(pid)
        if set(self.cmd) == set(process.cmdline):
          return pid
      except psutil.NoSuchProcess, e:
        # A process from get_pid_list() no longer exists, continue.
        LOG.info(e)
    LOG.info("No PID found for process cmdline: %s. Process is dead?" % self.cmd)
    return None

  def start(self):
    if self.container_id is None:
      LOG.info("Starting process: {0}".format(' '.join(self.cmd)))
      # Use os.system() to start 'cmd' in the background via a shell so its parent will be
      # init after the shell exits. Otherwise, the parent of 'cmd' will be py.test and we
      # cannot cleanly kill it until py.test exits. In theory, Popen(shell=True) should
      # achieve the same thing but it doesn't work on some platforms for some reasons.
      os.system(' '.join(self.cmd) + ' &')
    else:
      LOG.info("Starting container: {0}".format(self.container_id))
      check_call(["docker", "container", "start", self.container_id])

  def kill(self, signal=SIGKILL):
    """
    Kills the given processes.
    """
    if self.container_id is None:
      pid = self.get_pid()
      if pid is None:
        assert 0, "No processes %s found" % self.cmd
      LOG.info('Killing: %s (PID: %d) with signal %s' % (' '.join(self.cmd), pid, signal))
      exec_process("kill -%d %d" % (signal, pid))
    else:
      LOG.info("Stopping container: {0}".format(self.container_id))
      check_call(["docker", "container", "stop", self.container_id])


  def restart(self):
    """Kills and restarts the process"""
    self.kill()
    self.wait_for_exit()
    self.start()

  def wait_for_exit(self):
    """Wait until the process exits (or return immediately if it already has exited."""
    LOG.info('Waiting for exit: {0} (PID: {1})'.format(
        ' '.join(self.cmd), self.get_pid()))
    while self.__get_pid() is not None:
      sleep(0.01)

  def __str__(self):
    return "Command: %s PID: %s" % (self.cmd, self.get_pid())


# Base class for all Impala processes
class BaseImpalaProcess(Process):
  def __init__(self, cmd, container_id=None, port_map=None):
    super(BaseImpalaProcess, self).__init__(cmd, container_id, port_map)
    self.hostname = self._get_hostname()

  def _get_webserver_port(self, default=None):
    return int(self._get_port('webserver_port', default))

  def _get_webserver_certificate_file(self):
    # TODO: if this is containerised, the path will likely not be the same on the host.
    return self._get_arg_value("webserver_certificate_file", "")

  def _get_hostname(self):
    return self._get_arg_value("hostname", socket.gethostname())

  def _get_arg_value(self, arg_name, default=None):
    """Gets the argument value for given argument name"""
    for arg in self.cmd:
      if ('%s=' % arg_name) in arg.strip().lstrip('-'):
        return arg.split('=')[1]
    if default is None:
      assert 0, "No command line argument '%s' found." % arg_name
    return default

  def _get_port(self, arg_name, default):
    """Return the host port for the specified by the command line argument 'arg_name'.
    If 'self.port_map' is set, maps from container ports to host ports."""
    port = int(self._get_arg_value(arg_name, default))
    if self.port_map is not None:
      port = self.port_map.get(port, port)
    return port


# Represents an impalad process
class ImpaladProcess(BaseImpalaProcess):
  def __init__(self, cmd, container_id=None, port_map=None):
    super(ImpaladProcess, self).__init__(cmd, container_id, port_map)
    self.service = ImpaladService(self.hostname,
                                  self._get_webserver_port(
                                      default=DEFAULT_IMPALAD_WEBSERVER_PORT),
                                  self.__get_beeswax_port(),
                                  self.__get_be_port(),
                                  self.__get_hs2_port(),
                                  self._get_webserver_certificate_file())

  def __get_beeswax_port(self):
    return int(self._get_port('beeswax_port', DEFAULT_BEESWAX_PORT))

  def __get_be_port(self):
    return int(self._get_port('be_port', DEFAULT_BE_PORT))

  def __get_hs2_port(self):
    return int(self._get_port('hs2_port', DEFAULT_HS2_PORT))

  def start(self, wait_until_ready=True):
    """Starts the impalad and waits until the service is ready to accept connections."""
    restart_cmd = [IMPALAD_PATH] + self.cmd[1:] + ['&']
    LOG.info("Starting Impalad process: %s" % ' '.join(restart_cmd))
    os.system(' '.join(restart_cmd))
    if wait_until_ready:
      self.service.wait_for_metric_value('impala-server.ready',
                                         expected_value=1, timeout=30)

  def wait_for_catalog(self):
    """Waits for a catalog copy to be received by the impalad. When its received,
       additionally waits for client ports to be opened."""
    start_time = time.time()
    beeswax_port_is_open = False
    hs2_port_is_open = False
    num_dbs = 0
    num_tbls = 0
    while ((time.time() - start_time < CLUSTER_WAIT_TIMEOUT_IN_SECONDS) and
        not (beeswax_port_is_open and hs2_port_is_open)):
      try:
        num_dbs, num_tbls = self.service.get_metric_values(
            ["catalog.num-databases", "catalog.num-tables"])
        beeswax_port_is_open = self.service.beeswax_port_is_open()
        hs2_port_is_open = self.service.hs2_port_is_open()
      except Exception:
        LOG.exception(("Client services not ready. Waiting for catalog cache: "
            "({num_dbs} DBs / {num_tbls} tables). Trying again ...").format(
                num_dbs=num_dbs,
                num_tbls=num_tbls))
      sleep(0.5)

    if not hs2_port_is_open or not beeswax_port_is_open:
      raise RuntimeError(
          "Unable to open client ports within {num_seconds} seconds.".format(
              num_seconds=CLUSTER_WAIT_TIMEOUT_IN_SECONDS))


# Represents a statestored process
class StateStoreProcess(BaseImpalaProcess):
  def __init__(self, cmd, container_id=None, port_map=None):
    super(StateStoreProcess, self).__init__(cmd, container_id, port_map)
    self.service = StateStoredService(self.hostname,
        self._get_webserver_port(default=DEFAULT_STATESTORED_WEBSERVER_PORT),
        self._get_webserver_certificate_file())


# Represents a catalogd process
class CatalogdProcess(BaseImpalaProcess):
  def __init__(self, cmd, container_id=None, port_map=None):
    super(CatalogdProcess, self).__init__(cmd, container_id, port_map)
    self.service = CatalogdService(self.hostname,
        self._get_webserver_port(default=DEFAULT_CATALOGD_WEBSERVER_PORT),
        self._get_webserver_certificate_file(), self.__get_port())

  def __get_port(self):
    return int(self._get_port('catalog_service_port', DEFAULT_CATALOG_SERVICE_PORT))

  def start(self, wait_until_ready=True):
    """Starts catalogd and waits until the service is ready to accept connections."""
    restart_cmd = [CATALOGD_PATH] + self.cmd[1:] + ["&"]
    LOG.info("Starting Catalogd process: %s" % ' '.join(restart_cmd))
    os.system(' '.join(restart_cmd))
    if wait_until_ready:
      self.service.wait_for_metric_value('statestore-subscriber.connected',
                                         expected_value=1, timeout=30)


def find_user_processes(binaries):
  """Returns an iterator over all processes owned by the current user with a matching
  binary name from the provided list."""
  for pid in psutil.get_pid_list():
    try:
      process = psutil.Process(pid)
      if process.username == getuser() and process.name in binaries: yield process
    except KeyError, e:
      if "uid not found" not in str(e):
        raise
    except psutil.NoSuchProcess, e:
      # Ignore the case when a process no longer exists.
      pass

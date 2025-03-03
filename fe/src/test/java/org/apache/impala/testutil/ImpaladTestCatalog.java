// Licensed to the Apache Software Foundation (ASF) under one
// or more contributor license agreements.  See the NOTICE file
// distributed with this work for additional information
// regarding copyright ownership.  The ASF licenses this file
// to you under the Apache License, Version 2.0 (the
// "License"); you may not use this file except in compliance
// with the License.  You may obtain a copy of the License at
//
//   http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing,
// software distributed under the License is distributed on an
// "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
// KIND, either express or implied.  See the License for the
// specific language governing permissions and limitations
// under the License.

package org.apache.impala.testutil;

import com.google.common.base.Preconditions;
import org.apache.impala.analysis.TableName;
import org.apache.impala.authorization.AuthorizationConfig;
import org.apache.impala.catalog.CatalogException;
import org.apache.impala.catalog.CatalogServiceCatalog;
import org.apache.impala.catalog.Db;
import org.apache.impala.catalog.FeDb;
import org.apache.impala.catalog.HdfsCachePool;
import org.apache.impala.catalog.HdfsTable;
import org.apache.impala.catalog.ImpaladCatalog;
import org.apache.impala.catalog.PrincipalPrivilege;
import org.apache.impala.catalog.Role;
import org.apache.impala.catalog.Table;
import org.apache.impala.catalog.User;
import org.apache.impala.thrift.TPrivilege;
import org.apache.impala.util.PatternMatcher;

import java.util.HashSet;
import java.util.Set;

/**
 * Mock catalog used for running FE tests that allows lazy-loading of tables without a
 * running catalogd/statestored.
 */
public class ImpaladTestCatalog extends ImpaladCatalog {
  // Used to load missing table metadata when running the FE tests.
  private final CatalogServiceCatalog srcCatalog_;

  public ImpaladTestCatalog() {
    this(AuthorizationConfig.createAuthDisabledConfig());
  }

  /**
   * Takes an AuthorizationConfig to bootstrap the backing CatalogServiceCatalog.
   */
  public ImpaladTestCatalog(AuthorizationConfig authzConfig) {
    super("127.0.0.1");
    CatalogServiceCatalog catalogServerCatalog = authzConfig.isEnabled() ?
        CatalogServiceTestCatalog.createWithAuth(authzConfig.getSentryConfig()) :
        CatalogServiceTestCatalog.create();
    // Bootstrap the catalog by adding all dbs, tables, and functions.
    for (FeDb db: catalogServerCatalog.getDbs(PatternMatcher.MATCHER_MATCH_ALL)) {
      // Adding DB should include all tables/fns in that database.
      addDb((Db)db);
    }
    authPolicy_ = catalogServerCatalog.getAuthPolicy();
    srcCatalog_ = catalogServerCatalog;
    setIsReady(true);
  }

  @Override
  public HdfsCachePool getHdfsCachePool(String poolName) {
    return srcCatalog_.getHdfsCachePool(poolName);
  }

  /**
   * Reloads all metadata from the source catalog.
   */
  public void reset() throws CatalogException { srcCatalog_.reset(); }

  /**
   * Returns the Table for the given name, loading the table's metadata if necessary.
   * Returns null if the database or table does not exist.
   */
  public Table getOrLoadTable(String dbName, String tblName) {
    Db db = getDb(dbName);
    if (db == null) return null;
    Table existingTbl = db.getTable(tblName);
    // Table doesn't exist or is already loaded.
    if (existingTbl == null || existingTbl.isLoaded()) return existingTbl;

    // The table was not yet loaded. Load it in to the catalog now.
    Table newTbl = null;
    try {
      newTbl = srcCatalog_.getOrLoadTable(dbName, tblName);
    } catch (CatalogException e) {
      throw new IllegalStateException("Unexpected table loading failure.", e);
    }
    Preconditions.checkNotNull(newTbl);
    Preconditions.checkState(newTbl.isLoaded());
    if (newTbl instanceof HdfsTable) {
      ((HdfsTable) newTbl).computeHdfsStatsForTesting();
    }
    db.addTable(newTbl);
    return newTbl;
  }

  /**
   * Fast loading path for FE unit testing. Immediately loads the given tables into
   * this catalog from this thread without involving the catalogd/statestored.
   */
  @Override
  public void prioritizeLoad(Set<TableName> tableNames) {
    for (TableName tbl: tableNames) getOrLoadTable(tbl.getDb(), tbl.getTbl());
  }

  /**
   * No-op. Metadata loading does not go through the catalogd/statestored in a
   * FE test environment.
   */
  @Override
  public void waitForCatalogUpdate(long timeoutMs) {
  }

  public Role addRole(String roleName) {
    return srcCatalog_.addRole(roleName, new HashSet<String>());
  }

  public Role addRoleGrantGroup(String roleName, String groupName)
      throws CatalogException {
    return srcCatalog_.addRoleGrantGroup(roleName, groupName);
  }

  public PrincipalPrivilege addRolePrivilege(String roleName, TPrivilege privilege)
      throws CatalogException {
    return srcCatalog_.addRolePrivilege(roleName, privilege);
  }

  public void removeRole(String roleName) { srcCatalog_.removeRole(roleName); }

  public User addUser(String userName) {
    return srcCatalog_.addUser(userName);
  }

  public PrincipalPrivilege addUserPrivilege(String userName, TPrivilege privilege)
      throws CatalogException {
    return srcCatalog_.addUserPrivilege(userName, privilege);
  }

  public void removeUser(String userName) { srcCatalog_.removeUser(userName); }

  @Override
  public void close() {
    super.close();
    srcCatalog_.close();
  }
}

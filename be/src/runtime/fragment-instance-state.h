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


#ifndef IMPALA_RUNTIME_FRAGMENT_INSTANCE_STATE_H
#define IMPALA_RUNTIME_FRAGMENT_INSTANCE_STATE_H

#include <string>
#include <boost/scoped_ptr.hpp>
#include <boost/thread/mutex.hpp>

#include "common/atomic.h"
#include "common/status.h"
#include "common/thread-debug-info.h"
#include "util/promise.h"

#include "gen-cpp/control_service.pb.h"
#include "gen-cpp/ImpalaInternalService_types.h"
#include "gutil/threading/thread_collision_warner.h" // for DFAKE_*
#include "runtime/row-batch.h"
#include "util/condition-variable.h"
#include "util/promise.h"
#include "util/runtime-profile.h"

namespace impala {

class TPlanFragmentCtx;
class TPlanFragmentInstanceCtx;
class TBloomFilter;
class TUniqueId;
class TNetworkAddress;
class TQueryCtx;
class QueryState;
class RuntimeProfile;
class ExecNode;
class PlanRootSink;
class Thread;
class DataSink;
class RuntimeState;

/// FragmentInstanceState handles all aspects of the execution of a single plan fragment
/// instance, including setup and finalization, both in the success and error case.
/// Close() happens automatically at the end of Exec() and frees all memory allocated
/// for this fragment instance and closes all data streams.
///
/// The FIS makes an aggregated profile for the entire fragment available, which
/// includes profile information for the plan itself as well as the output sink. It also
/// contains a timeline of events of the fragment instance.
///
/// This class is thread-safe.
/// All non-getter public functions other than Exec() block until the Prepare phase
/// finishes.
/// No member variables, other than the ones passed to the c'tor, are valid before
/// the Prepare phase finishes.
///
/// TODO:
/// - absorb RuntimeState?
class FragmentInstanceState {
 public:
  FragmentInstanceState(QueryState* query_state, const TPlanFragmentCtx& fragment_ctx,
      const TPlanFragmentInstanceCtx& instance_ctx);

  /// Main loop of fragment instance execution. Blocks until execution finishes and
  /// automatically releases resources. Returns execution status.
  /// Must only be called once.
  Status Exec() WARN_UNUSED_RESULT;

  /// Cancels execution. Idempotent.
  void Cancel();

  /// Blocks until the Prepare phase of Exec() is finished and the exec tree is
  /// opened, and returns that status. If the preparation phase encountered an error,
  /// GetOpenStatus() will return that error without blocking.
  Status WaitForOpen();

  /// Publishes filter with ID 'filter_id' to this fragment instance's filter bank.
  void PublishFilter(const TPublishFilterParams& params);

  /// Called periodically by query state thread to get the current status of this fragment
  /// instance. The fragment instance's status is stored in 'instance_status' and its
  /// Thrift runtime profile is stored in 'thrift_profile'.
  void GetStatusReport(FragmentInstanceExecStatusPB* instance_status,
      TRuntimeProfileTree* thrift_profile);

  /// Returns fragment instance's sink if this is the root fragment instance. Valid after
  /// the Prepare phase. May be nullptr.
  PlanRootSink* root_sink() { return root_sink_; }

  /// Returns a string description of 'state'.
  static const string& ExecStateToString(FInstanceExecStatePB state);

  /// Name of the counter that is tracking per query, per host peak mem usage.
  /// TODO: this doesn't look like it belongs here
  static const std::string PER_HOST_PEAK_MEM_COUNTER;

  QueryState* query_state() { return query_state_; }
  RuntimeState* runtime_state() { return runtime_state_; }
  RuntimeProfile* profile() const;
  const TQueryCtx& query_ctx() const;
  const TPlanFragmentCtx& fragment_ctx() const { return fragment_ctx_; }
  const TPlanFragmentInstanceCtx& instance_ctx() const { return instance_ctx_; }
  const TUniqueId& query_id() const { return query_ctx().query_id; }
  const TUniqueId& instance_id() const { return instance_ctx_.fragment_instance_id; }
  FInstanceExecStatePB current_state() const { return current_state_.Load(); }
  bool final_report_sent() const { return final_report_sent_; }
  const TNetworkAddress& coord_address() const { return query_ctx().coord_address; }
  bool IsDone() const { return current_state_.Load() == FInstanceExecStatePB::FINISHED; }
  ObjectPool* obj_pool();

  /// Returns true if the current thread is a thread executing the whole or part of
  /// a fragment instance.
  static bool IsFragmentExecThread() {
    const static size_t name_len =
        strlen(FragmentInstanceState::FINST_THREAD_NAME_PREFIX.c_str());
    const char* name = GetThreadDebugInfo()->GetThreadName();
    return name != nullptr &&
        (strncmp(name, FINST_THREAD_NAME_PREFIX.c_str(), name_len) == 0 ||
         strncmp(name, "join-build-thread", 17) == 0);
  }

  static const std::string FINST_THREAD_GROUP_NAME;
  static const std::string FINST_THREAD_NAME_PREFIX;

 private:
  QueryState* query_state_;
  const TPlanFragmentCtx& fragment_ctx_;
  const TPlanFragmentInstanceCtx& instance_ctx_;

  /// All following member variables that are initialized to nullptr are set
  /// in Prepare().

  ExecNode* exec_tree_ = nullptr; // lives in obj_pool()
  RuntimeState* runtime_state_ = nullptr;  // lives in obj_pool()

  /// A 'fake mutex' to detect any race condition in accessing 'report_seq_no_' below.
  /// There should be only one thread doing status report at the same time.
  DFAKE_MUTEX(report_status_lock_);

  /// Monotonically increasing sequence number used in status report to prevent
  /// duplicated or out-of-order reports.
  int64_t report_seq_no_ = 0;

  /// True iff the final report has already been sent. Read exclusively by the query
  /// state thread only. Written in GetStatusReport() by the query state thread.
  bool final_report_sent_ = false;

  /// Profile for timings for each stage of the plan fragment instance's lifecycle.
  /// Lives in obj_pool().
  RuntimeProfile* timings_profile_ = nullptr;

  /// Event sequence tracking the completion of various stages of this fragment instance.
  /// Updated in UpdateState().
  RuntimeProfile::EventSequence* event_sequence_ = nullptr;

  /// Events that change the current state of this instance's execution, which is kept in
  /// 'current_state_'. Events are issued throughout the execution by calling
  /// UpdateState(), which implements a state machine. See the implementation of
  /// UpdateState() for valid state transitions.
  enum class StateEvent {
    /// Indicates the start of execution.
    PREPARE_START,
    /// Indicates that codegen will get called. Omitted if not doing codegen.
    CODEGEN_START,
    /// Indicates the call to Open().
    OPEN_START,
    /// Indicates waiting for the first batch to arrive.
    WAITING_FOR_FIRST_BATCH,
    /// Indicates that a new batch was produced by this instance.
    BATCH_PRODUCED,
    /// Indicates that a batch has been sent.
    BATCH_SENT,
    /// Indicates that no new batches will be received.
    LAST_BATCH_SENT,
    /// Indicates the end of this instance's execution.
    EXEC_END
  };

  /// The current state of this fragment instance's execution. Only updated by the
  /// fragment instance thread in UpdateState() and read by the profile reporting threads.
  AtomicEnum<FInstanceExecStatePB> current_state_{FInstanceExecStatePB::WAITING_FOR_EXEC};

  /// Output sink for rows sent to this fragment. Created in Prepare(), lives in
  /// obj_pool().
  DataSink* sink_ = nullptr;

  /// Set if this fragment instance is the root of the entire plan, so that a consumer can
  /// pull results by calling root_sink_->GetNext(). Same object as sink_.
  PlanRootSink* root_sink_ = nullptr;

  /// should live in obj_pool(), but managed separately so we can delete it in Close()
  boost::scoped_ptr<RowBatch> row_batch_;

  /// Set when Prepare() returns.
  Promise<Status> prepared_promise_;

  /// Set when OpenInternal() returns.
  Promise<Status> opened_promise_;

  /// Returns the monotonically increasing sequence number.
  /// Called by query state thread only.
  int64_t AdvanceReportSeqNo() { return ++report_seq_no_; }

  /// A counter for the per query, per host peak mem usage. Note that this is not the
  /// max of the peak memory of all fragments running on a host since it needs to take
  /// into account when they are running concurrently. All fragments for a single query
  /// on a single host will have the same value for this counter.
  RuntimeProfile::Counter* per_host_mem_usage_ = nullptr;

  /// Number of rows returned by this fragment
  /// TODO: by this instance?
  RuntimeProfile::Counter* rows_produced_counter_ = nullptr;

  /// Average number of thread tokens for the duration of the fragment instance execution.
  /// Instances that do a lot of cpu work (non-coordinator fragment) will have at
  /// least 1 token.  Instances that contain a hdfs scan node will have 1+ tokens
  /// depending on system load.  Other nodes (e.g. hash join node) can also reserve
  /// additional tokens.
  /// This is a measure of how much CPU resources this instance used during the course
  /// of the execution.
  /// TODO-MT: remove
  RuntimeProfile::Counter* avg_thread_tokens_ = nullptr;

  /// Sampled memory usage at even time intervals.
  RuntimeProfile::TimeSeriesCounter* mem_usage_sampled_counter_ = nullptr;

  /// Sampled thread usage (tokens) at even time intervals.
  RuntimeProfile::TimeSeriesCounter* thread_usage_sampled_counter_ = nullptr;

  /// Prepare for execution. runtime_state() will not be valid until Prepare() is called.
  /// runtime_state() will always be valid after Prepare() returns.
  /// If request.query_options.mem_limit > 0, it is used as an
  /// approximate limit on the number of bytes this query can consume at runtime.  The
  /// query will be aborted (MEM_LIMIT_EXCEEDED) if it goes over that limit.
  ///
  /// A failure in Prepare() will result in partially-initialized state.
  Status Prepare() WARN_UNUSED_RESULT;

  /// Executes Open() logic and returns resulting status.
  Status Open() WARN_UNUSED_RESULT;

  /// Pulls row batches from exec_tree_ and pushes them to sink_ in a loop. Returns
  /// OK if the input was exhausted and sent to the sink successfully, an error otherwise.
  /// If ExecInternal() returns without an error condition, all rows will have been sent
  /// to the sink and the sink will have been flushed.
  Status ExecInternal() WARN_UNUSED_RESULT;

  /// Closes the underlying fragment instance and frees up all resources allocated in
  /// Prepare() and Open(). Can handle partially-finished Prepare().
  void Close();

  /// Handle the execution event 'event'. This implements a state machine and will update
  /// the current execution state of this fragment instance. Also marks an event in
  /// 'event_sequence_' for some states. Must not be called by multiple threads
  /// concurrently.
  void UpdateState(const StateEvent event);

  /// Releases the thread token for this fragment executor. Can handle
  /// partially-finished Prepare().
  void ReleaseThreadToken();

  /// Print stats about scan ranges for each volumeId in params to info log.
  void PrintVolumeIds();
};

}

#endif

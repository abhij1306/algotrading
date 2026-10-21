------------------------------ MODULE JobScheduler ------------------------------
(***************************************************************************)
(* Job idempotency and the scheduler start gate (10-ops §2, 03-schema §7). *)
(*                                                                         *)
(* One key (job, trade_date) is modelled; keys are independent because the *)
(* unique row is per key. NWorkers callers race on it, through the         *)
(* scheduler, which exists only when HAT is enabled.                       *)
(*                                                                         *)
(* Bounds: NWorkers = 2 concurrent callers of run_job for the one key;     *)
(*   MaxCalls = 3 run_job calls per caller; MaxRestarts = 2 process        *)
(*   restarts; BrokenMode names a deliberately broken variant ("none" is   *)
(*   the real protocol).                                                   *)
(* Assumptions: each action below is one database transaction (atomic);    *)
(*   the unique (job, trade_date) row serialises claims; a restart kills   *)
(*   every in-flight caller and runs recover_interrupted before any new    *)
(*   job; a caller that is alive and running its body eventually finishes  *)
(*   or fails (weak fairness); scheduling is otherwise arbitrary. A job    *)
(*   body is idempotent, or its effects commit with its recording: a crash *)
(*   between the body returning and the success being recorded is the      *)
(*   Restart action, after which the body runs again (see verify-scheduler *)
(*   "Known window").                                                      *)
(*                                                                         *)
(* Properties: AtMostOnceSuccess, FailedJobRetryable, NoJobWhenDisabled    *)
(*   (invariants) and NoStuckRunning (temporal).                           *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS NWorkers, MaxCalls, MaxRestarts, BrokenMode

ASSUME BrokenMode \in {"none", "retry_after_success", "split_claim", "no_reap",
                       "ignore_enabled", "no_retry"}

Workers == 1..NWorkers

VARIABLES
    enabled,       \* HAT_ENABLED
    schedulerUp,   \* the scheduler loop exists
    row,           \* status of the one hat_job_run row: none | running | succeeded | failed
    pc,            \* per caller: idle | checked | body | done
    seen,          \* per caller: row status read by a split (broken) claim
    calls,         \* per caller: run_job calls made
    successes,     \* successes recorded in the row
    restarts,      \* process restarts so far
    started,       \* run_job has been called at least once
    refusedFailed  \* a call found a failed run and refused to retry it

vars == <<enabled, schedulerUp, row, pc, seen, calls, successes, restarts, started,
          refusedFailed>>

Init ==
    /\ enabled \in BOOLEAN
    /\ schedulerUp = FALSE
    /\ row = "none"
    /\ pc = [w \in Workers |-> "idle"]
    /\ seen = [w \in Workers |-> "none"]
    /\ calls = [w \in Workers |-> 0]
    /\ successes = 0
    /\ restarts = 0
    /\ started = FALSE
    /\ refusedFailed = FALSE

\* A run can be taken when none exists, when the last one failed or was interrupted, and
\* (only in the broken variant) after it succeeded.
Claimable(r) ==
    \/ r = "none"
    \/ r = "failed" /\ BrokenMode # "no_retry"
    \/ r = "succeeded" /\ BrokenMode = "retry_after_success"

CanCall(w) == schedulerUp /\ pc[w] = "idle" /\ calls[w] < MaxCalls

\* code: backend/app/hat/ops/scheduler.py:start_scheduler
StartScheduler ==
    /\ ~schedulerUp
    /\ enabled \/ BrokenMode = "ignore_enabled"
    /\ schedulerUp' = TRUE
    /\ UNCHANGED <<enabled, row, pc, seen, calls, successes, restarts, started, refusedFailed>>

\* code: backend/app/hat/ops/jobs.py:_claim  (insert the row, or flip a failed row to running,
\* in one transaction; otherwise return the stored outcome)
Claim(w) ==
    /\ BrokenMode # "split_claim"
    /\ CanCall(w)
    /\ calls' = [calls EXCEPT ![w] = @ + 1]
    /\ started' = TRUE
    /\ IF Claimable(row)
          THEN /\ row' = "running"
               /\ pc' = [pc EXCEPT ![w] = "body"]
               /\ refusedFailed' = refusedFailed
          ELSE /\ pc' = [pc EXCEPT ![w] = "done"]
               /\ refusedFailed' = (refusedFailed \/ row = "failed")
               /\ row' = row
    /\ UNCHANGED <<enabled, schedulerUp, seen, successes, restarts>>

\* code: backend/app/hat/ops/jobs.py:_claim  (broken variant "split_claim" only: the real claim
\* is the single transaction above, never a read followed by a separate write)
CheckOnly(w) ==
    /\ BrokenMode = "split_claim"
    /\ CanCall(w)
    /\ calls' = [calls EXCEPT ![w] = @ + 1]
    /\ started' = TRUE
    /\ seen' = [seen EXCEPT ![w] = row]
    /\ pc' = [pc EXCEPT ![w] = "checked"]
    /\ UNCHANGED <<enabled, schedulerUp, row, successes, restarts, refusedFailed>>

\* code: backend/app/hat/ops/jobs.py:_claim  (broken variant "split_claim" only: the write half)
TakeAfterCheck(w) ==
    /\ pc[w] = "checked"
    /\ IF Claimable(seen[w])
          THEN /\ row' = "running"
               /\ pc' = [pc EXCEPT ![w] = "body"]
          ELSE /\ row' = row
               /\ pc' = [pc EXCEPT ![w] = "done"]
    /\ UNCHANGED <<enabled, schedulerUp, seen, calls, successes, restarts, started,
                   refusedFailed>>

\* code: backend/app/hat/ops/jobs.py:_finish  (the body returned: record succeeded)
FinishOk(w) ==
    /\ pc[w] = "body"
    /\ row = "running"
    /\ row' = "succeeded"
    /\ successes' = successes + 1
    /\ pc' = [pc EXCEPT ![w] = "done"]
    /\ UNCHANGED <<enabled, schedulerUp, seen, calls, restarts, started, refusedFailed>>

\* code: backend/app/hat/ops/jobs.py:_finish  (the body raised: record failed with a code)
FinishFail(w) ==
    /\ pc[w] = "body"
    /\ row = "running"
    /\ row' = "failed"
    /\ pc' = [pc EXCEPT ![w] = "done"]
    /\ UNCHANGED <<enabled, schedulerUp, seen, calls, successes, restarts, started,
                   refusedFailed>>

\* code: backend/app/hat/ops/scheduler.py:tick  (a later call by the same caller: the scheduler
\* ticks again)
Recall(w) ==
    /\ pc[w] = "done"
    /\ pc' = [pc EXCEPT ![w] = "idle"]
    /\ UNCHANGED <<enabled, schedulerUp, row, seen, calls, successes, restarts, started,
                   refusedFailed>>

\* code: backend/app/hat/ops/jobs.py:recover_interrupted  (new process: in-flight callers are
\* gone, runs still `running` become failed/interrupted before any job runs)
Restart ==
    /\ restarts < MaxRestarts
    /\ restarts' = restarts + 1
    /\ pc' = [w \in Workers |-> IF pc[w] \in {"body", "checked"} THEN "done" ELSE pc[w]]
    /\ row' = IF row = "running" /\ BrokenMode # "no_reap" THEN "failed" ELSE row
    /\ UNCHANGED <<enabled, schedulerUp, seen, calls, successes, started, refusedFailed>>

Next ==
    \/ StartScheduler
    \/ Restart
    \/ \E w \in Workers :
          Claim(w) \/ CheckOnly(w) \/ TakeAfterCheck(w) \/ FinishOk(w) \/ FinishFail(w)
          \/ Recall(w)

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(StartScheduler)
    /\ \A w \in Workers :
          WF_vars(Claim(w)) /\ WF_vars(TakeAfterCheck(w)) /\ WF_vars(FinishOk(w))
          /\ WF_vars(FinishFail(w)) /\ WF_vars(Recall(w))

TypeOK ==
    /\ row \in {"none", "running", "succeeded", "failed"}
    /\ pc \in [Workers -> {"idle", "checked", "body", "done"}]
    /\ successes \in 0..(NWorkers * MaxCalls)

\* A body succeeds at most once, and a body never runs while another does or after success.
AtMostOnceSuccess ==
    /\ successes <= 1
    /\ Cardinality({w \in Workers : pc[w] = "body"}) <= 1
    /\ (row = "succeeded" => \A w \in Workers : pc[w] # "body")

\* A call that finds a failed or interrupted run is allowed to run it again.
FailedJobRetryable == ~refusedFailed

\* With HAT disabled, no job is ever started.
NoJobWhenDisabled == ~enabled => (~schedulerUp /\ ~started)

\* A run never stays `running` forever (a crash is reaped at restart).
NoStuckRunning == (row = "running") ~> (row # "running")
==============================================================================

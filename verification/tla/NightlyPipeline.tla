---------------------------- MODULE NightlyPipeline ----------------------------
(***************************************************************************)
(* The nightly ingest window and the pipeline behind it:     *)
(* probe from 18:00 every 10 minutes, fetch only after a matching     *)
(* probe, the bhavcopy fallback at 22:00, then quality gate, features,     *)
(* regime, allocate, scan and watchlist digest in that order, with the     *)
(* corporate-action adjustment after the ingest whatever the gate decides  *)
(* and before features (PR 33), and the bot snapshot (drawdown kills) on   *)
(* its own, waiting on no step (PR 33). Split out of JobScheduler in PR   *)
(* 7: the claim protocol and this chain are independent, and their product *)
(* would not be checkable in a few seconds.                                *)
(*                                                                         *)
(* Bounds: Cut = 3 ten-minute slots between 18:00 (slot 0) and the 22:00  *)
(*   cutoff (slot Cut), standing for the 24 real slots; BrokenMode names   *)
(*   a deliberately broken variant ("none" is the real protocol). One      *)
(*   trade date, one open position whose exit falls due once, seven        *)
(*   pipeline steps (gate = 1, adjust = 2, features = 3, regime = 4,       *)
(*   allocate = 5, scan = 6, digest = 7) each run at most until it         *)
(*   succeeds, a failed step can run again.                                *)
(* Assumptions: each action below is one database transaction or one       *)
(*   environment change (atomic); the MCP server becomes ready at an       *)
(*   arbitrary slot or never; a fetch or the fallback may succeed or fail; *)
(*   a probe that fails counts as "not yet"; the clock only moves forward  *)
(*   and a probe, fetch or exit that can run eventually does (weak         *)
(*   fairness), while a pipeline step is never forced to run, except that  *)
(*   the adjustment eventually succeeds once the day is ingested (its      *)
(*   lookups never fail it; it is retried) and the snapshot eventually     *)
(*   runs (weak fairness on both); exits and stops are driven by live      *)
(*   prices, not by this chain.                                            *)
(*                                                                         *)
(* Properties: FallbackAt2200, NoDataNoScan, NoScanAfterGateFail,          *)
(*   NightlyPipelineOrder (invariants) and IngestDecides,                  *)
(*   ExitsIndependentOfPipeline, AdjustIndependentOfGate,                  *)
(*   SnapshotEveryNight (temporal).                                        *)
(***************************************************************************)
EXTENDS Integers, FiniteSets

CONSTANTS Cut, BrokenMode

ASSUME BrokenMode \in {"none", "probe_twice", "probe_at_cutoff", "fetch_without_match",
                       "fallback_always", "no_fallback", "nodata_no_kill",
                       "nodata_no_alert", "skip_order", "scan_ignores_gate",
                       "gate_fail_no_kill", "exit_needs_pipeline", "adjust_needs_gate",
                       "snapshot_needs_allocate"}

Steps == 1..7

VARIABLES
    now,           \* the ten-minute slot: 0 is 18:00, Cut is 22:00
    mcpReady,      \* the MCP bulk quote answers with the trade date
    phase,         \* polling | fetching | ingested | nodata
    matched,       \* a probe returned the trade date
    fetchStarted,  \* ghost: the full fetch has started
    probesInSlot,  \* probes started in the current slot
    lastProbe,     \* ghost: the slot of the latest probe (-1 for none)
    fallbackRuns,  \* bhavcopy fallback runs
    rawCommits,    \* committed raw partitions for the trade date
    healthKill,    \* the data-health kill: bots take no new entry
    alerted,       \* a health alert was raised
    st,            \* per pipeline step: none | succeeded | failed
    exitDue,       \* an exit rule has fired for the open position
    exitUsed,      \* the exit has fallen due once
    snapDone       \* snapshot.bots has measured the drawdown kills for the day

vars == <<now, mcpReady, phase, matched, fetchStarted, probesInSlot, lastProbe, fallbackRuns,
          rawCommits, healthKill, alerted, st, exitDue, exitUsed, snapDone>>

Init ==
    /\ now = 0
    /\ mcpReady = FALSE
    /\ phase = "polling"
    /\ matched = FALSE
    /\ fetchStarted = FALSE
    /\ probesInSlot = 0
    /\ lastProbe = -1
    /\ fallbackRuns = 0
    /\ rawCommits = 0
    /\ healthKill = FALSE
    /\ alerted = FALSE
    /\ st = [k \in Steps |-> "none"]
    /\ exitDue = FALSE
    /\ exitUsed = FALSE
    /\ snapDone = FALSE

\* code: backend/app/hat/data/ingest.py:wait_for_data  (the clock reaches the next 10-minute slot)
Tick ==
    /\ now < Cut
    /\ now' = now + 1
    /\ probesInSlot' = 0
    /\ UNCHANGED <<mcpReady, phase, matched, fetchStarted, lastProbe, fallbackRuns, rawCommits,
                   healthKill, alerted, st, exitDue, exitUsed, snapDone>>

\* code: backend/app/hat/data/ingest.py:_probe  (the server starts answering with the trade date)
McpBecomesReady ==
    /\ ~mcpReady
    /\ mcpReady' = TRUE
    /\ UNCHANGED <<now, phase, matched, fetchStarted, probesInSlot, lastProbe, fallbackRuns,
                   rawCommits, healthKill, alerted, st, exitDue, exitUsed, snapDone>>

\* code: backend/app/hat/data/ingest.py:wait_for_data  (one probe: at most one per slot, none at
\* or after the cutoff)
Probe ==
    /\ phase = "polling"
    /\ probesInSlot < (IF BrokenMode = "probe_twice" THEN 2 ELSE 1)
    /\ now < Cut \/ BrokenMode = "probe_at_cutoff"
    /\ probesInSlot' = probesInSlot + 1
    /\ lastProbe' = now
    /\ IF mcpReady
          THEN /\ matched' = TRUE
               /\ phase' = "fetching"
          ELSE /\ matched' = matched
               /\ phase' = phase
    /\ UNCHANGED <<now, mcpReady, fetchStarted, fallbackRuns, rawCommits, healthKill, alerted,
                   st, exitDue, exitUsed, snapDone>>

\* code: backend/app/hat/data/ingest.py:_fetch_mcp  (the full fetch and the raw write, or a
\* failed fetch that fails the run, trips the health kill and alerts)
Fetch ==
    /\ phase = "fetching" \/ (BrokenMode = "fetch_without_match" /\ phase = "polling")
    /\ fetchStarted' = TRUE
    /\ \/ /\ rawCommits' = rawCommits + 1
          /\ phase' = "ingested"
          /\ UNCHANGED <<healthKill, alerted>>
       \/ /\ phase' = "nodata"
          /\ healthKill' = (IF BrokenMode = "nodata_no_kill" THEN healthKill ELSE TRUE)
          /\ alerted' = (IF BrokenMode = "nodata_no_alert" THEN alerted ELSE TRUE)
          /\ UNCHANGED rawCommits
    /\ UNCHANGED <<now, mcpReady, matched, probesInSlot, lastProbe, fallbackRuns, st, exitDue,
                   exitUsed, snapDone>>

\* code: backend/app/hat/data/ingest.py:bhavcopy_day  (22:00 with no matching probe: the bhavcopy
\* download, at most once; it commits the day or finds nothing, which fails the run)
Fallback ==
    /\ BrokenMode # "no_fallback"
    /\ now = Cut
    /\ fallbackRuns = 0
    /\ phase = "polling" \/ (BrokenMode = "fallback_always" /\ phase # "nodata")
    /\ fallbackRuns' = 1
    /\ \/ /\ rawCommits' = rawCommits + 1
          /\ phase' = "ingested"
          /\ UNCHANGED <<healthKill, alerted>>
       \/ /\ phase' = "nodata"
          /\ healthKill' = (IF BrokenMode = "nodata_no_kill" THEN healthKill ELSE TRUE)
          /\ alerted' = (IF BrokenMode = "nodata_no_alert" THEN alerted ELSE TRUE)
          /\ UNCHANGED rawCommits
    /\ UNCHANGED <<now, mcpReady, matched, fetchStarted, probesInSlot, lastProbe, st, exitDue,
                   exitUsed, snapDone>>

\* code: backend/app/hat/data/quality.py:gate_body  (step 1: a pass clears the data-health kill; a
\* fail commits its rows, trips the kill and alerts, in one transaction)
Gate ==
    /\ phase = "ingested"
    /\ st[1] # "succeeded"
    /\ \/ /\ st' = [st EXCEPT ![1] = "succeeded"]
          /\ healthKill' = FALSE
          /\ UNCHANGED alerted
       \/ /\ st' = [st EXCEPT ![1] = "failed"]
          /\ healthKill' = (IF BrokenMode = "gate_fail_no_kill" THEN healthKill ELSE TRUE)
          /\ alerted' = TRUE
    /\ UNCHANGED <<now, mcpReady, phase, matched, fetchStarted, probesInSlot, lastProbe,
                   fallbackRuns, rawCommits, exitDue, exitUsed, snapDone>>

\* What a step waits for (backend/app/hat/daily/nightly.py:nightly_jobs, PR 33): the adjustment
\* only the ingested day; features the gate and the adjustment; every later step the one before.
Ready(k) ==
    CASE k = 2 -> phase = "ingested" /\ (BrokenMode # "adjust_needs_gate" \/ st[1] = "succeeded")
      [] k = 3 -> st[1] = "succeeded" /\ st[2] = "succeeded"
      [] OTHER -> st[k - 1] = "succeeded"

\* code: backend/app/hat/ops/scheduler.py:due  (steps 2 to 7: a step is registered `after` the
\* steps it waits for, so it starts only after they succeeded; a succeeded step never runs again)
Step(k, r) ==
    /\ k \in 2..7
    /\ st[k] # "succeeded"
    /\ IF BrokenMode = "skip_order" \/ (BrokenMode = "scan_ignores_gate" /\ k = 6)
          THEN phase = "ingested"
          ELSE Ready(k)
    /\ st' = [st EXCEPT ![k] = r]
    /\ UNCHANGED <<now, mcpReady, phase, matched, fetchStarted, probesInSlot, lastProbe,
                   fallbackRuns, rawCommits, healthKill, alerted, exitDue, exitUsed, snapDone>>

LaterStep(k) == \E r \in {"succeeded", "failed"} : Step(k, r)

\* code: backend/app/hat/daily/nightly.py:Nightly.snapshot  (snapshot.bots at 23:30 on the latest
\* allocation on or before the day: it waits on no step, PR 33)
Snapshot ==
    /\ ~snapDone
    /\ BrokenMode # "snapshot_needs_allocate" \/ st[5] = "succeeded"
    /\ snapDone' = TRUE
    /\ UNCHANGED <<now, mcpReady, phase, matched, fetchStarted, probesInSlot, lastProbe,
                   fallbackRuns, rawCommits, healthKill, alerted, st, exitDue, exitUsed>>

\* code: backend/app/hat/risk/decision.py:_exit_decision  (a price-driven exit rule fires, once)
ExitBecomesDue ==
    /\ ~exitUsed
    /\ exitDue' = TRUE
    /\ exitUsed' = TRUE
    /\ UNCHANGED <<now, mcpReady, phase, matched, fetchStarted, probesInSlot, lastProbe,
                   fallbackRuns, rawCommits, healthKill, alerted, st, snapDone>>

\* code: backend/app/hat/risk/decision.py:_exit_decision  (the exit is sent: it reads no pipeline state)
ExitSend ==
    /\ exitDue
    /\ BrokenMode # "exit_needs_pipeline" \/ st[7] = "succeeded"
    /\ exitDue' = FALSE
    /\ UNCHANGED <<now, mcpReady, phase, matched, fetchStarted, probesInSlot, lastProbe,
                   fallbackRuns, rawCommits, healthKill, alerted, st, exitUsed, snapDone>>

Next ==
    \/ Tick
    \/ McpBecomesReady
    \/ Probe
    \/ Fetch
    \/ Fallback
    \/ Gate
    \/ \E k \in Steps : LaterStep(k)
    \/ ExitBecomesDue
    \/ ExitSend
    \/ Snapshot

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(Tick)
    /\ WF_vars(Probe)
    /\ WF_vars(Fetch)
    /\ WF_vars(Fallback)
    /\ WF_vars(ExitSend)
    /\ WF_vars(Step(2, "succeeded"))
    /\ WF_vars(Snapshot)

TypeOK ==
    /\ now \in 0..Cut
    /\ phase \in {"polling", "fetching", "ingested", "nodata"}
    /\ st \in [Steps -> {"none", "succeeded", "failed"}]
    /\ probesInSlot \in 0..2
    /\ lastProbe \in -1..Cut
    /\ fallbackRuns \in 0..1
    /\ rawCommits \in 0..2

\* The full fetch starts only after a matching probe; no probe is closer than one slot to the
\* last or at the cutoff; the fallback runs only at 22:00 without a matching probe, at most once;
\* a trade date has at most one committed raw partition.
FallbackAt2200 ==
    /\ fetchStarted => matched
    /\ lastProbe < Cut
    /\ probesInSlot <= 1
    /\ fallbackRuns <= 1
    /\ fallbackRuns = 1 => (now = Cut /\ ~matched)
    /\ rawCommits <= 1

\* The ingest always reaches a decision: data committed, or no data.
IngestDecides == (phase = "polling") ~> (phase # "polling")

\* With no committed raw partition no step runs; a run that found no data tripped the kill and
\* alerted.
NoDataNoScan ==
    /\ rawCommits = 0 => \A k \in Steps : st[k] = "none"
    /\ phase = "nodata" => (healthKill /\ alerted /\ st[6] # "succeeded")

\* A failed gate means the kill is tripped and no scan has succeeded; a scan success needs a
\* gate success.
NoScanAfterGateFail ==
    /\ st[1] = "failed" => (healthKill /\ st[6] # "succeeded")
    /\ st[6] = "succeeded" => st[1] = "succeeded"

\* No step succeeds before the steps it waits for have: the gate and the adjustment need the day
\* ingested, features both of them, and every later step the one before it.
NightlyPipelineOrder ==
    /\ \A k \in 4..7 : st[k] = "succeeded" => st[k - 1] = "succeeded"
    /\ st[3] = "succeeded" => (st[1] = "succeeded" /\ st[2] = "succeeded")
    /\ st[2] = "succeeded" => phase = "ingested"
    /\ st[1] = "succeeded" => phase = "ingested"

\* A fired exit is sent whatever state the pipeline is in.
ExitsIndependentOfPipeline == exitDue ~> ~exitDue

\* An ingested day gets its corporate-action adjustment (the next session's actions of held
\* instruments looked up) even when the gate fails.
AdjustIndependentOfGate == (phase = "ingested") ~> (st[2] = "succeeded")

\* The drawdown kills are measured every trading night, whatever the ingest and the gate did. The
\* model's clock covers only 18:00-22:00, so `Snapshot` has no time guard: its 23:30 time is the
\* scheduler's (`ops.job_registry` checks it), and this property checks only that no step gates it.
SnapshotEveryNight == <>snapDone
==============================================================================
\* Checked by: verification/tla/run.sh NightlyPipeline

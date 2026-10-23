------------------------------ MODULE DailyCycle ------------------------------
(***************************************************************************)
(* The order of one trade date's jobs as the scheduler starts them:   *)
(* the intraday sequence (HAT-OPS-023) and the nightly regime ->   *)
(* allocate step (HAT-PORT-053). A job starts only on its date, at or      *)
(* after its time, before the end of its window, and only once every job   *)
(* it is registered `after` has succeeded for the same trade date          *)
(* (scheduler.py `due`). Split out of JobScheduler in PR 17 for the same   *)
(* reason as NightlyPipeline: the claim protocol is independent of the     *)
(* order and the product would not check in a few seconds.                 *)
(*                                                                         *)
(* Bounds: one trade date; the clock is eleven slots, one per scheduled    *)
(*   time (0 before 08:30, 1 08:30 login.prompt, 2 08:45 live.prepare, 3   *)
(*   09:00 preopen.gaps, 4 09:16 gapscan, 5 09:20 first bar final and the  *)
(*   regime-gate refresh, 6 15:20 exits.close, 7 15:25 gtt.swap, 8 15:30   *)
(*   trade.end_session, 9 15:40 reports, 10 18:00 the nightly chain);      *)
(*   MaxEntries = 2 entry orders in the day; one open position held from   *)
(*   an earlier day; BrokenMode names a deliberately broken variant        *)
(*   ("none" is the real design).                                          *)
(* Assumptions: each job run, entry and environment change is one atomic   *)
(*   step that reads the clock once; the clock only moves forward; the     *)
(*   scheduler runs from before 08:30, so a job's first start is at its    *)
(*   registered time or never (a restart later in the day starts what is   *)
(*   due at once, concurrently: outside this model); the order checked is  *)
(*   that of first starts, and a retry of a job whose outcome nothing      *)
(*   waits on (prompts, preparation, gap capture and scan, session end,    *)
(*   reports) is not modelled; the gate refresh, exits.close, regime and   *)
(*   allocate succeed or fail and a failed one may run again inside its    *)
(*   window; the broker session comes and goes at any time; an             *)
(*   exits.close run that started at 15:24 and still runs at 15:25 is      *)
(*   outside this model (each run is one step): gtt.swap leaves a          *)
(*   position whose exit sale is working and tries it again                *)
(*   (gtt_upkeep.py swap, EXIT_WORKING). The ingest, gate, adjustment      *)
(*   and features before regime are NightlyPipeline's; regime here         *)
(*   starts at 18:00 with them assumed done.                               *)
(*                                                                         *)
(* Properties: IntradayOrder, AllocateAfterRegime (invariants).            *)
(***************************************************************************)
EXTENDS Integers, FiniteSets, Sequences

CONSTANTS MaxEntries, BrokenMode

ASSUME BrokenMode \in {"none", "gapscan_before_preopen", "trigger_after_1520",
                       "no_gate_refresh", "no_session_check", "swap_before_exits",
                       "allocate_ignores_regime", "allocate_resizes"}
ASSUME MaxEntries \in Nat \ {0}

\* The intraday jobs in the order HAT-OPS-023 states; "gate" is the 09:20 regime-gate refresh.
Chain == <<"login", "prepare", "preopen", "gapscan", "gate", "exits", "swap", "endsession",
           "reports">>
Intraday == {Chain[i] : i \in 1..Len(Chain)}
Outcome == {"gate", "exits"}          \* intraday jobs whose success something waits on

Never == 11                           \* a window that does not end within the day

\* The registered time of each job (a slot) and the end of its window.
At(j) ==
    CASE j = "login" -> 1
      [] j = "prepare" -> 2
      [] j = "preopen" -> 3
      [] j = "gapscan" -> IF BrokenMode = "gapscan_before_preopen" THEN 3 ELSE 4
      [] j = "gate" -> 5
      [] j = "exits" -> 6
      [] j = "swap" -> IF BrokenMode = "swap_before_exits" THEN 6 ELSE 7
      [] j = "endsession" -> 8
      [] j = "reports" -> 9

Until(j) ==
    CASE j = "preopen" -> 4           \* 09:00-09:08, before the 09:16 slot
      [] j = "gate" -> 6              \* refreshed before the trigger window ends
      [] j = "exits" -> 7             \* 15:20-15:25
      [] OTHER -> Never

Pos(j) == CHOOSE i \in 1..Len(Chain) : Chain[i] = j

\* The first slot of the trigger window (the 09:20 bar is final) and its end (15:20).
TriggerFrom == 5
TriggerUntil == IF BrokenMode = "trigger_after_1520" THEN 7 ELSE 6

VARIABLES
    t,               \* the clock slot (see Bounds)
    ran,             \* intraday jobs started at least once today
    st,              \* gate, exits.close, regime, allocate: none | succeeded | failed
    loggedIn,        \* the account's hat_broker_session is live for the trade date
    entries,         \* entry orders placed today
    outOfOrder,      \* ghost: a job of Chain started after a later job of Chain had started
    exitsAfterSwap,  \* ghost: an exits.close run started after gtt.swap had started
    badEntry,        \* ghost: an entry was placed before 09:20, from 15:20, without today's
                     \*   gate refresh or without a live broker session
    posQty,          \* the open position's quantity (1 when opened)
    posStop,         \* the open position's stop (1 when opened)
    weight           \* the sleeve's weight for new entries: old | new

vars == <<t, ran, st, loggedIn, entries, outOfOrder, exitsAfterSwap, badEntry, posQty, posStop,
          weight>>

Init ==
    /\ t = 0
    /\ ran = {}
    /\ st = [j \in {"gate", "exits", "regime", "allocate"} |-> "none"]
    /\ loggedIn = FALSE
    /\ entries = 0
    /\ outOfOrder = FALSE
    /\ exitsAfterSwap = FALSE
    /\ badEntry = FALSE
    /\ posQty = 1
    /\ posStop = 1
    /\ weight = "old"

\* code: backend/app/hat/ops/scheduler.py:HatScheduler.tick  (the scheduler loop's clock reaches
\* the next registered time)
Tick ==
    /\ t < 10
    /\ t' = t + 1
    /\ UNCHANGED <<ran, st, loggedIn, entries, outOfOrder, exitsAfterSwap, badEntry, posQty,
                   posStop, weight>>

\* code: backend/app/hat/execution/session.py:record_session  (environment: the daily broker
\* login is recorded, or the session drops)
SessionFlip ==
    /\ loggedIn' = ~loggedIn
    /\ UNCHANGED <<t, ran, st, entries, outOfOrder, exitsAfterSwap, badEntry, posQty, posStop,
                   weight>>

\* code: backend/app/hat/ops/scheduler.py:due  (an intraday job starts when it falls due: its first
\* start is at its registered time; the gate refresh and exits.close may run again after a
\* failure, inside their windows; run_job records the outcome)
RunIntraday(j) ==
    /\ j \in Intraday
    /\ IF j \in ran
          THEN /\ j \in Outcome
               /\ st[j] = "failed"
               /\ t < Until(j)
          ELSE t = At(j)
    /\ ran' = ran \cup {j}
    /\ outOfOrder' = (outOfOrder \/ (j \notin ran /\ \E k \in ran : Pos(k) > Pos(j)))
    /\ exitsAfterSwap' = (exitsAfterSwap \/ (j = "exits" /\ "swap" \in ran))
    /\ IF j \in Outcome
          THEN \E r \in {"succeeded", "failed"} : st' = [st EXCEPT ![j] = r]
          ELSE UNCHANGED st
    /\ UNCHANGED <<t, loggedIn, entries, badEntry, posQty, posStop, weight>>

\* code: backend/app/hat/execution/router.py:placement_refusal  (a trigger fired on a final
\* 5-minute bar (engine/trigger.py in_window) is placed as an entry order: inside 09:20-15:20,
\* with today's gate refresh on record and a live broker session)
Entry ==
    /\ entries < MaxEntries
    /\ TriggerFrom <= t /\ t < TriggerUntil
    /\ BrokenMode = "no_gate_refresh" \/ st["gate"] = "succeeded"
    /\ BrokenMode = "no_session_check" \/ loggedIn
    /\ entries' = entries + 1
    /\ badEntry' = (badEntry \/ t < 5 \/ t >= 6 \/ st["gate"] # "succeeded" \/ ~loggedIn)
    /\ UNCHANGED <<t, ran, st, loggedIn, outOfOrder, exitsAfterSwap, posQty, posStop, weight>>

\* code: backend/app/hat/engine/portfolio/jobs.py:regime_job  (18:00 onward, after the nightly
\* chain before it: classify the trade date; a failure can run again)
Regime ==
    /\ t = 10
    /\ st["regime"] # "succeeded"
    /\ \E r \in {"succeeded", "failed"} : st' = [st EXCEPT !["regime"] = r]
    /\ UNCHANGED <<t, ran, loggedIn, entries, outOfOrder, exitsAfterSwap, badEntry, posQty,
                   posStop, weight>>

\* code: backend/app/hat/engine/portfolio/jobs.py:allocate_job  (registered after regime: the
\* new weights apply to new entries only; open positions keep quantity and stop)
Allocate ==
    /\ t = 10
    /\ st["allocate"] # "succeeded"
    /\ BrokenMode = "allocate_ignores_regime" \/ st["regime"] = "succeeded"
    /\ \E r \in {"succeeded", "failed"} :
          /\ st' = [st EXCEPT !["allocate"] = r]
          /\ weight' = (IF r = "succeeded" THEN "new" ELSE weight)
          /\ posQty' = (IF r = "succeeded" /\ BrokenMode = "allocate_resizes" THEN 0 ELSE posQty)
    /\ UNCHANGED <<t, ran, loggedIn, entries, outOfOrder, exitsAfterSwap, badEntry, posStop>>

Next ==
    \/ Tick
    \/ SessionFlip
    \/ \E j \in Intraday : RunIntraday(j)
    \/ Entry
    \/ Regime
    \/ Allocate

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ t \in 0..10
    /\ ran \subseteq Intraday
    /\ st \in [{"gate", "exits", "regime", "allocate"} -> {"none", "succeeded", "failed"}]
    /\ entries \in 0..MaxEntries
    /\ posQty \in 0..1
    /\ weight \in {"old", "new"}

\* The intraday jobs start in the order (login prompt, live preparation, pre-open gap
\* capture, gap scan, the 09:20 gate refresh, MA exits, GTT swap, session end, reports); no
\* exits.close run starts once gtt.swap has; every entry order is placed inside 09:20-15:20,
\* after today's gate refresh and with a live broker session.
IntradayOrder ==
    /\ ~outOfOrder
    /\ ~exitsAfterSwap
    /\ ~badEntry

\* allocate starts for the trade date only after regime succeeded for it, and allocation never
\* changes the open position's quantity or stop.
AllocateAfterRegime ==
    /\ st["allocate"] # "none" => st["regime"] = "succeeded"
    /\ posQty = 1
    /\ posStop = 1
==============================================================================

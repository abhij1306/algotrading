---------------------------- MODULE StopPlacement ----------------------------
(***************************************************************************)
(* Broker-side stops of one real position (07 §6; 01 B17; 03 §8; 06 §7;   *)
(* 10-ops §3).                                                             *)
(*                                                                         *)
(* An entry fill gets an intraday stop order before anything else touches  *)
(* the position (a stop that already rests is resized instead). The stop   *)
(* follows `current_stop` up, never down. Around 15:25 a GTT is placed and *)
(* the intraday stop cancelled only once the GTT is confirmed; at the      *)
(* close the broker expires any intraday stop. Each night the GTT is       *)
(* refreshed to the current stop, or alerted and placed again. A GTT can   *)
(* trigger and not fill (a gap below its limit): the sync or the verify    *)
(* sees it, alerts, and protects the position again.                       *)
(*                                                                         *)
(* Bounds: MaxQty = 2 entry fills (a partial fill, then the rest); MaxStop *)
(*   = 3 stop levels (the stop rises at most twice); sessions repeat       *)
(*   without bound through four phases (open, swap from 15:25, after the   *)
(*   close, night); BrokenMode names a deliberately broken variant ("none" *)
(*   is the real code).                                                    *)
(* Assumptions: each action is one database transaction plus at most one   *)
(*   broker call, or one change of the environment; the database and the   *)
(*   broker survive a crash, so a restart resumes from the stored state    *)
(*   (an unprotected fill is still `protecting`); any broker call may fail *)
(*   (a rejection after the one retry, or no answer), and a failure that   *)
(*   leaves the position without a stop raises a health alert; a stop      *)
(*   placed anew is placed at the current stop; the quantity a stop covers *)
(*   is not modelled (resizing is proved by PBT manage.real_fill_protected *)
(*   on the real code); the scheduler runs gtt.swap before the close and   *)
(*   gtt.verify at night (weak fairness), and time passes (weak fairness); *)
(*   a day stop is a stop-market order, so when it triggers it fills.      *)
(*                                                                         *)
(* Properties: StopAfterFill, AlwaysProtected, BrokerStopMatchesCurrent,   *)
(*   NoUnprotectedGap, NightlyVerifyCovers, StopOnlyTightens (invariants)  *)
(*   and GttUnfilledAlerted (temporal).                                    *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxQty, MaxStop, BrokenMode

ASSUME BrokenMode \in {"none", "act_before_stop", "no_protect", "place_above",
                       "cancel_first", "verify_skips_missing", "reset_on_follow",
                       "no_unfilled_alert"}

Phases == {"open", "swap", "after", "night"}
InSession == {"open", "swap"}
Prices == 0..(MaxStop + 1)
None == [st |-> "none", px |-> 0]

VARIABLES
    qty,            \* the open quantity of the real position
    closed,         \* the position was sold (stop or exit fill)
    cur,            \* hat_position.current_stop
    day,            \* the intraday stop at the broker: [st: none | working, px]
    gtt,            \* the GTT at the broker: [st: none | working | gone, px]; gone = triggered,
                    \* not filled, not yet seen by HAT
    phase,          \* open | swap (from 15:25) | after (the close) | night
    pc,             \* idle | protecting (a fill whose stop work has not run yet)
    acted,          \* another HAT action on the position ran while it was protecting
    alerted,        \* a health alert names the position and no stop was placed since
    lastPx,         \* the last price a broker stop was set to
    lowered,        \* some broker stop price was set below the previous one
    cancelledEarly, \* the intraday stop was cancelled while no GTT was confirmed
    swapTried,      \* gtt.swap ran for the position this session
    swapFailed,     \* the GTT of the swap failed
    verifyDone,     \* gtt.verify ran tonight
    nightAlert,     \* tonight's verify alerted the position
    replaceTried    \* tonight's verify tried to place a new GTT

vars == <<qty, closed, cur, day, gtt, phase, pc, acted, alerted, lastPx, lowered,
          cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried>>

Held == ~closed /\ qty > 0
Protected == day.st = "working" \/ gtt.st = "working"
\* HAT's book shows a working stop (a GTT that triggered unseen still looks working to HAT).
Booked == day.st = "working" \/ gtt.st # "none"
\* The price a new stop is placed at (the broken variant places it above the current stop).
NewPx == IF BrokenMode = "place_above" THEN cur + 1 ELSE cur
SetPx(p) == /\ lowered' = (lowered \/ p < lastPx)
            /\ lastPx' = p

Init ==
    /\ qty = 0
    /\ closed = FALSE
    /\ cur = 1
    /\ day = None
    /\ gtt = None
    /\ phase = "open"
    /\ pc = "idle"
    /\ acted = FALSE
    /\ alerted = FALSE
    /\ lastPx = 0
    /\ lowered = FALSE
    /\ cancelledEarly = FALSE
    /\ swapTried = FALSE
    /\ swapFailed = FALSE
    /\ verifyDone = FALSE
    /\ nightAlert = FALSE
    /\ replaceTried = FALSE

\* A HAT action on the position other than its stop work: in the real code it waits for the
\* stop work of a fill (broken variant "act_before_stop": it does not).
MayAct == pc = "idle" \/ BrokenMode = "act_before_stop"
NoteAct == acted' = (acted \/ pc = "protecting")

\* code: backend/app/hat/execution/fills.py:FillSync._apply  (an entry fill is recorded, then its
\* stop work runs: the position is `protecting` until it has)
EntryFill ==
    /\ phase \in InSession
    /\ ~closed
    /\ qty < MaxQty
    /\ qty' = qty + 1
    /\ pc' = IF BrokenMode = "no_protect" THEN pc ELSE "protecting"
    /\ UNCHANGED <<closed, cur, day, gtt, phase, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/execution/protect.py:StopDesk.protect  (a resting stop is resized; with
\* none, a day stop in the session before 15:25 or a GTT from then on; a failure is alerted)
Protect ==
    /\ pc = "protecting"
    /\ pc' = "idle"
    /\ IF Booked
          THEN UNCHANGED <<day, gtt, alerted, lastPx, lowered, swapFailed>>
          ELSE \E ok \in BOOLEAN :
                 IF ok
                    THEN /\ IF phase = "open"
                               THEN /\ day' = [st |-> "working", px |-> NewPx]
                                    /\ gtt' = gtt
                               ELSE /\ gtt' = [st |-> "working", px |-> NewPx]
                                    /\ day' = day
                         /\ swapFailed' = (swapFailed /\ phase = "open")
                         /\ alerted' = FALSE
                         /\ SetPx(NewPx)
                    ELSE /\ alerted' = TRUE
                         /\ UNCHANGED <<day, gtt, lastPx, lowered, swapFailed>>
    /\ UNCHANGED <<qty, closed, cur, phase, acted, cancelledEarly, swapTried,
                   verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/execution/fills.py:FillSync._unprotected  (a held position HAT's book
\* shows without a working stop, after an alerted failure, is protected again by the next sync)
Reprotect ==
    /\ pc = "idle"
    /\ Held
    /\ ~Booked
    /\ pc' = "protecting"
    /\ UNCHANGED <<qty, closed, cur, day, gtt, phase, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/engine/live_trades.py:evaluate_real_position  (a live rule raises
\* current_stop; the database refuses a lower one)
RaiseStop ==
    /\ phase \in InSession
    /\ MayAct
    /\ Held
    /\ cur < MaxStop
    /\ cur' = cur + 1
    /\ NoteAct
    /\ UNCHANGED <<qty, closed, day, gtt, phase, pc, alerted, lastPx, lowered, cancelledEarly,
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried>>

\* The price a follow modifies a stop to (broken variant "reset_on_follow": back to the
\* initial stop).
FollowPx == IF BrokenMode = "reset_on_follow" THEN 1 ELSE cur

\* code: backend/app/hat/execution/protect.py:StopDesk.follow  (a working stop below the current
\* stop is modified up to it; a failed modify leaves it and is alerted)
Follow ==
    /\ MayAct
    /\ Held
    /\ \/ day.st = "working" /\ day.px < cur
       \/ gtt.st = "working" /\ gtt.px < cur
    /\ \E ok \in BOOLEAN :
          IF ok
             THEN /\ day' = IF day.st = "working" THEN [day EXCEPT !.px = FollowPx] ELSE day
                  /\ gtt' = IF gtt.st = "working" THEN [gtt EXCEPT !.px = FollowPx] ELSE gtt
                  /\ SetPx(FollowPx)
                  /\ alerted' = alerted
             ELSE /\ alerted' = TRUE
                  /\ UNCHANGED <<day, gtt, lastPx, lowered>>
    /\ NoteAct
    /\ UNCHANGED <<qty, closed, cur, phase, pc, cancelledEarly, swapTried, swapFailed,
                   verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/execution/protect.py:StopDesk.swap  (the GTT first; a failed GTT leaves
\* the day stop working and is alerted; the job tries again a minute later, after the close too)
SwapPlaceGtt ==
    /\ phase \in {"swap", "after"}
    /\ MayAct
    /\ Held
    /\ ~swapTried \/ swapFailed
    /\ swapTried' = TRUE
    /\ IF gtt.st # "none"
          THEN /\ swapFailed' = FALSE
               /\ UNCHANGED <<gtt, alerted, lastPx, lowered>>
          ELSE \E ok \in BOOLEAN :
                 IF ok
                    THEN /\ gtt' = [st |-> "working", px |-> NewPx]
                         /\ SetPx(NewPx)
                         /\ alerted' = FALSE
                         /\ swapFailed' = FALSE
                    ELSE /\ alerted' = TRUE
                         /\ swapFailed' = TRUE
                         /\ UNCHANGED <<gtt, lastPx, lowered>>
    /\ NoteAct
    /\ UNCHANGED <<qty, closed, cur, day, phase, pc, cancelledEarly, verifyDone, nightAlert,
                   replaceTried>>

\* code: backend/app/hat/execution/protect.py:StopDesk._cancel  (the day stop is cancelled once
\* the GTT is confirmed; broken variant "cancel_first": before)
SwapCancelDay ==
    /\ phase = "swap"
    /\ MayAct
    /\ day.st = "working"
    /\ swapTried \/ BrokenMode = "cancel_first"
    /\ gtt.st # "none" \/ BrokenMode = "cancel_first"
    /\ day' = None
    /\ cancelledEarly' = (cancelledEarly \/ gtt.st = "none")
    /\ NoteAct
    /\ UNCHANGED <<qty, closed, cur, gtt, phase, pc, alerted, lastPx, lowered, swapTried,
                   swapFailed, verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/ops/scheduler.py:HatScheduler.due_jobs  (time passes; at the close the
\* broker expires any day stop; a new session starts a new night)
Clock ==
    \/ /\ phase = "open"
       /\ phase' = "swap"
       /\ UNCHANGED <<day, swapTried, verifyDone, nightAlert, replaceTried, swapFailed>>
    \/ /\ phase = "swap"
       /\ swapTried \/ ~Held
       /\ phase' = "after"
       /\ day' = None
       /\ UNCHANGED <<swapTried, verifyDone, nightAlert, replaceTried, swapFailed>>
    \/ /\ phase = "after"
       /\ phase' = "night"
       /\ UNCHANGED <<day, swapTried, verifyDone, nightAlert, replaceTried, swapFailed>>
    \/ /\ phase = "night"
       /\ verifyDone \/ ~Held
       /\ phase' = "open"
       /\ swapTried' = FALSE
       /\ swapFailed' = FALSE
       /\ verifyDone' = FALSE
       /\ nightAlert' = FALSE
       /\ replaceTried' = FALSE
       /\ day' = day
    /\ UNCHANGED <<qty, closed, cur, gtt, pc, acted, alerted, lastPx, lowered, cancelledEarly>>

\* code: backend/app/hat/execution/fills.py:FillSync._apply_gtt_trade  (the GTT triggered and its
\* limit order filled: the position is sold)
GttFills ==
    /\ phase \in InSession
    /\ Held
    /\ gtt.st = "working"
    /\ closed' = TRUE
    /\ qty' = 0
    /\ gtt' = None
    /\ UNCHANGED <<cur, day, phase, pc, acted, alerted, lastPx, lowered, cancelledEarly,
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/execution/fills.py:FillSync._apply  (the day stop triggered; a stop-market
\* order fills)
DayStopFills ==
    /\ phase \in InSession
    /\ Held
    /\ day.st = "working"
    /\ closed' = TRUE
    /\ qty' = 0
    /\ day' = None
    /\ UNCHANGED <<cur, gtt, phase, pc, acted, alerted, lastPx, lowered, cancelledEarly,
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/execution/fills.py:FillSync._unfilled_trigger  (the GTT triggered and
\* its limit order did not fill: the broker no longer holds a stop)
GttTriggersUnfilled ==
    /\ phase \in InSession
    /\ Held
    /\ gtt.st = "working"
    /\ gtt' = [gtt EXCEPT !.st = "gone"]
    /\ UNCHANGED <<qty, closed, cur, day, phase, pc, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/execution/fills.py:FillSync._gtt_unfilled  (the sync sees the untagged
\* sell that ended unfilled: alert, end the GTT, protect again)
SeeUnfilled ==
    /\ phase \in InSession
    /\ gtt.st = "gone"
    /\ gtt' = None
    /\ alerted' = (alerted \/ BrokenMode # "no_unfilled_alert")
    /\ pc' = IF Held THEN "protecting" ELSE pc
    /\ UNCHANGED <<qty, closed, cur, day, phase, acted, lastPx, lowered, cancelledEarly,
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/execution/protect.py:StopDesk.cancel_all  (a sold position's resting
\* stops are cancelled)
CancelAfterSale ==
    /\ closed
    /\ day.st = "working" \/ gtt.st = "working"
    /\ day' = None
    /\ gtt' = None
    /\ UNCHANGED <<qty, closed, cur, phase, pc, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried>>

\* code: backend/app/hat/execution/protect.py:StopDesk.verify  (refresh the GTT to the current
\* stop; a GTT the broker does not confirm is alerted and placed again; an unreachable broker is
\* alerted and nothing is placed)
Verify ==
    /\ phase = "night"
    /\ ~verifyDone
    /\ MayAct
    /\ verifyDone' = TRUE
    /\ NoteAct
    /\ \E reachable \in BOOLEAN, ok \in BOOLEAN :
          CASE ~Held ->
                 UNCHANGED <<gtt, alerted, nightAlert, replaceTried, lastPx, lowered>>
            [] ~reachable ->
                 /\ alerted' = TRUE
                 /\ nightAlert' = TRUE
                 /\ UNCHANGED <<gtt, replaceTried, lastPx, lowered>>
            [] gtt.st = "working" ->
                 /\ gtt' = [gtt EXCEPT !.px = cur]
                 /\ SetPx(cur)
                 /\ UNCHANGED <<alerted, nightAlert, replaceTried>>
            [] BrokenMode = "verify_skips_missing" ->
                 UNCHANGED <<gtt, alerted, nightAlert, replaceTried, lastPx, lowered>>
            [] OTHER ->
                 /\ nightAlert' = TRUE
                 /\ replaceTried' = TRUE
                 /\ IF ok
                       THEN /\ gtt' = [st |-> "working", px |-> NewPx]
                            /\ SetPx(NewPx)
                            /\ alerted' = FALSE
                       ELSE /\ gtt' = None
                            /\ alerted' = TRUE
                            /\ UNCHANGED <<lastPx, lowered>>
    /\ UNCHANGED <<qty, closed, cur, day, phase, pc, cancelledEarly, swapTried, swapFailed>>

Next ==
    \/ EntryFill \/ Protect \/ Reprotect \/ RaiseStop \/ Follow \/ SwapPlaceGtt \/ SwapCancelDay
    \/ Clock \/ GttFills \/ DayStopFills \/ GttTriggersUnfilled \/ SeeUnfilled
    \/ CancelAfterSale \/ Verify

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(Protect) /\ WF_vars(SwapPlaceGtt) /\ WF_vars(SwapCancelDay)
    /\ WF_vars(Clock) /\ WF_vars(SeeUnfilled) /\ WF_vars(Verify)

TypeOK ==
    /\ qty \in 0..MaxQty
    /\ closed \in BOOLEAN
    /\ cur \in 1..MaxStop
    /\ day \in [st : {"none", "working"}, px : Prices]
    /\ gtt \in [st : {"none", "working", "gone"}, px : Prices]
    /\ phase \in Phases
    /\ pc \in {"idle", "protecting"}
    /\ lastPx \in Prices

\* HAT-EXEC-060: after a real entry fill, its broker stop is placed (or resized) before any other
\* action on the position.
StopAfterFill == ~acted

\* HAT-DEC-B17: a held real position always has a resting broker stop, except while the stop
\* work of a fill has not run yet, after a failure that was alerted, or while a GTT that
\* triggered without a fill has not been seen (GttUnfilledAlerted).
AlwaysProtected ==
    Held => (Protected \/ pc = "protecting" \/ alerted \/ gtt.st = "gone")

\* HAT-EXEC-064: a broker stop is never above the current stop (it eventually equals it: Follow
\* and the nightly refresh are retried, and a new stop is placed at the current stop).
BrokerStopMatchesCurrent ==
    /\ day.st = "working" => day.px <= cur
    /\ gtt.st # "none" => gtt.px <= cur

\* HAT-EXEC-061: the intraday stop is cancelled only once the GTT is confirmed placed, and a
\* failed GTT placement is alerted (the day stop keeps working until the close).
NoUnprotectedGap ==
    /\ ~cancelledEarly
    /\ (swapFailed /\ Held) => (alerted \/ Protected)

\* HAT-EXEC-063, HAT-OPS-024: after tonight's verify, a held position has a GTT at the current
\* stop, or the verify alerted it (and tried a new GTT when the broker answered).
NightlyVerifyCovers ==
    (phase = "night" /\ verifyDone /\ Held)
        => ((gtt.st = "working" /\ gtt.px = cur) \/ nightAlert)

\* HAT-RISK-070, HAT-SCH-081, HAT-EXEC-064: the broker stop prices of a position never go down.
StopOnlyTightens == ~lowered

\* HAT-EXEC-062: a GTT that triggered without a fill under a held position is eventually alerted:
\* by the sync in the session (gtt.unfilled), else by the nightly verify (gtt.missing, then placed
\* again), unless the position was sold first.
GttUnfilledAlerted == (gtt.st = "gone" /\ Held) ~> (alerted \/ nightAlert \/ ~Held)
==============================================================================

---------------------------- MODULE StopPlacement ----------------------------
(***************************************************************************)
(* Broker-side stops of one real position (HAT-DEC-B17).                                          *)
(*                                                                         *)
(* An entry fill gets an intraday stop order before anything else touches  *)
(* the position (a stop that already rests is resized instead). The stop   *)
(* follows `current_stop` up, never down. Around 15:25 a GTT is placed and *)
(* the intraday stop cancelled only once the GTT is confirmed; at the      *)
(* close the broker expires any intraday stop. Each night the GTT is       *)
(* refreshed to the current stop, or alerted and placed again. A GTT can   *)
(* trigger and not fill (a gap below its limit): the sync or the verify    *)
(* sees it, alerts, and protects the position again. A GTT can also       *)
(* trigger with its limit sale left resting below the market's price: the *)
(* sync sees the working sale, alerts, and has it sold at the market       *)
(* (HAT-EXEC-066); nothing else sells or stops the shares it holds. A      *)
(* GTT's placement                                                         *)
(* answer can be lost (it may or may not have landed): it is settled       *)
(* against the broker's GTT book (adopted when there, else ended and the   *)
(* position protected again), and until then it never holds a sale back.  *)
(* The entry is a DAY limit that can fill in parts: once a sale empties    *)
(* the position, the rest of the entry is cancelled, and the position      *)
(* closes only when the broker shows the entry ended; a part that fills    *)
(* first (the cancel too late) is held and protected (F2).                 *)
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
(*   a day stop is a stop-market order, so when it triggers it fills; a    *)
(*   sale sells the quantity HAT holds; the cancel of the entry remainder  *)
(*   is asked in the step of the sale that empties the position (for a     *)
(*   stop fill, the sync's protect right after it), and the broker ends    *)
(*   the entry eventually (the cancel, or the DAY expiry at the close);    *)
(*   the fill sync runs (every minute) between a GTT's trigger and the     *)
(*   close, and a market sale fills before the close (a stock locked at    *)
(*   its lower price band is the exception, alerted once by the sync); a   *)
(*   triggered GTT's sale still working at the close expires with it; the  *)
(*   broker accepts the change of a resting sale to a market sale         *)
(*   eventually (a refusal is alerted and asked again by the next sync);   *)
(*   a triggered GTT's working sale holds the shares, so no exit sale is   *)
(*   placed over it.                                                       *)
(*                                                                         *)
(*   The GTT book can be read eventually (weak fairness on Settle).        *)
(*                                                                         *)
(* Properties: StopAfterFill, AlwaysProtected, BrokerStopMatchesCurrent,   *)
(*   NoUnprotectedGap, NightlyVerifyCovers, StopOnlyTightens,              *)
(*   LostNeverBlocksSale, SharesTracked, RestCancelledOnExit (invariants)  *)
(*   and GttUnfilledAlerted, LostGttSettled, EmptiedCloses, RestingSold    *)
(*   (temporal).                                                           *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS MaxQty, MaxStop, BrokenMode

ASSUME BrokenMode \in {"none", "act_before_stop", "no_protect", "place_above",
                       "cancel_first", "verify_skips_missing", "reset_on_follow",
                       "no_unfilled_alert", "never_settle", "lost_blocks_sale",
                       "close_with_rest", "keep_rest", "no_close_step",
                       "no_resting_market"}

Phases == {"open", "swap", "after", "night"}
InSession == {"open", "swap"}
Prices == 0..(MaxStop + 1)
None == [st |-> "none", px |-> 0]

VARIABLES
    qty,            \* the open quantity of the real position
    closed,         \* the position was sold (stop or exit fill)
    cur,            \* hat_position.current_stop
    day,            \* the intraday stop at the broker: [st: none | working, px]
    gtt,            \* the GTT at the broker: [st: none | working | resting | seen | marketed |
                    \* gone, px]; resting = triggered, its limit sale working unfilled below the
                    \* limit, not yet seen by HAT; seen = seen and alerted, left at its limit
                    \* (broken variant only); marketed = HAT had that sale made a market sale;
                    \* gone = triggered, its sale ended unfilled, not yet seen by HAT
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
    replaceTried,   \* tonight's verify tried to place a new GTT
    lostGtt,        \* HAT's book holds a GTT whose placement answer was lost (no broker id)
    saleRefused,    \* an exit sale was held back by a stop of unknown outcome
    bqty,           \* the shares the broker holds for the position (its fills less its sales)
    rest,           \* the entry order still works at the broker (part of it may still fill)
    exited,         \* a sale has emptied the position at least once
    cancelAsked     \* HAT asked the broker to cancel the rest of the entry

vars == <<qty, closed, cur, day, gtt, phase, pc, acted, alerted, lastPx, lowered,
          cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried,
          lostGtt, saleRefused, bqty, rest, exited, cancelAsked>>
entryVars == <<bqty, rest, exited, cancelAsked>>

Held == ~closed /\ qty > 0
Protected == day.st = "working" \/ gtt.st = "working"
\* A triggered GTT whose sale still works at the broker: the sale holds the shares.
SaleWorks == {"resting", "seen", "marketed"}
\* HAT's book shows a working stop (a GTT that triggered unseen still looks working to HAT, and
\* so does one whose answer was lost).
Booked == day.st = "working" \/ gtt.st # "none" \/ lostGtt
\* The price a new stop is placed at (the broken variant places it above the current stop).
NewPx == IF BrokenMode = "place_above" THEN cur + 1 ELSE cur
SetPx(p) == /\ lowered' = (lowered \/ p < lastPx)
            /\ lastPx' = p

\* A GTT placement whose answer was lost: alerted; it landed at the broker or it did not.
LostGttPlace ==
    /\ lostGtt' = TRUE
    /\ alerted' = TRUE
    /\ \E landed \in BOOLEAN :
          IF landed
             THEN /\ gtt' = [st |-> "working", px |-> NewPx]
                  /\ SetPx(NewPx)
             ELSE UNCHANGED <<gtt, lastPx, lowered>>

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
    /\ lostGtt = FALSE
    /\ saleRefused = FALSE
    /\ bqty = 0
    /\ rest = TRUE
    /\ exited = FALSE
    /\ cancelAsked = FALSE

\* A HAT action on the position other than its stop work: in the real code it waits for the
\* stop work of a fill (broken variant "act_before_stop": it does not).
MayAct == pc = "idle" \/ BrokenMode = "act_before_stop"
NoteAct == acted' = (acted \/ pc = "protecting")

\* code: backend/app/hat/execution/fills.py:FillSync._apply  (the broker fills part of the entry;
\* the fill is recorded, then its stop work runs: the position is `protecting` until it has. The
\* last share ends the entry order. A fill of a position already closed is refused and alerted
\* (fill.refused), which only the broken variant "close_with_rest" can reach)
EntryFill ==
    /\ phase \in InSession
    /\ rest
    /\ bqty < MaxQty
    /\ bqty' = bqty + 1
    /\ rest' = (bqty + 1 < MaxQty)
    /\ IF closed
          THEN /\ alerted' = TRUE
               /\ UNCHANGED <<qty, pc>>
          ELSE /\ qty' = qty + 1
               /\ pc' = IF BrokenMode = "no_protect" THEN pc ELSE "protecting"
               /\ UNCHANGED alerted
    /\ UNCHANGED <<closed, cur, day, gtt, phase, acted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried,
                   lostGtt, saleRefused, exited, cancelAsked>>

\* A sale that sells every share HAT holds (a stop fill, an exit or a flatten): the broker's
\* shares drop by as many, the rest of the entry is asked to be cancelled (broken variant
\* "keep_rest": it is not, the main code before PR 20), and the position closes only when no entry
\* order still works (broken variant "close_with_rest": at once, the main code before PR 20).
\* code: backend/app/hat/engine/live_trades.py:apply_exit_fill, protect.py:StopDesk.cancel_entry
SellAll ==
    /\ qty' = 0
    /\ bqty' = bqty - qty
    /\ exited' = TRUE
    /\ cancelAsked' = (cancelAsked \/ BrokenMode # "keep_rest")
    /\ closed' = (BrokenMode = "close_with_rest" \/ ~rest)
    /\ UNCHANGED rest

\* code: backend/app/hat/execution/fills.py:FillSync.run step 7, live_trades.close_if_emptied
\* (the broker ends the entry order: the cancel HAT asked for, or the DAY limit expiring at the
\* close; an emptied position then closes; broken variant "no_close_step": the sync never closes
\* it)
EntryEnds ==
    /\ rest
    /\ cancelAsked \/ phase \in {"after", "night"}
    /\ rest' = FALSE
    /\ closed' = (closed \/ (exited /\ qty = 0 /\ BrokenMode # "no_close_step"))
    /\ UNCHANGED <<qty, cur, day, gtt, phase, pc, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried,
                   lostGtt, saleRefused, bqty, exited, cancelAsked>>

\* code: backend/app/hat/execution/protect.py:StopDesk.protect  (a resting stop is resized; with
\* none, a day stop in the session before 15:25 or a GTT from then on; a failure is alerted; a
\* GTT whose answer is lost is alerted and may or may not have landed)
Protect ==
    /\ pc = "protecting"
    /\ pc' = "idle"
    /\ IF Booked
          THEN UNCHANGED <<day, gtt, alerted, lastPx, lowered, swapFailed, lostGtt>>
          ELSE \E r \in {"ok", "failed", "lost"} :
                 CASE r = "ok" ->
                        /\ IF phase = "open"
                              THEN /\ day' = [st |-> "working", px |-> NewPx]
                                   /\ gtt' = gtt
                              ELSE /\ gtt' = [st |-> "working", px |-> NewPx]
                                   /\ day' = day
                        /\ swapFailed' = (swapFailed /\ phase = "open")
                        /\ alerted' = FALSE
                        /\ SetPx(NewPx)
                        /\ lostGtt' = FALSE
                   [] r = "lost" /\ phase # "open" ->
                        /\ LostGttPlace
                        /\ UNCHANGED <<day, swapFailed>>
                   [] OTHER ->
                        /\ alerted' = TRUE
                        /\ UNCHANGED <<day, gtt, lastPx, lowered, swapFailed, lostGtt>>
    /\ UNCHANGED <<qty, closed, cur, phase, acted, cancelledEarly, swapTried,
                   verifyDone, nightAlert, replaceTried, saleRefused>>
    /\ UNCHANGED entryVars

\* code: backend/app/hat/execution/fills.py:FillSync._unprotected  (a held position HAT's book
\* shows without a working stop, after an alerted failure, is protected again by the next sync)
Reprotect ==
    /\ pc = "idle"
    /\ Held
    /\ ~Booked
    /\ pc' = "protecting"
    /\ UNCHANGED <<qty, closed, cur, day, gtt, phase, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried,
                   lostGtt, saleRefused>>
    /\ UNCHANGED entryVars

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
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried, lostGtt,
                   saleRefused>>
    /\ UNCHANGED entryVars

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
                   verifyDone, nightAlert, replaceTried, lostGtt, saleRefused>>
    /\ UNCHANGED entryVars

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
               /\ UNCHANGED <<gtt, alerted, lastPx, lowered, lostGtt>>
          ELSE IF lostGtt
          \* a GTT whose answer was lost and not settled yet: the swap fails and is alerted
          THEN /\ alerted' = TRUE
               /\ swapFailed' = TRUE
               /\ UNCHANGED <<gtt, lastPx, lowered, lostGtt>>
          ELSE \E r \in {"ok", "failed", "lost"} :
                 CASE r = "ok" ->
                        /\ gtt' = [st |-> "working", px |-> NewPx]
                        /\ SetPx(NewPx)
                        /\ alerted' = FALSE
                        /\ swapFailed' = FALSE
                        /\ UNCHANGED lostGtt
                   [] r = "lost" ->
                        /\ LostGttPlace
                        /\ swapFailed' = TRUE
                   [] OTHER ->
                        /\ alerted' = TRUE
                        /\ swapFailed' = TRUE
                        /\ UNCHANGED <<gtt, lastPx, lowered, lostGtt>>
    /\ NoteAct
    /\ UNCHANGED <<qty, closed, cur, day, phase, pc, cancelledEarly, verifyDone, nightAlert,
                   replaceTried, saleRefused>>
    /\ UNCHANGED entryVars

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
                   swapFailed, verifyDone, nightAlert, replaceTried, lostGtt, saleRefused>>
    /\ UNCHANGED entryVars

\* code: backend/app/hat/ops/scheduler.py:HatScheduler.due_jobs  (time passes; at the close the
\* broker expires any day stop; a new session starts a new night)
Clock ==
    \/ /\ phase = "open"
       /\ phase' = "swap"
       /\ UNCHANGED <<day, gtt, pc, swapTried, verifyDone, nightAlert, replaceTried, swapFailed>>
    \/ /\ phase = "swap"
       /\ swapTried \/ ~Held
       \* the sync sees a resting sale before the close, and a market sale fills before it
       /\ gtt.st \notin {"resting", "marketed"}
       /\ phase' = "after"
       /\ day' = None
       \* a sale left at its limit expires at the close (seen next by the sync, as `gone`)
       /\ gtt' = IF gtt.st = "seen" THEN [gtt EXCEPT !.st = "gone"] ELSE gtt
       \* the sync sees the expired day stop and protects a held position again
       \* (fills.py:FillSync._statuses): a late entry fill after the swap may rest on it alone
       /\ pc' = IF Held /\ day.st = "working" /\ gtt.st = "none" THEN "protecting" ELSE pc
       /\ UNCHANGED <<swapTried, verifyDone, nightAlert, replaceTried, swapFailed>>
    \/ /\ phase = "after"
       /\ phase' = "night"
       /\ UNCHANGED <<day, gtt, pc, swapTried, verifyDone, nightAlert, replaceTried, swapFailed>>
    \/ /\ phase = "night"
       /\ verifyDone \/ ~Held
       /\ phase' = "open"
       /\ swapTried' = FALSE
       /\ swapFailed' = FALSE
       /\ verifyDone' = FALSE
       /\ nightAlert' = FALSE
       /\ replaceTried' = FALSE
       /\ UNCHANGED <<day, gtt, pc>>
    /\ UNCHANGED <<qty, closed, cur, acted, alerted, lastPx, lowered, cancelledEarly,
                   lostGtt, saleRefused>>
    /\ UNCHANGED entryVars

\* code: backend/app/hat/execution/fills.py:FillSync._apply_gtt_trade  (the GTT triggered and its
\* limit order filled: the position is sold)
GttFills ==
    /\ phase \in InSession
    /\ Held
    /\ gtt.st = "working"
    /\ SellAll
    /\ gtt' = None
    /\ UNCHANGED <<cur, day, phase, pc, acted, alerted, lastPx, lowered, cancelledEarly,
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried, lostGtt,
                   saleRefused>>

\* code: backend/app/hat/execution/fills.py:FillSync._apply  (the day stop triggered; a stop-market
\* order fills)
DayStopFills ==
    /\ phase \in InSession
    /\ Held
    /\ day.st = "working"
    /\ SellAll
    /\ day' = None
    /\ UNCHANGED <<cur, gtt, phase, pc, acted, alerted, lastPx, lowered, cancelledEarly,
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried, lostGtt,
                   saleRefused>>

\* code: backend/app/hat/execution/fills.py:FillSync._unfilled_trigger  (the GTT triggered and
\* its limit order did not fill: the broker no longer holds a stop)
GttTriggersUnfilled ==
    /\ phase \in InSession
    /\ Held
    /\ gtt.st = "working"
    /\ gtt' = [gtt EXCEPT !.st = "gone"]
    /\ UNCHANGED <<qty, closed, cur, day, phase, pc, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried,
                   lostGtt, saleRefused>>
    /\ UNCHANGED entryVars

\* code: backend/app/hat/execution/fills.py:FillSync._gtt_unfilled  (the sync sees the untagged
\* sell that ended unfilled: alert, end the GTT, protect again)
SeeUnfilled ==
    /\ phase \in InSession
    /\ gtt.st = "gone"
    /\ gtt' = None
    /\ alerted' = (alerted \/ BrokenMode # "no_unfilled_alert")
    /\ pc' = IF Held THEN "protecting" ELSE pc
    /\ UNCHANGED <<qty, closed, cur, day, phase, acted, lastPx, lowered, cancelledEarly,
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried, lostGtt,
                   saleRefused>>
    /\ UNCHANGED entryVars

\* code: backend/app/hat/execution/fills.py:FillSync._resting_trigger  (the GTT triggered and its
\* limit sale rests unfilled below the limit: the sale holds the shares, it is not a stop)
GttTriggersResting ==
    /\ phase \in InSession
    /\ Held
    /\ gtt.st = "working"
    /\ gtt' = [gtt EXCEPT !.st = "resting"]
    /\ UNCHANGED <<qty, closed, cur, day, phase, pc, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried,
                   lostGtt, saleRefused>>
    /\ UNCHANGED entryVars

\* code: backend/app/hat/execution/fills.py:FillSync._gtt_resting  (the sync sees the untagged
\* sale resting below its limit: alert once, then have it made a market sale; broken variant
\* "no_resting_market": it is left at its limit, the main code before PR 27)
SeeResting ==
    /\ phase \in InSession
    /\ gtt.st = "resting"
    /\ alerted' = TRUE
    /\ gtt' = [gtt EXCEPT !.st = IF BrokenMode = "no_resting_market" THEN "seen" ELSE "marketed"]
    /\ UNCHANGED <<qty, closed, cur, day, phase, pc, acted, lastPx, lowered, cancelledEarly,
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried, lostGtt,
                   saleRefused>>
    /\ UNCHANGED entryVars

\* code: backend/app/hat/execution/fills.py:FillSync._apply_gtt_trade  (the triggered GTT's sale
\* fills: at its limit when the price comes back, or at the market once HAT had it changed)
SaleFills ==
    /\ phase \in InSession
    /\ Held
    /\ gtt.st \in SaleWorks
    /\ SellAll
    /\ gtt' = None
    /\ UNCHANGED <<cur, day, phase, pc, acted, alerted, lastPx, lowered, cancelledEarly,
                   swapTried, swapFailed, verifyDone, nightAlert, replaceTried, lostGtt,
                   saleRefused>>

\* A market sale fills (fairness): the same step as SaleFills, from a marketed sale.
MarketFills == gtt.st = "marketed" /\ SaleFills

\* code: backend/app/hat/execution/protect.py:StopDesk.cancel_all  (a sold position's resting
\* stops are cancelled, once it holds no shares even while its entry still works)
CancelAfterSale ==
    /\ ~Held
    /\ ~lostGtt
    /\ (day.st = "working" \/ gtt.st = "working")
    /\ day' = None
    /\ gtt' = None
    /\ UNCHANGED <<qty, closed, cur, phase, pc, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried,
                   lostGtt, saleRefused>>
    /\ UNCHANGED entryVars

\* code: backend/app/hat/execution/protect.py:StopDesk._settle  (a GTT whose answer was lost is
\* looked up in the broker's GTT book: there, it is adopted; absent, it never landed: the row
\* ends and a held position is protected again; broken variant "never_settle": it is never
\* looked up, the main code before PR 18)
Settle ==
    /\ lostGtt
    /\ BrokenMode # "never_settle"
    /\ lostGtt' = FALSE
    /\ pc' = IF gtt.st \notin {"working"} \cup SaleWorks /\ Held THEN "protecting" ELSE pc
    /\ UNCHANGED <<qty, closed, cur, day, gtt, phase, acted, alerted, lastPx, lowered,
                   cancelledEarly, swapTried, swapFailed, verifyDone, nightAlert, replaceTried,
                   saleRefused>>
    /\ UNCHANGED entryVars

\* code: backend/app/hat/execution/manage.py:TradeManager._sell_locked  (an exit sale in the
\* session: make_room cancels the resting stops HAT knows and skips one whose answer is still
\* unknown, then the sale sells the position; broken variant "lost_blocks_sale": such a stop
\* holds the sale back, the main code before PR 18)
Sell ==
    /\ phase \in InSession
    /\ MayAct
    /\ Held
    /\ gtt.st \notin SaleWorks
    /\ NoteAct
    /\ IF lostGtt /\ BrokenMode = "lost_blocks_sale"
          THEN /\ saleRefused' = TRUE
               /\ UNCHANGED <<qty, closed, day, gtt, entryVars>>
          ELSE /\ SellAll
               /\ day' = None
               /\ gtt' = IF lostGtt THEN gtt ELSE None
               /\ UNCHANGED saleRefused
    /\ UNCHANGED <<cur, phase, pc, alerted, lastPx, lowered, cancelledEarly, swapTried,
                   swapFailed, verifyDone, nightAlert, replaceTried, lostGtt>>

\* code: backend/app/hat/execution/protect.py:StopDesk.verify  (a GTT whose answer was lost is
\* settled first; refresh the GTT to the current stop; a GTT the broker does not confirm is
\* alerted and placed again; an unreachable broker is alerted and nothing is placed)
Verify ==
    /\ phase = "night"
    /\ ~verifyDone
    /\ MayAct
    /\ verifyDone' = TRUE
    /\ NoteAct
    /\ \E reachable \in BOOLEAN, ok \in BOOLEAN :
          /\ lostGtt' = (lostGtt /\ ~reachable)
          /\ CASE ~Held ->
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
    /\ UNCHANGED <<qty, closed, cur, day, phase, pc, cancelledEarly, swapTried, swapFailed,
                   saleRefused>>
    /\ UNCHANGED entryVars

Next ==
    \/ EntryFill \/ Protect \/ Reprotect \/ RaiseStop \/ Follow \/ SwapPlaceGtt \/ SwapCancelDay
    \/ Clock \/ GttFills \/ DayStopFills \/ GttTriggersUnfilled \/ SeeUnfilled
    \/ CancelAfterSale \/ Settle \/ Sell \/ Verify \/ EntryEnds
    \/ GttTriggersResting \/ SeeResting \/ SaleFills

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(Protect) /\ WF_vars(SwapPlaceGtt) /\ WF_vars(SwapCancelDay)
    /\ WF_vars(Clock) /\ WF_vars(SeeUnfilled) /\ WF_vars(Verify) /\ WF_vars(Settle)
    /\ WF_vars(EntryEnds) /\ WF_vars(SeeResting) /\ WF_vars(MarketFills)

TypeOK ==
    /\ qty \in 0..MaxQty
    /\ closed \in BOOLEAN
    /\ cur \in 1..MaxStop
    /\ day \in [st : {"none", "working"}, px : Prices]
    /\ gtt \in [st : {"none", "working", "gone"} \cup SaleWorks, px : Prices]
    /\ phase \in Phases
    /\ pc \in {"idle", "protecting"}
    /\ lastPx \in Prices
    /\ lostGtt \in BOOLEAN
    /\ saleRefused \in BOOLEAN
    /\ bqty \in 0..MaxQty
    /\ rest \in BOOLEAN
    /\ exited \in BOOLEAN
    /\ cancelAsked \in BOOLEAN

\* HAT-EXEC-060: after a real entry fill, its broker stop is placed (or resized) before any other
\* action on the position.
StopAfterFill == ~acted

\* HAT-DEC-B17: a held real position always has a resting broker stop, except while the stop
\* work of a fill has not run yet, after a failure that was alerted, or while a GTT that
\* triggered without a fill has not been seen (GttUnfilledAlerted).
AlwaysProtected ==
    Held => (Protected \/ pc = "protecting" \/ alerted \/ gtt.st = "gone" \/ gtt.st \in SaleWorks)

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

\* HAT-EXEC-065: a GTT whose placement answer was lost is eventually settled (adopted, or ended
\* and the position protected again), so it never leaves a held position without a stop HAT can
\* place.
LostGttSettled == (lostGtt /\ Held) ~> (~lostGtt \/ ~Held)

\* HAT-EXEC-065: a stop whose outcome is unknown never holds an exit sale back.
LostNeverBlocksSale == ~saleRefused

\* HAT-EXEC-060, HAT-DEC-B17 (F2): HAT holds exactly the shares the broker holds, so none is
\* left untracked (and unprotected, AlwaysProtected) by a part of the entry that filled late.
SharesTracked == bqty = qty

\* F2: once a sale has emptied the position, the rest of a still-working entry has been asked to
\* be cancelled (it holds no risk budget and buys no shares HAT is selling).
RestCancelledOnExit == (exited /\ rest /\ qty = 0) => cancelAsked

\* F2: a position its sales emptied eventually closes, unless a late part of the entry is held.
EmptiedCloses == (exited /\ qty = 0 /\ ~closed) ~> (closed \/ qty > 0)

\* HAT-EXEC-066: a GTT that triggered with its limit sale resting unfilled under a held position
\* is eventually sold (the sync has the sale made a market sale), never left at its limit.
RestingSold == (gtt.st = "resting" /\ Held) ~> ~Held
==============================================================================

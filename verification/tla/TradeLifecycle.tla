------------------------------ MODULE TradeLifecycle ------------------------------
(***************************************************************************)
(* The trade lifecycle, its event log and the per-bot lock           *)
(* (HAT-EXEC-040..045; SCH-057, SCH-081, BOT-083).                          *)
(*                                                                         *)
(* NTrades signals of one bot, each moving through the 11 states only     *)
(* along the transition table. Every step is appended to the trade's log   *)
(* in the same action that changes its state, and the state is the fold of *)
(* the log (the engine derives it from events). An entry is evaluated and  *)
(* inserted while the bot's advisory lock is held, so two entries of one   *)
(* bot cannot both take the last position slot. A full exit that fires     *)
(* places the exit order and closes the trade; the closing step writes the *)
(* one trade result. Shadow (virtual) trades follow the same table.        *)
(*                                                                         *)
(* Bounds: NTrades = 2 signals of the one bot (the smallest number that    *)
(*   can race for a slot); Cap = 1 position slot, so a second entry must   *)
(*   be refused while the first is held; MaxPartials = 1 partial exit      *)
(*   before the full exit; the strategy version is 1 or 2 (one promotion); *)
(*   an entry is Watchlisted to Open or Rejected in one commit, so the     *)
(*   states Triggered, AwaitingApproval and EntryPlaced are steps inside   *)
(*   that commit, not states a reader can observe (confirm and auto are    *)
(*   PR 15); BrokenMode names a deliberately broken variant ("none" is     *)
(*   the real rule).                                                       *)
(* Assumptions: each action is one database transaction; the exit rules,   *)
(*   the stop and the session end are arbitrary environment inputs; the    *)
(*   lock is released when the transaction ends; the bot's cap is a count  *)
(*   of trades in EntryPlaced, Open, PartiallyExited or ExitPending.       *)
(*                                                                         *)
(* Properties (invariants): TypeOK, OnlyAllowedTransitions,                *)
(*   EndingsTerminal, StateMatchesLog, ShadowSameMachine, ResultOnClose,   *)
(*   VersionPinned, PerBotCapHolds; (temporal): EventuallyEnds.            *)
(***************************************************************************)
EXTENDS Naturals, Sequences, FiniteSets

CONSTANTS NTrades, Cap, MaxPartials, BrokenMode

ASSUME BrokenMode \in {"none", "no_lock", "state_without_event", "skip_exit_pending",
                       "virtual_skips_exit_pending", "reopen", "result_late",
                       "exits_use_live", "exit_stalls"}

Trades == 1..NTrades
States == {"Watchlisted", "Triggered", "AwaitingApproval", "EntryPlaced", "Open",
           "PartiallyExited", "ExitPending", "Closed", "Rejected", "Expired", "Cancelled"}
Endings == {"Closed", "Rejected", "Expired", "Cancelled"}
Holding == {"EntryPlaced", "Open", "PartiallyExited", "ExitPending"}
Held == {"Open", "PartiallyExited"}

\* The table as (from, step, to); lifecycle.TRANSITIONS is checked equal to it.
Trans == {
    <<"Watchlisted", "signal.triggered", "Triggered">>,
    <<"Watchlisted", "watchlist.expired", "Expired">>,
    <<"Triggered", "signal.rejected", "Rejected">>,
    <<"Triggered", "approval.sent", "AwaitingApproval">>,
    <<"Triggered", "order.placed.entry", "EntryPlaced">>,
    <<"AwaitingApproval", "order.placed.entry", "EntryPlaced">>,
    <<"AwaitingApproval", "signal.expired", "Expired">>,
    <<"AwaitingApproval", "signal.skipped", "Cancelled">>,
    <<"AwaitingApproval", "kill.tripped", "Cancelled">>,
    <<"AwaitingApproval", "signal.rejected", "Rejected">>,
    <<"EntryPlaced", "position.opened", "Open">>,
    <<"EntryPlaced", "order.rejected", "Rejected">>,
    <<"EntryPlaced", "order.cancelled", "Cancelled">>,
    <<"Open", "exit.partial", "PartiallyExited">>,
    <<"PartiallyExited", "exit.partial", "PartiallyExited">>,
    <<"Open", "order.placed.exit", "ExitPending">>,
    <<"PartiallyExited", "order.placed.exit", "ExitPending">>,
    <<"ExitPending", "position.closed", "Closed">>}

StepNames == {t[2] : t \in Trans}

VARIABLES
    st,         \* the state stored on the signal and its position
    log,        \* the lifecycle steps of each trade's events, in order
    virt,       \* the trade is virtual (shadow)
    phase,      \* an entry is "idle" or "decided" (evaluated, not yet inserted)
    snap,       \* the number of held slots the entry's evaluation read
    lock,       \* the trade that holds the bot's lock, or 0
    exitDue,    \* a full exit rule or the stop has fired
    partials,   \* partial exits so far
    result,     \* the trade's one hat_trade_result exists
    pinned,     \* the strategy version the trade entered under (0 before entry)
    liveVer,    \* the version the bot currently runs
    rulesRead   \* the version whose exit rules the last evaluation read (0: none yet)

vars == <<st, log, virt, phase, snap, lock, exitDue, partials, result, pinned, liveVer,
          rulesRead>>

Step(s, e) ==
    IF \E x \in Trans : x[1] = s /\ x[2] = e
    THEN (CHOOSE x \in Trans : x[1] = s /\ x[2] = e)[3]
    ELSE "ERR"

RECURSIVE Fold(_)
Fold(sq) ==
    IF sq = <<>> THEN "Watchlisted"
    ELSE IF Fold(SubSeq(sq, 1, Len(sq) - 1)) = "ERR" THEN "ERR"
    ELSE Step(Fold(SubSeq(sq, 1, Len(sq) - 1)), sq[Len(sq)])

Held_(u) == st[u] \in Holding

Init ==
    /\ st = [t \in Trades |-> "Watchlisted"]
    /\ log = [t \in Trades |-> <<>>]
    /\ virt = [t \in Trades |-> FALSE]
    /\ phase = [t \in Trades |-> "idle"]
    /\ snap = [t \in Trades |-> 0]
    /\ lock = 0
    /\ exitDue = [t \in Trades |-> FALSE]
    /\ partials = [t \in Trades |-> 0]
    /\ result = [t \in Trades |-> FALSE]
    /\ pinned = [t \in Trades |-> 0]
    /\ liveVer = 1
    /\ rulesRead = [t \in Trades |-> 0]

Move(t, steps, to) ==
    /\ st' = [st EXCEPT ![t] = to]
    /\ log' = [log EXCEPT ![t] = @ \o steps]

\* code: backend/app/hat/engine/botlock.py:lock_bot  (take the bot's advisory lock, then read the
\* snapshot the caps use; the broken variant "no_lock" reads without the lock)
Evaluate(t) ==
    /\ st[t] = "Watchlisted"
    /\ phase[t] = "idle"
    /\ BrokenMode = "no_lock" \/ lock = 0
    /\ lock' = IF BrokenMode = "no_lock" THEN lock ELSE t
    /\ snap' = [snap EXCEPT ![t] = Cardinality({u \in Trades : Held_(u)})]
    /\ phase' = [phase EXCEPT ![t] = "decided"]
    /\ \E v \in BOOLEAN : virt' = [virt EXCEPT ![t] = v]
    /\ UNCHANGED <<st, log, exitDue, partials, result, pinned, liveVer, rulesRead>>

\* code: backend/app/hat/engine/trades.py:open_entry  (insert the signal, the virtual order and
\* fill and the position with their events in one commit; the commit releases the lock)
Commit(t) ==
    /\ phase[t] = "decided"
    /\ IF snap[t] < Cap
       THEN /\ Move(t, <<"signal.triggered", "order.placed.entry", "position.opened">>, "Open")
            /\ pinned' = [pinned EXCEPT ![t] = liveVer]
       ELSE /\ Move(t, <<"signal.triggered", "signal.rejected">>, "Rejected")
            /\ UNCHANGED pinned
    /\ phase' = [phase EXCEPT ![t] = "idle"]
    /\ lock' = IF BrokenMode = "no_lock" THEN lock ELSE 0
    /\ UNCHANGED <<virt, snap, exitDue, partials, result, liveVer, rulesRead>>

\* code: backend/app/hat/engine/trades.py:expire_watchlist  (the session ends, the `watchlist.expire`
\* job at 15:30. A trade that never triggered has no signal and so no hat_event: its record is the
\* hat_watchlist_entry row, and this step is that row's status change, which the code writes
\* through lifecycle.next_state; no `watchlist.expired` event row exists. The broken variant
\* "state_without_event" changes the state without the step)
Expire(t) ==
    /\ st[t] = "Watchlisted"
    /\ phase[t] = "idle"
    /\ IF BrokenMode = "state_without_event"
       THEN /\ st' = [st EXCEPT ![t] = "Expired"]
            /\ UNCHANGED log
       ELSE Move(t, <<"watchlist.expired">>, "Expired")
    /\ UNCHANGED <<virt, phase, snap, lock, exitDue, partials, result, pinned, liveVer,
                   rulesRead>>

\* code: backend/app/hat/engine/exits.py:evaluate  (a full exit rule or the stop fires)
FireExit(t) ==
    /\ st[t] \in Held
    /\ ~exitDue[t]
    /\ exitDue' = [exitDue EXCEPT ![t] = TRUE]
    /\ UNCHANGED <<st, log, virt, phase, snap, lock, partials, result, pinned, liveVer,
                   rulesRead>>

\* code: backend/app/hat/engine/trades.py:evaluate_position  (a partial exit rule fires and
\* fills; a full exit that has fired beats it, so none is taken while exitDue)
PartialExit(t) ==
    /\ st[t] \in Held
    /\ ~exitDue[t]
    /\ partials[t] < MaxPartials
    /\ Move(t, <<"exit.partial">>, "PartiallyExited")
    /\ partials' = [partials EXCEPT ![t] = @ + 1]
    /\ rulesRead' = [rulesRead EXCEPT ![t] = IF BrokenMode = "exits_use_live"
                                             THEN liveVer ELSE pinned[t]]
    /\ UNCHANGED <<virt, phase, snap, lock, exitDue, result, pinned, liveVer>>

\* code: backend/app/hat/engine/trades.py:evaluate_position  (the full exit order is placed; a
\* virtual trade is placed the same way, the broken variants skip ExitPending)
PlaceExit(t) ==
    /\ st[t] \in Held
    /\ exitDue[t]
    /\ IF BrokenMode = "skip_exit_pending" \/ (BrokenMode = "virtual_skips_exit_pending" /\ virt[t])
       THEN /\ Move(t, <<"position.closed">>, "Closed")
            /\ result' = [result EXCEPT ![t] = TRUE]
       ELSE /\ Move(t, <<"order.placed.exit">>, "ExitPending")
            /\ UNCHANGED result
    /\ rulesRead' = [rulesRead EXCEPT ![t] = IF BrokenMode = "exits_use_live"
                                             THEN liveVer ELSE pinned[t]]
    /\ UNCHANGED <<virt, phase, snap, lock, exitDue, partials, pinned, liveVer>>

\* code: backend/app/hat/engine/trades.py:_virtual_exit  (the exit fills: the position closes and
\* its one trade result is written in the same transaction)
FillExit(t) ==
    /\ st[t] = "ExitPending"
    /\ BrokenMode # "exit_stalls"
    /\ Move(t, <<"position.closed">>, "Closed")
    /\ result' = [result EXCEPT ![t] = BrokenMode # "result_late"]
    /\ UNCHANGED <<virt, phase, snap, lock, exitDue, partials, pinned, liveVer, rulesRead>>

\* code: backend/app/hat/engine/positions.py:write_result  (broken variant "result_late" only: the
\* result is written after the close, in a later transaction)
WriteResultLate(t) ==
    /\ BrokenMode = "result_late"
    /\ st[t] = "Closed"
    /\ ~result[t]
    /\ result' = [result EXCEPT ![t] = TRUE]
    /\ UNCHANGED <<st, log, virt, phase, snap, lock, exitDue, partials, pinned, liveVer,
                   rulesRead>>

\* code: backend/app/hat/engine/lifecycle.py:next_state  (broken variant "reopen" only: a closed
\* trade takes a step the table does not allow)
Reopen(t) ==
    /\ BrokenMode = "reopen"
    /\ st[t] = "Closed"
    /\ Move(t, <<"position.opened">>, "Open")
    /\ UNCHANGED <<virt, phase, snap, lock, exitDue, partials, result, pinned, liveVer,
                   rulesRead>>

\* code: backend/app/hat/engine/strategy/versions.py:publish_version  (the bot moves to version 2)
Promote ==
    /\ liveVer = 1
    /\ liveVer' = 2
    /\ UNCHANGED <<st, log, virt, phase, snap, lock, exitDue, partials, result, pinned,
                   rulesRead>>

Next ==
    \/ Promote
    \/ \E t \in Trades :
          \/ Evaluate(t)
          \/ Commit(t)
          \/ Expire(t)
          \/ FireExit(t)
          \/ PartialExit(t)
          \/ PlaceExit(t)
          \/ FillExit(t)
          \/ WriteResultLate(t)
          \/ Reopen(t)

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ \A t \in Trades :
          /\ WF_vars(Evaluate(t)) /\ WF_vars(Commit(t)) /\ WF_vars(Expire(t))
          /\ WF_vars(PlaceExit(t)) /\ WF_vars(FillExit(t)) /\ WF_vars(WriteResultLate(t))

TypeOK ==
    /\ st \in [Trades -> States]
    /\ \A t \in Trades : Len(log[t]) <= 8 /\ \A i \in 1..Len(log[t]) : log[t][i] \in StepNames
    /\ virt \in [Trades -> BOOLEAN]
    /\ phase \in [Trades -> {"idle", "decided"}]
    /\ snap \in [Trades -> 0..NTrades]
    /\ lock \in 0..NTrades
    /\ exitDue \in [Trades -> BOOLEAN]
    /\ partials \in [Trades -> 0..MaxPartials]
    /\ result \in [Trades -> BOOLEAN]
    /\ pinned \in [Trades -> 0..2]
    /\ liveVer \in 1..2
    /\ rulesRead \in [Trades -> 0..2]

\* HAT-EXEC-040: every step of every trade is a row of the table.
OnlyAllowedTransitions == \A t \in Trades : Fold(log[t]) # "ERR"

\* HAT-EXEC-041: no step follows an ending.
EndingsTerminal ==
    \A t \in Trades : \A i \in 1..Len(log[t]) - 1 : Fold(SubSeq(log[t], 1, i)) \notin Endings

\* HAT-EXEC-042: the stored state is the fold of the log.
StateMatchesLog == \A t \in Trades : st[t] = Fold(log[t])

\* HAT-EXEC-044: a virtual trade is entered and closed through the same steps as a real one.
ShadowSameMachine ==
    \A t \in Trades :
        /\ (virt[t] /\ st[t] \in Holding \cup {"Closed"}) => log[t][2] = "order.placed.entry"
        /\ (virt[t] /\ st[t] = "Closed") =>
              (log[t][Len(log[t]) - 1] = "order.placed.exit"
               /\ log[t][Len(log[t])] = "position.closed")

\* HAT-SCH-057: a closed trade has its one result.
ResultOnClose == \A t \in Trades : st[t] = "Closed" => result[t]

\* HAT-BOT-083: the exit rules a trade reads are those of the version it entered under.
VersionPinned == \A t \in Trades : rulesRead[t] \in {0, pinned[t]}

\* HAT-EXEC-045: the bot never holds more slots than its cap.
PerBotCapHolds == Cardinality({u \in Trades : Held_(u)}) <= Cap

\* HAT-EXEC-041: an unopened trade reaches Open or an ending; a fired exit reaches Closed.
EventuallyEnds ==
    \A t \in Trades :
        /\ st[t] = "Watchlisted" ~> st[t] # "Watchlisted"
        /\ exitDue[t] ~> st[t] = "Closed"

==============================================================================

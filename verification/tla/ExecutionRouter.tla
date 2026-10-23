---------------------------- MODULE ExecutionRouter ----------------------------
(***************************************************************************)
(* The execution router (HAT-DEC-B1, B11, B20, I9).    *)
(*                                                                         *)
(* One bot on one broker account, NSignals triggers on one instrument,     *)
(* sessions that open and close without end. A trigger passes the one risk *)
(* check (evaluate_signal) and is routed by the bot's effective mode: the  *)
(* mode in force at the session open, gated by the account's GTT for       *)
(* delivery. Shadow opens a virtual position; confirm writes an approval   *)
(* card; auto places a real order once the signal has committed. The       *)
(* placement and a Take re-read the live conditions (hours, feed, regime   *)
(* gate, GTT), and a Take re-checks max-chase, before the real order; a    *)
(* Skip or an expiry continues as a virtual veto trade, which holds no     *)
(* slot. Exits never wait for the user. Mode changes are queued outside    *)
(* market hours and take effect at the next session open. A real entry is  *)
(* sent only while the broker session is live. The chat sees a card or a   *)
(* shadow message only for a routed signal, never for a rejected one, and  *)
(* no user action from the chat sets a size or creates an entry (PR 16).   *)
(*                                                                         *)
(* Bounds: NSignals = 2 triggers on one instrument (enough for a second    *)
(*   entry on a held instrument); sessions, mode requests, feed, gate,     *)
(*   GTT, broker session and price flips repeat without bound (every       *)
(*   variable has a finite domain); a Take may carry any size the user     *)
(*   chose, which the router must ignore; one approval card per confirm    *)
(*   signal, whose 5-minute window is the boolean `expired`; BrokenMode    *)
(*   names a deliberately broken variant ("none" is the real router).      *)
(* Assumptions: each action is one database transaction or one change of   *)
(*   the environment; an auto signal commits in Triggered and its entry is *)
(*   placed in a later step, under the conditions read at placement (the   *)
(*   environment may change between); the entry is a LIMIT at the price    *)
(*   the trigger fixed, so the price running past max-chase after the risk *)
(*   check cannot fill it beyond that level; a placed entry stands for the *)
(*   open real position (fills, broker stops and rejections are            *)
(*   OrderReconciliation's and StopPlacement's); the quality gate and the  *)
(*   feed are one input `stale`; the expiry sweep runs and time passes     *)
(*   (weak fairness); an exit that is due is eventually sent (weak         *)
(*   fairness).                                                            *)
(*                                                                         *)
(* Properties: RouteByMode, ShadowNeverCallsBroker, RiskBeforeRoute,       *)
(*   EntryHasStop, MaxChaseAlwaysApplied, SkipBecomesShadow, ExitsNeverAsk,*)
(*   NoOrderOutsideHours, NoOrderWhenStale, GateFreshBeforeFirstEntry,     *)
(*   GateOffNoEntry, ModeDomain, LongOnly, AutoNeedsGtt,                   *)
(*   NoAddToOpenPosition, ModeChangeNextSession, NoEntryWithoutSession,    *)
(*   RejectNoCard, UserCannotRaiseRisk (invariants) and                    *)
(*   ApprovalsDecided, ExitsComplete, EntriesSettled (temporal).           *)
(*                                                                         *)
(* PR 52 (audit T-F7): a signal commits before its entry is placed (an     *)
(* auto signal in Triggered; a Take's decision, then its placement), and   *)
(* the process can die in between (Crash). Place and PlaceTaken stand for  *)
(* the live call and for the restart's resume (Desks.resume_entries), so   *)
(* they are weakly fair; with no resume a crashed one waits for ever.      *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS NSignals, BrokenMode

ASSUME BrokenMode \in {"none", "confirm_places_now", "shadow_calls_broker",
                       "route_without_risk", "no_stop_check", "take_skips_chase",
                       "skip_drops_trade", "exit_asks", "take_ignores_hours",
                       "take_ignores_feed", "gate_unknown_ok", "take_ignores_gate",
                       "any_mode", "short_entry", "no_gtt_gate", "pyramid", "apply_now",
                       "no_expiry_sweep", "exits_wait", "place_unchecked",
                       "no_session_check", "card_on_reject", "user_resizes",
                       "user_creates_entry", "no_resume"}

Signals == 1..NSignals
Modes == {"shadow", "confirm", "auto"}
Asked == IF BrokenMode = "any_mode" THEN Modes \cup {"live"} ELSE Modes

SigStates == {"idle", "rejected", "triggered", "awaiting", "taken", "placed", "virtual", "veto",
              "ended", "closed"}
\* The states that hold the bot's slot on the instrument (a veto trade holds none).
Holding == {"triggered", "awaiting", "taken", "placed", "virtual"}
\* Committed and waiting for the entry to be placed (an auto signal, a passed Take).
Unplaced == {"triggered", "taken"}

VARIABLES
    phase,       \* the calendar session: closed | open
    mode,        \* the mode in force (hat_bot.execution_mode)
    queued,      \* the mode change queued for the next open, or "none"
    gtt,         \* the account is active and reports GTT for delivery
    stale,       \* the live feed is stale or the day's data failed the quality gate
    gate,        \* the regime gate for this session: unknown | on | off
    chased,      \* the price is past the trigger's max-chase now
    sig,         \* per signal: its lifecycle state as the router sees it
    smode,       \* per signal: the mode it was routed by, or "none"
    card,        \* per signal: none | open | taken | skipped | expired
    expired,     \* per signal: the card's 5 minutes are up
    stop,        \* per signal: the risk decision carried a stop
    exitDue,     \* per signal: an exit rule fired for its position
    badEntry,    \* ghost: what was in force when an entry was accepted (any mode)
    badOrder,    \* ghost: what was in force when a real order was sent
    exitAsked,   \* ghost: an exit waited for the user
    midChange,   \* ghost: the mode in force changed during a session
    live,        \* the account's broker session is live
    msg,         \* per signal: the relay sent its card or shadow message
    userRisk,    \* ghost: a user action from the chat set a size or created an entry
    lost         \* per signal: its process died after it committed, before its placement

tgVars == <<live, msg, userRisk>>
\* Everything but `lost`, which only Crash, Place and PlaceTaken change.
routerVars == <<phase, mode, queued, gtt, stale, gate, chased, sig, smode, card, expired, stop,
                exitDue, badEntry, badOrder, exitAsked, midChange, live, msg, userRisk>>
vars == <<phase, mode, queued, gtt, stale, gate, chased, sig, smode, card, expired, stop,
          exitDue, badEntry, badOrder, exitAsked, midChange, live, msg, userRisk, lost>>

Init ==
    /\ phase = "closed"
    /\ mode = "shadow"
    /\ queued = "none"
    /\ gtt \in BOOLEAN
    /\ stale \in BOOLEAN
    /\ gate = "unknown"
    /\ chased \in BOOLEAN
    /\ sig = [s \in Signals |-> "idle"]
    /\ smode = [s \in Signals |-> "none"]
    /\ card = [s \in Signals |-> "none"]
    /\ expired = [s \in Signals |-> FALSE]
    /\ stop = [s \in Signals |-> FALSE]
    /\ exitDue = [s \in Signals |-> FALSE]
    /\ badEntry = {}
    /\ badOrder = {}
    /\ exitAsked = FALSE
    /\ midChange = FALSE
    /\ live \in BOOLEAN
    /\ msg = [s \in Signals |-> FALSE]
    /\ userRisk = FALSE
    /\ lost = [s \in Signals |-> FALSE]

\* The effective mode (modes.bot_mode / effective_mode): confirm and auto need GTT.
Effective ==
    IF BrokenMode # "no_gtt_gate" /\ mode # "shadow" /\ ~gtt THEN "shadow" ELSE mode

Held(s) == \E t \in Signals \ {s} : sig[t] \in Holding

GateOpen == gate = "on" \/ (BrokenMode = "gate_unknown_ok" /\ gate = "unknown")

\* evaluate_signal's state and cap checks that the model sees: market hours, the feed and the
\* quality gate, the regime gate, max-chase, and no second position on a held instrument.
RiskOK(s) ==
    /\ phase = "open"
    /\ ~stale
    /\ GateOpen
    /\ ~chased
    /\ (~Held(s) \/ BrokenMode = "pyramid")

\* What was wrong when an entry went ahead (empty for a correct one).
EntryFaults(s) ==
    {f \in {"closed", "stale", "gateunknown", "gateoff", "chased", "held"} :
        \/ f = "closed" /\ phase # "open"
        \/ f = "stale" /\ stale
        \/ f = "gateunknown" /\ gate = "unknown"
        \/ f = "gateoff" /\ gate = "off"
        \/ f = "chased" /\ chased
        \/ f = "held" /\ Held(s)}

OrderFaults(s, m) ==
    EntryFaults(s) \cup (IF ~gtt THEN {"nogtt"} ELSE {}) \cup (IF m = "shadow" THEN {"shadow"} ELSE {})
        \cup (IF BrokenMode = "short_entry" THEN {"sell"} ELSE {})
        \cup (IF ~live THEN {"nosession"} ELSE {})

\* code: backend/app/hat/execution/router.py:route  (open_entry runs evaluate_signal under the
\* bot's lock and writes what the effective mode needs; an auto signal commits in Triggered)
Route(s) ==
    /\ sig[s] = "idle"
    /\ RiskOK(s) \/ BrokenMode = "route_without_risk"
    /\ \E st \in BOOLEAN :
         /\ st \/ BrokenMode \in {"no_stop_check", "route_without_risk"}
         /\ stop' = [stop EXCEPT ![s] = st]
    /\ smode' = [smode EXCEPT ![s] = Effective]
    /\ badEntry' = badEntry \cup EntryFaults(s) \cup (IF RiskOK(s) THEN {} ELSE {"unrisked"})
    /\ CASE Effective = "shadow" ->
              /\ sig' = [sig EXCEPT ![s] = "virtual"]
              /\ card' = card
              /\ badOrder' = IF BrokenMode = "shadow_calls_broker"
                                THEN badOrder \cup OrderFaults(s, "shadow") ELSE badOrder
         [] Effective = "confirm" /\ BrokenMode = "confirm_places_now" ->
              /\ sig' = [sig EXCEPT ![s] = "placed"]
              /\ card' = card
              /\ badOrder' = badOrder \cup OrderFaults(s, "confirm") \cup {"untaken"}
         [] Effective = "confirm" /\ BrokenMode # "confirm_places_now" ->
              /\ sig' = [sig EXCEPT ![s] = "awaiting"]
              /\ card' = [card EXCEPT ![s] = "open"]
              /\ badOrder' = badOrder
         [] OTHER ->
              /\ sig' = [sig EXCEPT ![s] = "triggered"]
              /\ card' = card
              /\ badOrder' = badOrder
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, expired, exitDue, exitAsked,
                   midChange>>
    /\ UNCHANGED tgVars

\* The live conditions the auto placement re-reads (router.placement_refusal, the GTT re-check).
PlaceLive ==
    \/ BrokenMode = "place_unchecked"
    \/ /\ phase = "open" /\ ~stale /\ gate = "on"
       /\ gtt \/ BrokenMode = "no_gtt_gate"
       /\ live \/ BrokenMode = "no_session_check"

\* A process that dies after it committed may not place. Without the restart's resume
\* (BrokenMode "no_resume") nothing places it after that.
Resumable(s) == ~lost[s] \/ BrokenMode # "no_resume"

\* code: backend/app/hat/execution/router.py:place_entry  (after the signal committed and the
\* bot's lock was released: the real entry is sent when the conditions still allow it, else the
\* signal ends Rejected; the LIMIT price fixed at the trigger bounds the chase. After a crash
\* the restart's resume (daily/accounts.py:Desks.resume_entries) runs it, or the next trade
\* date's sweep ends the signal: router.expire)
Place(s) ==
    /\ sig[s] = "triggered"
    /\ Resumable(s)
    /\ sig' = [sig EXCEPT ![s] = IF PlaceLive THEN "placed" ELSE "rejected"]
    /\ badOrder' = IF PlaceLive THEN badOrder \cup (OrderFaults(s, smode[s]) \ {"chased"})
                                 ELSE badOrder
    /\ lost' = [lost EXCEPT ![s] = FALSE]
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, smode, card, expired, stop,
                   exitDue, badEntry, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/execution/router.py:_place_committed  (the process dies, or the
\* placement raises, after the signal or the Take committed and before the entry is placed)
Crash(s) ==
    /\ sig[s] \in Unplaced /\ ~lost[s]
    /\ lost' = [lost EXCEPT ![s] = TRUE]
    /\ UNCHANGED routerVars

\* code: backend/app/hat/risk/service.py:evaluate_signal  (a refused trigger ends Rejected)
Reject(s) ==
    /\ sig[s] = "idle"
    /\ ~RiskOK(s)
    /\ sig' = [sig EXCEPT ![s] = "rejected"]
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, smode, card, expired, stop,
                   exitDue, badEntry, badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* The live conditions a Take re-reads (router.placement_refusal and the GTT re-check; the broker
\* session is send_order's check, submit._check_allowed).
TakeLive ==
    /\ live \/ BrokenMode = "no_session_check"
    /\ phase = "open" \/ BrokenMode = "take_ignores_hours"
    /\ ~stale \/ BrokenMode = "take_ignores_feed"
    /\ gate = "on" \/ BrokenMode = "take_ignores_gate"
    /\ gtt \/ BrokenMode = "no_gtt_gate"

\* code: backend/app/hat/engine/approvals.py:take  (the decision, committed first: before expiry,
\* the live conditions and the max-chase re-check pass and the card is taken, or the signal ends
\* Rejected; a size the user asked for is never read)
Take(s, userSize) ==
    /\ card[s] = "open" /\ ~expired[s]
    /\ card' = [card EXCEPT ![s] = "taken"]
    /\ LET ok == TakeLive /\ (~chased \/ BrokenMode = "take_skips_chase")
       IN /\ sig' = [sig EXCEPT ![s] = IF ok THEN "taken" ELSE "rejected"]
          /\ badEntry' = IF ok /\ chased THEN badEntry \cup {"chased"} ELSE badEntry
          /\ userRisk' = (userRisk \/ (ok /\ userSize /\ BrokenMode = "user_resizes"))
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, smode, expired, stop, exitDue,
                   badOrder, exitAsked, midChange, live, msg>>

\* code: backend/app/hat/execution/router.py:take -> place_entry  (the passed Take's entry is
\* placed at the size the risk check fixed when the live conditions still allow it, else the
\* signal ends Rejected; after a crash the restart's resume runs it)
PlaceTaken(s) ==
    /\ sig[s] = "taken"
    /\ Resumable(s)
    /\ sig' = [sig EXCEPT ![s] = IF TakeLive THEN "placed" ELSE "rejected"]
    /\ badOrder' = IF TakeLive THEN badOrder \cup (OrderFaults(s, smode[s]) \ {"chased"})
                                ELSE badOrder
    /\ lost' = [lost EXCEPT ![s] = FALSE]
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, smode, card, expired, stop,
                   exitDue, badEntry, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* The signal's state once its card is skipped or expired: the veto trade, opened in the same
\* transaction (trades.open_veto_trade).
DecidedAway == IF BrokenMode = "skip_drops_trade" THEN "ended" ELSE "veto"

\* code: backend/app/hat/execution/router.py:skip
Skip(s) ==
    /\ card[s] = "open" /\ ~expired[s]
    /\ card' = [card EXCEPT ![s] = "skipped"]
    /\ sig' = [sig EXCEPT ![s] = DecidedAway]
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, smode, expired, stop, exitDue,
                   badEntry, badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/engine/approvals.py:_open_decision  (time passes: the card's 5
\* minutes run out)
Tick(s) ==
    /\ card[s] = "open" /\ ~expired[s]
    /\ expired' = [expired EXCEPT ![s] = TRUE]
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, sig, smode, card, stop,
                   exitDue, badEntry, badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/execution/router.py:expire  (the sweep expires a card past its window)
Expire(s) ==
    /\ card[s] = "open" /\ expired[s]
    /\ BrokenMode # "no_expiry_sweep"
    /\ card' = [card EXCEPT ![s] = "expired"]
    /\ sig' = [sig EXCEPT ![s] = DecidedAway]
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, smode, expired, stop, exitDue,
                   badEntry, badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/engine/trades.py:evaluate_position  (an exit rule fires)
ExitDue(s) ==
    /\ sig[s] \in {"placed", "virtual", "veto"} /\ ~exitDue[s]
    /\ exitDue' = [exitDue EXCEPT ![s] = TRUE]
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, sig, smode, card, expired,
                   stop, badEntry, badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/engine/trades.py:evaluate_position  (the exit goes out by itself in
\* every mode; no card is written)
ExitSend(s) ==
    /\ exitDue[s]
    /\ ~(BrokenMode = "exits_wait" /\ smode[s] = "confirm" /\ sig[s] = "placed")
    /\ sig' = [sig EXCEPT ![s] = "closed"]
    /\ exitDue' = [exitDue EXCEPT ![s] = FALSE]
    /\ exitAsked' = (exitAsked \/ (BrokenMode = "exit_asks" /\ smode[s] = "confirm"))
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, smode, card, expired, stop,
                   badEntry, badOrder, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/execution/mode_changes.py:request_mode_change  (outside market hours only;
\* confirm and auto need GTT; the change waits for the next open)
RequestMode(m) ==
    /\ phase = "closed" \/ BrokenMode = "apply_now"
    /\ m = "shadow" \/ gtt \/ BrokenMode = "no_gtt_gate"
    /\ IF BrokenMode = "apply_now"
          THEN /\ mode' = m
               /\ midChange' = (midChange \/ (phase = "open" /\ m # mode))
               /\ queued' = queued
          ELSE /\ queued' = m
               /\ UNCHANGED <<mode, midChange>>
    /\ UNCHANGED <<phase, gtt, stale, gate, chased, sig, smode, card, expired, stop, exitDue,
                   badEntry, badOrder, exitAsked>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/execution/mode_changes.py:apply_mode_changes  (the session opens: the queued
\* mode comes into force and the regime gate is not yet fresh for the day)
SessionOpen ==
    /\ phase = "closed"
    /\ phase' = "open"
    /\ mode' = IF queued = "none" THEN mode ELSE queued
    /\ queued' = "none"
    /\ gate' = "unknown"
    /\ UNCHANGED <<gtt, stale, chased, sig, smode, card, expired, stop, exitDue, badEntry,
                   badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/risk/sessions.py:state  (the calendar session closes)
SessionClose ==
    /\ phase = "open"
    /\ phase' = "closed"
    /\ UNCHANGED <<mode, queued, gtt, stale, gate, chased, sig, smode, card, expired, stop,
                   exitDue, badEntry, badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/engine/portfolio/jobs.py:regime_job  (the day's regime gate is known)
GateRefresh ==
    /\ gate = "unknown"
    /\ gate' \in {"on", "off"}
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, chased, sig, smode, card, expired, stop,
                   exitDue, badEntry, badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/data/staleness.py:check  (environment: the feed goes silent or ticks
\* again, or the day's data fails the quality gate)
FeedFlip ==
    /\ stale' = ~stale
    /\ UNCHANGED <<phase, mode, queued, gtt, gate, chased, sig, smode, card, expired, stop,
                   exitDue, badEntry, badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/execution/accounts.py:onboard_account  (environment: the account's
\* status or GTT capability changes)
GttFlip ==
    /\ gtt' = ~gtt
    /\ UNCHANGED <<phase, mode, queued, stale, gate, chased, sig, smode, card, expired, stop,
                   exitDue, badEntry, badOrder, exitAsked, midChange>>
    /\ UNCHANGED tgVars

\* code: backend/app/hat/engine/criteria/live.py:max_chase  (environment: the price runs past
\* the max-chase level or comes back)
PriceMove ==
    /\ chased' = ~chased
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, sig, smode, card, expired, stop,
                   exitDue, badEntry, badOrder, exitAsked, midChange>>

    /\ UNCHANGED tgVars

\* code: backend/app/hat/execution/session.py:record_session  (environment: the broker session
\* goes live after the daily login or is lost)
SessionFlip ==
    /\ live' = ~live
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, sig, smode, card, expired, stop,
                   exitDue, badEntry, badOrder, exitAsked, midChange, msg, userRisk>>

\* code: backend/app/hat/telegram/relay.py:message_for  (the relay sends one card for an open
\* approval or one shadow message for a virtual position; a rejected signal has neither)
Relay(s) ==
    /\ ~msg[s]
    /\ \/ card[s] = "open"
       \/ sig[s] = "virtual"
       \/ BrokenMode = "card_on_reject" /\ sig[s] = "rejected"
    /\ msg' = [msg EXCEPT ![s] = TRUE]
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, sig, smode, card, expired, stop,
                   exitDue, badEntry, badOrder, exitAsked, midChange, live, userRisk>>

\* code: backend/app/hat/telegram/commands.py:run_command  (the chat's command set has no entry
\* command: no text names a price, a size or a stop; the broken variant adds one)
UserEntry(s) ==
    /\ BrokenMode = "user_creates_entry"
    /\ sig[s] = "idle"
    /\ sig' = [sig EXCEPT ![s] = "placed"]
    /\ smode' = [smode EXCEPT ![s] = "auto"]
    /\ stop' = [stop EXCEPT ![s] = TRUE]
    /\ userRisk' = TRUE
    /\ UNCHANGED <<phase, mode, queued, gtt, stale, gate, chased, card, expired, exitDue,
                   badEntry, badOrder, exitAsked, midChange, live, msg>>

\* Every step but Crash, Place and PlaceTaken keeps `lost`.
RouterNext ==
    \/ SessionOpen \/ SessionClose \/ GateRefresh \/ FeedFlip \/ GttFlip \/ PriceMove
    \/ SessionFlip
    \/ \E m \in Asked : RequestMode(m)
    \/ \E s \in Signals :
          Route(s) \/ Reject(s) \/ (\E u \in BOOLEAN : Take(s, u)) \/ Skip(s)
          \/ Tick(s) \/ Expire(s) \/ ExitDue(s) \/ ExitSend(s) \/ Relay(s) \/ UserEntry(s)

Next ==
    \/ (RouterNext /\ UNCHANGED lost)
    \/ \E s \in Signals : Place(s) \/ PlaceTaken(s) \/ Crash(s)

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ \A s \in Signals :
          WF_routerVars(Tick(s)) /\ WF_routerVars(Expire(s)) /\ WF_routerVars(ExitSend(s))
    /\ \A s \in Signals : WF_vars(Place(s)) /\ WF_vars(PlaceTaken(s))

TypeOK ==
    /\ phase \in {"closed", "open"}
    /\ gtt \in BOOLEAN /\ stale \in BOOLEAN /\ chased \in BOOLEAN
    /\ gate \in {"unknown", "on", "off"}
    /\ sig \in [Signals -> SigStates]
    /\ card \in [Signals -> {"none", "open", "taken", "skipped", "expired"}]
    /\ expired \in [Signals -> BOOLEAN]
    /\ stop \in [Signals -> BOOLEAN]
    /\ exitDue \in [Signals -> BOOLEAN]
    /\ live \in BOOLEAN
    /\ msg \in [Signals -> BOOLEAN]
    /\ lost \in [Signals -> BOOLEAN]

\* Each routed signal took the route of the mode it was routed by: shadow a virtual position,
\* confirm a card (a real order only after a Take), auto a real order.
RouteByMode ==
    /\ "untaken" \notin badOrder
    /\ \A s \in Signals :
         /\ sig[s] = "virtual" => smode[s] = "shadow"
         /\ sig[s] = "awaiting" => smode[s] = "confirm" /\ card[s] = "open"
         /\ sig[s] = "placed" /\ smode[s] = "confirm" => card[s] = "taken"
         /\ sig[s] = "taken" => smode[s] = "confirm" /\ card[s] = "taken"
         /\ card[s] # "none" => smode[s] = "confirm"

\* A shadow signal never reaches the broker.
ShadowNeverCallsBroker == "shadow" \notin badOrder

\* Nothing is routed without passing the risk check.
RiskBeforeRoute == "unrisked" \notin badEntry

\* Every accepted entry carries its stop.
EntryHasStop == \A s \in Signals : sig[s] \in Holding => stop[s]

\* Max-chase is applied to every entry: at the trigger, and again at a Take.
MaxChaseAlwaysApplied == "chased" \notin badEntry \cup badOrder

\* A skipped or expired card continues as a virtual veto trade, never as a real order.
SkipBecomesShadow ==
    \A s \in Signals : card[s] \in {"skipped", "expired"} => sig[s] \in {"veto", "closed"}

\* No exit waits for the user.
ExitsNeverAsk == ~exitAsked

\* No entry and no order outside the calendar session.
NoOrderOutsideHours == "closed" \notin badEntry \cup badOrder

\* No entry and no order on a stale feed or a failed quality gate.
NoOrderWhenStale == "stale" \notin badEntry \cup badOrder

\* No entry before the session's regime gate is known.
GateFreshBeforeFirstEntry == "gateunknown" \notin badEntry \cup badOrder

\* No entry while the regime gate is off.
GateOffNoEntry == "gateoff" \notin badEntry \cup badOrder

\* The bot's mode is always one of the three.
ModeDomain ==
    /\ mode \in Modes
    /\ queued \in Modes \cup {"none"}
    /\ \A s \in Signals : smode[s] \in Modes \cup {"none"}

\* Every entry order buys.
LongOnly == "sell" \notin badOrder

\* No real order without GTT for delivery.
AutoNeedsGtt == "nogtt" \notin badOrder

\* At most one of the bot's positions (or pending entries) on the instrument.
NoAddToOpenPosition == Cardinality({s \in Signals : sig[s] \in Holding}) <= 1

\* The mode in force changes only at a session open.
ModeChangeNextSession == ~midChange

\* No real entry is sent while the broker session is not live (T14, HAT-BRK-082).
NoEntryWithoutSession == "nosession" \notin badOrder

\* A signal the risk check rejected gets no card and no shadow message (HAT-EXEC-043).
RejectNoCard == \A s \in Signals : sig[s] = "rejected" /\ smode[s] = "none" => ~msg[s]

\* No user action sets a size or creates an entry: the levers only reduce risk (HAT-GOV-080).
UserCannotRaiseRisk == ~userRisk

\* Every card is decided: taken, skipped or expired.
ApprovalsDecided == \A s \in Signals : [](sig[s] = "awaiting" => <>(sig[s] # "awaiting"))

\* Every committed entry is placed or ended: a crash between a signal's (or a Take's) commit
\* and its placement never leaves it waiting, holding its slot (PR 52, audit T-F7).
EntriesSettled == \A s \in Signals : [](sig[s] \in Unplaced => <>(sig[s] \notin Unplaced))

\* An exit that is due is eventually sent, in every mode.
ExitsComplete == \A s \in Signals : [](exitDue[s] => <>(sig[s] = "closed"))
================================================================================

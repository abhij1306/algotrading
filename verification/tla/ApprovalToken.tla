----------------------------- MODULE ApprovalToken -----------------------------
(***************************************************************************)
(* Approval cards of confirm mode (07 §3; 08 §4).                          *)
(*                                                                         *)
(* NCards approval cards on one clock. A card is sent with its signal and  *)
(* expires Window ticks later. It is decided once, under the bot's lock:   *)
(* a Take before expiry re-checks the price against the max-chase level    *)
(* fixed at the trigger and places the real order only when it passes; a   *)
(* Skip before expiry ends it; a card still undecided at its expiry, or a  *)
(* Take or Skip that arrives at or after it, ends Expired. The token's     *)
(* signature and single use at the Telegram edge are PR 16's (this model   *)
(* checks the decision service behind it).                                 *)
(*                                                                         *)
(* Bounds: NCards = 2 cards; the clock runs 0..MaxTime with MaxTime = 4;   *)
(*   Window = 2 ticks stands for the 5 minutes (a card is sent only while  *)
(*   its whole window fits on the clock); the price crosses the max-chase  *)
(*   level and back without bound; BrokenMode names a deliberately broken  *)
(*   variant ("none" is the real service).                                 *)
(* Assumptions: each action is one database transaction holding the bot's *)
(*   lock; the decision compares the clock with the stored expires_at in   *)
(*   that transaction; time passes and the expiry sweep runs (weak         *)
(*   fairness); the price is arbitrary.                                    *)
(*                                                                         *)
(* Properties: NoTakeAfterExpiry, ChaseRecheckOnTake, SingleDecision       *)
(*   (invariants) and CardsDecided (temporal).                             *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS NCards, Window, MaxTime, BrokenMode

ASSUME BrokenMode \in {"none", "take_after_expiry", "no_chase_recheck", "decide_twice",
                       "no_sweep"}
ASSUME Window >= 1 /\ MaxTime >= Window

Cards == 1..NCards
Decisions == {"none", "take", "rejected", "skip", "expired"}

VARIABLES
    clock,     \* the current tick
    sent,      \* per card: 0 when not sent, else the tick it was sent plus 1
    decision,  \* per card: none | take | rejected (max-chase) | skip | expired
    decided,   \* per card: how many decisions were recorded
    chased,    \* the price is past the max-chase level now
    lateTake,  \* ghost: a card was taken at or after its expiry
    chaseTake  \* ghost: an order was placed while the price was past max-chase

vars == <<clock, sent, decision, decided, chased, lateTake, chaseTake>>

Init ==
    /\ clock = 0
    /\ sent = [c \in Cards |-> 0]
    /\ decision = [c \in Cards |-> "none"]
    /\ decided = [c \in Cards |-> 0]
    /\ chased \in BOOLEAN
    /\ lateTake = FALSE
    /\ chaseTake = FALSE

IsSent(c) == sent[c] > 0
ExpiresAt(c) == sent[c] - 1 + Window
Open(c) == IsSent(c) /\ (decision[c] = "none" \/ (BrokenMode = "decide_twice" /\ decided[c] < 2))
Live(c) == clock < ExpiresAt(c)

\* code: backend/app/hat/engine/trades.py:_send_approval  (the card is written with its signal,
\* expiring APPROVAL_WINDOW after it is sent)
Send(c) ==
    /\ ~IsSent(c)
    /\ clock + Window <= MaxTime
    /\ sent' = [sent EXCEPT ![c] = clock + 1]
    /\ UNCHANGED <<clock, decision, decided, chased, lateTake, chaseTake>>

\* code: backend/app/hat/engine/approvals.py:take  (before expiry: the max-chase re-check passes
\* and the order may be placed, or the card ends Rejected)
Take(c) ==
    /\ Open(c)
    /\ Live(c) \/ BrokenMode = "take_after_expiry"
    /\ LET pass == ~chased \/ BrokenMode = "no_chase_recheck"
       IN /\ decision' = [decision EXCEPT ![c] = IF pass THEN "take" ELSE "rejected"]
          /\ chaseTake' = (chaseTake \/ (pass /\ chased))
          /\ lateTake' = (lateTake \/ (pass /\ ~Live(c)))
    /\ decided' = [decided EXCEPT ![c] = @ + 1]
    /\ UNCHANGED <<clock, sent, chased>>

\* code: backend/app/hat/engine/approvals.py:skip
Skip(c) ==
    /\ Open(c) /\ Live(c)
    /\ decision' = [decision EXCEPT ![c] = "skip"]
    /\ decided' = [decided EXCEPT ![c] = @ + 1]
    /\ UNCHANGED <<clock, sent, chased, lateTake, chaseTake>>

\* code: backend/app/hat/engine/approvals.py:expire_due  (the sweep, or a late Take or Skip,
\* expires an undecided card at or past its expiry)
Expire(c) ==
    /\ IsSent(c) /\ decision[c] = "none" /\ ~Live(c)
    /\ BrokenMode # "no_sweep"
    /\ decision' = [decision EXCEPT ![c] = "expired"]
    /\ decided' = [decided EXCEPT ![c] = @ + 1]
    /\ UNCHANGED <<clock, sent, chased, lateTake, chaseTake>>

\* code: backend/app/hat/engine/approvals.py:_open_decision  (time passes)
Tick ==
    /\ clock < MaxTime
    /\ clock' = clock + 1
    /\ UNCHANGED <<sent, decision, decided, chased, lateTake, chaseTake>>

\* code: backend/app/hat/engine/criteria/live.py:max_chase  (environment: the price crosses
\* the max-chase level)
PriceMove ==
    /\ chased' = ~chased
    /\ UNCHANGED <<clock, sent, decision, decided, lateTake, chaseTake>>

Next ==
    \/ Tick \/ PriceMove
    \/ \E c \in Cards : Send(c) \/ Take(c) \/ Skip(c) \/ Expire(c)

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ WF_vars(Tick)
    /\ \A c \in Cards : WF_vars(Expire(c))

TypeOK ==
    /\ clock \in 0..MaxTime
    /\ sent \in [Cards -> 0..(MaxTime + 1)]
    /\ decision \in [Cards -> Decisions]
    /\ decided \in [Cards -> 0..2]
    /\ chased \in BOOLEAN

\* No card is taken at or after its expiry.
NoTakeAfterExpiry == ~lateTake

\* No Take places an order at a price past the max-chase level.
ChaseRecheckOnTake == ~chaseTake

\* A card is decided at most once.
SingleDecision == \A c \in Cards : decided[c] <= 1

\* Every card that is sent is decided: taken, rejected, skipped or expired.
CardsDecided == \A c \in Cards : [](IsSent(c) => <>(decision[c] # "none"))
================================================================================

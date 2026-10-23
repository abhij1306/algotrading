----------------------------- MODULE ApprovalToken -----------------------------
(***************************************************************************)
(* Approval cards of confirm mode.                          *)
(*                                                                         *)
(* NCards approval cards on one clock. A card is sent with its signal and  *)
(* expires Window ticks later. It is decided once, under the bot's lock:   *)
(* a Take before expiry re-checks the price against the max-chase level    *)
(* fixed at the trigger and places the real order only when it passes; a   *)
(* Skip before expiry ends it; a card still undecided at its expiry, or a  *)
(* Take or Skip that arrives at or after it, ends Expired. The token's     *)
(* signature is checked by PBT telegram.token_forgery_rejected; single use *)
(* is SingleDecision.                                                      *)
(*                                                                         *)
(* Second confirmations (PR 16): /mode … auto, /arm and /flatten    *)
(* only ask; the lever acts when the Confirm button of that ask is pressed *)
(* in the allowlisted chat before the ask expires (Window, the same 5      *)
(* minutes), and an ask is consumed by its first press.                    *)
(*                                                                         *)
(* Bounds: NCards = 2 cards; the clock runs 0..MaxTime with MaxTime = 4;   *)
(*   Window = 2 ticks stands for the 5 minutes (a card is sent only while  *)
(*   its whole window fits on the clock); the price crosses the max-chase  *)
(*   level and back without bound; the three levers (auto, arm, flatten)   *)
(*   each hold at most one pending ask, re-asked without bound; a press    *)
(*   comes from the allowlisted chat or another one; BrokenMode names a    *)
(*   deliberately broken variant ("none" is the real service).             *)
(* Assumptions: each action is one database transaction holding the bot's *)
(*   lock; the decision compares the clock with the stored expires_at in   *)
(*   that transaction; time passes and the expiry sweep runs (weak         *)
(*   fairness); the price is arbitrary.                                    *)
(*                                                                         *)
(* Properties: NoTakeAfterExpiry, ChaseRecheckOnTake, SingleDecision,      *)
(*   SecondConfirmRequired (invariants) and CardsDecided (temporal).       *)
(***************************************************************************)
EXTENDS Naturals

CONSTANTS NCards, Window, MaxTime, BrokenMode

ASSUME BrokenMode \in {"none", "take_after_expiry", "no_chase_recheck", "decide_twice",
                       "no_sweep", "act_without_confirm", "confirm_twice",
                       "confirm_after_expiry", "other_chat"}
ASSUME Window >= 1 /\ MaxTime >= Window

Cards == 1..NCards
Decisions == {"none", "take", "rejected", "skip", "expired"}
Levers == {"auto", "arm", "flatten"}

VARIABLES
    clock,     \* the current tick
    sent,      \* per card: 0 when not sent, else the tick it was sent plus 1
    decision,  \* per card: none | take | rejected (max-chase) | skip | expired
    decided,   \* per card: how many decisions were recorded
    chased,    \* the price is past the max-chase level now
    lateTake,  \* ghost: a card was taken at or after its expiry
    chaseTake, \* ghost: an order was placed while the price was past max-chase
    ask,       \* per lever: 0 when nothing is pending, else the tick it was asked plus 1
    used,      \* per lever: the pending ask was already pressed once
    unconfirmed \* ghost: a lever acted without a fresh, unused ask pressed in the chat

cardVars == <<clock, sent, decision, decided, chased, lateTake, chaseTake>>
leverVars == <<ask, used, unconfirmed>>
vars == <<clock, sent, decision, decided, chased, lateTake, chaseTake, ask, used, unconfirmed>>

Init ==
    /\ clock = 0
    /\ sent = [c \in Cards |-> 0]
    /\ decision = [c \in Cards |-> "none"]
    /\ decided = [c \in Cards |-> 0]
    /\ chased \in BOOLEAN
    /\ lateTake = FALSE
    /\ chaseTake = FALSE
    /\ ask = [l \in Levers |-> 0]
    /\ used = [l \in Levers |-> FALSE]
    /\ unconfirmed = FALSE

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
    /\ UNCHANGED leverVars

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
    /\ UNCHANGED leverVars

\* code: backend/app/hat/engine/approvals.py:skip
Skip(c) ==
    /\ Open(c) /\ Live(c)
    /\ decision' = [decision EXCEPT ![c] = "skip"]
    /\ decided' = [decided EXCEPT ![c] = @ + 1]
    /\ UNCHANGED <<clock, sent, chased, lateTake, chaseTake>>
    /\ UNCHANGED leverVars

\* code: backend/app/hat/engine/approvals.py:expire_due  (the sweep, or a late Take or Skip,
\* expires an undecided card at or past its expiry)
Expire(c) ==
    /\ IsSent(c) /\ decision[c] = "none" /\ ~Live(c)
    /\ BrokenMode # "no_sweep"
    /\ decision' = [decision EXCEPT ![c] = "expired"]
    /\ decided' = [decided EXCEPT ![c] = @ + 1]
    /\ UNCHANGED <<clock, sent, chased, lateTake, chaseTake>>
    /\ UNCHANGED leverVars

\* code: backend/app/hat/engine/approvals.py:_open_decision  (time passes)
Tick ==
    /\ clock < MaxTime
    /\ clock' = clock + 1
    /\ UNCHANGED <<sent, decision, decided, chased, lateTake, chaseTake>>
    /\ UNCHANGED leverVars

\* code: backend/app/hat/engine/criteria/live.py:max_chase  (environment: the price crosses
\* the max-chase level)
PriceMove ==
    /\ chased' = ~chased
    /\ UNCHANGED <<clock, sent, decision, decided, lateTake, chaseTake>>
    /\ UNCHANGED leverVars

\* code: backend/app/hat/telegram/confirm.py:ask  (/mode … auto, /arm or /flatten: the command
\* only registers a pending ask with a fresh nonce; act_without_confirm acts at once)
Ask(l) ==
    /\ ask' = [ask EXCEPT ![l] = clock + 1]
    /\ used' = [used EXCEPT ![l] = FALSE]
    /\ unconfirmed' = (unconfirmed \/ BrokenMode = "act_without_confirm")
    /\ UNCHANGED cardVars

\* code: backend/app/hat/telegram/confirm.py:take  (the Confirm button: the ask is removed
\* first, then it acts only from the same chat before its expiry; callbacks.py:_confirmed)
Confirm(l, own) ==
    /\ ask[l] > 0
    /\ LET live == clock < ask[l] - 1 + Window
           acts == /\ live \/ BrokenMode = "confirm_after_expiry"
                   /\ own \/ BrokenMode = "other_chat"
       IN unconfirmed' = (unconfirmed \/ (acts /\ (~live \/ ~own \/ used[l])))
    /\ IF BrokenMode = "confirm_twice"
          THEN /\ ask' = ask
               /\ used' = [used EXCEPT ![l] = TRUE]
          ELSE /\ ask' = [ask EXCEPT ![l] = 0]
               /\ used' = used
    /\ UNCHANGED cardVars

Next ==
    \/ Tick \/ PriceMove
    \/ \E c \in Cards : Send(c) \/ Take(c) \/ Skip(c) \/ Expire(c)
    \/ \E l \in Levers : Ask(l) \/ \E own \in BOOLEAN : Confirm(l, own)

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
    /\ ask \in [Levers -> 0..(MaxTime + 1)]
    /\ used \in [Levers -> BOOLEAN]

\* No card is taken at or after its expiry.
NoTakeAfterExpiry == ~lateTake

\* No Take places an order at a price past the max-chase level.
ChaseRecheckOnTake == ~chaseTake

\* A card is decided at most once.
SingleDecision == \A c \in Cards : decided[c] <= 1

\* Auto, a re-arm and a flatten act only on the first press, in the allowlisted chat, of an ask
\* that has not expired (HAT-TG-033).
SecondConfirmRequired == ~unconfirmed

\* Every card that is sent is decided: taken, rejected, skipped or expired.
CardsDecided == \A c \in Cards : [](IsSent(c) => <>(decision[c] # "none"))
================================================================================

------------------------- MODULE OrderReconciliation -------------------------
(***************************************************************************)
(* Persist before call, tag idempotency, unconfirmed submits, reconcile    *)
(* before the first order, mismatches, account onboarding and session      *)
(* ownership (07 §9-§11, 01 T8 B24, 10 §OPS-032).                          *)
(*                                                                         *)
(* One signal with one entry purpose is modelled. Its logical orders are   *)
(* tags 0..MaxTags-1 (the sequence character): tag 0 is the order, tag     *)
(* t+1 exists only as the one allowed retry after tag t was rejected. Each *)
(* tag has a hat_order row (none, pending, unconfirmed, placed, rejected), *)
(* a broker book count (how many broker orders carry the tag), and the    *)
(* send protocol: stamp the attempt, call the broker, record the answer.   *)
(*                                                                         *)
(* Bounds: MaxTags = 3 tags (the original and up to two retries; the real  *)
(*   protocol allows one); MaxRetries = 1 retry after a rejection;         *)
(*   MaxBook = 2 broker orders per tag counted (2 means a double place);   *)
(*   MaxDiverge = 1 broker/HAT divergences; NOwners = 2 session services;  *)
(*   MaxRestarts = 2 process restarts; BrokenMode names a deliberately     *)
(*   broken variant ("none" is the real protocol).                         *)
(* Assumptions: each action is one database transaction or one broker call *)
(*   (atomic), and a crash can fall between any two of them (Restart); the *)
(*   broker never invents an order, answers a lookup truthfully and a      *)
(*   rejection means nothing was placed; an answer can be lost with the    *)
(*   order landed or not; the tag lookup is consistent with the broker's   *)
(*   book (a real risk at Fyers: the order book may lag, see verify-       *)
(*   execution "Known window"); the capability check, a divergence and its *)
(*   repair are environment steps; exits and stops are not modelled (they  *)
(*   never consult the entry block, see the PBT refusals property);        *)
(*   scheduling is otherwise arbitrary, with no fairness needed (every     *)
(*   property here is an invariant).                                       *)
(*                                                                         *)
(* Properties: PersistBeforeCall, AtMostOneEntryPerSignal,                 *)
(*   UnconfirmedResolvedByTag, ReconcileBeforeFirstOrder,                  *)
(*   MismatchBlocksEntries, AlertOncePerMismatch, AccountActiveAfterCheck, *)
(*   OneSessionOwner, ProductIsCNC, RetryAtMostOnce (all invariants).      *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS MaxTags, MaxRetries, MaxBook, MaxDiverge, NOwners, MaxRestarts, BrokenMode

ASSUME BrokenMode \in {"none", "no_persist", "retry_new_tag", "retry_without_lookup",
                       "no_reconcile", "ignore_mismatch", "alert_every_run",
                       "inactive_account", "two_owners", "non_cnc", "unlimited_retry"}

Tags == 0..(MaxTags - 1)
Owners == 1..NOwners

VARIABLES
    row,          \* per tag: none | pending | unconfirmed | placed | rejected
    attempted,    \* per tag: the attempt was stamped in hat_order.placed_at
    stage,        \* per tag: idle | stamped | called
    answer,       \* per tag, while called: accepted | rejected | lost_landed | lost_not_landed
    unresolved,   \* per tag: the outcome is unknown and no tag lookup has run since
    book,         \* per tag: broker orders carrying the tag
    product,      \* per tag: none | CNC | OTHER (the product the intent was built with)
    retries,      \* retries after a rejection so far
    acct,         \* new | active | failed
    checked,      \* the capability check passed
    owners,       \* session services holding the account's live session
    reconciled,   \* a reconciliation succeeded in this process lifetime
    diverged,     \* the broker's book and HAT's differ (an unresolved mismatch exists)
    divergeCount, \* divergences so far
    blocked,      \* entries are blocked for the bot
    alerts,       \* mismatch alerts raised
    restarts,     \* process restarts so far
    violUnresolved, violUnreconciled, violBlocked, violInactive  \* history flags of a place call

vars == <<row, attempted, stage, answer, unresolved, book, product, retries, acct, checked,
          owners, reconciled, diverged, divergeCount, blocked, alerts, restarts,
          violUnresolved, violUnreconciled, violBlocked, violInactive>>

Init ==
    /\ row = [t \in Tags |-> "none"]
    /\ attempted = [t \in Tags |-> FALSE]
    /\ stage = [t \in Tags |-> "idle"]
    /\ answer = [t \in Tags |-> "none"]
    /\ unresolved = [t \in Tags |-> FALSE]
    /\ book = [t \in Tags |-> 0]
    /\ product = [t \in Tags |-> "none"]
    /\ retries = 0
    /\ acct = "new"
    /\ checked = FALSE
    /\ owners = {}
    /\ reconciled = FALSE
    /\ diverged = FALSE
    /\ divergeCount = 0
    /\ blocked = FALSE
    /\ alerts = 0
    /\ restarts = 0
    /\ violUnresolved = FALSE
    /\ violUnreconciled = FALSE
    /\ violBlocked = FALSE
    /\ violInactive = FALSE

\* code: backend/app/hat/execution/accounts.py:onboard_account  (funds, positions and holdings
\* are fetched; the account becomes active only when all three succeed, else failed)
CheckAccount ==
    /\ acct = "new"
    /\ \/ acct' = "active" /\ checked' = TRUE
       \/ acct' = "failed" /\ checked' = FALSE
    /\ UNCHANGED <<row, attempted, stage, answer, unresolved, book, product, retries, owners,
                   reconciled, diverged, divergeCount, blocked, alerts, restarts,
                   violUnresolved, violUnreconciled, violBlocked, violInactive>>

\* code: backend/app/hat/execution/session.py:acquire  (one owner per account session)
AcquireSession(o) ==
    /\ o \notin owners
    /\ owners = {} \/ BrokenMode = "two_owners"
    /\ owners' = owners \cup {o}
    /\ UNCHANGED <<row, attempted, stage, answer, unresolved, book, product, retries, acct,
                   checked, reconciled, diverged, divergeCount, blocked, alerts, restarts,
                   violUnresolved, violUnreconciled, violBlocked, violInactive>>

\* code: backend/app/hat/execution/session.py:release
ReleaseSession(o) ==
    /\ o \in owners
    /\ owners' = owners \ {o}
    /\ UNCHANGED <<row, attempted, stage, answer, unresolved, book, product, retries, acct,
                   checked, reconciled, diverged, divergeCount, blocked, alerts, restarts,
                   violUnresolved, violUnreconciled, violBlocked, violInactive>>

\* code: backend/app/hat/execution/submit.py:reserve_order  (insert the PENDING row and commit,
\* before any broker call; tag t>0 is the one retry, allowed only after tag t-1 was rejected)
Reserve(t, p) ==
    /\ row[t] = "none"
    /\ \/ t = 0
       \/ /\ t > 0
          /\ \/ row[t - 1] = "rejected"
             \/ (BrokenMode = "retry_new_tag" /\ row[t - 1] = "unconfirmed")
          /\ \/ retries < MaxRetries
             \/ BrokenMode = "unlimited_retry"
    /\ row' = [row EXCEPT ![t] = "pending"]
    /\ product' = [product EXCEPT ![t] = p]
    /\ retries' = IF t > 0 THEN retries + 1 ELSE retries
    /\ UNCHANGED <<attempted, stage, answer, unresolved, book, acct, checked, owners, reconciled,
                   diverged, divergeCount, blocked, alerts, restarts, violUnresolved,
                   violUnreconciled, violBlocked, violInactive>>

\* code: backend/app/hat/execution/submit.py:send_order  (the guards, then commit the attempt in
\* hat_order.placed_at before the broker call; an order whose outcome is unknown needs a tag
\* lookup first: unresolved is cleared only by Lookup)
Stamp(t) ==
    /\ \/ row[t] \in {"pending", "unconfirmed"}
       \/ BrokenMode = "no_persist" /\ t = 0 /\ row[t] = "none"
    /\ stage[t] = "idle"
    /\ \/ ~unresolved[t]
       \/ BrokenMode = "retry_without_lookup"
    /\ \/ acct = "active"
       \/ BrokenMode = "inactive_account"
    /\ \/ reconciled
       \/ BrokenMode = "no_reconcile"
    /\ \/ ~blocked
       \/ BrokenMode = "ignore_mismatch"
    /\ \/ product[t] = "CNC"
       \/ BrokenMode \in {"non_cnc", "no_persist"}
    /\ attempted' = [attempted EXCEPT ![t] = TRUE]
    /\ stage' = [stage EXCEPT ![t] = "stamped"]
    /\ UNCHANGED <<row, answer, unresolved, book, product, retries, acct, checked, owners,
                   reconciled, diverged, divergeCount, blocked, alerts, restarts,
                   violUnresolved, violUnreconciled, violBlocked, violInactive>>

\* code: backend/app/hat/execution/fyers_adapter.py:place_order  (one broker call, after the guard
\* is read again: a reconciliation that blocked the bot or a restart since the stamp stops the
\* call; the answer is accepted, rejected (nothing placed), or lost with the order landed or not)
PlaceCall(t, a) ==
    /\ stage[t] = "stamped"
    /\ \/ reconciled
       \/ BrokenMode = "no_reconcile"
    /\ \/ ~blocked
       \/ BrokenMode = "ignore_mismatch"
    /\ \/ a \in {"accepted", "lost_landed"} /\ book[t] < MaxBook
       \/ a \in {"rejected", "lost_not_landed"}
    /\ book' = [book EXCEPT ![t] = IF a \in {"accepted", "lost_landed"} THEN @ + 1 ELSE @]
    /\ answer' = [answer EXCEPT ![t] = a]
    /\ stage' = [stage EXCEPT ![t] = "called"]
    /\ violUnresolved' = (violUnresolved \/ unresolved[t])
    /\ violUnreconciled' = (violUnreconciled \/ ~reconciled)
    /\ violBlocked' = (violBlocked \/ blocked)
    /\ violInactive' = (violInactive \/ acct # "active")
    /\ UNCHANGED <<row, attempted, unresolved, product, retries, acct, checked, owners,
                   reconciled, diverged, divergeCount, blocked, alerts, restarts>>

\* code: backend/app/hat/execution/submit.py:send_order  (record the answer: placed, rejected, or
\* unconfirmed, and commit)
Record(t) ==
    /\ stage[t] = "called"
    /\ row' = [row EXCEPT ![t] = CASE answer[t] = "accepted" -> "placed"
                                  [] answer[t] = "rejected" -> "rejected"
                                  [] OTHER -> "unconfirmed"]
    /\ unresolved' = [unresolved EXCEPT ![t] = answer[t] \in {"lost_landed", "lost_not_landed"}]
    /\ stage' = [stage EXCEPT ![t] = "idle"]
    /\ answer' = [answer EXCEPT ![t] = "none"]
    /\ UNCHANGED <<attempted, book, product, retries, acct, checked, owners, reconciled,
                   diverged, divergeCount, blocked, alerts, restarts, violUnresolved,
                   violUnreconciled, violBlocked, violInactive>>

\* code: backend/app/hat/execution/submit.py:resolve_by_tag  (look the tag up in the broker's
\* order book: a found order is adopted with its broker id, absent clears the unknown state)
Lookup(t) ==
    /\ stage[t] = "idle"
    /\ unresolved[t]
    /\ row' = [row EXCEPT ![t] = IF book[t] > 0 THEN "placed" ELSE @]
    /\ unresolved' = [unresolved EXCEPT ![t] = FALSE]
    /\ UNCHANGED <<attempted, stage, answer, book, product, retries, acct, checked, owners,
                   reconciled, diverged, divergeCount, blocked, alerts, restarts,
                   violUnresolved, violUnreconciled, violBlocked, violInactive>>

\* code: backend/app/hat/execution/submit.py:send_order  (the process dies between two steps: an
\* attempt that was stamped or called now has an unknown outcome; the guard forgets)
Restart ==
    /\ restarts < MaxRestarts
    /\ restarts' = restarts + 1
    /\ reconciled' = FALSE
    /\ owners' = {}
    /\ unresolved' = [t \in Tags |-> unresolved[t] \/ stage[t] # "idle"]
    /\ stage' = [t \in Tags |-> "idle"]
    /\ answer' = [t \in Tags |-> "none"]
    /\ UNCHANGED <<row, attempted, book, product, retries, acct, checked, diverged,
                   divergeCount, blocked, alerts, violUnresolved, violUnreconciled,
                   violBlocked, violInactive>>

\* code: backend/app/hat/execution/reconcile.py:run  (read the broker's book; block entries and
\* raise one alert for a new mismatch; a cleared mismatch unblocks)
Reconcile ==
    /\ reconciled' = TRUE
    /\ blocked' = diverged
    /\ alerts' = IF diverged /\ (~blocked \/ BrokenMode = "alert_every_run")
                    THEN alerts + 1 ELSE alerts
    /\ UNCHANGED <<row, attempted, stage, answer, unresolved, book, product, retries, acct,
                   checked, owners, diverged, divergeCount, restarts, violUnresolved,
                   violUnreconciled, violBlocked, violInactive>>

\* code: backend/app/hat/execution/reconcile.py:find_mismatches  (environment: the broker holds
\* something HAT does not, or the reverse)
Diverge ==
    /\ divergeCount < MaxDiverge
    /\ diverged' = TRUE
    /\ divergeCount' = divergeCount + 1
    /\ UNCHANGED <<row, attempted, stage, answer, unresolved, book, product, retries, acct,
                   checked, owners, reconciled, blocked, alerts, restarts, violUnresolved,
                   violUnreconciled, violBlocked, violInactive>>

\* code: backend/app/hat/execution/reconcile.py:find_mismatches  (environment: the owner repairs
\* the difference; HAT never adopts or closes a position itself)
Repair ==
    /\ diverged
    /\ diverged' = FALSE
    /\ UNCHANGED <<row, attempted, stage, answer, unresolved, book, product, retries, acct,
                   checked, owners, reconciled, divergeCount, blocked, alerts, restarts,
                   violUnresolved, violUnreconciled, violBlocked, violInactive>>

Next ==
    \/ CheckAccount
    \/ Restart
    \/ Reconcile
    \/ Diverge
    \/ Repair
    \/ \E o \in Owners : AcquireSession(o) \/ ReleaseSession(o)
    \/ \E t \in Tags :
          \/ \E p \in {"CNC", "OTHER"} : Reserve(t, p)
          \/ Stamp(t)
          \/ \E a \in {"accepted", "rejected", "lost_landed", "lost_not_landed"} : PlaceCall(t, a)
          \/ Record(t)
          \/ Lookup(t)

Spec == Init /\ [][Next]_vars

Total == book[0] + book[1] + book[2]

TypeOK ==
    /\ row \in [Tags -> {"none", "pending", "unconfirmed", "placed", "rejected"}]
    /\ stage \in [Tags -> {"idle", "stamped", "called"}]
    /\ book \in [Tags -> 0..MaxBook]
    /\ product \in [Tags -> {"none", "CNC", "OTHER"}]
    /\ owners \subseteq Owners
    /\ acct \in {"new", "active", "failed"}
    /\ retries \in 0..MaxTags

\* Every broker order carries a tag whose hat_order row already exists (the PENDING row is
\* committed before the broker is called).
PersistBeforeCall == \A t \in Tags : book[t] > 0 => row[t] # "none"

\* One signal never yields two entry orders: at most one broker order per tag, and at most one
\* in total across the original and its retry.
AtMostOneEntryPerSignal == (\A t \in Tags : book[t] <= 1) /\ Total <= 1

\* No place call with a tag whose outcome was unknown happens before a tag lookup ran.
UnconfirmedResolvedByTag == ~violUnresolved

\* No place call happens in a process lifetime before a successful reconciliation.
ReconcileBeforeFirstOrder == ~violUnreconciled

\* No entry is placed while a mismatch is blocking the bot.
MismatchBlocksEntries == ~violBlocked

\* A mismatch raises one alert, however many reconciliations see it.
AlertOncePerMismatch == alerts <= divergeCount

\* A real order is placed only for an account that passed the capability check.
AccountActiveAfterCheck == ~violInactive /\ (acct = "active" => checked)

\* At most one session service holds the account's live session.
OneSessionOwner == Cardinality(owners) <= 1

\* Every broker order was built with product CNC.
ProductIsCNC == \A t \in Tags : book[t] > 0 => product[t] = "CNC"

\* A rejected order is retried at most once.
RetryAtMostOnce == retries <= 1
==============================================================================

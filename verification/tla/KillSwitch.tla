------------------------------ MODULE KillSwitch ------------------------------
(***************************************************************************)
(* Kill switches, pause and manage-only.            *)
(*                                                                         *)
(* One account and NBots bots. A bot is manage-only (takes no new entry)   *)
(* when the account kill is tripped, its own kill is tripped, its health   *)
(* kill is tripped, it is paused, or a live health input is failing.       *)
(* Exits are never gated. Drawdown and manual kills re-arm only through a  *)
(* second, confirmed step; the health kill clears by itself, each source  *)
(* (the nightly quality gate, the live feed) clearing only its own trip    *)
(* (PR 17: the feed recovering never clears a failed gate). /flatten only  *)
(* asks; the confirmed flatten trips the account kill, then sells every    *)
(* open position through the exit path (purpose flatten), whatever the     *)
(* kill or pause state (PR 16).                                            *)
(*                                                                         *)
(* Bounds: NBots = 2 bots; each bot has one position cycle (none, open,    *)
(*   closed) and one approval in flight; kills, health flips, pauses and   *)
(*   re-arms repeat without bound (the state space is finite because every *)
(*   variable has a finite domain).                                        *)
(* Assumptions: each action below is one database transaction (atomic) or  *)
(*   one environment change; an equity read (the snapshot.bots job, the    *)
(*   intraday drawdown step) reads the drawdown state and acts on it in    *)
(*   the same transaction; the                                             *)
(*   entry gate reads the live health inputs and the stored switches in    *)
(*   one step; an exit that is due and enabled is eventually sent, and a   *)
(*   health check that can act eventually does (weak fairness); the        *)
(*   environment (equity, health, due exits) is otherwise arbitrary.       *)
(*                                                                         *)
(* Properties: TripOnDrawdown, TripImpliesManageOnly, KillCancelsApprovals,*)
(*   DataHealthManageOnly, HealthClearsOwnSource, NoAutoRearm,             *)
(*   PauseBlocksEntriesOnly (invariants),                                  *)
(*   FlattenNeedsConfirm, FlattenExitOnly (invariants) and HealthAutoRearm,*)
(*   ExitsContinue (temporal).                                             *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS NBots


Bots == 1..NBots
Reasons == {"acct", "bot", "health", "unhealthy", "paused"}
\* The health sources, each a reason of its own (risk/kill.py HEALTH_REASONS): the quality gate
\* (data_health) and the live feed (feed_health); broker_health behaves the same way.
Sources == {"quality", "feed"}

VARIABLES
    acct,        \* account kill: armed | tripped
    bot,         \* per bot kill: armed | tripped
    health,      \* per health source: the account's stored health kill, armed | tripped
    unhealthy,   \* per health source: that input is failing now
    paused,      \* per bot: paused by the user
    appr,        \* per bot approval: none | pending | taken | cancelled
    pos,         \* per bot position: none | open | closed
    exitDue,     \* per bot: an exit rule has fired for the open position
    acctSeen,    \* an account drawdown breach was read since the last re-arm
    botSeen,     \* per bot: a bot drawdown breach was read since the last re-arm
    askedAcct,   \* the second confirmation step for the account re-arm was taken
    askedBot,    \* per bot: the same for the bot kill
    badEntry,    \* ghost: the reasons that were in force when an entry was placed
    badRearm,    \* ghost: a drawdown or manual kill was re-armed without the second step
    askedFlat,   \* /flatten asked and its confirmation is pending
    badFlat,     \* ghost: a flatten ran without the confirmed second step
    flatOpen,    \* ghost: a flatten opened or grew a position
    badClear     \* ghost: a source's health trip was cleared while that source still failed

flatVars == <<askedFlat, badFlat, flatOpen, badClear>>
vars == <<acct, bot, health, unhealthy, paused, appr, pos, exitDue, acctSeen, botSeen,
          askedAcct, askedBot, badEntry, badRearm, askedFlat, badFlat, flatOpen, badClear>>

Init ==
    /\ acct = "armed"
    /\ bot = [b \in Bots |-> "armed"]
    /\ health = [s \in Sources |-> "armed"]
    /\ unhealthy = [s \in Sources |-> FALSE]
    /\ paused = [b \in Bots |-> FALSE]
    /\ appr = [b \in Bots |-> "none"]
    /\ pos = [b \in Bots |-> "none"]
    /\ exitDue = [b \in Bots |-> FALSE]
    /\ acctSeen = FALSE
    /\ botSeen = [b \in Bots |-> FALSE]
    /\ askedAcct = FALSE
    /\ askedBot = [b \in Bots |-> FALSE]
    /\ badEntry = {}
    /\ badRearm = FALSE
    /\ askedFlat = FALSE
    /\ badFlat = FALSE
    /\ flatOpen = FALSE
    /\ badClear = FALSE

\* What blocks a new entry for bot b right now.
Blocking(b) ==
    {r \in Reasons :
        \/ r = "acct" /\ acct = "tripped"
        \/ r = "bot" /\ bot[b] = "tripped"
        \/ r = "health" /\ \E s \in Sources : health[s] = "tripped"
        \/ r = "unhealthy" /\ \E s \in Sources : unhealthy[s]
        \/ r = "paused" /\ paused[b]}

\* The entry gate (evaluate_entry): an entry is allowed only when nothing blocks.
EntryAllowed(b) == Blocking(b) = {}

\* code: backend/app/hat/risk/drawdown.py:evaluate_drawdown_kills  (snapshot.bots, and every
\* minute of the session Intraday.drawdown, reads the account equity marked to market against
\* its peak; a breach trips the account kill and ends pending approvals)
SnapshotAccount ==
    /\ \E breach \in BOOLEAN :
         /\ acctSeen' = (acctSeen \/ breach)
         /\ IF breach
               THEN /\ acct' = "tripped"
                    /\ appr' = [b \in Bots |-> IF appr[b] = "pending" THEN "cancelled" ELSE appr[b]]
                    /\ badRearm' = badRearm
               ELSE /\ acct' = acct
                    /\ appr' = appr
                    /\ badRearm' = badRearm
    /\ UNCHANGED <<bot, health, unhealthy, paused, pos, exitDue, botSeen, askedAcct, askedBot,
                   badEntry>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/drawdown.py:evaluate_drawdown_kills  (the same read for one bot's
\* equity against its own peak; other bots are untouched)
SnapshotBot(k) ==
    /\ \E breach \in BOOLEAN :
         /\ botSeen' = [botSeen EXCEPT ![k] = @ \/ breach]
         /\ IF breach
               THEN /\ bot' = [bot EXCEPT ![k] = "tripped"]
                    /\ appr' = [appr EXCEPT ![k] = IF @ = "pending" THEN "cancelled" ELSE @]
                    /\ badRearm' = badRearm
               ELSE /\ bot' = bot
                    /\ appr' = appr
                    /\ badRearm' = badRearm
    /\ UNCHANGED <<acct, health, unhealthy, paused, pos, exitDue, acctSeen, askedAcct, askedBot,
                   badEntry>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/kill_ops.py:trip_kill  (/kill: the account kill trips and every
\* undecided approval ends in the same transaction)
ManualKill ==
    /\ acct' = "tripped"
    /\ appr' = [b \in Bots |-> IF appr[b] = "pending" THEN "cancelled" ELSE appr[b]]
    /\ UNCHANGED <<bot, health, unhealthy, paused, pos, exitDue, acctSeen, botSeen, askedAcct,
                   askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/kill_ops.py:sync_health  (environment: a health source (the gate's
\* result, the feed) starts or stops failing; both act on the account key, so every bot reads it)
HealthFlip(src) ==
    /\ unhealthy' = [unhealthy EXCEPT ![src] = ~@]
    /\ UNCHANGED <<acct, bot, health, paused, appr, pos, exitDue, acctSeen, botSeen, askedAcct,
                   askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/kill_ops.py:sync_health  (the source's health check: trip its own
\* reason on the account key while failing, clear that reason by itself once healthy)
HealthEval(src) ==
    /\ health[src] # IF unhealthy[src] THEN "tripped" ELSE "armed"
    /\ health' = [t \in Sources |->
          IF t = src
             THEN IF unhealthy[src] THEN "tripped" ELSE "armed"
          ELSE health[t]]
    /\ badClear' = (badClear \/ \E t \in Sources :
                       health[t] = "tripped" /\ unhealthy[t] /\ health'[t] = "armed")
    /\ UNCHANGED <<acct, bot, unhealthy, paused, appr, pos, exitDue, acctSeen, botSeen,
                   askedAcct, askedBot, badEntry, badRearm, askedFlat, badFlat, flatOpen>>

\* code: backend/app/hat/risk/bots.py:pause_bot
Pause(k) ==
    /\ ~paused[k]
    /\ paused' = [paused EXCEPT ![k] = TRUE]
    /\ UNCHANGED <<acct, bot, health, unhealthy, appr, pos, exitDue, acctSeen, botSeen,
                   askedAcct, askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/bots.py:resume_bot  (lifts the pause only, never a kill)
Resume(k) ==
    /\ paused[k]
    /\ paused' = [paused EXCEPT ![k] = FALSE]
    /\ bot' = bot
    /\ badRearm' = badRearm
    /\ UNCHANGED <<acct, health, unhealthy, appr, pos, exitDue, acctSeen, botSeen, askedAcct,
                   askedBot, badEntry>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/decision.py:decide  (the entry gate runs when an approval is
\* created for a signal)
CreateApproval(k) ==
    /\ appr[k] = "none" /\ pos[k] = "none"
    /\ EntryAllowed(k)
    /\ appr' = [appr EXCEPT ![k] = "pending"]
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, pos, exitDue, acctSeen, botSeen,
                   askedAcct, askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/decision.py:decide  (a Take re-runs the gate; a refused take
\* leaves the approval pending until it expires)
Take(k) ==
    /\ appr[k] = "pending"
    /\ EntryAllowed(k)
    /\ appr' = [appr EXCEPT ![k] = "taken"]
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, pos, exitDue, acctSeen, botSeen,
                   askedAcct, askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/kill_ops.py:cancel_pending_approvals  (an undecided approval
\* ends as expired)
Expire(k) ==
    /\ appr[k] = "pending"
    /\ appr' = [appr EXCEPT ![k] = "cancelled"]
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, pos, exitDue, acctSeen, botSeen,
                   askedAcct, askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/decision.py:decide  (the entry is placed only after an ACCEPT;
\* the ghost records every reason that was in force at that moment)
PlaceEntry(k) ==
    /\ pos[k] = "none"
    /\ appr[k] \in {"none", "taken"}
    /\ EntryAllowed(k)
    /\ pos' = [pos EXCEPT ![k] = "open"]
    /\ appr' = [appr EXCEPT ![k] = "none"]
    /\ badEntry' = badEntry \cup Blocking(k)
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, exitDue, acctSeen, botSeen, askedAcct,
                   askedBot, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/decision.py:decide  (an exit rule fires for an open position)
ExitDue(k) ==
    /\ pos[k] = "open" /\ ~exitDue[k]
    /\ exitDue' = [exitDue EXCEPT ![k] = TRUE]
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, appr, pos, acctSeen, botSeen,
                   askedAcct, askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/decision.py:decide  (an exit is always accepted, whatever the
\* kill, pause or cap state)
ExitSend(k) ==
    /\ pos[k] = "open" /\ exitDue[k]
    /\ pos' = [pos EXCEPT ![k] = "closed"]
    /\ exitDue' = [exitDue EXCEPT ![k] = FALSE]
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, appr, acctSeen, botSeen, askedAcct,
                   askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/kill_ops.py:rearm_kill  (the first step: the user asks to re-arm)
RequestRearmAcct ==
    /\ acct = "tripped" /\ ~askedAcct
    /\ askedAcct' = TRUE
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, appr, pos, exitDue, acctSeen, botSeen,
                   askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/kill_ops.py:rearm_kill  (the second, confirmed step re-arms and
\* writes kill.rearmed)
ConfirmRearmAcct ==
    /\ acct = "tripped" /\ askedAcct
    /\ acct' = "armed" /\ askedAcct' = FALSE /\ acctSeen' = FALSE
    /\ UNCHANGED <<bot, health, unhealthy, paused, appr, pos, exitDue, botSeen, askedBot,
                   badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/kill_ops.py:rearm_kill  (the first step for a bot kill)
RequestRearmBot(k) ==
    /\ bot[k] = "tripped" /\ ~askedBot[k]
    /\ askedBot' = [askedBot EXCEPT ![k] = TRUE]
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, appr, pos, exitDue, acctSeen, botSeen,
                   askedAcct, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/kill_ops.py:rearm_kill  (the confirmed step for a bot kill)
ConfirmRearmBot(k) ==
    /\ bot[k] = "tripped" /\ askedBot[k]
    /\ bot' = [bot EXCEPT ![k] = "armed"]
    /\ askedBot' = [askedBot EXCEPT ![k] = FALSE]
    /\ botSeen' = [botSeen EXCEPT ![k] = FALSE]
    /\ UNCHANGED <<acct, health, unhealthy, paused, appr, pos, exitDue, acctSeen, askedAcct,
                   badEntry, badRearm>>

    /\ UNCHANGED flatVars

\* code: backend/app/hat/telegram/commands.py:_flatten  (the first step: /flatten only asks)
RequestFlatten ==
    /\ ~askedFlat
    /\ askedFlat' = TRUE
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, appr, pos, exitDue, acctSeen, botSeen,
                   askedAcct, askedBot, badEntry, badRearm, badFlat, flatOpen, badClear>>

\* code: backend/app/hat/telegram/callbacks.py:_flatten  (the confirmed step: the account kill
\* trips first and every undecided approval ends, as ManualKill),
\* backend/app/hat/execution/manage.py:flatten  (then every open position is sold through the
\* exit path, never gated by a kill or pause, and nothing is bought)
ConfirmFlatten ==
    /\ askedFlat
    /\ badFlat' = (badFlat \/ ~askedFlat)
    /\ askedFlat' = FALSE
    /\ acct' = "tripped"
    /\ appr' = [b \in Bots |-> IF appr[b] = "pending" THEN "cancelled" ELSE appr[b]]
    /\ pos' = [k \in Bots |->
                 IF pos[k] = "open" THEN "closed" ELSE pos[k]]
    /\ flatOpen' = (flatOpen \/ \E k \in Bots : pos[k] # "open" /\ pos'[k] = "open")
    /\ exitDue' = [k \in Bots |-> FALSE]
    /\ UNCHANGED <<bot, health, unhealthy, paused, acctSeen, botSeen, askedAcct,
                   askedBot, badEntry, badRearm, badClear>>

Next ==
    \/ SnapshotAccount
    \/ ManualKill
    \/ RequestRearmAcct
    \/ ConfirmRearmAcct
    \/ RequestFlatten
    \/ ConfirmFlatten
    \/ \E src \in Sources : HealthFlip(src) \/ HealthEval(src)
    \/ \E k \in Bots :
          SnapshotBot(k) \/ Pause(k) \/ Resume(k)
          \/ CreateApproval(k) \/ Take(k) \/ Expire(k) \/ PlaceEntry(k) \/ ExitDue(k)
          \/ ExitSend(k) \/ RequestRearmBot(k) \/ ConfirmRearmBot(k)

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ \A k \in Bots : WF_vars(ExitSend(k))
    /\ \A src \in Sources : WF_vars(HealthEval(src))

TypeOK ==
    /\ acct \in {"armed", "tripped"}
    /\ bot \in [Bots -> {"armed", "tripped"}]
    /\ health \in [Sources -> {"armed", "tripped"}]
    /\ unhealthy \in [Sources -> BOOLEAN]
    /\ paused \in [Bots -> BOOLEAN]
    /\ appr \in [Bots -> {"none", "pending", "taken", "cancelled"}]
    /\ pos \in [Bots -> {"none", "open", "closed"}]
    /\ badEntry \subseteq Reasons
    /\ askedFlat \in BOOLEAN

\* A drawdown breach read since the last re-arm leaves the kill tripped (account and bot).
TripOnDrawdown ==
    /\ acctSeen => acct = "tripped"
    /\ \A k \in Bots : botSeen[k] => bot[k] = "tripped"

\* No entry is ever placed while the account or the bot's own kill is tripped.
TripImpliesManageOnly == "acct" \notin badEntry /\ "bot" \notin badEntry

\* A tripped kill leaves no undecided approval (a decided one, taken, still has to pass the
\* gate at placement, which TripImpliesManageOnly covers).
KillCancelsApprovals ==
    /\ acct = "tripped" => \A k \in Bots : appr[k] # "pending"
    /\ \A k \in Bots : bot[k] = "tripped" => appr[k] # "pending"

\* No entry is placed while a data or broker health input is failing or its kill is tripped.
DataHealthManageOnly == "health" \notin badEntry /\ "unhealthy" \notin badEntry

\* A source's health trip is cleared only by that source's own check, once it is healthy: the
\* feed recovering never clears a failed quality gate (HAT-DATA-065, HAT-RISK-063).
HealthClearsOwnSource == ~badClear

\* A drawdown or manual kill is re-armed only after the confirmed second step.
NoAutoRearm == ~badRearm

\* A paused bot places no entry.
PauseBlocksEntriesOnly == "paused" \notin badEntry

\* A flatten runs only after the confirmed second step (HAT-GOV-082).
FlattenNeedsConfirm == ~badFlat

\* A flatten only closes positions: it never opens or grows one (HAT-TG-035).
FlattenExitOnly == ~flatOpen

\* A health kill clears by itself: once the checks are healthy, it is armed or fails again.
HealthAutoRearm ==
    \A src \in Sources :
        [](health[src] = "tripped" /\ ~unhealthy[src] => <>(health[src] = "armed" \/ unhealthy[src]))

\* An exit that is due for an open position is eventually sent, whatever the kill state.
ExitsContinue ==
    \A k \in Bots : [](pos[k] = "open" /\ exitDue[k] => <>(pos[k] = "closed"))
==============================================================================

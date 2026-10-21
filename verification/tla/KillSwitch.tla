------------------------------ MODULE KillSwitch ------------------------------
(***************************************************************************)
(* Kill switches, pause and manage-only (06 §6, §8; 03 §3).                *)
(*                                                                         *)
(* One account and NBots bots. A bot is manage-only (takes no new entry)   *)
(* when the account kill is tripped, its own kill is tripped, its health   *)
(* kill is tripped, it is paused, or a live health input is failing.       *)
(* Exits are never gated. Drawdown and manual kills re-arm only through a  *)
(* second, confirmed step; the health kill clears by itself. /flatten only *)
(* asks; the confirmed flatten sells every open position through the exit  *)
(* path (purpose flatten), whatever the kill or pause state (PR 16).       *)
(*                                                                         *)
(* Bounds: NBots = 2 bots; each bot has one position cycle (none, open,    *)
(*   closed) and one approval in flight; kills, health flips, pauses and   *)
(*   re-arms repeat without bound (the state space is finite because every *)
(*   variable has a finite domain); BrokenMode names a deliberately broken *)
(*   variant ("none" is the real protocol).                                *)
(* Assumptions: each action below is one database transaction (atomic) or  *)
(*   one environment change; an equity snapshot reads the drawdown state   *)
(*   and acts on it in the same transaction (the snapshot.bots job); the   *)
(*   entry gate reads the live health inputs and the stored switches in    *)
(*   one step; an exit that is due and enabled is eventually sent, and a   *)
(*   health check that can act eventually does (weak fairness); the        *)
(*   environment (equity, health, due exits) is otherwise arbitrary.       *)
(*                                                                         *)
(* Properties: TripOnDrawdown, TripImpliesManageOnly, KillCancelsApprovals,*)
(*   DataHealthManageOnly, NoAutoRearm, PauseBlocksEntriesOnly (invariants)*)
(*   FlattenNeedsConfirm, FlattenExitOnly (invariants) and HealthAutoRearm,*)
(*   ExitsContinue (temporal).                                             *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS NBots, BrokenMode

ASSUME BrokenMode \in {"none", "no_trip_on_drawdown", "entry_ignores_kill",
                       "kill_keeps_approvals", "entry_ignores_live_health",
                       "auto_rearm_on_recovery", "resume_clears_kill",
                       "entry_ignores_pause", "health_manual_rearm",
                       "exit_blocked_when_killed", "flatten_without_confirm",
                       "flatten_opens"}

Bots == 1..NBots
Reasons == {"acct", "bot", "health", "unhealthy", "paused"}

VARIABLES
    acct,        \* account kill: armed | tripped
    bot,         \* per bot kill: armed | tripped
    health,      \* per bot stored health kill: armed | tripped
    unhealthy,   \* per bot: a data or broker health input is failing now
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
    flatOpen     \* ghost: a flatten opened or grew a position

flatVars == <<askedFlat, badFlat, flatOpen>>
vars == <<acct, bot, health, unhealthy, paused, appr, pos, exitDue, acctSeen, botSeen,
          askedAcct, askedBot, badEntry, badRearm, askedFlat, badFlat, flatOpen>>

Init ==
    /\ acct = "armed"
    /\ bot = [b \in Bots |-> "armed"]
    /\ health = [b \in Bots |-> "armed"]
    /\ unhealthy = [b \in Bots |-> FALSE]
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

\* What blocks a new entry for bot b right now.
Blocking(b) ==
    {r \in Reasons :
        \/ r = "acct" /\ acct = "tripped"
        \/ r = "bot" /\ bot[b] = "tripped"
        \/ r = "health" /\ health[b] = "tripped"
        \/ r = "unhealthy" /\ unhealthy[b]
        \/ r = "paused" /\ paused[b]}

\* The entry gate (evaluate_entry): the real protocol allows an entry only when nothing blocks.
\* Each broken variant leaves one reason out of the gate.
Gated(b) ==
    Blocking(b) \ (IF BrokenMode = "entry_ignores_kill" THEN {"acct", "bot"} ELSE {}
                   \cup IF BrokenMode = "entry_ignores_live_health" THEN {"unhealthy"} ELSE {}
                   \cup IF BrokenMode = "entry_ignores_pause" THEN {"paused"} ELSE {})

EntryAllowed(b) == Gated(b) = {}

\* code: backend/app/hat/risk/controls.py:evaluate_drawdown_kills  (snapshot.bots reads the
\* account equity against its peak; a breach trips the account kill and ends pending approvals)
SnapshotAccount ==
    /\ \E breach \in BOOLEAN :
         /\ acctSeen' = (acctSeen \/ breach)
         /\ IF breach /\ BrokenMode # "no_trip_on_drawdown"
               THEN /\ acct' = "tripped"
                    /\ appr' = IF BrokenMode = "kill_keeps_approvals" THEN appr
                               ELSE [b \in Bots |-> IF appr[b] = "pending" THEN "cancelled" ELSE appr[b]]
                    /\ badRearm' = badRearm
               ELSE IF ~breach /\ acct = "tripped" /\ BrokenMode = "auto_rearm_on_recovery"
                       THEN /\ acct' = "armed"
                            /\ appr' = appr
                            /\ badRearm' = TRUE
                       ELSE /\ acct' = acct
                            /\ appr' = appr
                            /\ badRearm' = badRearm
    /\ UNCHANGED <<bot, health, unhealthy, paused, pos, exitDue, botSeen, askedAcct, askedBot,
                   badEntry>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:evaluate_drawdown_kills  (the same read for one bot's
\* equity against its own peak; other bots are untouched)
SnapshotBot(k) ==
    /\ \E breach \in BOOLEAN :
         /\ botSeen' = [botSeen EXCEPT ![k] = @ \/ breach]
         /\ IF breach /\ BrokenMode # "no_trip_on_drawdown"
               THEN /\ bot' = [bot EXCEPT ![k] = "tripped"]
                    /\ appr' = IF BrokenMode = "kill_keeps_approvals" THEN appr
                               ELSE [appr EXCEPT ![k] = IF @ = "pending" THEN "cancelled" ELSE @]
                    /\ badRearm' = badRearm
               ELSE /\ bot' = bot
                    /\ appr' = appr
                    /\ badRearm' = badRearm
    /\ UNCHANGED <<acct, health, unhealthy, paused, pos, exitDue, acctSeen, askedAcct, askedBot,
                   badEntry>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:trip_kill  (/kill: the account kill trips and every
\* undecided approval ends in the same transaction)
ManualKill ==
    /\ acct' = "tripped"
    /\ appr' = IF BrokenMode = "kill_keeps_approvals" THEN appr
               ELSE [b \in Bots |-> IF appr[b] = "pending" THEN "cancelled" ELSE appr[b]]
    /\ UNCHANGED <<bot, health, unhealthy, paused, pos, exitDue, acctSeen, botSeen, askedAcct,
                   askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:sync_health  (environment: a data or broker health
\* input starts or stops failing)
HealthFlip(k) ==
    /\ unhealthy' = [unhealthy EXCEPT ![k] = ~@]
    /\ UNCHANGED <<acct, bot, health, paused, appr, pos, exitDue, acctSeen, botSeen, askedAcct,
                   askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:sync_health  (the health check job: trip while
\* failing, clear by itself once healthy)
HealthEval(k) ==
    /\ health[k] # IF unhealthy[k] THEN "tripped" ELSE "armed"
    /\ health' = [health EXCEPT ![k] =
          IF unhealthy[k] THEN "tripped"
          ELSE IF BrokenMode = "health_manual_rearm" THEN @ ELSE "armed"]
    /\ ~(BrokenMode = "health_manual_rearm" /\ ~unhealthy[k])
    /\ UNCHANGED <<acct, bot, unhealthy, paused, appr, pos, exitDue, acctSeen, botSeen,
                   askedAcct, askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:pause_bot
Pause(k) ==
    /\ ~paused[k]
    /\ paused' = [paused EXCEPT ![k] = TRUE]
    /\ UNCHANGED <<acct, bot, health, unhealthy, appr, pos, exitDue, acctSeen, botSeen,
                   askedAcct, askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:resume_bot  (lifts the pause only, never a kill)
Resume(k) ==
    /\ paused[k]
    /\ paused' = [paused EXCEPT ![k] = FALSE]
    /\ IF BrokenMode = "resume_clears_kill"
          THEN /\ bot' = [bot EXCEPT ![k] = "armed"]
               /\ badRearm' = (badRearm \/ (bot[k] = "tripped"))
          ELSE /\ bot' = bot
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

\* code: backend/app/hat/risk/controls.py:cancel_pending_approvals  (an undecided approval
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
\* kill, pause or cap state; the broken variant gates it like an entry)
ExitSend(k) ==
    /\ pos[k] = "open" /\ exitDue[k]
    /\ BrokenMode # "exit_blocked_when_killed" \/ Blocking(k) = {}
    /\ pos' = [pos EXCEPT ![k] = "closed"]
    /\ exitDue' = [exitDue EXCEPT ![k] = FALSE]
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, appr, acctSeen, botSeen, askedAcct,
                   askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:rearm_kill  (the first step: the user asks to re-arm)
RequestRearmAcct ==
    /\ acct = "tripped" /\ ~askedAcct
    /\ askedAcct' = TRUE
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, appr, pos, exitDue, acctSeen, botSeen,
                   askedBot, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:rearm_kill  (the second, confirmed step re-arms and
\* writes kill.rearmed)
ConfirmRearmAcct ==
    /\ acct = "tripped" /\ askedAcct
    /\ acct' = "armed" /\ askedAcct' = FALSE /\ acctSeen' = FALSE
    /\ UNCHANGED <<bot, health, unhealthy, paused, appr, pos, exitDue, botSeen, askedBot,
                   badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:rearm_kill  (the first step for a bot kill)
RequestRearmBot(k) ==
    /\ bot[k] = "tripped" /\ ~askedBot[k]
    /\ askedBot' = [askedBot EXCEPT ![k] = TRUE]
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, appr, pos, exitDue, acctSeen, botSeen,
                   askedAcct, badEntry, badRearm>>
    /\ UNCHANGED flatVars

\* code: backend/app/hat/risk/controls.py:rearm_kill  (the confirmed step for a bot kill)
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
                   askedAcct, askedBot, badEntry, badRearm, badFlat, flatOpen>>

\* code: backend/app/hat/execution/manage.py:flatten  (the confirmed step: every open position
\* is sold through the exit path, never gated by a kill or pause, and nothing is bought)
ConfirmFlatten ==
    /\ askedFlat \/ BrokenMode = "flatten_without_confirm"
    /\ badFlat' = (badFlat \/ ~askedFlat)
    /\ askedFlat' = FALSE
    /\ pos' = [k \in Bots |->
                 IF pos[k] = "open" THEN "closed"
                 ELSE IF pos[k] = "none" /\ BrokenMode = "flatten_opens" THEN "open"
                 ELSE pos[k]]
    /\ flatOpen' = (flatOpen \/ \E k \in Bots : pos[k] # "open" /\ pos'[k] = "open")
    /\ exitDue' = [k \in Bots |-> FALSE]
    /\ UNCHANGED <<acct, bot, health, unhealthy, paused, appr, acctSeen, botSeen, askedAcct,
                   askedBot, badEntry, badRearm>>

Next ==
    \/ SnapshotAccount
    \/ ManualKill
    \/ RequestRearmAcct
    \/ ConfirmRearmAcct
    \/ RequestFlatten
    \/ ConfirmFlatten
    \/ \E k \in Bots :
          SnapshotBot(k) \/ HealthFlip(k) \/ HealthEval(k) \/ Pause(k) \/ Resume(k)
          \/ CreateApproval(k) \/ Take(k) \/ Expire(k) \/ PlaceEntry(k) \/ ExitDue(k)
          \/ ExitSend(k) \/ RequestRearmBot(k) \/ ConfirmRearmBot(k)

Spec ==
    /\ Init
    /\ [][Next]_vars
    /\ \A k \in Bots : WF_vars(ExitSend(k)) /\ WF_vars(HealthEval(k))

TypeOK ==
    /\ acct \in {"armed", "tripped"}
    /\ bot \in [Bots -> {"armed", "tripped"}]
    /\ health \in [Bots -> {"armed", "tripped"}]
    /\ unhealthy \in [Bots -> BOOLEAN]
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
    \A k \in Bots :
        [](health[k] = "tripped" /\ ~unhealthy[k] => <>(health[k] = "armed" \/ unhealthy[k]))

\* An exit that is due for an open position is eventually sent, whatever the kill state.
ExitsContinue ==
    \A k \in Bots : [](pos[k] = "open" /\ exitDue[k] => <>(pos[k] = "closed"))
==============================================================================

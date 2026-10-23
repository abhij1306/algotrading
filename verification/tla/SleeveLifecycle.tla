------------------------------ MODULE SleeveLifecycle ------------------------------
(***************************************************************************)
(* The sleeve lifecycle and its guards (HAT-PORT-060..064, 034, 040,*)
(* 063; HAT-SCH-083).                                                      *)
(*                                                                         *)
(* One sleeve. State: research, backtested, shadow, live, probation,       *)
(* retired. The engine moves it only along the allowed edges, each guarded *)
(* by evidence, each with its `sleeve.lifecycle_changed` event in the same *)
(* transaction. The regime changes every night and never touches the       *)
(* state. The nightly `allocate` step moves a live sleeve whose health is  *)
(* 0 (or whose expectancy trigger fired) into Probation before it writes   *)
(* the weights.                                                            *)
(*                                                                         *)
(* Bounds: one sleeve; ShadowNeeded = 2 shadow sessions and ProbNeeded = 2 *)
(*   probation periods stand in for the real 20 trading days and 3 months  *)
(*   or 50 trades (the real thresholds are proved by PBT lifecycle.        *)
(*   shadow_days and lifecycle.probation_trigger; the model proves what    *)
(*   follows from reaching them); MaxEvents = 8 lifecycle events (a full   *)
(*   cycle research to retired and back is 6); 4 regime states;            *)
(*   BrokenMode names a deliberately broken variant ("none" is the real    *)
(*   rule).                                                                *)
(* Assumptions: each action is one database transaction (the transition    *)
(*   and its event commit together, HAT-SCH-062); the backtest and         *)
(*   probation evidence, the health multiplier and the regime are          *)
(*   arbitrary environment inputs; the owner promotes a probation sleeve   *)
(*   by an explicit step, the engine takes every other edge.               *)
(*                                                                         *)
(* Properties (invariants): AllowedTransitions, NoLiveWithoutEvidence,     *)
(*   RegimeNeverChangesLifecycle, ProbationOnHealthZero.                   *)
(***************************************************************************)
EXTENDS Naturals, FiniteSets

CONSTANTS ShadowNeeded, ProbNeeded, MaxEvents, BrokenMode

ASSUME BrokenMode \in {"none", "regime_retires", "skip_shadow", "no_dsr_guard",
                       "short_shadow", "early_retire", "retired_to_live",
                       "promote_while_failing", "auto_promote", "no_event",
                       "no_probation_move"}

LStates == {"research", "backtested", "shadow", "live", "probation", "retired"}
Regimes == {"bull_quiet", "bull_volatile", "correction", "bear"}
Edges == {<<"research", "backtested">>, <<"backtested", "shadow">>, <<"shadow", "live">>,
          <<"live", "probation">>, <<"probation", "live">>, <<"probation", "retired">>,
          <<"retired", "research">>}

VARIABLES
    state,        \* hat_bot.lifecycle_state
    visited,      \* states entered since the sleeve last entered research
    dsrOk,        \* the backtest gate holds (DSR >= 0.95 and >= 200 trades)
    shadowDays,   \* trading sessions spent in shadow (capped at ShadowNeeded)
    healthZero,   \* the health multiplier is 0 (drawdown at the second band)
    ciFail,       \* the expectancy trigger fired (n >= 50, CI upper bound < 0)
    failing,      \* the probation condition still holds
    probAge,      \* probation periods elapsed (capped at ProbNeeded)
    regime,       \* the confirmed regime state
    events,       \* lifecycle events written so far
    weight,       \* the final weight of the last allocation: 0 or 1 (positive)
    allocFresh,   \* the last step was an allocation: weight is current
    pre,          \* the evidence and state before the last step
    lastAct,      \* what the last step was
    lastBy,       \* who took the last transition: engine | owner
    lastEvent     \* the (from, to) of the last lifecycle event

vars == <<state, visited, dsrOk, shadowDays, healthZero, ciFail, failing, probAge, regime,
          events, weight, allocFresh, pre, lastAct, lastBy, lastEvent>>

NoPre == [state |-> "none", dsrOk |-> FALSE, shadowDays |-> 0, healthZero |-> FALSE,
          ciFail |-> FALSE, failing |-> FALSE, probAge |-> 0, events |-> 0]

Snap == [state |-> state, dsrOk |-> dsrOk, shadowDays |-> shadowDays,
         healthZero |-> healthZero, ciFail |-> ciFail, failing |-> failing,
         probAge |-> probAge, events |-> events]

Init ==
    /\ state = "research"
    /\ visited = {"research"}
    /\ dsrOk = FALSE
    /\ shadowDays = 0
    /\ healthZero = FALSE
    /\ ciFail = FALSE
    /\ failing = TRUE
    /\ probAge = 0
    /\ regime \in Regimes
    /\ events = 0
    /\ weight = 0
    /\ allocFresh = FALSE
    /\ pre = NoPre
    /\ lastAct = "init"
    /\ lastBy = "engine"
    /\ lastEvent = <<"none", "none">>

\* One lifecycle change and its event, in one transaction.
Move(to, by) ==
    /\ events < MaxEvents
    /\ state' = to
    /\ visited' = IF to = "research" THEN {"research"} ELSE visited \cup {to}
    /\ shadowDays' = IF to = "shadow" THEN 0 ELSE shadowDays
    /\ probAge' = IF to = "probation" THEN 0 ELSE probAge
    /\ dsrOk' = IF to = "research" THEN FALSE ELSE dsrOk
    /\ events' = IF BrokenMode = "no_event" THEN events ELSE events + 1
    /\ lastEvent' = IF BrokenMode = "no_event" THEN lastEvent ELSE <<state, to>>
    /\ pre' = Snap
    /\ lastBy' = by
    /\ lastAct' = "Transition"
    /\ allocFresh' = FALSE
    /\ UNCHANGED <<healthZero, ciFail, failing, regime, weight>>

\* code: backend/app/hat/engine/portfolio/service.py:transition  (guard: lifecycle.backtest_gate)
ToBacktested ==
    /\ state = "research"
    /\ dsrOk \/ BrokenMode = "no_dsr_guard"
    /\ Move("backtested", "engine")

\* code: backend/app/hat/engine/portfolio/service.py:transition  (no condition beyond the edge)
ToShadow ==
    /\ state = "backtested"
    /\ Move("shadow", "engine")

\* code: backend/app/hat/engine/portfolio/service.py:transition  (guard: lifecycle.shadow_gate)
ToLive ==
    /\ state = "shadow"
    /\ shadowDays >= ShadowNeeded \/ BrokenMode = "short_shadow"
    /\ Move("live", "engine")

\* code: backend/app/hat/engine/portfolio/service.py:transition  (broken variant "skip_shadow"
\* only: the real code has no edge from backtested to live)
SkipShadow ==
    /\ BrokenMode = "skip_shadow"
    /\ state = "backtested"
    /\ Move("live", "engine")

\* code: backend/app/hat/engine/portfolio/service.py:transition  (the owner's promotion; guard:
\* lifecycle.still_failing is false)
Promote ==
    /\ state = "probation"
    /\ ~failing \/ BrokenMode = "promote_while_failing"
    /\ Move("live", IF BrokenMode = "auto_promote" THEN "engine" ELSE "owner")

\* code: backend/app/hat/engine/portfolio/service.py:advance_automatic  (guard:
\* lifecycle.retire_gate)
Retire ==
    /\ state = "probation"
    /\ failing
    /\ probAge >= ProbNeeded \/ BrokenMode = "early_retire"
    /\ Move("retired", "engine")

\* code: backend/app/hat/engine/portfolio/service.py:transition  (the only edge out of retired)
Reenter ==
    /\ state = "retired"
    /\ Move("research", "engine")

\* code: backend/app/hat/engine/portfolio/service.py:transition  (broken variant
\* "retired_to_live" only)
RetiredToLive ==
    /\ BrokenMode = "retired_to_live"
    /\ state = "retired"
    /\ Move("live", "engine")

\* code: backend/app/hat/engine/portfolio/service.py:run_allocation  (advance_automatic moves a
\* live sleeve whose health is 0 or whose expectancy trigger fired into probation, then the
\* weights are written: 0 for any state but live)
Allocate ==
    /\ events < MaxEvents
    /\ LET moves == /\ state = "live"
                    /\ (healthZero \/ ciFail)
                    /\ BrokenMode # "no_probation_move"
           new == IF moves THEN "probation" ELSE state
       IN /\ state' = new
          /\ visited' = IF moves THEN visited \cup {"probation"} ELSE visited
          /\ probAge' = IF moves THEN 0 ELSE probAge
          /\ events' = IF moves THEN events + 1 ELSE events
          /\ lastEvent' = IF moves THEN <<"live", "probation">> ELSE lastEvent
          /\ weight' = IF new = "live" THEN 1 ELSE 0
    /\ pre' = Snap
    /\ lastBy' = "engine"
    /\ lastAct' = "Allocate"
    /\ allocFresh' = TRUE
    /\ UNCHANGED <<dsrOk, shadowDays, healthZero, ciFail, failing, regime>>

\* code: backend/app/hat/engine/portfolio/regime.py:confirm_step  (the regime job changes the
\* confirmed regime state; the lifecycle state is not an input of it)
RegimeChange ==
    /\ \E g \in Regimes : g # regime /\ regime' = g
    /\ state' = IF BrokenMode = "regime_retires" /\ state = "live" THEN "probation" ELSE state
    /\ pre' = Snap
    /\ lastAct' = "Regime"
    /\ allocFresh' = FALSE
    /\ UNCHANGED <<visited, dsrOk, shadowDays, healthZero, ciFail, failing, probAge, events,
                   weight, lastBy, lastEvent>>

\* An environment step: one evidence input changes. The environment actions are the
\* backtest gate, the shadow and probation clocks, the health multiplier, the expectancy
\* trigger and the probation condition.
EnvStep(changed) ==
    /\ pre' = NoPre
    /\ lastAct' = "Env"
    /\ UNCHANGED <<state, visited, regime, events, weight, lastBy, lastEvent>>
    /\ changed

\* code: backend/app/hat/engine/portfolio/service.py:load_evidence  (backtest evidence)
SetDsr ==
    EnvStep(/\ dsrOk' \in BOOLEAN
            /\ allocFresh' = allocFresh
            /\ UNCHANGED <<shadowDays, healthZero, ciFail, failing, probAge>>)

\* code: backend/app/hat/engine/portfolio/service.py:trading_sessions  (a shadow session passes)
TickShadow ==
    /\ state = "shadow"
    /\ shadowDays < ShadowNeeded
    /\ EnvStep(/\ shadowDays' = shadowDays + 1
               /\ allocFresh' = allocFresh
               /\ UNCHANGED <<dsrOk, healthZero, ciFail, failing, probAge>>)

\* code: backend/app/hat/engine/portfolio/lifecycle.py:retire_gate  (a probation period passes)
TickProbation ==
    /\ state = "probation"
    /\ probAge < ProbNeeded
    /\ EnvStep(/\ probAge' = probAge + 1
               /\ allocFresh' = allocFresh
               /\ UNCHANGED <<dsrOk, shadowDays, healthZero, ciFail, failing>>)

\* code: backend/app/hat/engine/portfolio/health.py:health_of  (the drawdown crosses a band)
SetHealth ==
    EnvStep(/\ healthZero' \in BOOLEAN
            /\ allocFresh' = FALSE
            /\ UNCHANGED <<dsrOk, shadowDays, ciFail, failing, probAge>>)

\* code: backend/app/hat/engine/portfolio/lifecycle.py:expectancy_trigger  (live results move)
SetCi ==
    EnvStep(/\ ciFail' \in BOOLEAN
            /\ allocFresh' = FALSE
            /\ UNCHANGED <<dsrOk, shadowDays, healthZero, failing, probAge>>)

\* code: backend/app/hat/engine/portfolio/lifecycle.py:still_failing  (shadow results move)
SetFailing ==
    EnvStep(/\ failing' \in BOOLEAN
            /\ allocFresh' = allocFresh
            /\ UNCHANGED <<dsrOk, shadowDays, healthZero, ciFail, probAge>>)

Next ==
    \/ ToBacktested \/ ToShadow \/ ToLive \/ SkipShadow \/ Promote \/ Retire \/ Reenter
    \/ RetiredToLive \/ Allocate \/ RegimeChange
    \/ SetDsr \/ TickShadow \/ TickProbation \/ SetHealth \/ SetCi \/ SetFailing

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ state \in LStates
    /\ visited \subseteq LStates
    /\ shadowDays \in 0..ShadowNeeded
    /\ probAge \in 0..ProbNeeded
    /\ events \in 0..MaxEvents
    /\ weight \in {0, 1}
    /\ regime \in Regimes

\* Every lifecycle change is an allowed edge whose guard held before it, and comes with exactly
\* one event of the same (from, to) in the same step.
AllowedTransitions ==
    (pre.state # "none" /\ lastAct \in {"Transition", "Allocate"} /\ state # pre.state) =>
        /\ <<pre.state, state>> \in Edges
        /\ pre.state = "research" => pre.dsrOk
        /\ pre.state = "shadow" => pre.shadowDays >= ShadowNeeded
        /\ pre.state = "live" => (pre.healthZero \/ pre.ciFail)
        /\ (pre.state = "probation" /\ state = "live") => (lastBy = "owner" /\ ~pre.failing)
        /\ (pre.state = "probation" /\ state = "retired") =>
               (pre.failing /\ pre.probAge >= ProbNeeded)
        /\ lastEvent = <<pre.state, state>>
        /\ events = pre.events + 1

\* A live sleeve has been through backtested and shadow since it last entered research.
NoLiveWithoutEvidence ==
    state = "live" => {"backtested", "shadow"} \subseteq visited

\* The regime never changes the lifecycle state.
RegimeNeverChangesLifecycle ==
    lastAct = "Regime" => state = pre.state

\* After an allocation, no live sleeve has health 0, and every sleeve out of live has weight 0.
ProbationOnHealthZero ==
    allocFresh =>
        /\ ~(state = "live" /\ healthZero)
        /\ (state # "live" => weight = 0)
==============================================================================

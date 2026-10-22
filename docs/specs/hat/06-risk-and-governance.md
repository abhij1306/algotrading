# 06: Risk and Governance

Scope: the account-level risk policy, position sizing, caps, progressive exposure, the strategy regime gate, kill switches and manage-only, the six invariants, governance levers, risk-policy change rules, and the single risk authority, `evaluate_signal` in `app/hat/risk/service.py`. Requirement format and verification methods: see README.md and 11-verification.md.

Bots hold no risk numbers. Every risk number lives in one versioned risk-policy record (`hat_risk_policy_version.policy`), so a strategy edit can never raise risk (BL "Governance / Risk policy is account-level").

## §1 Risk policy record

The policy is a JSONB document on `hat_risk_policy_version` (schema in 03-schema). Fields and initial seed (version 1, `direction = initial`):

| Field | Meaning | Seed value | Source |
| --- | --- | --- | --- |
| `risk_pct` | Risk per trade as a share of bot equity | 0.0025 (0.25%) | BL "Position size"; B13, B14 |
| `p_max.stage2-contraction-breakout` | Max position share of bot equity, S1 | 0.20 | BL "Position size" (M 20%) |
| `p_max.momentum-breakout-ep` | Max position share of bot equity, S2 | 0.25 | BL "Position size" (Q 25%) |
| `adv_cap_pct` | Max share of 20-day ADV per position | 0.01 [A] | BL "Position size" |
| `global_open_risk_pct` | Sum of open risk across all bots, share of total equity | 0.03 | BL "Caps" |
| `max_open_positions` | Per bot | 6 | BL "Caps"; B21 |
| `max_positions_per_industry` | Per bot | 2 [A] | BL "Caps" |
| `max_entries_per_day` | Per bot | 3 [A] | BL "Caps" |
| `max_stopouts_per_day` | Per bot | 3 [A] | BL "Caps" |
| `exposure_ladder` | L1 2 pos / 0.5x, L2 4 pos / 1.0x, L3 6 pos / 1.0x; up after 2 consecutive winners, down after 2 consecutive losers | see §4 [A] | BL "Progressive exposure"; B16 |
| `kill.account_drawdown_pct` | Account equity below peak that trips the account kill | 0.15 | BL "Kill switches"; B26 |
| `kill.bot_drawdown_pct` | Bot equity below peak that trips the bot kill | 0.10 [A] | BL "Kill switches" |
| `base_weight.S1`, `base_weight.S2` | Each sleeve's base weight b | 0.50, 0.50 [OWNER-PENDING] | BL "Allocation"; PLAN Q3 |
| `cluster_cap` | Combined cap for the S1+S2 risk cluster | 1.00 [OWNER-PENDING] | BL "Sleeves"; PLAN Q3 |
| `multiplier_bounds` | Allowed range of regime and health multipliers | [0.25, 1.0] | BL "Allocation"; PL2 |
| `health_bands` | Sleeve drawdown bands 8% / 12% → 1.0 / 0.5 / 0 | see 05-portfolio [A] | BL "Health multiplier" |

#### HAT-RISK-010: Risk policy holds every risk number
- **Statement:** All sizing, cap, ladder, kill-switch, base-weight and multiplier-bound values are read from the effective `hat_risk_policy_version.policy`; no strategy version or bot row carries a risk number.
- **Source:** BL "Governance / Risk policy is account-level"; SCH "Strategy, risk policy and bots" (`hat_bot`: no risk columns); B15.
- **PR:** 10
- **Verification:** CONF `risk.policy_schema_fields`, CONF `risk.no_risk_keys_in_strategy_schema`, PBT `sizing.uses_effective_policy`
- **Acceptance:**
  - Property: the policy JSON schema contains exactly the fields of the §1 table, and the `hat_bot` table and the strategy-version config schema contain none of them. Harm: a strategy edit could quietly raise risk outside the cooling-off gate.
  - Property: for every pair of policy versions and every signal, the sizing and cap results depend only on the policy version whose `effective_from` is the latest one at or before the signal's `triggered_at` and that is not superseded. Harm: a pending increase would apply before its cooling-off ends.

#### HAT-RISK-011: Initial policy seed
- **Statement:** The initial risk-policy seed (version 1, `direction = initial`) carries exactly the §1 seed values.
- **Source:** BL "Position size", "Caps", "Progressive exposure", "Kill switches"; B14, B16, B21, B26; PLAN Q3.
- **PR:** 10
- **Verification:** CONF `risk.initial_seed_equals_spec`
- **Acceptance:**
  - Property: the seeded policy document equals the §1 seed column field by field, including `risk_pct = 0.0025` and `kill.account_drawdown_pct = 0.15`. Harm: the account starts with risk numbers nobody approved.
- **[A]** `adv_cap_pct` 0.01, industry cap 2, entries/day 3, stop-outs/day 3, ladder 2/4/6, bot kill 10%: defaults as listed; validated by slippage in shadow mode (ADV cap), live review (caps), live after 50 trades (ladder), backtest and shadow (bot kill) per BL "Defaults that are assumptions".
- **[OWNER-PENDING]** S1/S2 base weights 0.50/0.50, cluster cap 1.00, account equity read from broker funds at session start: default per PLAN Q3; the owner must set real numbers before any bot runs in confirm or auto.

## §2 Position size

```
q = floor( min( E·r / (P_entry − P_stop),  E·p_max / P_entry,  adv_cap_pct·ADV20,  C / P_entry ) )
```

E = bot equity (allocation + realised P&L) · r = risk per trade = the sleeve's `hat_allocation_daily.risk_pct` for the day (which already equals policy `risk_pct` × regime_mult × health_mult, HAT-PORT-052; the multipliers are not applied again) × the exposure-ladder risk multiplier · p_max = max position share of the bot (20% M, 25% Q) · ADV20 = 20-day average daily volume · adv_cap_pct = the policy's ADV cap (default 0.01, i.e. 1% [A]; BL writes the default into the formula as 0.01·ADV20) · C = free cash in the broker account. Worked example (prose only): E = 1,000,000, r = 0.25%, entry 100, stop 95, p_max 20%, ADV20 = 1,000,000, C = ∞ gives min(500, 2000, 10000, ∞) = 500, so q = 500.

#### HAT-RISK-020: Position size formula
- **Statement:** The quantity for every entry is q = floor(min(E·r/(P_entry − P_stop), E·p_max/P_entry, adv_cap_pct·ADV20, C/P_entry)), computed in Decimal, with `adv_cap_pct` read from the effective risk policy (default 0.01).
- **Source:** BL "Risk management / Position size"; B13, B14, B15.
- **PR:** 10
- **Verification:** PBT `sizing.formula_exact`, PBT `sizing.risk_bounded`, PBT `sizing.monotone`
- **Acceptance:**
  - Property: for every E > 0, r in (0, 1], P_entry > P_stop > 0, p_max in (0, 1], ADV20 ≥ 0 and C ≥ 0 (or unbounded), the returned q equals floor of the minimum of the four terms computed with Decimal arithmetic. Harm: positions are sized wrong, in either direction.
  - Property: for every input, q·(P_entry − P_stop) ≤ E·r, q·P_entry ≤ E·p_max, q ≤ adv_cap_pct·ADV20 and q·P_entry ≤ C. Harm: a single trade risks or commits more than the policy allows.
  - Property: q never increases when r, p_max, ADV20, C or E decreases, or when the stop distance widens. Harm: tightening a limit could enlarge a position.
  - Property: no float value is used anywhere in the computation (inputs coerced to Decimal; a float input is rejected). Harm: rounding drift makes the cap checks unreliable at the boundary.
- **[A]** ADV cap 1% of 20-day ADV: default 0.01; validated by slippage measured in shadow mode.

#### HAT-RISK-021: Zero quantity skips the trade
- **Statement:** When q = 0 the signal is rejected with a typed reason naming the binding term, and no order (real or virtual) is created.
- **Source:** BL "Position size" ("Quantity 0 → trade skipped with reason").
- **PR:** 10
- **Verification:** PBT `sizing.zero_skips_with_reason`
- **Acceptance:**
  - Property: for every input where q = 0, the decision is REJECT with a reason code identifying the smallest term, and the decision carries no quantity. Harm: a zero or fractional order reaches the router or the broker.
  - Property: for every input where q ≥ 1, the reason code names the binding term (risk, position share, liquidity or cash). Harm: the rejection digest and audits cannot explain sizes.

#### HAT-RISK-022: Stop must be below entry for a long
- **Statement:** Sizing refuses any long signal whose stop is not strictly below its entry price.
- **Source:** BL "Invariants" 3; BL "Position size" (denominator P_entry − P_stop).
- **PR:** 10
- **Verification:** PBT `sizing.stop_below_entry_required`
- **Acceptance:**
  - Property: for every input with P_stop ≥ P_entry or P_stop ≤ 0, the decision is REJECT and no quantity is produced. Harm: division by zero or a negative risk term sizes an unbounded position.

## §3 Caps

| Cap | Default | Scope | On breach |
| --- | --- | --- | --- |
| Global open risk (sum of (entry − stop) × qty) | 3% of total equity | All bots | New entry rejected |
| Max open positions | 6 | Per bot | New entry rejected |
| Max positions per industry | 2 [A] | Per bot | New entry rejected |
| Max new entries per day | 3 [A] | Per bot | Further triggers rejected |
| Stop-outs per day | 3 [A] | Per bot | No more entries that day |

Caps can only shrink or reject an entry; they never apply to exits.

#### HAT-RISK-030: Global open-risk cap
- **Statement:** A new entry is rejected when the sum over all bots' open positions of (current_stop distance × qty) plus the new trade's risk would exceed 3% of total equity. "All bots" is every bot of the owner, on every broker account (the bots whose allocations make up total equity), counting the slots of the entry's own mode (real or virtual): open positions and entries triggered, placed or awaiting approval. The owner's entries are decided one at a time under a Postgres advisory lock keyed by the owner (taken after the bot's lock), so two bots' triggers in one instant cannot both fit on one snapshot.
- **Source:** BL "Caps"; B15.
- **PR:** 10, 19
- **Verification:** PBT `caps.global_open_risk`, PBT `trades.global_open_risk_across_bots`, DB `runtime.owner_serialised`
- **Acceptance:**
  - Property: for every set of open positions and every candidate, the decision is ACCEPT only if Σ(entry − stop)·qty over the open slots of the entry's mode (real or virtual) plus the candidate's (P_entry − P_stop)·q ≤ 0.03 × total equity; otherwise REJECT with reason `global_open_risk`. Harm: correlated sleeves together risk more than the account can absorb.
  - Property: through `open_entry`, two bots of one owner on different broker accounts, whose triggers interleave in any order, are each rejected with `global_open_risk` exactly when the open risk of both plus the candidate's exceeds the cap, and the owner never holds more. Harm: each bot reads only its own positions, so N bots hold N × 3%.
  - Property: two real transactions of different bots of one owner, the first holding between its risk decision and its insert, with room for one entry, end with one accepted and one rejected. Harm: both read the snapshot before the other's slot is visible and together exceed the cap.

#### HAT-RISK-031: Max open positions per bot
- **Statement:** A bot with open positions equal to the lesser of 6 and its exposure-level maximum cannot open another.
- **Source:** BL "Caps", "Progressive exposure"; B21, B16.
- **PR:** 10
- **Verification:** PBT `caps.max_positions`
- **Acceptance:**
  - Property: for every bot state, after any sequence of accepted entries the bot's open position count never exceeds min(6, ladder level max). Harm: concentration beyond the policy.

#### HAT-RISK-032: Per-industry cap
- **Statement:** A bot cannot hold more than 2 [A] open positions in one industry (from `hat_industry`).
- **Source:** BL "Caps"; SCH `hat_industry` ("Feeds the per-industry cap").
- **PR:** 10
- **Verification:** PBT `caps.industry`
- **Acceptance:**
  - Property: for every bot and every industry, the count of open positions in that industry never exceeds the policy value after any accepted entry; an entry that would exceed it is REJECT with reason `industry_cap`. Harm: one sector shock hits several positions at once.
- **[A]** industry cap 2: validated by live review.

#### HAT-RISK-033: Daily entry cap
- **Statement:** A bot accepts at most 3 [A] new entries per trade date; further triggers that day are rejected.
- **Source:** BL "Caps".
- **PR:** 10
- **Verification:** PBT `caps.daily_entries`
- **Acceptance:**
  - Property: for every sequence of triggers within one trade date, the number of ACCEPT decisions for a bot is ≤ `max_entries_per_day`. Harm: over-trading on a noisy day.
- **[A]** entries/day 3: validated by live review.

#### HAT-RISK-034: Daily stop-out cap
- **Statement:** After 3 [A] stop-outs in one trade date, a bot accepts no more entries that day.
- **Source:** BL "Caps".
- **PR:** 10
- **Verification:** PBT `caps.daily_stopouts`
- **Acceptance:**
  - Property: for every sequence of events within a trade date, once the bot's count of positions closed by `hard_stop` reaches `max_stopouts_per_day`, every later entry decision that date is REJECT with reason `stopout_cap`. Harm: the bot keeps buying into a failing tape.
- **[A]** stop-outs/day 3: validated by live review.

#### HAT-RISK-035: Caps never block exits
- **Statement:** No cap, kill state or exposure level can reject an exit, stop move or protective order.
- **Source:** BL "Caps" ("New entry rejected"), "Kill switches" ("stops and exits keep running").
- **PR:** 10
- **Verification:** PBT `caps.exits_never_blocked`
- **Acceptance:**
  - Property: for every risk state, `evaluate_signal` or the exit path returns ACCEPT for every exit and protective-stop action. Harm: a position is left unprotected because a limit was hit.

## §4 Progressive exposure

| Level | Max open positions | Risk multiplier | Move up | Move down |
| --- | --- | --- | --- | --- |
| L1 (start) | 2 | 0.5x | 2 consecutive winners | none |
| L2 | 4 | 1.0x | 2 consecutive winners | 2 consecutive losers |
| L3 | 6 | 1.0x | none | 2 consecutive losers |

State is `hat_exposure_state` (bot_id, level, consecutive_wins, consecutive_losses, updated_at).

#### HAT-RISK-040: Ladder transitions
- **Statement:** Each bot's exposure level starts at L1, moves up one level after 2 consecutive winners and down one level after 2 consecutive losers, bounded at L1 and L3.
- **Source:** BL "Progressive exposure (B16)"; SCH `hat_exposure_state`.
- **PR:** 10
- **Verification:** PBT `exposure.ladder_transitions`
- **Acceptance:**
  - Property: for every sequence of closed-trade outcomes (win/loss), the level after each outcome equals the level given by the table: up after the 2nd consecutive win (except at L3), down after the 2nd consecutive loss (except at L1), and the opposite-outcome counter resets to 0 on every outcome. Harm: exposure grows during a losing streak.
  - Property: the level is always in {L1, L2, L3}. Harm: an undefined level sizes with an undefined multiplier.
- **[A]** ladder shape 2/4/6 positions, 0.5x/1.0x/1.0x, 2 winners/2 losers: validated live, after 50 trades.

#### HAT-RISK-041: Ladder bounds positions and risk
- **Statement:** The current level's max open positions and risk multiplier apply to every new entry.
- **Source:** BL "Progressive exposure (B16)".
- **PR:** 10
- **Verification:** PBT `exposure.level_applies`
- **Acceptance:**
  - Property: for every level and every sizing input, the r used in §2 equals `risk_pct` × allocation multipliers × level risk multiplier, and the open-position limit equals min(6, level max). Harm: a bot at L1 trades at full size.

#### HAT-RISK-042: Shadow trades do not move the ladder
- **Statement:** Only real (non-virtual) closed trades count as winners or losers for the ladder. [OWNER-PENDING]
- **Source:** BL "Progressive exposure" (silent on shadow); SCH `is_virtual`.
- **PR:** 10
- **Verification:** PBT `exposure.virtual_ignored`
- **Acceptance:**
  - Property: for every interleaving of virtual and real trade results, the ladder state equals the state computed from the real results alone. Harm: paper wins unlock real exposure.
- **[OWNER-PENDING]** shadow results excluded from the ladder: the docs are silent; default chosen as the conservative direction; owner to confirm.

## §5 Strategy regime gate

The strategy-level gate (stage 2 of the bot config, see 04) is binary. The portfolio regime (05) only scales weights on top.

#### HAT-RISK-050: Gate off blocks new entries only
- **Statement:** When a bot's regime gate is off, no new entry is accepted; open positions keep their stops and exits.
- **Source:** BL "Regime gate (B11, B12)"; B11.
- **PR:** 10, 11, 15
- **Verification:** PBT `caps.regime_gate_blocks_entries`, TLA `ExecutionRouter!GateOffNoEntry`
- **Acceptance:**
  - Property: for every bot state with gate = off, every entry decision is REJECT with reason `regime_gate`, and every exit or stop action is unaffected. Harm: the bot buys into a market its own rule forbids, or abandons stops.

#### HAT-RISK-051: Gate checked nightly and at 09:20
- **Statement:** The gate is evaluated nightly for the watchlist and again at 09:20 IST before the first trigger is acted on.
- **Source:** BL "Regime gate" ("Checked nightly ... and again at 09:20").
- **PR:** 11, 15
- **Verification:** TLA `ExecutionRouter!GateFreshBeforeFirstEntry`
- **Acceptance:**
  - Property: in every reachable day, no entry is accepted before a gate evaluation stamped at or after 09:20 IST of that trade date exists for the bot. Harm: an entry fires on yesterday's gate after an overnight break.

#### HAT-RISK-052: No regime override
- **Statement:** No mode, command or API can override the regime gate.
- **Source:** BL "Regime gate"; MA "Execution modes"; B12 (revised).
- **PR:** 10, 16
- **Verification:** CONF `risk.no_gate_override_path`, TLA `ExecutionRouter!GateOffNoEntry`
- **Acceptance:**
  - Property: the risk decision function has no parameter, flag or command that bypasses the gate, and in every reachable state of every mode an entry with gate = off is never routed. Harm: discretionary override undoes the regime protection.

## §6 Kill switches and manage-only

| Trigger | Effect | Re-arm |
| --- | --- | --- |
| Account equity ≥ 15% below its peak | All bots manage-only | Manual, via `/arm` in Telegram (second confirmation) |
| Bot equity ≥ 10% below its peak [A] | That bot manage-only | Manual |
| `/kill` in Telegram | All bots manage-only; pending approvals cancelled | Manual |
| Data or broker health check fails | Affected bots manage-only | Automatic when healthy |

Manage-only = no new entries; stops and exits keep running. Drawdown source: `hat_daily_bot_snapshot` (equity, equity_peak). State: `hat_kill_switch` (latest row per scope is current).

#### HAT-RISK-060: Account drawdown kill
- **Statement:** When account equity is ≥ 15% below its peak, the account kill trips and every bot becomes manage-only.
- **Source:** BL "Kill switches (B26)"; B26.
- **PR:** 10
- **Verification:** PBT `caps.kill_thresholds`, TLA `KillSwitch!TripImpliesManageOnly`
- **Acceptance:**
  - Property: for every equity series, the account kill is tripped exactly when (peak − equity)/peak ≥ 0.15 has occurred since the last re-arm. Harm: the account keeps losing past its stop-loss.
  - Property: in every reachable state where the account kill is tripped, every bot is manage-only. Harm: one bot keeps entering after the account limit.

#### HAT-RISK-061: Bot drawdown kill
- **Statement:** When a bot's equity is ≥ 10% [A] below its peak, that bot becomes manage-only.
- **Source:** BL "Kill switches".
- **PR:** 10
- **Verification:** PBT `caps.kill_thresholds`, TLA `KillSwitch!TripImpliesManageOnly`
- **Acceptance:**
  - Property: for every bot equity series, the bot kill is tripped exactly when (equity_peak − equity)/equity_peak ≥ 0.10 has occurred since its last re-arm, and other bots are unaffected by it. Harm: a broken bot keeps trading, or a healthy bot is stopped by another's loss.
- **[A]** bot kill 10%: validated by backtest and shadow drawdown distribution.

#### HAT-RISK-062: Manual kill
- **Statement:** `/kill` makes every bot manage-only and cancels every pending approval.
- **Source:** BL "Kill switches"; DS "Telegram / Commands".
- **PR:** 10, 16
- **Verification:** TLA `KillSwitch!KillCancelsApprovals`, TLA `KillSwitch!TripImpliesManageOnly`
- **Acceptance:**
  - Property: in every reachable state after a `/kill`, no approval is in state pending and no entry order is placed until a re-arm. Harm: an approval taken seconds after the kill still opens a position.

#### HAT-RISK-063: Health kill
- **Statement:** A failed data quality gate, stale feed or lost broker session makes the affected bots manage-only, and the state clears automatically when the check is healthy again.
- **Source:** BL "Kill switches"; MA "Failure handling"; DS "Bar builder rules".
- **PR:** 10, 14, 17
- **Verification:** TLA `KillSwitch!HealthAutoRearm`, TLA `KillSwitch!HealthClearsOwnSource`, PBT `kill.kill_service_matches_model`
- **Acceptance:**
  - Property: in every reachable state, while any health check for a bot is failing that bot accepts no entry, and once all its checks are healthy its health kill is cleared without user action. Harm: entries on bad data, or a bot left disabled after a transient fault.
  - Property: each health check trips and clears only its own reason (`data_health` for the quality gate and ingest, `feed_health` for the live feed, `broker_health`): a check turning healthy never clears another check's trip. Harm: the feed recovering at 09:15 clears a failed gate, and entries run on data the gate rejected.
- **Design (PR 17):** the reasons were one `health` channel, so `DataHealthKill(False)` re-armed the gate's trip. Each health reason is now its own stream of the kill key (`risk/kill.py:stream_of`); the feed writes `feed_health`.

#### HAT-RISK-064: Manual re-arm with second confirm
- **Statement:** Drawdown and manual kills re-arm only through the Telegram command `/arm` with a second confirmation; the dashboard is read-only (HAT-DEC-T3) and nothing re-arms them automatically.
- **Source:** BL "Kill switches"; DS "Telegram / Commands" (`/arm` second confirm).
- **PR:** 10, 16
- **Verification:** TLA `KillSwitch!NoAutoRearm`
- **Acceptance:**
  - Property: in every behaviour, a tripped account, bot or manual kill moves to re-armed only through a `rearm` action preceded by a confirmed second step, and every re-arm writes `kill.rearmed`. Harm: trading resumes after a drawdown without a human decision.

#### HAT-RISK-065: Manage-only keeps protecting positions
- **Statement:** In manage-only, stops and exits keep running for every open position.
- **Source:** BL "Kill switches" ("Manage-only = no new entries; stops and exits keep running"); MA "Runtime".
- **PR:** 10, 15
- **Verification:** TLA `KillSwitch!ExitsContinue`
- **Acceptance:**
  - Property: in every reachable state, an enabled exit or stop action for an open position stays enabled regardless of kill or pause state (liveness: an exit that fires is eventually sent). Harm: the kill switch strands positions without stops.

#### HAT-RISK-066: A planned reallocation is not a drawdown
- **Statement:** Drawdown kills measure losses from trading, not a change of the capital the allocator gave a sleeve: snapshot equity is allocation capital plus realised P&L (§2), so when the allocation changes, the previous peak is restated by the same ratio as the equity (the drawdown share stays what it was), and a snapshot with zero allocated capital is not measured.
- **Source:** 05 §5 (HAT-PORT-050, multipliers scale capital); §2 above (equity = allocation + realised P&L); CodeAnt review of PR 9 (Major).
- **PR:** 9
- **Verification:** PBT `kill.reallocation_is_not_drawdown`
- **Acceptance:**
  - Property: for every sequence of allocation capitals (cuts, rises and nights at zero) with no realised loss, no bot or account kill trips from the snapshots, and no stored peak is above the equity it was restated for. Harm: a regime or health cut of 11% trips the 10% bot kill, which needs a manual re-arm, and benches a sound sleeve.
  - Property: for a cut of capital together with a realised loss, the bot kill trips exactly when the loss is at least the policy's drawdown share of the capital it was measured against. Harm: restating the peak hides a real loss.

## §7 Invariants (not configurable)

1. A stop is never moved further from price.
2. No order adds to a losing position.
3. No entry order without a stop price attached.
4. No order when the day's data or live feed is stale.
5. No order outside market hours.
6. Max-chase always applies, even when a user deletes the criterion.

The invariants live in the risk manager, outside every config, so no edit can remove them (BL "Editing rules").

#### HAT-RISK-070: Invariant 1, stop never widens
- **Statement:** No code path moves a position's stop further from price; for a long, `current_stop` never decreases.
- **Source:** BL "Invariants" 1; BL "Evaluation rules"; SCH `hat_stop_only_tightens`.
- **PR:** 3, 10, 11
- **Verification:** TLA `StopPlacement!StopOnlyTightens`, DB `governance.stop_only_rises`, PBT `exits.never_lowers_stop`
- **Acceptance:**
  - Property: in every reachable state, for every position, `current_stop` is ≥ its value in every earlier state. Harm: a loser is allowed to lose more than its planned risk.
  - Property: for every exit-rule evaluation that would lower the stop, the result leaves the stop unchanged and logs the ignored rule. Harm: as above.

#### HAT-RISK-071: Invariant 2, never add to a loser
- **Statement:** No order increases the quantity of an open position that is below its average entry.
- **Source:** BL "Invariants" 2.
- **PR:** 10
- **Verification:** PBT `caps.no_add_to_loser`
- **Acceptance:**
  - Property: for every open position with last price < avg_entry, every buy decision for the same bot and instrument is REJECT with reason `add_to_loser`. Harm: averaging down multiplies a planned loss.

#### HAT-RISK-072: No adding to winners (B20)
- **Statement:** A bot never opens a second entry in an instrument it already holds.
- **Source:** MA "Decisions register"; B20.
- **PR:** 10
- **Verification:** PBT `caps.no_pyramiding`
- **Acceptance:**
  - Property: for every bot with an open position in an instrument, every new entry decision for that instrument is REJECT with reason `position_exists`. Harm: unplanned pyramiding raises risk past the per-trade budget.

#### HAT-RISK-073: Invariant 3, no entry without a stop
- **Statement:** Every entry order carries a stop price at creation, and no entry is routed without one.
- **Source:** BL "Invariants" 3.
- **PR:** 10, 15
- **Verification:** PBT `caps.entry_requires_stop`, TLA `ExecutionRouter!EntryHasStop`
- **Acceptance:**
  - Property: for every routed entry (real or virtual), `stop_price` is present and 0 < stop_price < entry price. Harm: an unprotected position.

#### HAT-RISK-074: Invariant 4, no order on stale data
- **Statement:** No entry order is placed when the day's quality gate failed or the live feed is stale (> 30 s silence in market hours).
- **Source:** BL "Invariants" 4; DS "Bar builder rules".
- **PR:** 10, 15
- **Verification:** TLA `ExecutionRouter!NoOrderWhenStale`, PBT `caps.stale_blocks_entry`
- **Acceptance:**
  - Property: in every reachable state where `feed_stale` or `quality_failed` holds, no entry order transitions to placed. Harm: orders priced on a frozen tape.

#### HAT-RISK-075: Invariant 5, no order outside market hours
- **Statement:** No order is placed outside the session from `hat_trading_calendar` (09:15–15:30 IST on trading days).
- **Source:** BL "Invariants" 5; MA "Phase progression" (NSE 09:15–15:30 IST); PLAN C7.
- **PR:** 10, 15
- **Verification:** PBT `caps.market_hours_only`, TLA `ExecutionRouter!NoOrderOutsideHours`
- **Acceptance:**
  - Property: for every instant and calendar, an order decision outside the open session (including holidays) is REJECT with reason `market_closed`. Harm: orders queue into an auction or holiday at an unknown price.

#### HAT-RISK-076: Invariant 6, max-chase always applies
- **Statement:** Max-chase is enforced on every entry even when the criterion is disabled or deleted from the config, using the playbook's last configured percentage or, if none was ever configured, the strategy's documented default (Minervini-style 5, Qullamaggie-style 2; HAT-CRIT-122), and 2% for any other strategy. [OWNER-PENDING]
- **Source:** BL "Invariants" 6; BL "Criteria library" (`max_chase`: M 5, Q 2).
- **PR:** 10, 15
- **Verification:** PBT `caps.max_chase_always`
- **Acceptance:**
  - Property: for every config (with, without, or with disabled `max_chase`) and every price, an entry whose price exceeds the trigger level by more than the effective max-chase %, or whose instrument is locked at upper circuit, is REJECT with reason `max_chase`. Harm: chasing a runaway price destroys the planned R.
- **[OWNER-PENDING]** fallback percentage for a strategy with no documented default: 2% (the stricter doc value); owner to confirm.

## §8 Governance levers and strategy release gate

| Lever | Who, when | How |
| --- | --- | --- |
| Take or skip an entry | You, live (confirm mode) | Telegram buttons; price, size and stop fixed by the engine |
| Exits, stop moves, partials | Engine only | No manual exit button for a single position |
| Pause a bot, kill switch | You, any time | Only reduces risk: no new entries; positions keep their stops and exits |
| Emergency flatten-all | You, any time, second confirm | Logged as a discipline breach |
| Execution mode | You, outside market hours | Effective next session |
| Strategy change | You, outside market hours | Draft version → automatic backtest → shadow for 20 trading days [A] → promote |
| Risk policy decrease | You, outside market hours | Effective next session |
| Risk policy increase | You, outside market hours | 7-day cooling-off [A] and evidence gate: ≥ 50 live trades with expectancy within tolerance |
| Invariants | No one | Code change only |

#### HAT-GOV-080: Live levers only reduce risk
- **Statement:** During market hours the user can only take/skip an engine-sized entry, pause a bot, kill, or flatten; none of these can increase size, move a stop, or create an entry.
- **Source:** BL "Governance — who can change what"; MA "Governance".
- **PR:** 10, 16
- **Verification:** CONF `gov.lever_catalogue`, TLA `ExecutionRouter!UserCannotRaiseRisk`
- **Acceptance:**
  - Property: in every reachable state, every user action available during market hours leaves the set of possible new entries, their quantities and their stops equal or smaller. Harm: discretionary edits bypass the risk policy.
  - Property: the command and API catalogue contains no action that sets price, size or stop of a signal, or exits a single position manually. Harm: as above.

#### HAT-GOV-081: Pause and resume
- **Statement:** Pausing a bot makes it manage-only immediately; resuming restores entries; both are logged.
- **Source:** BL "Governance"; DS "Commands" (`/pause`, `/resume`); SCH events `bot.paused`, `bot.resumed`.
- **PR:** 10, 16
- **Verification:** TLA `KillSwitch!PauseBlocksEntriesOnly`
- **Acceptance:**
  - Property: in every reachable state, a paused bot accepts no entry and keeps all exits running, and every pause/resume transition writes its event in the same transaction. Harm: a paused bot trades, or a pause leaves no audit trail.

#### HAT-GOV-082: Flatten-all is a logged discipline breach
- **Statement:** Flatten-all requires a second confirmation, closes every position through exit-only paths, and records `flatten.executed` as a discipline breach.
- **Source:** BL "Governance"; DS "Commands" (`/flatten`); SCH event `flatten.executed`.
- **PR:** 16
- **Verification:** TLA `KillSwitch!FlattenNeedsConfirm`
- **Acceptance:**
  - Property: in every behaviour, a flatten order is sent only after a confirmed second step, and every flatten writes `flatten.executed` with `actor = user` and the chat ID. Harm: an accidental flatten, or breaches missing from adherence metrics.

#### HAT-GOV-083: Strategy changes ship as gated releases
- **Statement:** A strategy change is created only as a draft version outside market hours and reaches live only through backtested → shadow (20 trading days [A]) → live.
- **Source:** BL "Governance"; SCH "Governance enforced in the database"; B5.
- **PR:** 2, 9
- **Verification:** TLA `SleeveLifecycle!NoLiveWithoutEvidence`, DB `governance.live_requires_evidence`
- **Acceptance:**
  - Property: in every reachable state, a strategy version with status live has a backtest run and a shadow start at least 20 trading days earlier. Harm: an untested edit trades real money.
- **[A]** shadow 20 trading days: validated by review after the first two releases.

#### HAT-GOV-084: Mode changes outside hours, effective next session
- **Statement:** Execution-mode changes are accepted only outside market hours and take effect at the next session open from `hat_trading_calendar`.
- **Source:** BL "Governance"; SCH "Mode changes take effect next session"; `hat_mode_change`.
- **PR:** 10
- **Verification:** TLA `ExecutionRouter!ModeChangeNextSession`, PBT `router.mode_change_takes_effect_next_session`
- **Acceptance:**
  - Property: in every behaviour, a bot's effective mode during a session equals the mode in effect at that session's open, and every accepted change has `effective_from` = the next session open. Harm: a mode flips mid-session and routes an entry differently from what was approved.
- **Implementation:** `execution/modes.py` (PR 15): `request_mode_change` refuses a confirm or auto request the account cannot run (HAT-EXEC-012) and queues the rest; `bot_mode` reads the mode in force at the open of the entry's session and answers shadow for a bot whose sleeve's lifecycle state does not route live (PR 18; 05 HAT-PORT-063); `apply_mode_changes` writes it to `hat_bot.execution_mode` at the open (wired by PR 17).

## §9 Risk-policy change rules

#### HAT-GOV-090: Decrease effective next session
- **Statement:** A policy change that only lowers or keeps risk numbers is `direction = decrease` and takes effect at the next session open.
- **Source:** BL "Governance" (Risk policy decrease).
- **PR:** 10
- **Verification:** PBT `caps.policy_direction_classification`
- **Acceptance:**
  - Property: for every pair (current policy, proposed policy), the direction is `decrease` iff no field is more permissive than current, and `increase` otherwise; a decrease's `effective_from` is the next session open. Harm: an increase is smuggled in as a decrease and skips the cooling-off.

#### HAT-GOV-091: Increase waits 7 days and passes the evidence gate
- **Statement:** An increase takes effect no earlier than `requested_at + 7 days` [A] and only with gate evidence of ≥ 50 live trades with expectancy within tolerance.
- **Source:** BL "Governance"; SCH CHECK `direction <> 'increase' or effective_from >= requested_at + interval '7 days'`.
- **PR:** 2, 10, 12, 17
- **Verification:** DB `governance.risk_increase_cooling_off`, PBT `caps.increase_gate`, PBT `dsr.gate_evidence`, PBT `caps.policy_change_inside_session_or_without_evidence_is_refused`
- **Acceptance:**
  - Property: the database rejects every insert with direction = increase and effective_from < requested_at + 7 days. Harm: risk rises on impulse.
  - Property: for every evidence set, an increase is accepted only when it records ≥ 50 live (non-virtual) closed trades and expectancy within tolerance in `gate_evidence`. Harm: risk rises without evidence.
  - Property: for every set of sleeves with completed backtest runs and closed trades, `runs.gate_evidence` equals `build_gate_evidence` over the user's closed trades (closing order, shadow ones ignored) against the discounted, trade-weighted mean expectancy of each sleeve's newest completed run of its live version; with no such run it is none. Harm: an increase is checked against a benchmark from a stale or foreign backtest.
- **[A]** cooling-off 7 days: validated by review after the first increase.
- **[OWNER-PENDING]** "expectancy within tolerance": the docs give no number; default = live expectancy (R) ≥ 0 and its 90% confidence interval overlapping the discounted backtest expectancy; owner to confirm.
- **Design (PR 12):** the evidence source is `app.hat.backtest.runs.gate_evidence` (09 HAT-BT-060); the discount is the lifecycle's (05 HAT-PORT-065).
- **Design (PR 17):** `request_policy_change` reads the evidence itself (`policy_service.backtest_evidence`, over `runs.gate_evidence`); a caller cannot hand it evidence. Property: with no completed backtest run, every increase is refused with `GateError` and writes nothing. Harm: an increase passes on evidence the caller made up.

#### HAT-GOV-092: Policy versions are immutable
- **Statement:** A published risk-policy version can never be updated or deleted except to set `superseded_at`.
- **Source:** SCH "Governance enforced in the database" (Risk versions are immutable).
- **PR:** 2
- **Verification:** DB `governance.risk_version_immutable`
- **Acceptance:**
  - Property: on Postgres, every UPDATE that changes a column other than `superseded_at`, and every DELETE, on `hat_risk_policy_version` raises. Harm: past trades lose the policy that sized them.

#### HAT-GOV-093: Multiplier bounds live in the policy
- **Statement:** Regime and health multiplier bounds (default 0.25–1.0) are policy fields, so allowing a multiplier above 1 is a risk increase.
- **Source:** BL "Allocation"; SCH "Two additions elsewhere"; PL2.
- **PR:** 9, 10
- **Verification:** PBT `caps.policy_direction_classification`
- **Acceptance:**
  - Property: for every proposed policy that raises the upper multiplier bound or base weights, the computed direction is `increase`. Harm: the allocator scales a sleeve up without the cooling-off.

#### HAT-GOV-094: Changes are events
- **Statement:** Every policy request and every policy taking effect writes `risk.change_requested` / `risk.change_effective`.
- **Source:** SCH "Event types (MVP)" Governance.
- **PR:** 10
- **Verification:** PBT `caps.policy_change_emits_event`
- **Acceptance:**
  - Property: for every sequence of policy requests and session opens, the effective policy changes only in a transaction that also appends `risk.change_effective`, and every request appends `risk.change_requested`. Harm: an unaudited risk change.

## §10 The risk authority

`evaluate_signal(signal_ctx) -> RiskDecision` in `app/hat/risk/service.py` is the single risk authority for HAT (PLAN C2; MA "Boundary rules"). It works out two outcomes itself, market open at the signal's instant and the entry price within the price band (20% of the last price, `MAX_PRICE_DEVIATION_PCT`), and composes them with the HAT policy, sizing, caps, kill switches and invariants. The HAT checks are pure functions in `app/hat/risk/`. [K1: it used to be `RiskManager.evaluate_signal` in the legacy `services/risk_manager.py`; it moved so HAT depends on nothing outside `app/hat`. The margin check is not composed: the sizing cash term is stricter.]

#### HAT-RISK-100: One risk authority
- **Statement:** Every HAT entry is sized and approved only by `evaluate_signal` (`app/hat/risk/service.py`); no other module sizes a HAT order.
- **Source:** MA "Boundary rules" ("One risk authority"); PLAN C2; OWN-20OCT.
- **PR:** 10, 15
- **Verification:** CONF `risk.single_sizing_path`, TLA `ExecutionRouter!RiskBeforeRoute`
- **Acceptance:**
  - Property: in every reachable router state, an entry is routed (shadow, confirm or auto) only after an ACCEPT decision from `evaluate_signal` for that signal id. Harm: a second, weaker risk path places orders.
  - Property: the import graph shows the HAT sizing functions are called only from `evaluate_signal`. Harm: as above.

#### HAT-RISK-101: Decision is typed and stored
- **Statement:** `evaluate_signal` returns ACCEPT (with q, stop, risk_amount) or REJECT (with a typed reason code) plus warnings, and the decision is stored on `hat_signal.risk_decision` with the risk-policy version id.
- **Source:** SCH `hat_signal` (risk_policy_version_id, risk_decision); BL "Trade lifecycle" (risk check at Triggered).
- **PR:** 10
- **Verification:** PBT `caps.decision_total`
- **Acceptance:**
  - Property: for every input, the function returns exactly one of ACCEPT or REJECT, never raises for invalid market data (fails closed as REJECT), and a REJECT always carries a reason code from the enumerated set. Harm: an unexplained or crashing decision path lets a signal through or hides why it failed.

#### HAT-RISK-102: Decimal money and fail closed
- **Statement:** All HAT risk money math uses Decimal; any missing input (equity, cash, ADV, price, calendar) produces REJECT.
- **Source:** SCH "Conventions" (Money never float); PLAN C2; trading-safety skill (fail closed).
- **PR:** 10
- **Verification:** PBT `caps.fail_closed`
- **Acceptance:**
  - Property: for every input with any required field missing, None or non-finite, the decision is REJECT with reason `missing_input`. Harm: a default of zero or infinity sizes a real order.

#### HAT-RISK-103: HAT's safety properties are pinned and no manual-trading path exists
- **Statement:** The Hypothesis properties that keep an order behind HAT's checks (exits never blocked, regime gate, stop required, stale feed, market hours, kill authority, caps, tag and idempotency rules, the broker's fail-closed rules) cannot be edited or weakened to fit a change, and the legacy manual-trading code (risk manager, order execution, position sync, live exit, the order routes and the legacy brokers package) is deleted, so `evaluate_signal` and the Fyers adapter are the only way to an order. [Changed in K4 (owner: no manual trading; D59): it pinned the pre-HAT risk test files and said the legacy `RiskManager` imports nothing from `app.hat`.]
- **Source:** PLAN C2; cleanup plan K4.
- **PR:** 10, K4
- **Verification:** CONF `risk.hat_safety_properties_pinned`, CONF `risk.no_manual_trading_path`
- **Acceptance:**
  - Property: every pinned safety property exists, is a `@given` property, matches its recorded digest and passes. Harm: a safety property is weakened or deleted so a change passes.
  - Property: none of the legacy manual-trading modules exists and no class named a risk manager, order-execution service or position-sync service is defined. Harm: a second path to a broker order that bypasses HAT's risk decision.

#### HAT-RISK-104: HAT's own kill switches are the only halt
- **Statement:** HAT's own kill switches (account kill, bot kill, health kill, pause) are the only halt of a HAT entry. The legacy VCP `TRADING_HALTED` flag is not read: `evaluate_signal` takes no database session, `SignalContext` has no halt field and the reason `smarttrader_halt` no longer exists. HAT's `/kill` affects HAT bots only. [Changed in K1 (D59/D60, the legacy is being deleted); it said the VCP flag also blocked HAT entries (OWNER-PENDING, PLAN Q6).]
- **Source:** PLAN Q6, C6; cleanup plan K1.
- **PR:** 10, K1
- **Verification:** PBT `caps.hat_kill_is_authority`, CONF `risk.no_legacy_halt_read`
- **Acceptance:**
  - Property: for every combination of the account kill, bot kill, health kill and pause, an entry that passes every other check is REJECT with the first tripped switch's reason, and ACCEPT when none is tripped. Harm: a HAT kill is ignored, or an entry is halted by state HAT does not own.
  - Property: no module of `app/hat` names `TRADING_HALTED`, `smarttrader_halt*` or the legacy `system_config` table. Harm: HAT silently depends on a legacy flag that is deleted with the VCP module.

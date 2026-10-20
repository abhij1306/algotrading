# 07: Execution and Brokers

Scope: execution modes, the execution router, confirm-mode approvals, the trade lifecycle, stop rules and broker-side stop placement, exit-stack evaluation, HAT's broker interface and its Fyers adapter, order tags and idempotency, reconciliation, live trigger and entry-filter evaluation, and veto scoring. Requirement format and verification methods: see README.md and 11-verification.md.

Build decision (OWN-20OCT): orders go through HAT's own broker interface (`app/hat/execution/broker.py`), implemented in the MVP by a direct Fyers adapter that reuses the tested `FyersBroker`/`FyersClient`. There is no OpenAlgo in the MVP (PLAN C5); `hat_broker_account.openalgo_ref` stays as a nullable column for a later adapter.

## §1 Execution modes

Mode is a per-bot switch (`hat_bot.execution_mode`): **shadow** (virtual fills, no broker), **confirm** (Telegram Take/Skip, then a real order), **auto** (real order without asking). Default shadow (B2, revised). All three run the same risk check. Auto is for the owner's own accounts only (MA "Execution modes").

#### HAT-EXEC-010: Shadow never reaches the broker
- **Statement:** A bot in shadow mode never calls any broker method; its orders, fills and positions are written with `is_virtual = true`.
- **Source:** BL "Shadow replaces signal-only"; SCH "Conventions" (Virtual vs real); B1, B2 (revised).
- **PR:** 11, 15
- **Verification:** TLA `ExecutionRouter!ShadowNeverCallsBroker`, PBT `exits.shadow_rows_virtual`
- **Acceptance:**
  - Property: in every reachable state of the router, for every signal whose bot mode was shadow at the session open, the count of broker calls attributed to that signal is 0. Harm: a paper strategy places real orders.
  - Property: for every shadow signal driven end to end through the real router against `FakeBroker`, every `hat_order`, `hat_fill` and `hat_position` row it produces has `is_virtual = true` and the fake records zero calls. Harm: shadow trades contaminate real position, P&L and reconciliation.

#### HAT-EXEC-011: Default mode is shadow
- **Statement:** Every new bot and both seeded default configs start in shadow mode.
- **Source:** BL "Minervini-style bot — default config", "Qullamaggie-style bot — default config" (`execution_mode: shadow`); B2 (revised).
- **PR:** 8, 15
- **Verification:** CONF `exec.default_mode_shadow`
- **Acceptance:**
  - Property: every seed config and the `hat_bot.execution_mode` column default equal `shadow`. Harm: a new bot trades real money before it has run in shadow.

#### HAT-EXEC-012: Auto requires a broker with delivery GTT
- **Statement:** A bot cannot run in auto mode on a broker account whose capabilities lack reliable GTT for delivery (CNC); because every real fill needs an overnight broker-side stop, confirm mode is blocked on such an account too.
- **Source:** BL "Stop placement at the broker (B17)" step 4; DS "Broker matrix"; MA "Risks" (GTT support).
- **PR:** 13, 15
- **Verification:** TLA `ExecutionRouter!AutoNeedsGtt`
- **Acceptance:**
  - Property: in every reachable state, a bot whose broker account has `capabilities.gtt_delivery = false` is never in effective mode auto, and an auto request for it is refused. Harm: an auto position is left without an overnight stop.
  - Property: in every reachable state, a bot whose broker account has `capabilities.gtt_delivery = false` is never in effective mode confirm either, so it places no real order. Harm: a confirmed entry fills and then goes overnight with no resting stop, breaking "protective stops sit at the broker on every real fill" (MA "Execution modes").
- **[OWNER-PENDING]** Fyers GTT for CNC: until verified on a live account, the Fyers adapter reports `gtt_delivery = false`, so the MVP runs shadow only until then (PLAN Q8 extended to confirm, which also places real orders); owner to confirm after verification.

#### HAT-EXEC-013: Delivery only
- **Statement:** Every HAT real order uses product CNC (delivery).
- **Source:** MA "Decisions register"; DS "Broker interface" (`intent` product CNC); B24.
- **PR:** 13
- **Verification:** PBT `tags.intent_product_cnc`
- **Acceptance:**
  - Property: for every order intent the router or exit path builds, `product = CNC`, and the adapter rejects any other product. Harm: an intraday product is auto-squared off by the broker, breaking the swing plan.

## §2 Router

The router receives a signal only after `RiskManager.evaluate_signal` returned ACCEPT (06 §10) and routes it by the bot's effective mode.

#### HAT-EXEC-020: One risk check, three routes
- **Statement:** The router routes an accepted signal to the virtual fill engine (shadow), an approval (confirm) or a real order (auto), and nothing else.
- **Source:** MA "Execution modes"; BL "Configuration model" stage 9.
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!RouteByMode`, TLA `ExecutionRouter!RiskBeforeRoute`
- **Acceptance:**
  - Property: in every reachable state, every routed signal has an ACCEPT risk decision and exactly one route matching its bot's effective mode. Harm: a signal is double-routed or routed under the wrong mode.

#### HAT-EXEC-021: Duplicate triggers are refused
- **Statement:** One signal per (bot, playbook, instrument, trade_date); the signal id is the idempotency key, and a second trigger never creates a second order.
- **Source:** MA "Failure handling" (Duplicate trigger); SCH `hat_signal` unique key.
- **PR:** 3, 15
- **Verification:** TLA `OrderReconciliation!AtMostOneEntryPerSignal`, DB `runtime.signal_unique_per_day`
- **Acceptance:**
  - Property: in every behaviour, including concurrent and repeated triggers, at most one entry order exists per signal key. Harm: a double position at double risk.

#### HAT-EXEC-022: Router honours invariants 4 and 5
- **Statement:** The router places no entry while the feed is stale or the day's data failed the gate, and no order outside market hours.
- **Source:** BL "Invariants" 4, 5; DS "Bar builder rules".
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!NoOrderWhenStale`, TLA `ExecutionRouter!NoOrderOutsideHours`
- **Acceptance:**
  - Property: in every reachable state where the feed is stale, the gate failed, or the clock is outside the session, no entry order is sent (real or virtual). Harm: orders on a frozen or closed market.

#### HAT-EXEC-023: Order rejection retried at most once
- **Statement:** A broker-rejected order is logged, alerted, and retried at most once.
- **Source:** MA "Failure handling" (Order rejected).
- **PR:** 15
- **Verification:** TLA `OrderReconciliation!RetryAtMostOnce`
- **Acceptance:**
  - Property: in every behaviour, for each (signal, purpose) the number of broker place attempts after a rejection is ≤ 1, and every rejection appends `order.rejected` and raises a health alert. Harm: a retry storm or a silent rejection.

## §3 Confirm mode, approval, expiry and max-chase re-check

Confirm mode sends a Telegram approval card with Take/Skip buttons; price, size and stop are fixed by the engine. Token rules (signed, single-use, tied to signal id and expiry) are in 08-telegram (HAT-TG).

#### HAT-EXEC-030: Approval expires after 5 minutes
- **Statement:** An approval not decided within 5 minutes of `sent_at` expires and no order is placed.
- **Source:** MA "Execution modes" ("Confirm expires. Default window 5 minutes"); SCH `hat_approval`.
- **PR:** 15
- **Verification:** TLA `ApprovalToken!NoTakeAfterExpiry`
- **Acceptance:**
  - Property: in every behaviour, no Take decided at or after `expires_at = sent_at + 5 min` produces an order, and every undecided approval reaches decision `expired` with `signal.expired`. Harm: a stale approval opens a position at a price the user never saw.

#### HAT-EXEC-031: Max-chase re-check on take
- **Statement:** On Take, the price is re-checked; if it has run more than the bot's max-chase % past the trigger, the order is refused.
- **Source:** MA "Execution modes"; BL "Criteria library" (`max_chase` M 5, Q 2); B25.
- **PR:** 15
- **Verification:** TLA `ApprovalToken!ChaseRecheckOnTake`, PBT `caps.max_chase_always`
- **Acceptance:**
  - Property: in every behaviour, a Take produces an order only if price_at_decision ≤ trigger_level × (1 + max_chase_pct/100) and the instrument is not locked at upper circuit; otherwise the decision is recorded and the order refused. Harm: the user approves an entry that has already run away.

#### HAT-EXEC-032: Skips continue as shadow trades
- **Statement:** A skipped or expired confirm signal continues as a virtual (shadow) trade for veto scoring.
- **Source:** BL "Your vetoes are measured"; SCH `hat_approval` notes.
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!SkipBecomesShadow`
- **Acceptance:**
  - Property: in every behaviour, every approval decided skip or expired is followed by exactly one virtual position for that signal and no real order. Harm: veto scoring is biased by missing skipped trades.

#### HAT-EXEC-033: Exits never ask
- **Statement:** Exits, stop moves and partials are automatic in confirm and auto and virtual in shadow; no exit waits for the user.
- **Source:** MA "Execution modes" ("Exits never ask"); BL "Evaluation rules".
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!ExitsNeverAsk`
- **Acceptance:**
  - Property: in every reachable state, no exit or stop action creates an approval, and every fired exit is eventually sent (real modes) or virtually filled (shadow). Harm: an exit stalls waiting for a human while the price falls.

#### HAT-EXEC-034: Catalyst required in confirm for EP
- **Statement:** For the episodic-pivot playbook, `catalyst_attached` is required in confirm mode and informational in auto unless marked hard.
- **Source:** BL "Qullamaggie-style bot" (`required_in: [confirm]`); DS "Announcements for EP catalysts".
- **PR:** 15
- **Verification:** PBT `criteria.catalyst_required_in_confirm`
- **Acceptance:**
  - Property: for every EP signal, in confirm mode an approval is sent only when an announcement for the instrument was published within 24 hours; in auto mode the absence of one does not block the trigger unless the criterion mode is hard. Harm: the user is asked to approve an EP without the catalyst they judge it by.

## §4 Trade lifecycle

The BL says a trade moves through eleven states and ends as Closed, Rejected, Expired or Cancelled; the diagram's state labels were lost in the text export. The states below are a reconstruction from the event catalogue (SCH "Event types (MVP)"). **[OWNER-PENDING]**

| # | State | Kind |
| --- | --- | --- |
| 1 | Watchlisted | active |
| 2 | Triggered | active (risk check runs here) |
| 3 | AwaitingApproval | active (confirm only) |
| 4 | EntryPlaced | active |
| 5 | Open | active |
| 6 | PartiallyExited | active |
| 7 | ExitPending | active |
| 8 | Closed | ending |
| 9 | Rejected | ending |
| 10 | Expired | ending |
| 11 | Cancelled | ending |

| From | To | Driving event | Condition |
| --- | --- | --- | --- |
| (none) | Watchlisted | `watchlist.published` | nightly scan row (EP: pre-open scan) |
| Watchlisted | Triggered | `signal.triggered` | trigger criteria pass live |
| Triggered | Rejected | `signal.rejected` | risk decision REJECT or entry filter fails |
| Triggered | AwaitingApproval | (approval sent) | mode confirm |
| Triggered | EntryPlaced | `order.placed` | mode auto, or shadow (virtual order) |
| AwaitingApproval | EntryPlaced | `signal.approved`, then `order.placed` | Take within expiry and chase re-check passes |
| AwaitingApproval | Expired | `signal.expired` | no decision within 5 minutes |
| AwaitingApproval | Cancelled | `signal.skipped` or `kill.tripped` | Skip (reason skipped) or `/kill`. On skip (and likewise on expiry, HAT-EXEC-032) the engine opens a separate virtual trade for the same signal, starting at EntryPlaced with a virtual order, which then follows this table to its own ending (HAT-EXEC-032) |
| AwaitingApproval | Rejected | `signal.rejected` | chase re-check fails on Take |
| EntryPlaced | Open | `order.filled`, `position.opened` | entry fill |
| EntryPlaced | Rejected | `order.rejected` | broker rejection after the single retry |
| EntryPlaced | Cancelled | `order.cancelled` | entry order unfilled at session end |
| Open | PartiallyExited | `exit.partial` | a partial exit fills |
| Open, PartiallyExited | ExitPending | (exit order placed) | full exit rule fires or stop triggers |
| PartiallyExited | PartiallyExited | `exit.partial` | another partial fills |
| ExitPending | Closed | `order.filled`, `position.closed` | remaining qty filled |

#### HAT-EXEC-040: Lifecycle states and transitions
- **Statement:** A trade's state is one of the 11 states above and changes only along the transition table.
- **Source:** BL "Trade lifecycle"; SCH "Event types (MVP)".
- **PR:** 11
- **Verification:** TLA `TradeLifecycle!TypeOK`, TLA `TradeLifecycle!OnlyAllowedTransitions`
- **Acceptance:**
  - Property: in every reachable state, every trade is in exactly one of the 11 states, and every step changes it only along a row of the transition table. Harm: a trade reaches an impossible state (for example Open without an entry fill) and is mismanaged.

#### HAT-EXEC-041: Endings are terminal and every trade ends
- **Statement:** Closed, Rejected, Expired and Cancelled are terminal; every trade that has not opened (Watchlisted, Triggered, AwaitingApproval, EntryPlaced) eventually reaches an ending or Open. An open trade has no deadline: it ends only when an exit rule or the stop fires.
- **Source:** BL "Trade lifecycle".
- **PR:** 11
- **Verification:** TLA `TradeLifecycle!EndingsTerminal`, TLA `TradeLifecycle!EventuallyEnds`
- **Acceptance:**
  - Property: no step leaves an ending state. Harm: a closed or rejected trade re-opens and holds risk budget.
  - Property: under weak fairness of the expiry, session-end and broker-update actions, every trade in Watchlisted, Triggered, AwaitingApproval or EntryPlaced eventually reaches Open or an ending (Watchlisted ends at the session end of its trade date). Harm: pending signals and entry orders linger and block the one-signal-per-day key or fill later at a stale price.
  - Property: whenever the model's environment fires the stop or a full exit rule for an Open or PartiallyExited trade, the trade eventually reaches Closed. Harm: an exit that fired leaves a position open.

#### HAT-EXEC-042: State derived from events in one transaction
- **Statement:** Every transition appends its event in the same transaction as the state change, and replaying the event log reproduces the current state.
- **Source:** BL "Trade lifecycle"; SCH "Event log" (same transaction).
- **PR:** 3, 11
- **Verification:** TLA `TradeLifecycle!StateMatchesLog`, PBT `exits.replay_reproduces_state`
- **Acceptance:**
  - Property: in every reachable state, the state stored for a trade equals the fold of its events over the transition table. Harm: the audit log and the trade table disagree and the system acts on the wrong one.
  - Property: for every generated sequence of lifecycle events, folding the persisted events reproduces the stored `hat_signal.state`/`hat_position.state`. Harm: as above.

#### HAT-EXEC-043: Rejections reach the daily digest only
- **Statement:** A risk-rejected signal is logged and shown only in the 15:35 rejection digest, not as a live alert.
- **Source:** BL "Trade lifecycle" ("a rejected signal is logged and shown only in the daily digest").
- **PR:** 15, 16
- **Verification:** TLA `ExecutionRouter!RejectNoCard`
- **Acceptance:**
  - Property: in every behaviour, a signal that reaches Rejected from Triggered produces no approval card or shadow-trade message, and appears in that day's rejection digest. Harm: noise trains the user to ignore cards.

#### HAT-EXEC-044: Shadow runs the same states
- **Statement:** Shadow trades pass through the same states on virtual fills, with no broker order.
- **Source:** BL "Trade lifecycle".
- **PR:** 11
- **Verification:** TLA `TradeLifecycle!ShadowSameMachine`
- **Acceptance:**
  - Property: the transition relation for virtual trades is identical to the one for real trades, with broker actions replaced by the virtual fill engine. Harm: shadow evidence does not predict live behaviour.

## §5 Stop rules

| Rule | Parameters | Used by |
| --- | --- | --- |
| `below_final_contraction_low` | buffer % (0.5), max % below entry (8); wider → skip | M |
| `low_of_day` | buffer ticks (1) | Q |
| `pct_below_entry` | % | Custom bots |
| `atr_multiple` | ATR days, multiple | Custom bots |
| `swing_low` | lookback days | Custom bots |

One stop rule per playbook; the initial stop is computed before entry (BL "Stops and exits").

#### HAT-EXEC-050: below_final_contraction_low
- **Statement:** Stop = final contraction low × (1 − buffer_pct/100) with buffer 0.5%; if (entry − stop)/entry > 8% the trade is skipped.
- **Source:** BL "Stop rules"; BL "Minervini-style bot" (`stop: {rule: below_final_contraction_low, buffer_pct: 0.5, max_pct: 8}`).
- **PR:** 11
- **Verification:** PBT `stops.final_contraction_low`
- **Acceptance:**
  - Property: for every final contraction low L > 0, entry P > 0 and buffer b, the stop equals L × (1 − b/100) rounded down to the tick, and the result is SKIP exactly when (P − stop)/P > max_pct/100. Harm: stops wider than the playbook allows are accepted, or valid trades are dropped.

#### HAT-EXEC-051: low_of_day
- **Statement:** Stop = day's low at entry minus 1 tick (buffer ticks default 1).
- **Source:** BL "Stop rules"; BL "Qullamaggie-style bot" (`stop: {rule: low_of_day}`).
- **PR:** 11
- **Verification:** PBT `stops.low_of_day`
- **Acceptance:**
  - Property: for every intraday bar sequence up to the entry bar and every tick size, the stop equals min(low of all bars that day up to and including entry) − buffer_ticks × tick, and is strictly below the entry price. Harm: a stop above the session low triggers on normal noise.

#### HAT-EXEC-052: Custom stop rules
- **Statement:** `pct_below_entry`, `atr_multiple` and `swing_low` compute entry × (1 − %/100), entry − multiple × ATR(days), and the lowest low over lookback days respectively.
- **Source:** BL "Stop rules".
- **PR:** 11
- **Verification:** PBT `stops.custom_rules`
- **Acceptance:**
  - Property: for every valid parameter set and bar history, each rule returns its formula's value and every result is strictly below entry; a result ≤ 0 or ≥ entry yields SKIP. Harm: a malformed stop sizes an unbounded position.

## §6 Stop placement at the broker

1. On fill: intraday stop order at the stop price (expires at day end).
2. At ~15:25: replace it with a GTT stop for overnight, refreshed nightly.
3. GTT limit price set 1% below trigger so moderate gap-downs still fill [A]. A gap below the limit price leaves the GTT unfilled; the offset reduces that risk but cannot remove it, so an open position whose GTT triggered without a fill raises a health alert.
4. A broker without reliable GTT for delivery cannot run auto mode (HAT-EXEC-012).

#### HAT-EXEC-060: Stop at the broker on every real fill
- **Statement:** Every real entry fill is followed by an intraday stop order (purpose `stop_day`) at the position's stop price.
- **Source:** BL "Stop placement at the broker (B17)" step 1; MA "Execution modes" ("Protective stops sit at the broker on every real fill"); B17.
- **PR:** 15
- **Verification:** TLA `StopPlacement!StopAfterFill`
- **Acceptance:**
  - Property: in every behaviour, after a real entry fill, a working broker stop for the open quantity exists before any other action on that position, and from then on there is always a working stop (day or GTT) while qty_open > 0. Harm: a position sits unprotected if the machine dies.

#### HAT-EXEC-061: Intraday stop swapped to GTT at ~15:25
- **Statement:** At ~15:25 each intraday stop is replaced by a GTT stop (purpose `stop_gtt`) for overnight protection, without a gap in coverage.
- **Source:** BL "Stop placement at the broker" step 2; MA "Daily cycle"; SCH events `gtt.placed`.
- **PR:** 15
- **Verification:** TLA `StopPlacement!NoUnprotectedGap`
- **Acceptance:**
  - Property: in every interleaving of the swap steps with fills, rejections and crashes, the GTT is confirmed placed before the intraday stop is cancelled, so at no state is a real open position without a working stop; a failed GTT placement leaves the intraday stop working and raises an alert. Harm: the position goes overnight unprotected.

#### HAT-EXEC-062: GTT limit offset
- **Statement:** The GTT limit price is set 1% [A] below its trigger price.
- **Source:** BL "Stop placement" step 3; BL "Defaults that are assumptions" (GTT limit offset).
- **PR:** 13, 15
- **Verification:** PBT `stops.gtt_limit_offset`
- **Acceptance:**
  - Property: for every trigger price T > 0 and tick size, the GTT limit equals T × (1 − 0.01) rounded down to the tick and is < T. Harm: a gap-down skips past the limit and the stop never fills.
  - Property: in every behaviour where a GTT triggers and is not filled, a health alert is raised for that position. Harm: the owner believes the position is protected after the stop failed to fill.
- **[A]** GTT limit offset 1% below trigger: validated by adapter tests per broker and observed fills.

#### HAT-EXEC-063: Nightly GTT verify
- **Statement:** Every night the engine confirms a GTT stop exists at the broker for every open real position and alerts on any gap.
- **Source:** MA "Daily cycle" step 9; BL "Stop placement" step 2 (refreshed nightly); SCH event `gtt.refreshed`.
- **PR:** 15
- **Verification:** TLA `StopPlacement!NightlyVerifyCovers`
- **Acceptance:**
  - Property: in every behaviour, after the nightly `gtt.verify` job, every open real position either has a broker GTT whose trigger equals `current_stop`, or a health alert was raised and a re-placement attempted. Harm: a missing GTT goes unnoticed for days.

#### HAT-EXEC-064: Stop moves follow the stop
- **Statement:** When `current_stop` rises, the working broker stop (day or GTT) is modified to the new price; it is never modified downward.
- **Source:** BL "Invariants" 1; SCH `stop.moved`; DS "Broker interface" (`modify_order`, `modify_gtt`).
- **PR:** 15
- **Verification:** TLA `StopPlacement!BrokerStopMatchesCurrent`, TLA `StopPlacement!StopOnlyTightens`
- **Acceptance:**
  - Property: in every reachable state, the broker stop price for an open position is ≤ `current_stop` and eventually equals it, and the sequence of broker stop prices is non-decreasing. Harm: the broker stop lags a raised stop and gives back profit, or is lowered.

## §7 Exit stack

| Rule | Parameters | Evaluated | Default in |
| --- | --- | --- | --- |
| `hard_stop` | none | Live (broker-side) | Both |
| `move_stop_to_breakeven` | at R, or after a partial | Live | Both |
| `partial_at_r` | R multiple, fraction | Live | M |
| `partial_after_days` | days, fraction, only if in profit | 15:25 on day N | Q |
| `close_below_sma` | SMA length, fraction | 15:25 | Both |
| `trailing_swing_low` | lookback days | 15:25 | Custom |
| `pct_gain_target` | %, fraction | Live | Custom |
| `time_stop` | days, min R reached | 15:25 | M (off) |
| `regime_exit` | fraction | Nightly | Off by default |

Evaluation rules (BL "Evaluation rules"): top to bottom on every price event or scheduled check; full exit wins over partials firing at the same time; each rule fires once per trade unless marked repeatable; fractions apply to the original quantity, quantities round down, minimum 1 share, and a remainder below 1 share is closed with the last exit; a rule that would lower the stop is ignored and logged; exits never wait for the user.

#### HAT-EXEC-070: Ordered evaluation, full beats partial
- **Statement:** Exit rules are evaluated top to bottom, and when a full exit and partials fire on the same evaluation only the full exit is executed.
- **Source:** BL "Evaluation rules"; B19.
- **PR:** 11
- **Verification:** PBT `exits.full_beats_partial`
- **Acceptance:**
  - Property: for every exit stack and every price event, if any enabled full-exit rule fires, the output is exactly one full exit of qty_open and no partial. Harm: overlapping partial and full exits oversell or leave a remainder.

#### HAT-EXEC-071: Fire once
- **Statement:** Each exit rule fires at most once per trade unless marked repeatable, recorded in `hat_position.rules_fired`.
- **Source:** BL "Evaluation rules"; SCH `hat_position.rules_fired`.
- **PR:** 11
- **Verification:** PBT `exits.fire_once`
- **Acceptance:**
  - Property: for every sequence of price events, a non-repeatable rule appears in the emitted exits at most once per position. Harm: a partial fires on every tick and empties the position.

#### HAT-EXEC-072: Fraction arithmetic
- **Statement:** Exit fractions apply to the original quantity, round down, are at least 1 share, and a remainder below 1 share closes with the last exit.
- **Source:** BL "Evaluation rules".
- **PR:** 11
- **Verification:** PBT `exits.fraction_arithmetic`
- **Acceptance:**
  - Property: for every original qty Q ≥ 1 and every sequence of fractions f in (0, 1], each partial sells max(1, floor(f × Q)) capped at qty_open, the sum sold never exceeds Q, qty_open never goes negative, and when the last exit fires qty_open becomes 0. Harm: overselling (an unintended short) or a stranded odd lot.

#### HAT-EXEC-073: Breakeven and partial rules
- **Statement:** `move_stop_to_breakeven` raises the stop to avg_entry at the configured R or after a partial; `partial_at_r` sells its fraction when price reaches entry + R × at_r; `partial_after_days` sells its fraction at 15:25 on day N only if in profit.
- **Source:** BL "Exit rules"; BL default configs (M: breakeven at 2R, partial at 3R 0.33; Q: partial after 3 days 0.33 only if profit, breakeven after partial).
- **PR:** 11, 15
- **Verification:** PBT `exits.rule_semantics`
- **Acceptance:**
  - Property: for every position and price path, each rule fires exactly at the first evaluation where its condition holds (R = (price − avg_entry)/(avg_entry − initial_stop)), and breakeven never lowers a stop already above avg_entry. Harm: exits fire early, late, or loosen protection.

#### HAT-EXEC-074: Scheduled exits at 15:25 on live price
- **Statement:** `close_below_sma`, `partial_after_days`, `trailing_swing_low` and `time_stop` are evaluated in the 15:20–15:25 window using the live price as the close proxy.
- **Source:** BL "Exit rules"; MA "Daily cycle" step 6; DS "Live data" (15:20–15:25); B18.
- **PR:** 15
- **Verification:** TLA `JobScheduler!ExitsCloseInWindow`
- **Acceptance:**
  - Property: in every simulated trading day, the `exits.close` job runs once within 15:20–15:25 IST and its exit orders are sent before 15:30. Harm: MA exits miss the session and the position carries overnight against the rule.
- **[A]** exit at 15:25 vs official close: default 15:25 live price; validated in shadow by logging both and comparing (PLAN Q10).

## §8 Broker interface

HAT's own contract (`app/hat/execution/broker.py`, protocol `HatBroker`); I7. No adapter-specific type leaks into the engine.

| Method | Purpose |
| --- | --- |
| `capabilities()` | GTT for delivery, order-tag field and length, order types, static-IP requirement |
| `login_url()` / `complete_login(code)` / `session_status()` | Daily login via Telegram link |
| `place_order(intent)` → broker order ID | Entry/exit; `intent` carries instrument, side, qty, product (CNC), type, price, trigger, tag |
| `modify_order`, `cancel_order` | Stop moves, expiries |
| `place_gtt_stop`, `modify_gtt`, `cancel_gtt` | Overnight protective stops |
| `orders()`, `trades()`, `positions()`, `holdings()`, `funds()` | Reconciliation, sizing cash |
| `instruments()` | Broker symbol map |
| `order_updates()` | Stream or poll of order status changes |

#### HAT-BRK-080: Interface completeness and boundary
- **Statement:** `HatBroker` exposes exactly the methods above, and engine, risk and router code depend only on it, never on `FyersBroker`, the fyers SDK or any OpenAlgo client.
- **Source:** DS "Broker interface"; MA "Boundary rules"; I7, I8; OWN-20OCT.
- **PR:** 13
- **Verification:** CONF `brk.interface_methods`, CONF `brk.import_boundary`
- **Acceptance:**
  - Property: the protocol's method set equals the table, and the import graph of `app/hat/{engine,risk,execution/router}` contains no import of `app.brokers`, `fyers_apiv3` or an OpenAlgo package. Harm: a broker switch requires engine changes, or a second broker path bypasses the interface.

#### HAT-BRK-081: Broker errors fail closed
- **Statement:** When the broker is unreachable or returns an error, read methods raise rather than return empty data, and no broker error text reaches API responses, Telegram messages or events visible to the user.
- **Source:** Trading-safety skill (fail closed, no broker text); MA "Failure handling".
- **PR:** 13
- **Verification:** PBT `tags.broker_errors_fail_closed`
- **Acceptance:**
  - Property: for every injected broker failure (timeout, circuit open, error payload), `orders/positions/holdings/funds/trades` raise `BrokerUnavailableError` and never return an empty list, and the surfaced message is a fixed code without the raw error string. Harm: reconciliation reads "no positions" and the engine re-enters or abandons stops; secrets or account data leak.

#### HAT-BRK-082: Broker session handling
- **Statement:** Session drops are retried with backoff, alerted after 2 minutes, and no entry is placed while the session is not live; `hat_broker_session` tracks the morning login.
- **Source:** MA "Failure handling" (Broker session drops); MA "Daily cycle" step 1; SCH `hat_broker_session`; T8, T14.
- **PR:** 13, 16
- **Verification:** TLA `ExecutionRouter!NoEntryWithoutSession`
- **Acceptance:**
  - Property: in every reachable state, no real entry order is placed while the account's session status is not live; and if the session stays lost for 2 minutes, `broker.session_lost` and a health alert are eventually emitted. Harm: orders fail mid-flight or the user is not told the bot is blind.

## §9 Fyers adapter

The MVP adapter (`app/hat/execution/fyers_adapter.py`) wraps the existing `FyersBroker` (place/modify/cancel/positions/funds, circuit breaker, guarded timeout, `SUBMIT_UNCONFIRMED` handling, `find_order_by_tag`), `FyersClient` and the existing auth flow (`login_url()`/`exchange()`), plus GTT calls through the fyers SDK (`place_gtt_order`, `modify_gtt_order`, `cancel_gtt_order`, `gtt_orderbook`). Fyers secrets stay where SmartTrader keeps them today (PLAN C5).

#### HAT-BRK-090: Exact Fyers payloads
- **Statement:** The adapter maps every `intent` and GTT request to an exact, documented Fyers payload with product CNC.
- **Source:** DS "Broker integration"; PLAN PR 13 (payload-exact mapping); trading-safety skill rule 4.
- **PR:** 13
- **Verification:** PBT `tags.fyers_payload_mapping`
- **Acceptance:**
  - Property: for every valid intent (side, qty ≥ 1, type, price, trigger, tag) the produced payload has exactly the documented keys and values (symbol via `symbol_master.to_fyers`, `productType = CNC`, `orderTag = tag`), and every invalid intent raises before any SDK call. Harm: a malformed order reaches the exchange.

#### HAT-BRK-091: Capabilities reported honestly
- **Statement:** The Fyers adapter reports its capabilities (GTT delivery, tag length 20, order types, static IP required) and `gtt_delivery = false` until verified on a live account.
- **Source:** DS "Broker matrix" (*verify* rows); PLAN Q8.
- **PR:** 13
- **Verification:** CONF `brk.fyers_capabilities`
- **Acceptance:**
  - Property: `capabilities()` returns `tag_max_len = 20`, `static_ip_required = true` and `gtt_delivery = false` in the MVP build. Harm: auto mode is enabled on an unverified overnight stop path.
- **[OWNER-PENDING]** GTT delivery support on Fyers: default false (PLAN Q8); owner flips it after a live verification.

#### HAT-BRK-092: Onboarding and capability check
- **Statement:** A broker account becomes usable only after the capability check (funds, positions and holdings fetched) and is recorded in `hat_broker_account` with its capabilities.
- **Source:** DS "Onboarding a broker" (steps 4–5, without OpenAlgo per OWN-20OCT); I9.
- **PR:** 13
- **Verification:** TLA `OrderReconciliation!AccountActiveAfterCheck`
- **Acceptance:**
  - Property: in every behaviour, a bot bound to a broker account places a real order only if that account's status is active, which it reaches only after a passing capability check. Harm: orders go to an account whose permissions or funds were never confirmed.

## §10 Tags and idempotency

Fyers order tags must match `^[A-Za-z0-9]{1,20}$`; SmartTrader's terminal uses prefix `ST`. HAT tags (PLAN C12): `HT` + 2-character purpose code + 16-character encoding of the signal uuid = 20 characters, alphanumeric.

The 2-character purpose code is one purpose letter plus one sequence character, so that a purpose that recurs for the same signal (a nightly GTT replacement, a second partial exit, the one allowed retry after a rejection) still gets a unique tag. The 16 characters are the Crockford base32 encoding (upper case, alphanumeric) of the lowest 80 bits of the signal's uuid7, which hold its 74 random bits; the tag is not decoded back to the full uuid, the order row is found by `hat_order.tag`.

| Purpose (`hat_order.purpose`) | Letter |
| --- | --- |
| entry | E |
| stop_day | D |
| stop_gtt | G |
| exit_partial | P |
| exit_full | F |
| flatten | L |

Sequence character: `0`-`9` then `A`-`Z` (36 values), the count of earlier orders with the same signal and purpose. The nightly GTT refresh modifies the existing GTT (`modify_gtt`, event `gtt.refreshed`) and keeps its row and tag; a new `stop_gtt` order, and so a new sequence value, is needed only when a GTT is placed afresh (after the intraday stop swap, or when the old one was cancelled or triggered). If a purpose would need a 37th order for one signal, the order is refused (fail closed) and a health alert is raised.

The letters, the sequence character and the bit selection are an [OWNER-PENDING] choice (PLAN C12 fixes only the shape: `HT` + 2-character purpose code + 16-character signal encoding = 20 characters).

#### HAT-BRK-100: Tag shape and uniqueness
- **Statement:** Every HAT order tag is `HT` + purpose letter + sequence character + 16 base32 characters from the signal uuid, exactly 20 alphanumeric characters, unique per (signal, purpose, sequence).
- **Source:** SCH "Runtime" (tag = compact encoding of the signal ID plus purpose code; unique `hat_order (tag)`); PLAN C12; I10.
- **PR:** 13
- **Verification:** PBT `tags.shape_and_roundtrip`, DB `runtime.order_tag_unique`
- **Acceptance:**
  - Property: for every uuid7, purpose and sequence in 0-35, the tag matches `^HT[EDGPFL][0-9A-Z][0-9A-HJKMNP-TV-Z]{16}$`, has length 20, and decodes back to the purpose, the sequence and the 80-bit signal fragment it was built from. Harm: the broker rejects the tag, or reconciliation maps an order to the wrong signal.
  - Property: for every population of distinct uuid7 values generated in the same millisecond, distinct (signal, purpose, sequence) triples give distinct tags, and the database rejects a duplicate `hat_order.tag`. Harm: two orders share an idempotency key, so the second is silently treated as a retry of the first.
  - Property: the sequence for a new order equals the number of existing `hat_order` rows with the same signal and purpose, so a retry of the same logical order reuses its tag and a new logical order never does. Harm: a retry places a second order, or a legitimate second exit is refused as a duplicate.
  - Property: for every position, the nightly refresh changes no `hat_order.tag`, and a request that would need sequence 36 or more is refused with an alert instead of reusing a tag. Harm: tags run out after weeks of nightly refreshes, or a reused tag collides with an older order.
  - Property: no HAT tag starts with `ST`. Harm: HAT and terminal orders are confused.
- **[OWNER-PENDING]** purpose letters, sequence character and bit selection above; owner to confirm.

#### HAT-BRK-101: Persist before call
- **Statement:** A real order is persisted as `hat_order` with status PENDING and its tag before the broker is called; a retry with the same tag never places a second order.
- **Source:** MA "Failure handling" (signal ID is the idempotency key); DS "Broker interface"; trading-safety skill (persist before call).
- **PR:** 13
- **Verification:** TLA `OrderReconciliation!PersistBeforeCall`, TLA `OrderReconciliation!AtMostOneEntryPerSignal`
- **Acceptance:**
  - Property: in every behaviour, including crashes between any two steps, every broker place call is preceded by a committed PENDING row with the same tag, and at most one broker order exists per tag. Harm: a crash or retry double-places a real order.

#### HAT-BRK-102: Unconfirmed submit resolved by tag
- **Statement:** When a submit's outcome is unknown, the adapter resolves it by looking the order up by tag before any retry.
- **Source:** PLAN "Broker" (`SUBMIT_UNCONFIRMED`, `find_order_by_tag`); MA "Failure handling".
- **PR:** 13
- **Verification:** TLA `OrderReconciliation!UnconfirmedResolvedByTag`
- **Acceptance:**
  - Property: in every behaviour where a submit times out, no new place call with that tag occurs until a tag lookup has returned "absent"; a found order is adopted with its broker order id. Harm: a timed-out order is placed twice.

## §11 Reconciliation

#### HAT-BRK-110: Reconcile before the first order
- **Statement:** On startup and before the first order of each session, broker orders and positions are matched to `hat_order`/`hat_position` by tag; no order is placed until reconciliation succeeds.
- **Source:** MA "Runtime" (Live service reconciles before its first order); MA "Failure handling" (Machine down).
- **PR:** 13, 15
- **Verification:** TLA `OrderReconciliation!ReconcileBeforeFirstOrder`
- **Acceptance:**
  - Property: in every behaviour, no place call happens in a session before a successful reconciliation of that session. Harm: after a crash the engine re-enters a position it already holds or loses track of an open stop.

#### HAT-BRK-111: Reconciliation fails closed
- **Statement:** A mismatch (broker position without a HAT row, HAT open position missing at the broker, quantity difference, or an unknown HT-tagged order) blocks new entries for the affected bot and raises an alert; it never auto-closes or auto-adopts a position.
- **Source:** MA "Failure handling"; PLAN PR 13 (fail closed).
- **PR:** 13
- **Verification:** TLA `OrderReconciliation!MismatchBlocksEntries`
- **Acceptance:**
  - Property: in every reachable state with an unresolved mismatch for a bot, that bot accepts no entry and has raised exactly one alert per mismatch, while its exits and stops keep running. Harm: the engine trades on a false view of its book.

#### HAT-BRK-112: Terminal orders ignored
- **Statement:** Reconciliation considers only HT-tagged orders and HAT positions; orders tagged by the SmartTrader terminal (`ST`) or untagged are out of scope.
- **Source:** PLAN C12.
- **PR:** 13
- **Verification:** PBT `tags.reconcile_scope`
- **Acceptance:**
  - Property: for every broker order book mixing ST, HT and untagged orders, the reconciliation result depends only on the HT-tagged subset. Harm: manual trades are adopted or cancelled by HAT.

## §12 Live triggers and entry filters

Parameters, defaults and data needs are specified in 04-criteria-and-bots §4. This section covers evaluation in the live service. Triggers evaluate on final 5-minute bars (09:20–15:20) built by the bar builder (02 §11); entry filters run at trigger time.

#### HAT-EXEC-120: Triggers on final bars inside the window
- **Statement:** Trigger criteria evaluate only on final 5-minute bars and only between 09:20 and 15:20 IST.
- **Source:** MA "Daily cycle" steps 4–5; DS "Live data" (09:20–15:20 triggers); B8, B9.
- **PR:** 15
- **Verification:** PBT `bars.trigger_only_on_final_bars`
- **Acceptance:**
  - Property: for every tick stream, `signal.triggered` timestamps lie in [09:20, 15:20] IST and each is derived from a bar marked final (close + 2 s). Harm: triggers fire on half-built bars or in the auction.

#### HAT-EXEC-121: All trigger criteria must pass
- **Statement:** A playbook triggers only when every enabled trigger criterion passes on the same bar (price breaks level and volume pace, plus EP gap, early volume and catalyst rules).
- **Source:** BL "Configuration model" stage 4; BL default configs.
- **PR:** 15
- **Verification:** PBT `criteria.trigger_conjunction`
- **Acceptance:**
  - Property: for every bar and criteria set, the playbook triggers iff the conjunction of enabled trigger criteria is true, and a triggered signal records each criterion's value. Harm: entries on half the setup.

#### HAT-EXEC-122: Entry filters skip with reason
- **Statement:** At trigger, `adtr_filter` and `max_chase` (always on) run; any failure records a skip reason and no order is routed.
- **Source:** BL "Configuration model" stage 5; BL "Criteria library" (`adtr_filter`, `max_chase`); B25.
- **PR:** 15
- **Verification:** PBT `criteria.entry_filters_skip`
- **Acceptance:**
  - Property: for every signal, if (entry − stop) > max_multiple × ADTR20 or price > level × (1 + chase%) or the stock is locked at upper circuit, the signal is Rejected with the failing filter as reason. Harm: wide-stop or chased entries pass.

## §13 Veto scoring

#### HAT-EXEC-130: Skips are measured
- **Statement:** Every confirm-mode skip or expiry is tracked as a shadow trade, and the weekly digest reports the R the skips avoided and the R they missed.
- **Source:** BL "Your vetoes are measured"; SCH `hat_approval`, `hat_trade_result`.
- **PR:** 15, 16
- **Verification:** PBT `exits.veto_score`
- **Acceptance:**
  - Property: for every set of skipped-trade results, avoided R = −Σ(r for r < 0) and missed R = Σ(r for r > 0) over the week's closed virtual trades from skips, and the digest totals equal these sums. Harm: the user cannot tell whether their vetoes add value.

#### HAT-EXEC-131: Veto verdict after 50 decisions
- **Statement:** After 50 confirm decisions (take or skip), the digest states whether skips beat takes on R, as evidence for moving to auto.
- **Source:** BL "Your vetoes are measured" ("After 50 decisions").
- **PR:** 16
- **Verification:** PBT `exits.veto_verdict_threshold`
- **Acceptance:**
  - Property: for every decision history, the verdict is present iff the count of decided approvals is ≥ 50, and it compares mean R of skipped trades with mean R of taken trades; when either group has no closed trade, that mean is reported as not available and the verdict is "insufficient data". Harm: a verdict drawn from too few decisions, or a division by zero when every decision went one way.

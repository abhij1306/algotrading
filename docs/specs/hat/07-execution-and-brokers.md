# 07: Execution and Brokers

Scope: execution modes, the execution router, confirm-mode approvals, the trade lifecycle, stop rules and broker-side stop placement, exit-stack evaluation, HAT's broker interface and its Fyers adapter, order tags and idempotency, reconciliation, live trigger and entry-filter evaluation, and veto scoring. Requirement format and verification methods: see README.md and 11-verification.md.

Build decision (OWN-20OCT): orders go through HAT's own broker interface (`app/hat/execution/broker.py`), implemented in the MVP by a direct Fyers adapter over HAT's own `FyersBroker`, `FyersSession` and `FyersAuthService` (moved from the legacy modules in K2, which now inherit from them). There is no OpenAlgo in the MVP (PLAN C5); `hat_broker_account.openalgo_ref` stays as a nullable column for a later adapter.

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
- **Verification:** TLA `ExecutionRouter!AutoNeedsGtt`, PBT `router.routes_by_mode`
- **Acceptance:**
  - Property: in every reachable state, a bot whose broker account has `capabilities.gtt_delivery = false` is never in effective mode auto, and an auto request for it is refused. Harm: an auto position is left without an overnight stop.
  - Property: a Take whose account no longer reports GTT for delivery (or is no longer active) when the user decides places no order and ends the signal Rejected (`no_gtt_delivery`). Harm: a card sent before a capability change still buys without an overnight stop.
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

The router receives a signal only after `evaluate_signal` returned ACCEPT (06 §10) and routes it by the bot's effective mode.

Implementation (`app/hat/execution/router.py`, PR 15): the effective mode is the queued mode in force at the open of the trigger's session (`execution/modes.py:bot_mode`, HAT-GOV-084; shadow for a sleeve whose lifecycle state does not route live, HAT-PORT-063), gated by the account's GTT capability (HAT-EXEC-012). `engine/trades.py:open_entry` runs the risk check under the bot's lock and, in one transaction, opens the virtual position (shadow), writes the approval card (confirm, `hat_approval`, the signal moves to AwaitingApproval) or leaves the accepted signal in Triggered (auto). The router then places an auto entry, or a passed Take, as a LIMIT BUY CNC at the entry price the engine fixed at the trigger, through `submit.reserve_order` and `submit.send_order` only. Before the session's first order the broker book is reconciled (HAT-BRK-110). The signal commits (releasing the bot's lock) before the order is sent, so a placement first re-reads the live conditions at placement time, from the caller (`route` and `take` take `now` and a `PlacementCheck` read then, never the trigger's snapshot): the calendar session, the feed, the quality gate, the regime gate (HAT-EXEC-022), the kill switches and pause (06 §6) and the account's GTT capability (HAT-EXEC-012). On a refusal nothing is sent: a reserved entry that never reached the broker is cancelled and one that may have is left to reconciliation, never placed again. An entry that cannot be placed (a refusal on the order path, an unreachable broker, a second rejection, a refused live condition) ends the signal Rejected with a fixed reason and raises the `entry.not_placed` health alert; an order whose outcome is unknown is left to the tag lookup and reconciliation. A signal is placed only on its own trade date: a placement on a later date ends it Rejected with `signal_expired` (an entry left Triggered by a process that stopped is resumed after the restart only once the feed has a fresh tick and the account is reconciled, 10 §3). Every broker call of an account goes through the account's session owner (`daily/accounts.py:OwnedBroker`, HAT-DEC-T8), and one broker login serves one active account.

#### HAT-EXEC-020: One risk check, three routes
- **Statement:** The router routes an accepted signal to the virtual fill engine (shadow), an approval (confirm) or a real order (auto), and nothing else.
- **Source:** MA "Execution modes"; BL "Configuration model" stage 9.
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!RouteByMode`, TLA `ExecutionRouter!RiskBeforeRoute`, PBT `router.routes_by_mode`, PBT `router.refused_entry_is_not_sent`
- **Acceptance:**
  - Property: in every reachable state, every routed signal has an ACCEPT risk decision and exactly one route matching its bot's effective mode. Harm: a signal is double-routed or routed under the wrong mode.
  - Property: for every mode and GTT capability, one accepted trigger routed through the real router against `FakeBroker` gives exactly: a virtual position and zero broker calls (shadow), an approval card expiring 5 minutes later and zero broker calls (confirm), or one HT-tagged LIMIT BUY CNC order for the risk-sized quantity at the entry price, after the session's reconciliation, when the live conditions read at placement allow it, and otherwise none and the signal Rejected with the reason (auto). A routed signal without a position holds a slot valued at its planned entry price. Harm: a route places an order its mode never allows, or a pending entry's open risk is understated.
  - Property: an auto entry whose bot became manage-only (a kill switch or a pause) between the signal's commit and the placement sends nothing; a reserved order that never reached the broker is cancelled and the signal ends Rejected with the kill or pause reason, and one that may have reached it is left to reconciliation and neither placed again nor rejected. Harm: a bot buys after its kill switch, or a refusal re-sends an order the broker may hold.

#### HAT-EXEC-021: Duplicate triggers are refused
- **Statement:** One signal per (bot, playbook, instrument, trade_date); the signal id is the idempotency key, and a second trigger never creates a second order.
- **Source:** MA "Failure handling" (Duplicate trigger); SCH `hat_signal` unique key.
- **PR:** 3, 11, 15
- **Verification:** TLA `OrderReconciliation!AtMostOneEntryPerSignal`, DB `runtime.signal_unique_per_day`, PBT `trades.retry_returns_existing_signal`
- **Acceptance:**
  - Property: in every behaviour, including concurrent and repeated triggers, at most one entry order exists per signal key. Harm: a double position at double risk.
  - Property: a repeated trigger returns the existing signal before any entry rule is re-checked, so a retry after a mode or universe change finds the trade it already made and writes nothing. Harm: a retried job fails and its caller cannot tell the trade was taken.

#### HAT-EXEC-022: Router honours invariants 4 and 5
- **Statement:** The router places no entry while the feed is stale or the day's data failed the gate, and no order outside market hours.
- **Source:** BL "Invariants" 4, 5; DS "Bar builder rules".
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!NoOrderWhenStale`, TLA `ExecutionRouter!NoOrderOutsideHours`, PBT `router.take_rechecks_max_chase`
- **Acceptance:**
  - Property: in every reachable state where the feed is stale, the gate failed, or the clock is outside the session, no entry order is sent (real or virtual). Harm: orders on a frozen or closed market.
  - Property: a Take re-reads the conditions when the user decides: outside the calendar session, on a stale or unknown feed, on a failed quality gate or with the regime gate off it places nothing and ends the signal Rejected with the risk check's reason code. Harm: a card taken after the close or after the feed froze buys blind.

#### HAT-EXEC-023: Order rejection retried at most once
- **Statement:** A broker-rejected order is logged, alerted, and retried at most once.
- **Source:** MA "Failure handling" (Order rejected).
- **PR:** 15
- **Verification:** TLA `OrderReconciliation!RetryAtMostOnce`, PBT `router.rejected_entry_is_retried_once`
- **Acceptance:**
  - Property: in every behaviour, for each (signal, purpose) the number of broker place attempts after a rejection is ≤ 1, and every rejection appends `order.rejected` and raises a health alert. Harm: a retry storm or a silent rejection.
  - Property: for every sequence of broker replies, a rejected entry raises the `order.rejected` alert and is sent once more under the next tag; a second rejection ends the signal Rejected (`order_rejected`) with one `entry.not_placed` alert, and no third order is sent. Harm: a rejected order is re-sent without limit, or a failure is silent.

## §3 Confirm mode, approval, expiry and max-chase re-check

Confirm mode sends a Telegram approval card with Take/Skip buttons; price, size and stop are fixed by the engine. Token rules (signed, single-use, tied to signal id and expiry) are in 08-telegram (HAT-TG).

#### HAT-EXEC-030: Approval expires after 5 minutes
- **Statement:** An approval not decided within 5 minutes of `sent_at` expires and no order is placed.
- **Source:** MA "Execution modes" ("Confirm expires. Default window 5 minutes"); SCH `hat_approval`.
- **PR:** 15
- **Verification:** TLA `ApprovalToken!NoTakeAfterExpiry`
- **Acceptance:**
  - Property: in every behaviour, no Take decided at or after `expires_at = sent_at + 5 min` produces an order, and every undecided approval reaches decision `expired` with `signal.expired`. Harm: a stale approval opens a position at a price the user never saw.
- **Implementation:** a Take or Skip at or after `expires_at` is refused and expires the approval in that transaction; the expiry sweep (`router.expire`, wired by PR 17) expires every other undecided approval past its window.

#### HAT-EXEC-031: Max-chase re-check on take
- **Statement:** On Take, the price is re-checked; if it has run more than the bot's max-chase % past the trigger, the order is refused.
- **Source:** MA "Execution modes"; BL "Criteria library" (`max_chase` M 5, Q 2); B25.
- **PR:** 15
- **Verification:** TLA `ApprovalToken!ChaseRecheckOnTake`, PBT `caps.max_chase_always`, PBT `router.take_rechecks_max_chase`
- **Acceptance:**
  - Property: in every behaviour, a Take produces an order only if price_at_decision ≤ trigger_level × (1 + max_chase_pct/100) and the instrument is not locked at upper circuit; otherwise the decision is recorded and the order refused. Harm: the user approves an entry that has already run away.
- **Implementation:** `max_chase_pct` is the bot's effective percentage, fixed at the trigger in the `signal.triggered` payload; the re-check runs the same `max_chase` criterion as the entry filter (`LiveContext.for_entry`, HAT-CRIT-122). A refused Take ends the signal Rejected (`signal.rejected`, reason `max_chase`, stage `approval`).

#### HAT-EXEC-032: Skips continue as shadow trades
- **Statement:** A skipped or expired confirm signal continues as a virtual (shadow) trade for veto scoring.
- **Source:** BL "Your vetoes are measured"; SCH `hat_approval` notes.
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!SkipBecomesShadow`, PBT `router.skip_and_expiry_become_one_veto_trade`
- **Acceptance:**
  - Property: in every behaviour, every approval decided skip or expired is followed by exactly one virtual position for that signal and no real order. Harm: veto scoring is biased by missing skipped trades.
  - Property: the veto trade holds none of the bot's slots, its stop-out is not one of the bot's, and it is in neither the sleeve's real nor its shadow trade results; it feeds the veto score only. Harm: a skip blocks or shrinks the bot's next real entries, or bends its health and promotion evidence.
- **Implementation:** the veto trade is opened in the transaction that records the skip or the expiry, at the card's entry price, quantity and stop. Its events carry `veto: true` and move only the position; its replay starts at Triggered (`trades.replay_veto_state`).

#### HAT-EXEC-033: Exits never ask
- **Statement:** Exits, stop moves and partials are automatic in confirm and auto and virtual in shadow; no exit waits for the user.
- **Source:** MA "Execution modes" ("Exits never ask"); BL "Evaluation rules".
- **PR:** 15
- **Verification:** TLA `ExecutionRouter!ExitsNeverAsk`
- **Acceptance:**
  - Property: in every reachable state, no exit or stop action creates an approval, and every fired exit is eventually sent (real modes) or virtually filled (shadow). Harm: an exit stalls waiting for a human while the price falls.

#### HAT-EXEC-034: Catalyst required in confirm for EP
- **Statement:** For the episodic-pivot playbook, `catalyst_attached` is required in confirm mode and informational in auto, as its `required_in: [confirm]` says (HAT-CRIT-118).
- **Source:** BL "Qullamaggie-style bot" (`required_in: [confirm]`); DS "Announcements for EP catalysts".
- **PR:** 15
- **Verification:** PBT `criteria.catalyst_required_in_confirm`
- **Acceptance:**
  - Property: for every EP signal, in confirm mode an approval is sent only when an announcement for the instrument was published within the criterion's `within_hours` (24 in the seed); in shadow and auto mode the absence of one does not block the trigger unless `required_in` lists that mode. Harm: the user is asked to approve an EP without the catalyst they judge it by.

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
| Watchlisted | Expired | (session end, no event) | the trade date's session ended and the signal never triggered; the watchlist row's status becomes Expired |
| Triggered | Rejected | `signal.rejected` | risk decision REJECT or entry filter fails; or (auto) the router could not place the entry: a refusal on the order path, an unreachable broker, a second broker rejection, a refused live condition (reason fixed, stage `route`) |
| Triggered | AwaitingApproval | (approval sent) | mode confirm; the `hat_approval` row, written in the same transaction, stands for this step on replay |
| Triggered | EntryPlaced | `order.placed` | mode auto, or shadow (virtual order) |
| AwaitingApproval | EntryPlaced | `signal.approved`, then `order.placed` | Take within expiry and chase re-check passes |
| AwaitingApproval | Expired | `signal.expired` | no decision within 5 minutes |
| AwaitingApproval | Cancelled | `signal.skipped` or `kill.tripped` | Skip (reason skipped) or `/kill`. On skip (and likewise on expiry, HAT-EXEC-032) the engine opens a separate virtual trade for the same signal, starting at EntryPlaced with a virtual order, which then follows this table to its own ending (HAT-EXEC-032) |
| AwaitingApproval | Rejected | `signal.rejected` | chase re-check or a live condition fails on Take, or the passed Take could not be placed |
| EntryPlaced | Open | `order.filled`, `position.opened` | entry fill |
| EntryPlaced | Rejected | `order.rejected` | broker rejection after the single retry: only an `order.rejected` whose payload has `final: true` drives this step; one a retry may still follow carries none |
| EntryPlaced | Cancelled | `order.cancelled` | entry order unfilled at session end |
| Open | PartiallyExited | `exit.partial` | a partial exit fills |
| Open, PartiallyExited | ExitPending | (exit order placed) | full exit rule fires or stop triggers |
| PartiallyExited | PartiallyExited | `exit.partial` | another partial fills |
| ExitPending | Closed | `order.filled`, `position.closed` | remaining qty filled |

How the real (broker) side drives these rows (PR 15c, `engine/live_trades.py`, `execution/fills.py`):

- The entry's first fill moves EntryPlaced to Open; a later fill of the same order adds to the position at the weighted average price. The entry is a DAY limit order: a remainder still unfilled at the close expires at the broker and the filled part is kept as the position. Once the position holds no shares (a stop fill, an exit or a flatten sold them), or before a sale of every share, HAT asks the broker to cancel the remainder (`StopDesk.cancel_entry`, once per entry, again only after the broker could not be reached); the sale never waits for it. A position its sales emptied stays open at quantity 0 (ExitPending) until the broker's book shows the entry ended (an entry sent on an earlier trade date counts as ended: its DAY limit expired at that close), then closes at its last sale's price, rule and time (`live_trades.close_if_emptied`); a part of the remainder that fills first adds to it (the average entry weighted by the shares bought), is protected like any entry fill, and fires again the rules of the exit sale that emptied it, so the next evaluation sells it too (audit 2026-10-22 F2). Only an entry order that ended with nothing filled drives EntryPlaced → Cancelled; an `order.cancelled` of a stop or a sale moves no state. **[OWNER-PENDING]** (partial-entry policy; this is the default).
- No sale is placed for a broker stop: the first fill of a stop (or any fill that empties a position no full exit order was placed for) carries `exit_triggered: true` on its `order.filled`, which stands for the "exit order placed" step (ExitPending) on replay.
- An entry the exchange rejects after the broker took it (nothing filled) records a final `order.rejected` and ends its signal Rejected.
- An entry HAT may have sent (pending or unconfirmed) that the broker's book does not hold at the session end, after a good reconciliation, ends its signal Rejected with reason `entry_not_found`; one sent less than the settle wait ago (HAT-EXEC-065) is left for the next run, since it may still be on its way. An entry first found by its tag already cancelled, expired or rejected with nothing filled (its signal never reached EntryPlaced) records a non-final `order.rejected` carrying the broker status, and its signal ends Rejected with reason `entry_not_filled` when it is adopted; one adopted while its signal is in EntryPlaced ends as a broker update would end it (Cancelled, or Rejected by a final `order.rejected`). One that filled in part before it ended is adopted as placed, and its fills open the position.

#### HAT-EXEC-040: Lifecycle states and transitions
- **Statement:** A trade's state is one of the 11 states above and changes only along the transition table.
- **Source:** BL "Trade lifecycle"; SCH "Event types (MVP)".
- **PR:** 11
- **Verification:** TLA `TradeLifecycle!TypeOK`, TLA `TradeLifecycle!OnlyAllowedTransitions`, CONF `lifecycle_transitions_match_spec`
- **Acceptance:**
  - Property: in every reachable state, every trade is in exactly one of the 11 states, and every step changes it only along a row of the transition table. Harm: a trade reaches an impossible state (for example Open without an entry fill) and is mismanaged.
  - Property: the states and transitions the code holds equal the table above, row for row. Harm: the machine that runs is not the machine the owner approved.

#### HAT-EXEC-041: Endings are terminal and every trade ends
- **Statement:** Closed, Rejected, Expired and Cancelled are terminal; every trade that has not opened (Watchlisted, Triggered, AwaitingApproval, EntryPlaced) eventually reaches an ending or Open. An open trade has no deadline: it ends only when an exit rule or the stop fires.
- **Source:** BL "Trade lifecycle".
- **PR:** 11, 15
- **Verification:** TLA `TradeLifecycle!EndingsTerminal`, TLA `TradeLifecycle!EventuallyEnds`, PBT `manage.entry_ended_by_broker`
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

#### HAT-EXEC-045: One bot's entries are evaluated and written one at a time
- **Statement:** A bot's risk evaluation, its signal and its entry order are written in one transaction that holds a per-bot lock, so two concurrent triggers can never both take the last position slot; every bot's signals, from every playbook, count against the same caps.
- **Source:** CodeAnt review of the risk PR (#88); HAT-RISK-031, HAT-RISK-100; HAT-BOT-050; BL "Trade lifecycle".
- **PR:** 11
- **Verification:** TLA `TradeLifecycle!PerBotCapHolds`, DB `runtime.per_bot_serialised`, PBT `trades.shared_bot_cap`
- **Acceptance:**
  - Property: in every reachable state, the number of one bot's trades that hold a slot (EntryPlaced, Open, PartiallyExited, ExitPending) is at most the position cap. Harm: two simultaneous triggers each see one free slot and the bot holds more positions than its cap, and so more open risk than its budget.
  - Property: for two concurrent evaluations of one bot with one slot left, exactly one is accepted and the other rejected with `max_positions`; with the lock removed (broken variant) both are accepted. Harm: as above.
  - Property: for every sequence of triggers across a bot's playbooks, the accepted entries never exceed the cap, whichever playbook they come from. Harm: two playbooks double the bot's risk budget.

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
- **Statement:** Stop = day's low at entry minus 1 tick (buffer ticks default 1), rounded down to the tick grid. The day's low exists only for a session watched from the open with no feed gap (HAT-DATA-114); without it the entry skips with `stop_input_missing`.
- **Source:** BL "Stop rules"; BL "Qullamaggie-style bot" (`stop: {rule: low_of_day}`).
- **PR:** 11, 22
- **Verification:** PBT `stops.low_of_day`, PBT `bars.trigger_never_uses_a_partial_session`
- **Acceptance:**
  - Property: for every intraday bar sequence up to the entry bar whose day low exists (HAT-DATA-114; else the entry skips with `stop_input_missing`) and every tick size, the stop equals min(low of all bars that day up to and including entry) − buffer_ticks × tick, and is strictly below the entry price. Harm: a stop above the session low triggers on normal noise.

#### HAT-EXEC-052: Custom stop rules
- **Statement:** `pct_below_entry`, `atr_multiple` and `swing_low` compute entry × (1 − %/100), entry − multiple × ATR(days), and the lowest low over lookback days respectively.
- **Source:** BL "Stop rules".
- **PR:** 11
- **Verification:** PBT `stops.custom_rules`
- **Acceptance:**
  - Property: for every valid parameter set and bar history, each rule returns its formula's value and every result is strictly below entry; a result ≤ 0 or ≥ entry yields SKIP. Harm: a malformed stop sizes an unbounded position.

#### HAT-EXEC-053: Stops are priced on the instrument's real tick
- **Statement:** Every stop rounds to the instrument's NSE tick for the trade date. The tick comes from the price slabs NSE has used since 15 April 2025 (below ₹250: 0.01; ₹250 to ₹1,000: 0.05; to ₹5,000: 0.10; to ₹10,000: 0.50; to ₹20,000: 1.00; above: 5.00), applied to the close of the last trading day of the month before the trade date. There is no default tick: a stop with no known tick is skipped. The live code reads only the two closes this can pick per instrument (the last before the month start and the latest), and gives the same tick as reading every close.
- **Source:** NSE circulars on price-linked tick sizes (June 2024 below ₹250; April 2025 six slabs; monthly review on the previous month's last close); BL "Stop rules"; PR 11 follow-up (stops used a fixed 0.05).
- **PR:** 15
- **Verification:** PBT `stops.tick_size_slabs`, PBT `ops.daily_tick_matches_reference`
- **Acceptance:**
  - Property: for every price p > 0 the tick is the slab's (0.01 iff p < 250; 0.05 iff 250 <= p <= 1000; and so on), it is monotone non-decreasing in p, and for every daily-close history the tick of a trade date depends only on the last close before the first day of that date's month (the latest earlier close when there is none). Harm: an off-grid stop is refused by the exchange, or a stop on a 0.01-tick stock sits up to 4 paise looser than the rule.
- **[OWNER-PENDING]** a stock listed in the current month: NSE fixes its tick from the issue price, which HAT does not hold; default = its latest close before the trade date.

## §6 Stop placement at the broker

1. On fill: intraday stop order at the stop price (expires at day end).
2. At ~15:25: replace it with a GTT stop for overnight, refreshed nightly.
3. GTT limit price set 1% below trigger so moderate gap-downs still fill [A]. A gap below the limit price leaves the GTT unfilled; the offset reduces that risk but cannot remove it, so an open position whose GTT triggered without a fill raises a health alert.
4. A broker without reliable GTT for delivery cannot run auto mode (HAT-EXEC-012).

Implementation (PR 15c; `execution/protect.py` `StopDesk`, `execution/fills.py` `FillSync`, `execution/manage.py` `TradeManager`, `ops/trade_jobs.py`; model `verification/tla/StopPlacement.tla`):

- The fill sync reads the broker's order and trade books (fail closed) and protects a position right after recording each of its fills, before any other action on it. With no working stop it places an intraday SL-M SELL CNC stop in the session before 15:25 and a GTT from 15:25 on and after the close (a day stop placed after the swap would expire with no GTT behind it); a working stop for another quantity is resized. A stop the broker refuses after the one retry is alerted (`stop.not_placed`) and placed again by the next sync, which protects every open real position HAT's book shows without a working stop; a resize the broker refuses is alerted (`stop.not_moved`) and sent again by the next sync, which also resizes every placed stop whose quantity differs from the position's. While a sale the broker accepted is still working (placed or part filled), no stop is placed or grown over it: `make_room` gave the sale the shares, and the position is protected again once the sale ends. A sale whose answer was lost does not hold protection back.
- A broker stop's trigger is `current_stop` rounded down to the instrument's tick, so it is never above `current_stop`; `follow` modifies it up and never down, and a refused modify is alerted (`stop.not_moved`).
- `gtt.swap` (15:25, retried every minute until it succeeds) places the GTT first and cancels the intraday stop only once the GTT is confirmed; a failure leaves the intraday stop working and is alerted (`gtt.swap_failed`). An intraday stop the broker did not confirm cancelled fails the run, so the cancel is tried again (two resting stops would sell the same shares twice). A position with a sale still working is swapped on the retry.
- `gtt.verify` (23:00) reads the fills first (when they cannot be read the open quantities may be stale: each position is alerted `gtt.unverified` and no GTT is modified or placed), then refreshes each open real position's GTT to the current stop (`gtt.refreshed`). A GTT whose refresh the broker refuses is cancelled, then alerted (`gtt.missing`) and placed again; a position with no GTT in HAT's book is alerted and gets one. A GTT is never placed on top of one that may still rest at the broker: when the broker cannot be reached, the refused GTT cannot be cancelled, a GTT's placement answer was lost (GTTs carry no tag to look up), or no tick is known, the position is alerted (`gtt.unverified`) and nothing is placed. A GTT whose placement answer was lost is settled first against the broker's GTT book (HAT-EXEC-065), so it blocks a new GTT only while the book cannot be read or the answer is younger than the settle wait.
- Fyers GTTs carry no order tag, so an untagged SELL trade on the symbol of the one open real position with a working GTT is attributed to that GTT, and an untagged SELL order on it that ended short of its quantity is the GTT triggering without a fill (`gtt.unfilled`, then protected again). A symbol two open positions share is not attributed. **[verify on a live account]**
- An exit sale first shrinks the position's resting stops to the shares that stay (cancels them for a full exit), since a broker holds the shares a resting CNC sell covers; a stop that cannot be changed holds the sale back (`exit.not_placed`, reason `stop_not_moved`), and a sale that is refused or rejected twice has the position protected again at once. A sale rejected twice in a session is not tried again that session. A sale the broker accepted and later rejected, cancelled or expired short of its quantity (seen by the fill sync) records `order.rejected` (reason `exchange_rejected`), un-fires the rules it carried (`hat_order.rule_ids`), so the next evaluation sends it again, and counts toward the two rejections; the second raises `exit.not_placed` (reason `order_rejected`) (HAT-EXEC-075).
- `hard_stop` is never sold by HAT while a broker-confirmed stop rests (working, with a broker id): the broker stop is the hard stop. A stop sent with its answer lost is settled first (HAT-EXEC-065); one still unsettled does not count, and HAT sells at market (a stop that did land makes the broker refuse the duplicate CNC sale, which is alerted and tried again). A position whose stop the broker keeps refusing and that has not hit its stop is alerted, not sold at market.
- A lost broker answer never freezes a position (HAT-EXEC-065). A stop whose placement answer was lost is settled once it is older than the settle wait (60 s, `protect.LOST_ANSWER_SETTLE`, so an SDK request still in flight is not mistaken for absent): a day stop by its tag in the order book, a GTT in the broker's GTT book (`gtts()`: the single-leg CNC GTTs that rest or have triggered, so a cancelled, rejected or expired one, an OCO or another product is left out, a triggered one is adopted and its sale attributed by the fill sync, and a single-leg CNC row with a status code the adapter does not know makes the book unreadable; matched by symbol, side, quantity and trigger, never one another row already owns). Found, the row is adopted (placed, with its broker id); absent, it ends `expired` and the position is protected again. An answer older than the settle wait that could not be settled is alerted; a younger one is not. A sale whose answer was lost is settled the same way by its tag before an exit decides whether to send; a reserved sale that was never sent is cancelled; one found with shares sold is left to the fill sync, which records its fills with it, and no other sale goes out meanwhile; one of an earlier session that today's book does not hold may have sold, so it is left unsure, nothing more is sold and the owner is alerted (`exit.not_placed`, reason `sale_unresolved`) while the broker stop keeps protecting the position. **[OWNER-PENDING]** how such a sale is resolved; default: by hand after reconciliation. A position closed while one of its stops was still unsure has that stop settled and cancelled by the next fill sync, so no stop rests under shares already sold. Two senders of one reserved, never-sent order (the fill sync and a stop job) never both send it: `send_order` stamps every attempt with one conditional update on the attempt and status it read, and the sender that loses sends nothing, as does a sender whose order the fill sync adopted from the book since its lookup; each attempt's own time starts its settle wait. While a book cannot be read the row stays unsure: no stop is placed on top of it (`stop.not_placed`, reason `gtt_unconfirmed` or `stop_unconfirmed`) and no sale is resent (`exit.not_placed`, reason `sale_unresolved`); an unsettled stop never holds an exit sale back. `FyersBroker` answers a place it never sent because its circuit breaker opened as a definite rejection, not a lost answer.
- Nothing in stop placement checks a kill switch or pause (06 §6).

#### HAT-EXEC-060: Stop at the broker on every real fill
- **Statement:** Every real entry fill is followed by a broker stop at the position's stop price: an intraday stop order (purpose `stop_day`) in the session before the 15:25 swap, a GTT stop (purpose `stop_gtt`) from then on and after the close; this includes a fill of the entry's remainder after the position's sales emptied it.
- **Source:** BL "Stop placement at the broker (B17)" step 1; MA "Execution modes" ("Protective stops sit at the broker on every real fill"); B17; audit 2026-10-22 tests-hat F2.
- **PR:** 15, 20
- **Verification:** TLA `StopPlacement!StopAfterFill`, TLA `StopPlacement!SharesTracked`, TLA `StopPlacement!RestCancelledOnExit`, TLA `StopPlacement!EmptiedCloses`, PBT `manage.real_fill_protected`, PBT `manage.a_resting_sale_is_never_stopped_over`, PBT `manage.entry_remainder_tracked`
- **Acceptance:**
  - Property: in every behaviour, after a real entry fill, a working broker stop for the open quantity exists before any other action on that position, and from then on there is always a working stop (day or GTT) while qty_open > 0. Harm: a position sits unprotected if the machine dies.
  - Property: in every interleaving of part fills of the entry with stop fills, exits and flattens, HAT's open quantity equals the shares the broker holds and its live stops cover them; once a sale empties the position the entry's remainder is asked to be cancelled, and the position closes only when no entry order still works. Harm: shares the broker bought after HAT closed the position sit untracked and unprotected.

#### HAT-EXEC-061: Intraday stop swapped to GTT at ~15:25
- **Statement:** At ~15:25 each intraday stop is replaced by a GTT stop (purpose `stop_gtt`) for overnight protection, without a gap in coverage.
- **Source:** BL "Stop placement at the broker" step 2; MA "Daily cycle"; SCH events `gtt.placed`.
- **PR:** 15
- **Verification:** TLA `StopPlacement!NoUnprotectedGap`, PBT `manage.swap_leaves_no_gap`
- **Acceptance:**
  - Property: in every interleaving of the swap steps with fills, rejections and crashes, the GTT is confirmed placed before the intraday stop is cancelled, so at no state is a real open position without a working stop; a failed GTT placement leaves the intraday stop working and raises an alert. Harm: the position goes overnight unprotected.

#### HAT-EXEC-062: GTT limit offset
- **Statement:** The GTT limit price is set 1% [A] below its trigger price.
- **Source:** BL "Stop placement" step 3; BL "Defaults that are assumptions" (GTT limit offset).
- **PR:** 13, 15
- **Verification:** PBT `stops.gtt_limit_offset`, TLA `StopPlacement!GttUnfilledAlerted`, PBT `manage.gtt_trigger_handled`
- **Acceptance:**
  - Property: for every trigger price T > 0 and tick size, the GTT limit equals T × (1 − 0.01) rounded down to the tick and is < T. Harm: a gap-down skips past the limit and the stop never fills.
  - Property: in every behaviour where a GTT triggers and is not filled, a health alert is raised for that position. Harm: the owner believes the position is protected after the stop failed to fill.
- **[A]** GTT limit offset 1% below trigger: validated by adapter tests per broker and observed fills.

#### HAT-EXEC-063: Nightly GTT verify
- **Statement:** Every night the engine confirms a GTT stop exists at the broker for every open real position and alerts on any gap.
- **Source:** MA "Daily cycle" step 9; BL "Stop placement" step 2 (refreshed nightly); SCH event `gtt.refreshed`.
- **PR:** 15
- **Verification:** TLA `StopPlacement!NightlyVerifyCovers`, PBT `manage.verify_covers`
- **Acceptance:**
  - Property: in every behaviour, after the nightly `gtt.verify` job, every open real position either has a broker GTT whose trigger equals `current_stop`, or a health alert was raised and a re-placement attempted. Harm: a missing GTT goes unnoticed for days.

#### HAT-EXEC-064: Stop moves follow the stop
- **Statement:** When `current_stop` rises, the working broker stop (day or GTT) is modified to the new price; it is never modified downward, by a follow or by the nightly verify (a tick that coarsened since the stop last moved leaves the resting trigger and limit where they are; a refused refresh of such a GTT is alerted `gtt.unverified` and nothing is placed, since a new GTT would rest lower).
- **Source:** BL "Invariants" 1; SCH `stop.moved`; DS "Broker interface" (`modify_order`, `modify_gtt`); audit 2026-10-22 tests-hat F24.
- **PR:** 15, 20
- **Verification:** TLA `StopPlacement!BrokerStopMatchesCurrent`, TLA `StopPlacement!StopOnlyTightens`, PBT `manage.broker_stop_only_tightens`, PBT `manage.verify_covers`
- **Acceptance:**
  - Property: in every reachable state, the broker stop price for an open position is ≤ `current_stop` and eventually equals it, and the sequence of broker stop prices is non-decreasing. Harm: the broker stop lags a raised stop and gives back profit, or is lowered.
  - Property: for every pair of ticks, a GTT the nightly verify refreshes keeps a trigger at least the one it rested at. Harm: a coarser tick lowers the stop by less than a tick each night.

#### HAT-EXEC-065: A lost broker answer never freezes a position
- **Statement:** A stop or sale whose placement answer was lost is settled against the broker's books (GTT book or tag) and adopted or ended, so an open real position always ends with a working stop or a sale it can send.
- **Source:** Audit 2026-10-22 money-safety-execution F1, F2; trading-safety skill rule 3 (resolve a lost answer before any retry); BL "Invariants" 1.
- **PR:** 18
- **Verification:** TLA `StopPlacement!LostGttSettled`, TLA `StopPlacement!LostNeverBlocksSale`, PBT `manage.lost_answers_never_freeze`, PBT `manage.a_settled_answer_never_sells_twice`, PBT `manage.the_close_ends_only_settled_answers`, PBT `tags.the_gtt_book_reads_only_resting_stops`, PBT `tags.a_reserved_tag_is_sent_by_one_sender`
- **Acceptance:**
  - Property: in every behaviour where a GTT placement answer is lost, the row is eventually settled (adopted when the GTT rests at the broker, ended and replaced when it does not) while the GTT book can be read, and no second GTT is placed while it is unsure. Harm: a position goes nights without a stop HAT will place, or two GTTs sell the same shares twice.
  - Property: for every sequence of lost, landed, refused and unreadable answers on stops and sales, an exit or hard stop due on an open real position is sent once the books can be read, an unsure stop never holds it back, and a sale already resting is never sent twice. Harm: a stop-loss exit is frozen while the price falls, or the position is oversold.
  - Property: settling never leaves a second seller: a stop that landed under a position since closed is cancelled once settled, a lost sale that sold is not followed by another before its fills are recorded, an earlier session's lost sale holds further sales instead of firing its rules again, only GTTs that rest or triggered are matched, one attempt of an order is sent by one sender, and the session end writes off only an order the book does not hold and that is older than the settle wait. Harm: a GTT rests under shares already sold, or the same shares are sold twice.

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
- **PR:** 15, 17
- **Verification:** TLA `JobScheduler!ExitsCloseInWindow`, PBT `manage.no_sale_after_close`, PBT `ops.window_not_starved`
- **Acceptance:**
  - Property: in every simulated trading day, the `exits.close` job runs once within 15:20–15:25 IST and its exit orders are sent before 15:30. Harm: MA exits miss the session and the position carries overnight against the rule.
- **[A]** exit at 15:25 vs official close: default 15:25 live price; validated in shadow by logging both and comparing (PLAN Q10).
- **Implementation (PR 15c):** `exits.close` is a scheduled job with a window (`ScheduledJob.until`): the scheduler starts it at or after 15:20 and never from 15:25 IST on, and a failed run is tried again a minute later inside the window. Its body (`TradeManager.close_exits`) refuses outside the window, runs the close-phase rules of every open position of the account's bots (virtual through `trades.evaluate_position`, real through the same `exits.evaluate`), records each `close_below_sma` evaluation with the live price (`exit.close_evaluated`, HAT-OPS-034), and sends each sale as a MARKET SELL CNC order. Whether a sale may go out is read at the moment it is sent, from the injected clock or else as the run's start time plus the monotonic time the run has taken, so nothing is sent from 15:30 on even by a run that started at 15:24.

#### HAT-EXEC-075: An exit the exchange rejects is retried once
- **Statement:** A sale the broker accepted and later rejected (or cancelled or expired before selling everything) records `order.rejected`, un-fires its exit rules so the sale is sent again, and the second rejection in a session raises `exit.not_placed`.
- **Source:** Audit 2026-10-22 money-safety-execution F3; BL "Evaluation rules" (exits never wait for the user); §6.
- **PR:** 18
- **Verification:** PBT `manage.async_sale_rejection_retried`
- **Acceptance:**
  - Property: for every exit rule and every asynchronous rejection of its sale, `order.rejected` is recorded, the rule is no longer in `rules_fired`, the next evaluation sends one new sale, and a second rejection raises `exit.not_placed` and sends no third sale that session. Harm: a rule marked fired on a sale that never sold leaves the position open against its plan with no alert.

## §8 Broker interface

HAT's own contract (`app/hat/execution/broker.py`, protocol `HatBroker`); I7. No adapter-specific type leaks into the engine.

| Method | Purpose |
| --- | --- |
| `capabilities()` | GTT for delivery, order-tag field and length, order types, static-IP requirement |
| `login_url()` / `complete_login(code)` / `session_status()` | Daily login via Telegram link |
| `place_order(intent)` → broker order ID | Entry/exit; `intent` carries instrument, side, qty, product (CNC), type, price, trigger, tag |
| `modify_order`, `cancel_order` | Stop moves, expiries |
| `place_gtt_stop`, `modify_gtt`, `cancel_gtt` | Overnight protective stops |
| `gtts()` | The GTT book: settles a GTT whose placement answer was lost (HAT-EXEC-065) |
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
- **PR:** 13, 18
- **Verification:** PBT `tags.broker_errors_fail_closed`, PBT `tags.a_keyless_book_never_reads_as_absent`, PBT `fyers_broker.an_ok_reply_without_its_list_is_unreadable`, PBT `fyers_broker.a_circuit_opening_mid_place_is_not_sent`
- **Acceptance:**
  - Property: for every injected broker failure (timeout, circuit open, error payload, an ok reply without its list), `orders/trades/positions/holdings/gtts/funds` raise `BrokerUnavailableError` and never return an empty list, and the surfaced message is a fixed code without the raw error string. Harm: reconciliation reads "no positions" and the engine re-enters or abandons stops; secrets or account data leak.
  - Property: for every place, a circuit breaker that opens between the caller's check and the send answers a definite rejection, never a lost answer. Harm: an order that never left is chased as unconfirmed and its position frozen.

#### HAT-BRK-082: Broker session handling
- **Statement:** Session drops are retried with backoff, alerted after 2 minutes, and no entry is placed while the session is not live; `hat_broker_session` tracks the morning login. A token the broker itself refuses is an expired session, not an outage: for Fyers, code -16 ("Could not authenticate the user", "Token is Invalid or Expired") on any read raises `FyersSessionExpiredError`, the read fails with `broker_session_expired`, and `session_status` (one profile read per token per 30 s when the token file is in date, the window ended at once by a -16 on any read) reports `expired`, so the outage watch alerts and the 09:05 prompt asks for a login; any other failed read is an outage (`broker_unavailable`) and leaves the status as the token file says. The login reads the user id (`fy_id`) from the account's profile (at most 5 s, the reply checked as every profile read), since the token reply carries none; the login status reports it, or none when the profile could not be read. [Changed in PR 23 after the live checks of 2026-10-22: -16 read as an outage and a revoked token read live.]
- **Source:** MA "Failure handling" (Broker session drops); MA "Daily cycle" step 1; SCH `hat_broker_session`; T8, T14; live checks 2026-10-22.
- **PR:** 13, 16, 23
- **Verification:** TLA `ExecutionRouter!NoEntryWithoutSession`, PBT `broker.token_refused_is_session_expired`, PBT `broker.token_refused_on_a_read_ends_the_check`, PBT `fyers_auth.good_exchange_order`
- **Acceptance:**
  - Property: in every reachable state, no real entry order is placed while the account's session status is not live; and if the session stays lost for 2 minutes, `broker.session_lost` and a health alert are eventually emitted. Harm: orders fail mid-flight or the user is not told the bot is blind.
  - Property: for every broker reply of code -16 the session status is `expired` and every read fails with `broker_session_expired`, and for every other failure the status is unchanged and reads fail with `broker_unavailable`. Harm: a revoked token reads as an outage and nobody is asked to log in.

## §9 Fyers adapter

The MVP adapter (`app/hat/execution/fyers_adapter.py`) wraps HAT's `FyersBroker` in `fyers_broker.py` (place/modify/cancel, GTT place/modify/cancel, positions, funds, order and trade books; per-instance circuit breaker, guarded timeout, `SUBMIT_UNCONFIRMED` for a lost, garbled or id-less reply, no non-finite price sent), `FyersSession` (`fyers_session.py`) and `FyersAuthService` (`fyers_auth.py`, routes under `/api/hat/auth/fyers`). Tag lookup is the adapter's own (`orders()` filtered by tag), not a broker method. Fyers secrets are read only by `fyers_auth.py`, `fyers_tokens.py` and `fyers_session.py` (PLAN C5). [Reworded in K2; it wrapped the legacy `FyersBroker`, `FyersClient` and auth flow.]

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
- **Verification:** TLA `OrderReconciliation!ReconcileBeforeFirstOrder`, PBT `router.reconcile_before_first_order`, PBT `router.routine_reconcile_keeps_gate`
- **Acceptance:**
  - Property: in every behaviour, no place call happens in a session before a successful reconciliation of that session. Harm: after a crash the engine re-enters a position it already holds or loses track of an open stop.
  - Property: the router's first real entry of a session runs the reconciliation first; a broker that cannot be read, or a book holding an HT order HAT does not know, places nothing and ends the entry Rejected with an `entry.not_placed` alert. Harm: an order goes out on top of a book HAT has not matched.
  - Property: a routine reconciliation (the periodic one every 5 minutes) never closes the gate a successful one opened; only the first reconciliation of the process or of a new trade date starts a new session that needs one. Harm: an entry placed while a routine pass runs is refused as "not reconciled", or waits for a pass that is already done.
- **Implementation:** mismatch alerts reach the user through the `Notifier` (`reconcile.notifier_sink`: one `reconcile.mismatch` health alert per new mismatch, carrying its kind, its tag or symbol and the bot, or `account` when the bot cannot be told; Telegram from PR 16, the log until then).

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
- **Statement:** Trigger criteria evaluate only on final 5-minute bars and only between 09:20 and 15:20 IST. A trigger whose signal write fails is evaluated again on the next final bar; one whose signal is stored never fires again that day.
- **Source:** MA "Daily cycle" steps 4–5; DS "Live data" (09:20–15:20 triggers); B8, B9.
- **PR:** 15
- **Verification:** PBT `bars.trigger_only_on_final_bars`
- **Acceptance:**
  - Property: for every tick stream, `signal.triggered` timestamps lie in [09:20, 15:20] IST and each is derived from a bar marked final (close + 2 s). Harm: triggers fire on half-built bars or in the auction.

#### HAT-EXEC-121: All trigger criteria must pass
- **Statement:** A playbook triggers only when every enabled trigger criterion passes on the same bar (price breaks level and volume pace, plus EP gap, early volume and catalyst rules). `catalyst_attached` counts only in the modes its `required_in` lists (HAT-CRIT-118); a playbook with no blocking trigger criterion (none enabled, or only a `catalyst_attached` that does not block in the bot's mode) never triggers. A trigger criterion over daily bars (`neglect`) is evaluated once on the bars before the trade date when the candidate is built, and fails closed without them.
- **Source:** BL "Configuration model" stage 4; BL default configs.
- **PR:** 15
- **Verification:** PBT `criteria.trigger_conjunction`
- **Acceptance:**
  - Property: for every bar and set of enabled trigger criteria, the playbook triggers iff at least one of them is blocking and the conjunction of the blocking ones is true, and a triggered signal records each criterion's value. Harm: entries on half the setup.

#### HAT-EXEC-122: Entry filters skip with reason
- **Statement:** At trigger, the initial stop is computed (HAT-EXEC-050..053), then the enabled entry filters run in their configured order with `max_chase` always among them (HAT-CRIT-122); the first failure records a skip reason and no order is routed. The reason is the failing filter's type (`adtr_filter`, `max_chase`) or the stop rule's skip reason, and the signal is stored Rejected with it before any risk check.
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
- **Implementation:** `engine/approvals.py:veto_score` (exact Decimal sums) over `skipped_results`, the R multiples of the veto trades of skipped or expired approvals closed in the week (PR 15); the weekly digest that reports them is PR 16.

#### HAT-EXEC-131: Veto verdict after 50 decisions
- **Statement:** After 50 confirm decisions (take or skip), the digest states whether skips beat takes on R, as evidence for moving to auto.
- **Source:** BL "Your vetoes are measured" ("After 50 decisions").
- **PR:** 16
- **Verification:** PBT `exits.veto_verdict_threshold`
- **Acceptance:**
  - Property: for every decision history, the verdict is present iff the count of decided approvals is ≥ 50, and it compares mean R of skipped trades with mean R of taken trades; when either group has no closed trade, that mean is reported as not available and the verdict is "insufficient data". Harm: a verdict drawn from too few decisions, or a division by zero when every decision went one way.

## §14 Corporate actions on open positions

A split or bonus with factor f (a 1:2 split is 0.5, a 1:1 bonus 0.5, a 1:2 bonus 2/3; 02 §5) puts every price after its ex-date on a new scale. A position opened before the ex-date and still open on it holds prices of the old scale: its stop sits above the new price, so without a rescale `hard_stop` sells it on the ex-date open for nothing but the action, a shadow position books a fake loss and stop-out, `close_below_sma` fires against an unadjusted SMA, and a broker stop re-placed at the old trigger is refused (audit 2026-10-22 F3). Implementation: `engine/rescale.py`.

#### HAT-EXEC-140: Next-session actions are fetched the night before
- **Statement:** The nightly `ca.adjust` step looks up `get_corporate_actions` with ex-date = the next session for every instrument with an open position or a watchlist row of the day, and stores what it finds (HAT-DATA-052) before `features` runs, so the night's features, scan and the morning's rescale see it. A failed or incomplete lookup (including one with a row that cannot be read), or one that cannot run (no MCP client, a database error), is alerted (`corporate_action.unchecked`, symbol `*` when the whole lookup failed) and never fails the step, so the nightly chain goes on.
- **Source:** audit 2026-10-22 F3.
- **PR:** 21
- **Verification:** PBT `rescale.next_session_lookup`
- **Acceptance:**
  - Property: for every set of open positions, watchlist rows and MCP answers, the symbols looked up are exactly the held and watchlisted ones, each over (next session, next session); every action returned is stored once; and every failed lookup raises one alert while the step still succeeds; with no MCP client the step succeeds and raises one alert. Harm: a split of a held name is first seen after its ex-date close, when the fake stop-out has already happened.

#### HAT-EXEC-141: Open positions are rescaled before the open
- **Statement:** `ca.rescale` (08:40, trading days) applies every stored split or bonus with factor below 1 whose ex-date is after the day a position opened and on or before today, and that the position has not been rescaled for (an `other` action such as a rights issue, or a consolidation, changes no share count HAT can know and is not rescaled): quantity ⌊q / f⌋ (a fractional entitlement is the broker's cash), average entry × f (half-even) and both stops × f (rounded down, 4 places), in one transaction with the event `position.rescaled` (`action`, `factor`, `from`, `to`). The stop guard accepts this one downward move (HAT-SCH-081). The entry quantity behind R and the partial exits is scaled the same way, so R and the exit fractions stay on one scale. Each action is applied once per position. The factor is read as the ratio with denominator up to 1000 within 1e-9 of it; a factor that is no such ratio (one sent to 4 places, 0.6667) is refused, so the position stays manage-only and is alerted rather than rescaled by a guessed ratio.
- **Source:** audit 2026-10-22 F3.
- **PR:** 21
- **Verification:** PBT `rescale.preserves_position`, PBT `rescale.applied_once`, PBT `rescale.factor_exact`, DB `governance.rescale_moves_stop`
- **Acceptance:**
  - Property: for every quantity, entry, stops and split or bonus ratio, the rescaled position keeps quantity × entry within one share's value, its open risk (entry − stop) × quantity within rounding, and the stop's distance in R, (current − initial) / (entry − initial), within rounding; `current_stop ≥ initial_stop` still holds; and on the ex-date open at the old close × f no exit rule fires (no `hard_stop`, no sale) from the action alone. Harm: the ex-date open sells the position at a fake stop or books a fake loss.
  - Property: for every sequence of `ca.rescale` runs (repeated, with actions added between), each (position, action) is applied exactly once, a position opened on or after an ex-date is never rescaled for it, and an `other` action or a consolidation is never applied. Harm: a split applied twice halves the position's prices again, or a rights issue adds shares the account does not hold.
  - Property: for every ratio n/d below 1 with d ≤ 1000, the stored 12-place factor reads back as n/d exactly, and a factor sent to 5 places or fewer that is not exactly such a ratio is refused. Harm: 0.0110 read as 11/1000 instead of 1/91 sizes the position at a tenth of its shares.

#### HAT-EXEC-142: Until rescaled, the instrument is manage-only and its exits wait
- **Statement:** While an open position has a stored action it was not rescaled for, the exits' `ViewSource` gives it no view (no exit rule runs on its old-scale prices), and any trigger of that instrument is stored skipped with reason `corporate_action_pending`. A failed rescale fails the job (retried every 5 minutes) and alerts `corporate_action.pending`.
- **Source:** audit 2026-10-22 F3.
- **PR:** 21
- **Verification:** PBT `rescale.pending_is_manage_only`
- **Acceptance:**
  - Property: for every position with a pending action and every live price, the view source returns None and a trigger of that instrument is stored Rejected with `corporate_action_pending`; once rescaled, the view returns and the trigger is decided by the risk authority. Harm: an exit or an entry is decided on prices of the wrong scale.

#### HAT-EXEC-143: The broker stop follows the rescale down
- **Statement:** After a rescale, each fill sync first moves every live broker stop of the position resting above the rescaled stop down to it (the stop rounded down to the tick), keeping the quantity the stop already covers: a GTT by `modify_gtt`, a day stop by `modify_order`. Only then does the fill sync resize the stop to the rescaled quantity, so no stop is ever larger than the old size while its trigger is on the old scale, and a refused resize (the new shares not yet in the holdings) leaves the old shares under the new stop and is tried again by the next sync. The move waits while a sale of the position is working. This is the only downward move a broker stop makes, and only for a position with a `position.rescaled` event; a refused move is alerted (`stop.not_moved`) and tried again by the next sync. *[verify: whether Fyers cancels or adjusts a resting GTT itself on an ex-date, and when the new shares reach the holdings; until they do, reconciliation sees a quantity mismatch and blocks the account's entries.]*
- **Source:** audit 2026-10-22 F3.
- **PR:** 21
- **Verification:** PBT `rescale.broker_stop_follows`
- **Acceptance:**
  - Property: for every rescaled real position and resting broker stop, after a sync every live stop rests at the rescaled trigger and quantity, no modify on the way sized the stop above the old size while its trigger was above the rescaled stop, while the broker refuses the larger size the stop rests at the rescaled trigger for the old size, and for a position never rescaled no stop is moved down. Harm: the old GTT at the pre-split trigger fires on the ex-date open and sells part of the position.

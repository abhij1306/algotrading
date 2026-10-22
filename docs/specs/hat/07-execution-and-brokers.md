# 07: Execution and Brokers

Scope: execution modes, the execution router, confirm-mode approvals, the trade lifecycle, stop rules and broker-side stop placement, exit-stack evaluation, HAT's broker interface and its Fyers adapter, order tags and idempotency, reconciliation, live trigger and entry-filter evaluation, and veto scoring. Requirement format and verification methods: see README.md and 11-verification.md.

Build decision (OWN-20OCT): orders go through HAT's own broker interface (`app/hat/execution/broker.py`), implemented in the MVP by a direct Fyers adapter over HAT's own `FyersBroker`, `FyersSession` and `FyersAuthService` (moved from the legacy modules in K2, which now inherit from them). There is no OpenAlgo in the MVP (PLAN C5); `hat_broker_account.openalgo_ref` stays as a nullable column for a later adapter.

## §1 Execution modes

Mode is a per-bot switch (`hat_bot.execution_mode`): **shadow** (virtual fills, no broker), **confirm** (Telegram Take/Skip, then a real order), **auto** (real order without asking). A new bot asks for confirm and runs shadow until its gates pass (B2, revised; HAT-EXEC-011, HAT-GOV-085). All three run the same risk check. Auto is for the owner's own accounts only (MA "Execution modes").

#### HAT-EXEC-010: Shadow never reaches the broker
- **Statement:** A bot in shadow mode never calls any broker method; its orders, fills and positions are written with `is_virtual = true`.
- **Source:** BL "Shadow replaces signal-only"; SCH "Conventions" (Virtual vs real); B1, B2 (revised).
- **PR:** 11, 15
- **Verification:** TLA `ExecutionRouter!ShadowNeverCallsBroker`, PBT `exits.shadow_rows_virtual`
- **Acceptance:**
  - Property: in every reachable state of the router, for every signal whose bot mode was shadow at the session open, the count of broker calls attributed to that signal is 0. Harm: a paper strategy places real orders.
  - Property: for every shadow signal driven end to end through the real router against `FakeBroker`, every `hat_order`, `hat_fill` and `hat_position` row it produces has `is_virtual = true` and the fake records zero calls. Harm: shadow trades contaminate real position, P&L and reconciliation.

#### HAT-EXEC-011: Default mode is confirm (revised, D130/D132)
- **Statement:** Every new bot asks for confirm mode and runs shadow until its sleeve is Live and the gates pass (HAT-EXEC-012, HAT-GOV-085); the owner switches a bot with `/mode <bot> shadow|confirm|auto`.
- **Source:** D130 ("the default is manual"), D132; BL default configs (`execution_mode: shadow`, revised); B2 (revised).
- **PR:** 8, 15, 45
- **Verification:** CONF `exec.default_mode_confirm`
- **Acceptance:**
  - Property: the `hat_bot.execution_mode` column default (model and database) is `confirm`, a new bot cannot be created in another mode, an unknown mode reads as shadow, and confirm with any gate missing reads as shadow. Harm: a new bot trades real money before the owner's gates, or the owner's chosen default is lost.
- **Note (PR 45):** the strategy config's `execution_mode` key is not read for a bot's mode; the seed YAMLs still say `shadow`. They are lane E files whose change would move the backtest golden digest; dropping the key is a follow-up.

#### HAT-EXEC-014: Auto places only high-confidence signals
- **Statement:** In auto mode an accepted signal labelled `high` confidence is placed as a real order; any other label, or none, is routed as in confirm: an approval card for the owner (D149, Q40). Until the confidence labels land (D132, plan audit PR 71) no signal carries a label, so every auto entry asks the owner.
- **Source:** D149; D132; plan audit 2026-10-22 PR 45.
- **PR:** 45
- **Verification:** PBT `router.auto_places_only_high_confidence`
- **Acceptance:**
  - Property: for every confidence value (high, medium, low, none, anything else), an accepted auto signal labelled `high` sends exactly one entry order when the live conditions read at placement allow it (§2; otherwise none and the signal Rejected with the reason), and any other label writes an approval card, moves the signal to AwaitingApproval and calls no broker. Harm: a medium- or low-confidence trade is bought without the owner's approval.

#### HAT-EXEC-012: Auto requires a broker with delivery GTT
- **Statement:** A bot cannot run in auto mode on a broker account whose capabilities lack reliable GTT for delivery (CNC); because every real fill needs an overnight broker-side stop, confirm mode is blocked on such an account too.
- **Source:** BL "Stop placement at the broker (B17)" step 4; DS "Broker matrix"; MA "Risks" (GTT support).
- **PR:** 13, 15, 25, 29
- **Verification:** TLA `ExecutionRouter!AutoNeedsGtt`, PBT `router.routes_by_mode`, PBT `router.a_stored_capability_never_enables_a_real_entry`, PBT `tags.refusals_happen_before_the_broker`
- **Acceptance:**
  - Property: in every reachable state, a bot whose broker account has `capabilities.gtt_delivery = false` is never in effective mode auto, and an auto request for it is refused. Harm: an auto position is left without an overnight stop.
  - Property: a Take whose account no longer reports GTT for delivery (or is no longer active) when the user decides places no order and ends the signal Rejected (`no_gtt_delivery`). Harm: a card sent before a capability change still buys without an overnight stop.
  - Property: in every reachable state, a bot whose broker account has `capabilities.gtt_delivery = false` is never in effective mode confirm either, so it places no real order. Harm: a confirmed entry fills and then goes overnight with no resting stop, breaking "protective stops sit at the broker on every real fill" (MA "Execution modes").
  - Property: a stored `gtt_delivery = true` never enables a real order the adapter does not report: the router refuses such an entry at placement (`no_gtt_delivery`, before any order is reserved) and `send_order` refuses it before the broker call; a stop or a sale is never refused for the capability. Harm: a hand-written or stale account row buys real shares that get no overnight stop.
- **Implementation:** `modes.real_orders_allowed` (active, stored GTT, the adapter's GTT now) gates `router._gtt_refusal` and `submit._check_allowed` for an entry; `bot_mode` and `/mode` read the stored value, which the desks set at start and every session check keeps (HAT-BRK-092).
- **Fyers GTT for CNC** (was OWNER-PENDING, PLAN Q8): verified on the live account on 2026-10-22 (a single-leg CNC sell GTT placed with and without a tag, read in the GTT book, modified and cancelled; `tests/hat/fixtures/fyers_live_writes.json`). The Fyers adapter reports `gtt_delivery = true` since PR 25 (HAT-BRK-091), so confirm and auto run once the owner queues them and the owner's gates pass (HAT-GOV-085); every bot asks for confirm and runs shadow until then (HAT-EXEC-011).

#### HAT-EXEC-013: Delivery only
- **Statement:** Every HAT real order uses product CNC (delivery).
- **Source:** MA "Decisions register"; DS "Broker interface" (`intent` product CNC); B24.
- **PR:** 13
- **Verification:** PBT `tags.intent_product_cnc`
- **Acceptance:**
  - Property: for every order intent the router or exit path builds, `product = CNC`, and the adapter rejects any other product. Harm: an intraday product is auto-squared off by the broker, breaking the swing plan.

## §2 Router

The router receives a signal only after `evaluate_signal` returned ACCEPT (06 §10) and routes it by the bot's effective mode.

Implementation (`app/hat/execution/router.py`, PR 15): the effective mode is the queued mode in force at the open of the trigger's session (`execution/modes.py:bot_mode`, HAT-GOV-084; shadow for a sleeve whose lifecycle state does not route live, HAT-PORT-063), gated by the account's GTT capability (HAT-EXEC-012). `engine/trades.py:open_entry` runs the risk check under the bot's lock and, in one transaction, opens the virtual position (shadow), writes the approval card (confirm, `hat_approval`, the signal moves to AwaitingApproval) or leaves the accepted signal in Triggered (auto). The router then places an auto entry, or a passed Take, as a LIMIT BUY CNC at the entry price the engine fixed at the trigger, through `submit.reserve_order` and `submit.send_order` only. Before the trade date's first entry the broker book is reconciled (HAT-BRK-110). The signal commits (releasing the bot's lock) before the order is sent, so a placement first re-reads the live conditions at placement time, from the caller (`route` and `take` take `now` and a `PlacementCheck` read then, never the trigger's snapshot): the calendar session, the feed, the quality gate, the regime gate (HAT-EXEC-022), the instrument's dated live price (`daily/live.py:LivePrices`; refused when unknown, 30 seconds old, outside the day's band or at or below the signal's stop, HAT-DATA-116, PR 51), the kill switches and pause (06 §6) and the account's GTT capability (HAT-EXEC-012). On a refusal nothing is sent: a reserved entry that never reached the broker is cancelled and one that may have is left to reconciliation, never placed again. An entry that cannot be placed (a refusal on the order path, an unreachable broker, a second rejection, a refused live condition) ends the signal Rejected with a fixed reason and raises the `entry.not_placed` health alert; an order whose outcome is unknown is left to the tag lookup and reconciliation. A signal is placed only on its own trade date: a placement on a later date ends it Rejected with `signal_expired` (an entry left Triggered by a process that stopped is resumed after the restart only once the feed has a fresh tick and the account is reconciled, 10 §3). Every broker call of an account goes through the account's session owner (`daily/accounts.py:OwnedBroker`, HAT-DEC-T8), and one broker login serves one active account.

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
- **Statement:** On Take, the price is re-checked; if it has run more than the bot's max-chase % past the trigger, the order is refused. The price re-checked is the instrument's dated live price (HAT-DATA-116, PR 51): a Take or a placement with no usable price (unknown, stale or outside the band) or a price at or below the signal's stop is refused, and the circuit locks, upper and lower, read the stored price band (02 §2).
- **Source:** MA "Execution modes"; BL "Criteria library" (`max_chase` M 5, Q 2); B25; ME-F7, MX-F11 (2026-10-22 audit).
- **PR:** 15, 51
- **Verification:** TLA `ApprovalToken!ChaseRecheckOnTake`, PBT `caps.max_chase_always`, PBT `router.take_rechecks_max_chase`, PBT `bars.circuit_locks_read_the_band`, PBT `bars.exits_and_placement_read_the_checked_price`
- **Acceptance:**
  - Property: in every behaviour, a Take produces an order only if price_at_decision ≤ trigger_level × (1 + max_chase_pct/100) and the instrument is not locked at upper circuit; otherwise the decision is recorded and the order refused. Harm: the user approves an entry that has already run away.
  - Property: for every Take before expiry whose other live conditions pass (session, feed, quality gate, regime gate; HAT-EXEC-022 refuses first otherwise) and whose live price is unknown, stale, out of band or at or below the signal's stop, nothing is placed and the signal ends Rejected with that reason (`price_unknown`, `stale_price`, `price_out_of_band`, `price_at_or_below_stop`). Harm: a Take buys on a stale print, or into a stop already hit, so the entry is under water at once.
  - Property: for every previous close, band and tick, the upper circuit price is the band's top rounded down to the tick grid and the lower one its bottom rounded up; a price is locked at a circuit exactly when it is at or beyond it, neither lock holds for an F&O name and both are unknown without a band row. Harm: an entry is taken into a frozen upper circuit, or an exit is evaluated as if a lower-circuit print could be sold.
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
| `below_final_contraction_low` | buffer % (0.5), max % below entry (8); wider → skip; min % below entry (0 = off); narrower → skip | M |
| `low_of_day` | buffer ticks (1) | Q |
| `pct_below_entry` | % | Custom bots |
| `atr_multiple` | ATR days, multiple | Custom bots |
| `swing_low` | lookback days | Custom bots |

One stop rule per playbook; the initial stop is computed before entry (BL "Stops and exits").

#### HAT-EXEC-050: below_final_contraction_low
- **Statement:** Stop = final contraction low × (1 − buffer_pct/100) with buffer 0.5%; if (entry − stop)/entry > 8% the trade is skipped (`stop_too_wide`). With `min_pct` above 0 (default 0 = off; `min_pct` < `max_pct`), a stop less than `min_pct` percent below entry is skipped too (`stop_too_narrow`, PR 54).
- **Source:** BL "Stop rules"; BL "Minervini-style bot" (`stop: {rule: below_final_contraction_low, buffer_pct: 0.5, max_pct: 8}`).
- **PR:** 11, 54
- **Verification:** PBT `stops.final_contraction_low`, PBT `stops.contraction_stop_at_the_fill`
- **Acceptance:**
  - Property: for every final contraction low L > 0, entry P > 0 and buffer b, the stop equals L × (1 − b/100) rounded down to the tick, and the result is SKIP exactly when the stop is not below P, when (P − stop)/P > max_pct/100 (`stop_too_wide`), or when min_pct > 0 and (P − stop)/P < min_pct/100 (`stop_too_narrow`); with min_pct = 0 no stop is too narrow, and a config with min_pct >= max_pct is refused. Harm: stops wider than the playbook allows are accepted, stops inside the base's noise are taken, or valid trades are dropped.
  - Property: the stop planned overnight at the pivot (which leaves `min_pct` to the entry) and re-checked at a fill at or above the pivot equals the live plan at that fill. Harm: the backtest skips trades live would take, so its results are not the live strategy's.

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
3. GTT limit price set 1% below trigger so moderate gap-downs still fill [A]. A gap below the limit price leaves the GTT unfilled; the offset reduces that risk but cannot remove it, so an open position whose GTT triggered without a fill raises a health alert. A GTT that triggers while the price is already below its limit leaves its limit sale working unfilled: the fill sync alerts it and has the broker make that sale a market sale (HAT-EXEC-066). Under SEBI market price protection (from 2026-04-01) a market sale is itself a limit a band from the price, and can rest unfilled too (research G02): HAT records the order type and price the broker's book shows for each of its sales, and (behind `HAT_MPP_RESEND`, until the O2(c) live check confirms the book's shape) sends a market sale that rests unfilled again fresh at every sync until it fills, paging the owner after N misses; a sale of an instrument locked at its lower circuit waits in the queue and is alerted once (HAT-EXEC-075, PR 48).
4. A broker without reliable GTT for delivery cannot run auto mode (HAT-EXEC-012).

Implementation (PR 15c; `execution/protect.py` `StopDesk`, `execution/fills.py` `FillSync`, `execution/manage.py` `TradeManager`, `ops/trade_jobs.py`; model `verification/tla/StopPlacement.tla`):

- The fill sync reads the broker's order and trade books (fail closed) and protects a position right after recording each of its fills, before any other action on it. With no working stop it places an intraday SL-M SELL CNC stop in the session before 15:25 and a GTT from 15:25 on and after the close (a day stop placed after the swap would expire with no GTT behind it); a working stop for another quantity is resized. A stop the broker refuses after the one retry is alerted (`stop.not_placed`) and placed again by the next sync, which protects every open real position HAT's book shows without a working stop; a resize the broker refuses is alerted (`stop.not_moved`) and sent again by the next sync, which also resizes every placed stop whose quantity differs from the position's. While a sale the broker accepted is still working (placed or part filled), no stop is placed or grown over it: `make_room` gave the sale the shares, and the position is protected again once the sale ends. A sale whose answer was lost does not hold protection back.
- A broker stop's trigger is `current_stop` rounded down to the instrument's tick, so it is never above `current_stop`; `follow` modifies it up and never down, and a refused modify is alerted (`stop.not_moved`).
- `gtt.swap` (15:25, retried every minute until it succeeds) places the GTT first and cancels the intraday stop only once the GTT is confirmed; a failure leaves the intraday stop working and is alerted (`gtt.swap_failed`). An intraday stop the broker did not confirm cancelled fails the run, so the cancel is tried again (two resting stops would sell the same shares twice). A position with a sale still working is swapped on the retry.
- `gtt.verify` (23:00) reads the fills first (when they cannot be read the open quantities may be stale: each position is alerted `gtt.unverified` and no GTT is modified or placed), then refreshes each open real position's GTT to the current stop (`gtt.refreshed`). A GTT whose refresh the broker refuses is cancelled, then alerted (`gtt.missing`) and placed again; a position with no GTT in HAT's book is alerted and gets one. A GTT is never placed on top of one that may still rest at the broker: when the broker cannot be reached, the refused GTT cannot be cancelled, a GTT's placement answer was lost (the GTT book can lag one that landed, so it is never sent again), or no tick is known, the position is alerted (`gtt.unverified`) and nothing is placed. A GTT whose placement answer was lost is settled first against the broker's GTT book (HAT-EXEC-065), so it blocks a new GTT only while the book cannot be read or the answer is younger than the settle wait. A GTT placed 300 days ago or more (`protect.GTT_RENEW_AFTER`, PR 47) is cancelled and placed again at the current stop: Fyers expires a GTT a year after placement and a modify may not reset that clock **[verify on a live account]**. The cancel goes first (the market is shut, so nothing triggers meanwhile, and two GTTs never rest together); a refused cancel leaves it refreshed in place and the next night tries again, and one whose new trigger would rest below the old (a coarser tick) is not renewed.
- `gtt.ex_date` (08:40, PR 47, registered only with `HAT_EXDATE_GTT_CHECK` on): Fyers cancels a resting GTT before a corporate action's ex-date, a dividend's included, and on a series change (research G01, G17). Every open real position with a stored action of ex-date today or the next session, or on an instrument that changed series, has its GTT checked against the broker's GTT book and is protected again when the broker cancelled it (HAT-EXEC-145, HAT-EXEC-146).
- A GTT carries its stop_gtt tag (`orderTag`; Fyers shows it on the GTT book row as `ordertag` "<n>:GTT<tag>", and an untagged GTT as "<n>:GTTUntagged", recorded 2026-10-22). An order or trade under one of HAT's GTT tags (bare, or as the GTT book shows it) is that GTT's sale (`gtt_tag`; whether Fyers puts the tag on a triggered GTT's sale is **[verify on a live account]**), attributed to the working GTT with that tag whatever the symbols other positions share; one under the tag of a GTT HAT has no working row for is not attributed. An untagged SELL is attributed as before GTTs were tagged: a trade on the symbol of the one open real position with a working GTT goes to that GTT. A GTT's SELL order that ended short of its quantity is the GTT triggering without a fill (`gtt.unfilled`, then protected again). One still working short of its quantity, under the GTT's tag or (untagged) a CNC sale of the GTT's quantity at the GTT's limit price, is the GTT's sale resting below its limit (HAT-EXEC-066; an untagged order at another price or quantity is not HAT's and is never touched): once it has worked a minute (`fills.RESTING_AFTER`, so a sale on its way to a fill is left to fill), the sync records `gtt.triggered` (with the broker order id, which identifies the sale from then on) and raises `gtt.unfilled` (code `gtt_triggered_resting`); every sync then asks the broker to make that order a market order (`modify_order` with `order_type` MARKET, `StopDesk.market_sale`) until the order book shows it as one, and a refused change is alerted once (code `gtt_sale_not_marketed`) and asked again at the next sync. The GTT row stays live meanwhile: its sale holds the shares, so no exit or flatten sale is sent (the next evaluation sees what it left) and `follow` and the rescale leave the triggered GTT where it is (`StopDesk.triggered_gtts`). The sale is a DAY order, so only a trigger of the current trade date holds anything back: one whose end the sync never saw (the book was not read before the next day) leaves the GTT to the exits and the nightly verify. A sale that fills ends as any GTT fill; one still working at the close expires and is handled as a GTT that ended unfilled. An untagged sale on a symbol two open positions share is not attributed. **[verify on a live account]**
- An exit sale first shrinks the position's resting stops to the shares that stay (cancels them for a full exit), since a broker holds the shares a resting CNC sell covers; a stop that cannot be changed holds the sale back (`exit.not_placed`, reason `stop_not_moved`), and a sale that is refused or rejected twice has the position protected again at once. A sale rejected twice in a session is sent once more as a LIMIT at the day's lower circuit price (PR 48; the lowest price that can trade today, so no price check refuses it) and the owner is told (`sale.unfilled`, code `band_limit`); without a known band, or once that sale is refused too, it is not tried again that session. A sale the broker accepted and later rejected, cancelled or expired short of its quantity (seen by the fill sync) records `order.rejected` (reason `exchange_rejected`), un-fires the rules it carried (`hat_order.rule_ids`), so the next evaluation sends it again, and counts toward the two rejections; the second raises `exit.not_placed` (reason `order_rejected`) (HAT-EXEC-075).
- `hard_stop` is never sold by HAT while a broker-confirmed stop rests (working, with a broker id): the broker stop is the hard stop. A stop sent with its answer lost is settled first (HAT-EXEC-065); one still unsettled does not count, and HAT sells at market (a stop that did land makes the broker refuse the duplicate CNC sale, which is alerted and tried again). A position whose stop the broker keeps refusing and that has not hit its stop is alerted, not sold at market.
- A lost broker answer never freezes a position (HAT-EXEC-065). A stop whose placement answer was lost is settled once it is older than the settle wait (60 s, `protect.LOST_ANSWER_SETTLE`, so an SDK request still in flight is not mistaken for absent): a day stop by its tag in the order book, a GTT in the broker's GTT book (`gtts()`: the single-leg CNC GTTs that rest or have triggered, so a cancelled, rejected or expired one, an OCO or another product is left out, a triggered one is adopted and its sale attributed by the fill sync, and a single-leg CNC row with a status code the adapter does not know makes the book unreadable; matched by its tag, else, for a GTT shown without a HAT tag, by symbol, side, quantity and trigger; never one under another HAT tag or a foreign tag, never one another row already owns). Found, the row is adopted (placed, with its broker id); absent, it ends `expired` and the position is protected again. An answer older than the settle wait that could not be settled is alerted; a younger one is not. A sale whose answer was lost is settled the same way by its tag before an exit decides whether to send; a reserved sale that was never sent is cancelled; one found with shares sold is left to the fill sync, which records its fills with it, and no other sale goes out meanwhile; one of an earlier session that today's book does not hold may have sold, so it is left unsure, nothing more is sold and the owner is alerted (`exit.not_placed`, reason `sale_unresolved`) while the broker stop keeps protecting the position. **[OWNER-PENDING]** how such a sale is resolved; default: by hand after reconciliation. A position closed while one of its stops was still unsure has that stop settled and cancelled by the next fill sync, so no stop rests under shares already sold. Two senders of one reserved, never-sent order (the fill sync and a stop job) never both send it: `send_order` stamps every attempt with one conditional update on the attempt and status it read, and the sender that loses sends nothing, as does a sender whose order the fill sync adopted from the book since its lookup; each attempt's own time starts its settle wait. While a book cannot be read the row stays unsure: no stop is placed on top of it (`stop.not_placed`, reason `gtt_unconfirmed` or `stop_unconfirmed`) and no sale is resent (`exit.not_placed`, reason `sale_unresolved`); an unsettled stop never holds an exit sale back. `FyersBroker` answers a place it never sent because its circuit breaker opened as a definite rejection, not a lost answer.
- Nothing in stop placement checks a kill switch or pause (06 §6).

#### HAT-EXEC-060: Stop at the broker on every real fill
- **Statement:** Every real entry fill is followed by a broker stop at the position's stop price: an intraday stop order (purpose `stop_day`) in the session before the 15:25 swap, a GTT stop (purpose `stop_gtt`) from then on and after the close; this includes a fill of the entry's remainder after the position's sales emptied it.
- **Source:** BL "Stop placement at the broker (B17)" step 1; MA "Execution modes" ("Protective stops sit at the broker on every real fill"); B17; audit 2026-10-22 tests-hat F2; design review 2026-10-22 item 1 (the fill was seen up to a minute late).
- **PR:** 15, 20, 26, 50
- **Verification:** TLA `StopPlacement!StopAfterFill`, TLA `StopPlacement!SharesTracked`, TLA `StopPlacement!RestCancelledOnExit`, TLA `StopPlacement!EmptiedCloses`, PBT `manage.real_fill_protected`, PBT `manage.a_resting_sale_is_never_stopped_over`, PBT `manage.entry_remainder_tracked`, PBT `router.fill_sync_cadence`
- **Acceptance:**
  - Property: in every behaviour, after a real entry fill, a working broker stop for the open quantity exists before any other action on that position, and from then on there is always a working stop (day or GTT) while qty_open > 0. Harm: a position sits unprotected if the machine dies.
  - Property: in every interleaving of part fills of the entry with stop fills, exits and flattens, HAT's open quantity equals the shares the broker holds and its live stops cover them; once a sale empties the position the entry's remainder is asked to be cancelled, and the position closes only when no entry order still works. Harm: shares the broker bought after HAT closed the position sit untracked and unprotected.
  - Property: while a real entry of the account's signals of the current trade date may still fill (pending, unconfirmed, placed or partially filled), or a stop of an open real position was sent and its answer is not in yet (pending or unconfirmed), the fill sync reads the broker's books at every periodic step (5 s), so a fill gets its stop request, and an unsure stop is settled, within one step; otherwise it reads them once a minute. A stop the broker refused is retried at that minute, never faster. A sync that could not read the books (the broker unreachable, or the call budget refusing the read, HAT-BRK-082) is tried again at the next step: the 5-second cadence yields to the budget rather than queue behind it. The models take the broker fill and HAT reading it as one step; this bound is the time between them. Harm: an entry filled just after a sync sits up to a minute at the broker with no stop, or a refused stop is re-sent every few seconds until its tag's sequence runs out.

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
- **Statement:** Every night the engine confirms a GTT stop exists at the broker for every open real position and alerts on any gap. A GTT placed 300 days ago or more is cancelled and placed again (PR 47).
- **Source:** MA "Daily cycle" step 9; BL "Stop placement" step 2 (refreshed nightly); SCH event `gtt.refreshed`; research G24 (GTT expiry).
- **PR:** 15, 47
- **Verification:** TLA `StopPlacement!NightlyVerifyCovers`, PBT `manage.verify_covers`, PBT `manage.verify_renews_old_gtt`
- **Acceptance:**
  - Property: in every behaviour, after the nightly `gtt.verify` job, every open real position either has a broker GTT whose trigger equals `current_stop`, or a health alert was raised and a re-placement attempted. Harm: a missing GTT goes unnoticed for days.
  - Property: for every GTT age and cancel reply, a GTT placed 300 days ago or more whose cancel the broker confirms is cancelled before a new GTT is placed at the current stop, with no `gtt.missing` alarm; a younger GTT, or one whose cancel is refused, is refreshed in place. Harm: the broker expires the GTT a year after placement and the position is left without a stop.
- **[verify on a live account]** that Fyers expires a GTT a year after placement, and whether a modify resets that clock; 300 days leaves a margin either way.

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
- **PR:** 18, 25
- **Verification:** TLA `StopPlacement!LostGttSettled`, TLA `StopPlacement!LostNeverBlocksSale`, PBT `manage.lost_answers_never_freeze`, PBT `manage.a_settled_answer_never_sells_twice`, PBT `manage.the_close_ends_only_settled_answers`, PBT `tags.the_gtt_book_reads_only_resting_stops`, PBT `tags.a_reserved_tag_is_sent_by_one_sender`, PBT `tags.a_lost_gtt_is_found_by_its_tag`
- **Acceptance:**
  - Property: in every behaviour where a GTT placement answer is lost, the row is eventually settled (adopted when the GTT rests at the broker, ended and replaced when it does not) while the GTT book can be read, and no second GTT is placed while it is unsure. Harm: a position goes nights without a stop HAT will place, or two GTTs sell the same shares twice.
  - Property: for every sequence of lost, landed, refused and unreadable answers on stops and sales, an exit or hard stop due on an open real position is sent once the books can be read, an unsure stop never holds it back, and a sale already resting is never sent twice. Harm: a stop-loss exit is frozen while the price falls, or the position is oversold.
  - Property: settling never leaves a second seller: a stop that landed under a position since closed is cancelled once settled, a lost sale that sold is not followed by another before its fills are recorded, an earlier session's lost sale holds further sales instead of firing its rules again, only GTTs that rest or triggered are matched, one attempt of an order is sent by one sender, and the session end writes off only an order the book does not hold and that is older than the settle wait. Harm: a GTT rests under shares already sold, or the same shares are sold twice.
  - Property: for every GTT book, a lost GTT settles to a GTT no other row holds on its symbol: the one under its own tag when the book shows one, else one shown untagged of exactly its side, quantity and trigger, never one under another HAT GTT's tag or a foreign one. Harm: another position's stop is adopted and later moved or cancelled, leaving that position unprotected.

#### HAT-EXEC-066: A triggered GTT's resting sale is sold at the market
- **Statement:** A GTT that triggered with its limit sale left working unfilled below the limit for a minute (the price gapped through it) is alerted once and its sale changed to a market sale, asked again at every fill sync until the broker shows it so; no other sale or stop change is sent over it that trade date. Only the GTT's own sale (a CNC SELL under its tag, or an untagged one of its quantity at its limit price) is changed. With `HAT_MPP_RESEND` on (PR 48), a sale the book already shows at the market but still resting (market price protection made it a limit the price left) is asked to go at the market again at every sync, paging the owner at the `HAT_MPP_PAGE_AFTER`th time, and left in the queue (alerted once) while its instrument is locked at the lower circuit.
- **Source:** Design review 2026-10-22 item 2; BL "Stop placement" step 3; BL "Invariants" 1; research G02, G03 (2026-10-22).
- **PR:** 25, 27, 48
- **Verification:** TLA `StopPlacement!RestingSold`, PBT `manage.gtt_resting_marketed`, PBT `mpp.gtt_market_sale_asked_again`
- **Acceptance:**
  - Property: in every behaviour where a GTT triggers with its sale resting below the limit under a held position, the position is eventually sold (the sale is made a market sale), never left at its limit until the close. Harm: the stop triggered and the shares sit unsold all session while the price keeps falling.
  - Property: for every share of the sale already filled, run of refused changes and number of syncs, the fill sync leaves a sale first seen to fill, then records `gtt.triggered` and raises `gtt_triggered_resting` once, asks for exactly a market order type on that order at every sync until the book shows it, alerts a refused change once, and sends no other order, stop or GTT change (a flatten waits, a raised stop leaves the triggered GTT) while the sale works; once it fills the position closes with no stop left; a sale under the GTT's tag is the GTT's at any price, and an untagged sale at another price or one under another GTT's tag is never touched; the next trade date a flatten is sent again. Harm: the shares are sold twice, a stop moves a GTT the broker no longer holds, an order HAT did not place is dumped at the market, or a trigger seen once blocks every exit for good.
  - Property: for every number of syncs and lower-circuit lock, with `HAT_MPP_RESEND` on a GTT's sale the book shows at the market but still resting is asked exactly `{order_type: MARKET}` again at each sync it is not locked, the owner is paged once at the `HAT_MPP_PAGE_AFTER`th ask, and a lock is alerted once; with it off nothing is sent. Harm: the triggered stop's sale sits below a stale protection band all session.
- **[verify on a live account]** (O2(c)) whether a MARKET modify of a protected sale re-prices its protection band.
- **[verify on a live account]** Fyers accepts a change of a triggered GTT's child order to MARKET (`modify_order` with `type` 2), and the book reports the child with the GTT's quantity.

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
- **Statement:** A sale the broker accepted and later rejected (or cancelled or expired before selling everything) records `order.rejected`, un-fires its exit rules so the sale is sent again, and the second rejection in a session raises `exit.not_placed`; the sale then goes once more as a LIMIT at the day's lower circuit price when the band is known (PR 48). A market sale HAT itself cancels to send again is not a rejection: under market price protection a market sale is a limit a band from the price, so (behind `HAT_MPP_RESEND`) one still resting a minute after its send is cancelled and the shares it left sent again as a fresh market sale at every sync until it fills, the owner is paged at the `HAT_MPP_PAGE_AFTER`th miss (default 3), and while the instrument's last trade is at its lower circuit it is left in the queue and alerted once.
- **Source:** Audit 2026-10-22 money-safety-execution F3; BL "Evaluation rules" (exits never wait for the user); §6; research G02, G03, G22 (2026-10-22); plan audit O2(c).
- **PR:** 18, 48
- **Verification:** PBT `manage.async_sale_rejection_retried`, PBT `mpp.resting_market_sale_resent`, PBT `mpp.lower_circuit_lock_holds_the_sale`, PBT `mpp.rejected_twice_goes_at_the_band`
- **Acceptance:**
  - Property: for every exit rule and every asynchronous rejection of its sale, `order.rejected` is recorded, the rule is no longer in `rules_fired`, the next evaluation sends one new sale, and a second rejection raises `exit.not_placed` and sends no third market sale that session. Harm: a rule marked fired on a sale that never sold leaves the position open against its plan with no alert.
  - Property: for every order-book shape of a protected market sale (`fyers_mpp_sale_assumed.json`), number of misses and part fills, with `HAT_MPP_RESEND` on each market sale still resting a minute after its send is cancelled (recording the book's type and price), and once the book shows it ended exactly one fresh market sale of what is open goes out; no miss records `order.rejected`, raises `exit.not_placed` or counts toward the two rejections; the owner is paged once at the `HAT_MPP_PAGE_AFTER`th miss; with it off nothing is cancelled. Harm: a stop-loss sale sits unfilled below its protection band all session while the price falls, or a re-send sells the shares twice.
  - Property: for every run of lower-circuit lock readings, a resting sale is not cancelled while the last trade is at the lower circuit, is alerted once, and is cancelled at the first reading that is not a lock. Harm: a locked sale loses its place in the queue every minute, or the owner is not told it cannot be sold.
  - Property: for a sale refused twice (at once, or after the broker took it), exactly one LIMIT SELL at the day's lower circuit price for the open quantity follows when the band is known, with one `sale.unfilled` (`band_limit`) alert; refused too, nothing more goes that session; without a band nothing follows. Harm: a stop-loss sale refused twice leaves the position unsold all session.
- **[verify on a live account]** (O2(c)) how the order book shows a protected market sale (the fixture's shapes), its band per price slab, that a cancel ends it so a fresh sale gets a new band, and how a lower-circuit lock shows (last trade at the band with no bid). The lock is read from the last trade alone: HAT's tick carries no bid quantity.

## §8 Broker interface

HAT's own contract (`app/hat/execution/broker.py`, protocol `HatBroker`); I7. No adapter-specific type leaks into the engine.

| Method | Purpose |
| --- | --- |
| `capabilities()` | GTT for delivery, order-tag field and length, order types, static-IP requirement |
| `login_url()` / `complete_login(code)` / `session_status()` | Daily login via Telegram link |
| `place_order(intent)` → broker order ID | Entry/exit; `intent` carries instrument, side, qty, product (CNC), type, price, trigger, tag |
| `modify_order`, `cancel_order` | Stop moves, expiries, a triggered GTT's resting sale made a market sale (HAT-EXEC-066) |
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
- **PR:** 13, 18, 25
- **Verification:** PBT `tags.broker_errors_fail_closed`, PBT `tags.a_keyless_book_never_reads_as_absent`, PBT `fyers_broker.an_ok_reply_without_its_list_is_unreadable`, PBT `fyers_broker.a_circuit_opening_mid_place_is_not_sent`, PBT `fyers_broker.a_rejection_with_an_id_stays_rejected`
- **Acceptance:**
  - Property: for every injected broker failure (timeout, circuit open, error payload, an ok reply without its list), `orders/trades/positions/holdings/gtts/funds` raise `BrokerUnavailableError` and never return an empty list, and the surfaced message is a fixed code without the raw error string. Harm: reconciliation reads "no positions" and the engine re-enters or abandons stops; secrets or account data leak.
  - Property: for every place, a circuit breaker that opens between the caller's check and the send answers a definite rejection, never a lost answer. Harm: an order that never left is chased as unconfirmed and its position frozen.
  - Property: an explicit error stays a rejection though it carries the id of the order the broker created and refused (Fyers answers an RMS refusal `{"s": "error", "code": -99, "id": ..., "id_fyers": ...}`, recorded 2026-10-22): no order id is kept and the result is never placed or unconfirmed. Harm: a refused order is tracked as placed, or a refused stop as resting, and the position goes unprotected.

#### HAT-BRK-082: Broker session handling
- **Statement:** Session drops are retried with backoff, alerted after 2 minutes (an outage counted from 08:30 IST, HAT-OPS-031), and no entry is placed while the session is not live; `hat_broker_session` tracks the morning login. A token the broker itself refuses is an expired session, not an outage: for Fyers, code -16 ("Could not authenticate the user", "Token is Invalid or Expired") on any read raises `FyersSessionExpiredError`, the read fails with `broker_session_expired`, and `session_status` (one profile read per token per 30 s when the token file is in date, the window ended at once by a -16 on any read) reports `expired`, so the outage watch alerts and the 09:05 prompt asks for a login; Fyers' code -360 ("Standard plan limit of 5,000 requests/day", every call refused until the next day) is its own code, neither an outage nor a lost session: a read fails closed with `broker_rate_limited` (`FyersRateLimitedError`), a write is refused with `broker_rate_limited` and counts as not sent, as when the broker is unreachable (nothing was placed: the order stays pending and burns no retry), the session status is unchanged, and the minute-history load stops its run with `history_rate_limited` instead of failing the symbol [PR 25]. Every adapter call to Fyers first takes a slot from one process-wide call budget (`CallBudget`, `process_budget()`), whose limits come from config: `HAT_BROKER_CALLS_PER_SECOND` (default 8), `HAT_BROKER_CALLS_PER_MINUTE` (default 180) and `HAT_BROKER_CALLS_PER_DAY` (default 5,000, the Standard plan's count of every call; `off` for none), so a plan with higher limits (Fyers Prime, D154) is a config change only. Writes (orders, stops and GTTs) go ahead of reads: a read may use only 80% of each window (at least one call, so reads still run) and of the day (no minimum, so a read never spends the day's last call), and waits while a write is waiting; a call that waited while another call opened the circuit sends nothing. A call the budget does not grant in time (a write within 3 s, a read within 0.5 s, at once once the day is spent) is not sent and is answered `broker_rate_limited`, as is an HTTP 429 from Fyers, which also stops every call for its `Retry-After` (1 s without one, at most 60 s), even when it lands after the caller stopped waiting (that caller saw a timeout: a lost answer, one breaker failure); neither counts toward the circuit breaker [PR 50]. Any other failed read is an outage (`broker_unavailable`) and leaves the status as the token file says. The login reads the user id (`fy_id`) from the account's profile (at most 5 s, the reply checked as every profile read), since the token reply carries none; the login status reports it, or none when the profile could not be read. [Changed in PR 23 after the live checks of 2026-10-22: -16 read as an outage and a revoked token read live.]
- **Source:** MA "Failure handling" (Broker session drops); MA "Daily cycle" step 1; SCH `hat_broker_session`; T8, T14; live checks 2026-10-22.
- **PR:** 13, 16, 23, 25, 50
- **Verification:** TLA `ExecutionRouter!NoEntryWithoutSession`, PBT `broker.token_refused_is_session_expired`, PBT `broker.token_refused_on_a_read_ends_the_check`, PBT `fyers_auth.a_good_exchange_saves_once_and_reports_in_order`, PBT `broker.rate_limit_is_its_own_code`, PBT `bars.minute_history_rate_limit_stops_run`, PBT `tags.an_unreachable_broker_is_not_a_rejection`, PBT `fyers_broker.budget_windows_hold`, PBT `fyers_broker.throttled_call_not_sent`, PBT `fyers_broker.writes_go_ahead_of_reads`, PBT `fyers_broker.limits_from_settings`, PBT `fyers_broker.late_429_still_pauses`, PBT `fyers_broker.breaker_opened_while_waiting_sends_nothing`
- **Acceptance:**
  - Property: in every reachable state, no real entry order is placed while the account's session status is not live; and if the session stays lost for 2 minutes, `broker.session_lost` and a health alert are eventually emitted. Harm: orders fail mid-flight or the user is not told the bot is blind.
  - Property: for every broker reply of code -16 the session status is `expired` and every read fails with `broker_session_expired`, and for every other failure the status is unchanged and reads fail with `broker_unavailable`. Harm: a revoked token reads as an outage and nobody is asked to log in.
  - Property: for every read and write answered with code -360, the read fails with `broker_rate_limited`, the write is refused with `broker_rate_limited`, the session stays live, and no broker text is kept; a minute-history run stops at the first -360 with `history_rate_limited`, asking nothing more, the symbol it stopped on neither failed nor done. Harm: a spent daily limit reads as an outage or a failed symbol, and the run keeps calling a broker that refuses everything.
  - Property: for every sequence of read and write requests at any times and over day changes, with any limits, the budget never grants more calls than the per-second, per-minute or per-day limit in any window, grants reads at most their share, grants nothing during a pause, and grants every refused call once it has waited as long as it was told, unless the day is spent. Harm: HAT exceeds the broker's limits and every call, stops included, is refused until the next day.
  - Property: for every operation, a call the budget refuses and a call answered HTTP 429 (JSON or not, with any `Retry-After`) is answered `broker_rate_limited`, not sent again, counted neither as a breaker failure nor as success, and a 429 pauses the budget for its `Retry-After`. Harm: a busy broker opens the circuit and blocks stops, or an order is re-sent into the limit.
  - Property: while writes and reads contend for a full budget, every write is granted before any read that waited with it. Harm: a stop waits behind book reads and the position sits unprotected.
  - Property: a call that waited for the budget while the circuit opened sends nothing, and an HTTP 429 that lands after its call timed out still pauses the budget. Harm: queued calls reach a broker declared down, or a slow 429 is lost and HAT keeps calling into the limit.
  - Property: for every value of the three variables, the budget's limits are those values, an unreadable or non-positive one gives the default, and only the per-day limit can be switched off. Harm: a plan change needs a code change, or a typo removes the limit.

## §9 Fyers adapter

The MVP adapter (`app/hat/execution/fyers_adapter.py`) wraps HAT's `FyersBroker` in `fyers_broker.py` (place/modify/cancel, GTT place/modify/cancel, positions, funds, order and trade books; per-instance circuit breaker, one process-wide call budget (HAT-BRK-082), guarded timeout, `SUBMIT_UNCONFIRMED` for a lost, garbled or id-less reply, no non-finite price sent), `FyersSession` (`fyers_session.py`) and `FyersAuthService` (`fyers_auth.py`, routes under `/api/hat/auth/fyers`). Tag lookup is the adapter's own (`orders()` filtered by tag), not a broker method. Fyers secrets are read only by `fyers_auth.py`, `fyers_tokens.py` and `fyers_session.py` (PLAN C5). The SDK's HTTP client is imported only by `fyers_ipv4.py`, which pins Fyers HTTP to IPv4 (HAT-BRK-093). [Reworded in K2; it wrapped the legacy `FyersBroker`, `FyersClient` and auth flow.]

#### HAT-BRK-090: Exact Fyers payloads
- **Statement:** The adapter maps every `intent` and GTT request to an exact, documented Fyers payload with product CNC. A GTT is placed with its stop_gtt HT tag as `orderTag` (Fyers accepts a tag on a GTT, recorded 2026-10-22; the recording used an 8-character tag, so a full 20-character one is **[verify on a live account]**), and a GTT request without one is refused before any SDK call. The GTT book is read with the keys recorded live: `tran_side`, `product_type`, `ordertag` and the flat `price_trigger`, `price_limit`, `qty` [Changed in PR 25: the adapter read `side` and `productType`, which the live book does not have, so a non-empty book was unreadable].
- **Source:** DS "Broker integration"; PLAN PR 13 (payload-exact mapping); trading-safety skill rule 4; live write test 2026-10-22 (`audits/fyers-live/trading_05_writes.json`).
- **PR:** 13, 25
- **Verification:** PBT `tags.fyers_payload_mapping`, PBT `tags.gtt_payloads`, PBT `tags.the_gtt_book_reads_only_resting_stops`, CONF `brk.recorded_writes`
- **Acceptance:**
  - Property: for every valid intent (side, qty ≥ 1, type, price, trigger, tag) the produced payload has exactly the documented keys and values (symbol via `symbol_master.to_fyers`, `productType = CNC`, `orderTag = tag`), and every invalid intent raises before any SDK call. Harm: a malformed order reaches the exchange.
  - Property: for every GTT request the place payload is `{side, symbol, productType: CNC, orderInfo: {leg1: {price, triggerPrice, qty}}, orderTag}` with the request's stop_gtt tag, a request without one is refused before any SDK call, and every GTT book row in the recorded shape reads as its GTT with that tag, or with none for "GTTUntagged", a foreign tag or another purpose's HAT tag. Harm: HAT's own GTT is not recognised and a second one is placed over the same shares, or another position's GTT is adopted.

#### HAT-BRK-091: Capabilities reported honestly
- **Statement:** The Fyers adapter reports its capabilities (GTT delivery, tag length 20, order types, static IP required). `gtt_delivery = true`: GTT for delivery was verified on the live account on 2026-10-22 [Changed in PR 25; it reported false until then].
- **Source:** DS "Broker matrix" (*verify* rows); PLAN Q8; live write test 2026-10-22 (`audits/fyers-live/trading_05_writes.json`, recorded in `tests/hat/fixtures/fyers_live_writes.json`).
- **PR:** 13, 25
- **Verification:** CONF `brk.fyers_capabilities`, CONF `brk.recorded_writes`, PBT `broker.the_fyers_adapter_unlocks_modes_on_an_active_account`
- **Acceptance:**
  - Property: `capabilities()` returns `tag_max_len = 20`, `static_ip_required = true` and `gtt_delivery = true`, backed by the recorded GTT place, book, modify and cancel replies. Harm: auto mode is enabled on an unverified overnight stop path, or a verified one stays off.
  - Property: with the adapter's capabilities, every mode runs as asked on an active account and only shadow on any other; with `gtt_delivery = false` only shadow runs anywhere. Harm: a real entry is placed with no overnight stop path.
- **GTT delivery on Fyers** (was OWNER-PENDING, PLAN Q8): verified on 2026-10-22; the owner's live write test is the evidence.

#### HAT-BRK-093: Fyers HTTP leaves over IPv4
- **Statement:** Every Fyers HTTP request (orders, GTTs, history, quotes and the login exchange) connects over IPv4 only. Fyers takes writes only from the account's whitelisted static IP, an IPv4 address; a machine with IPv6 reached `api-t1.fyers.in` over IPv6 by default and Fyers refused every write with code -50 (live write test 2026-10-22). One place sets it: `execution/fyers_ipv4.py` is the only importer of the SDK's HTTP client (`fyers_apiv3.fyersModel`) and installs, before it hands the SDK out, a hook on urllib3's `create_connection` that connects a Fyers host (`fyers.in` or a subdomain) only to its IPv4 addresses, in the resolver's order, and fails with none rather than fall back to IPv6; any other host is untouched, and TLS still checks the Fyers host name. The data websocket and an HTTPS proxy are outside the hook.
- **Source:** Live checks 2026-10-22 (`audits/2026-10-22-live-checks.md`); HAT-DEC-BD2.
- **PR:** 25
- **Verification:** PBT `fyers_ipv4.fyers_http_leaves_over_ipv4`, PBT `fyers_ipv4.is_fyers_host_exact`, CONF `brk.fyers_adapter_wraps_existing`
- **Acceptance:**
  - Property: for every resolver answer, a `requests` call to a Fyers host opens sockets of family AF_INET only, to its IPv4 addresses in order until one connects, and none when it has no IPv4 address; a call to any other host, a look-alike included, resolves and connects as urllib3 does by itself. Harm: an order leaves over IPv6, Fyers refuses it as off the whitelist, and the position goes without its stop.
  - Property: the SDK's HTTP client is imported only by `fyers_ipv4`, after the hook is installed, and the session and the login service take it from there. Harm: a second import path builds a client that connects over IPv6.

#### HAT-BRK-092: Onboarding and capability check
- **Statement:** A broker account becomes usable only after the capability check (funds, positions and holdings fetched) and is recorded in `hat_broker_account` with its capabilities. The stored capabilities are the adapter's (a local value, read without a broker call or the session owner): the desks rewrite them at start, before any routing reads them, and every session check (`record_session`, after its session row is committed) rewrites them again; a change records `broker.capabilities_changed`, which the relay sends as a health alert. A flip of the adapter's value reaches an existing account at the next start or session check, in either direction; a refresh that fails (the adapter's answer, the row or the event write) is logged and rolled back alone, never undoes the session row, and is retried by the next check. A stale row fails closed: a real entry needs both the stored and the adapter's GTT value (HAT-EXEC-012).
- **Source:** DS "Onboarding a broker" (steps 4–5, without OpenAlgo per OWN-20OCT); I9; design review 2026-10-22 item 4.
- **PR:** 13, 29
- **Verification:** TLA `OrderReconciliation!AccountActiveAfterCheck`, PBT `broker.session_check_refreshes_capabilities`, PBT `router.desks_start_with_the_adapters_capabilities`
- **Acceptance:**
  - Property: in every behaviour, a bot bound to a broker account places a real order only if that account's status is active, which it reaches only after a passing capability check. Harm: orders go to an account whose permissions or funds were never confirmed.
  - Property: for every sequence of session checks, live or not, with the adapter reporting GTT, not, or failing, the stored capabilities equal the adapter's after each refresh that committed and are left unchanged by one that failed (the adapter's answer or the write), each change records one `broker.capabilities_changed` (GTT before and after, the changed keys), and the session row is written whatever the refresh did. Harm: a flip of the adapter's GTT value never reaches an existing account, a revoked one keeps routing real orders, or a failed refresh loses the day's login.
  - Property: after the desks are built, each account's stored capabilities are its adapter's, so the first signal routes auto only when the adapter reports GTT; a capability read never waits behind a broker call in flight. Harm: a stale row sends cards or entries the placement then refuses.

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
- **Statement:** When a submit's outcome is unknown, the order is looked up by tag before anything else is sent for it, and a found order is adopted. An entry of unknown outcome is never placed again: a lookup that finds nothing leaves it unknown and raises the `entry.unresolved` alert (the fill sync and reconciliation adopt it when the book shows it; the session end ends its signal Rejected `entry_not_found` when the book never does, 07 §4). A day stop or a sale found absent may be placed again under its tag once its settle wait has passed (HAT-EXEC-065).
- **Source:** PLAN "Broker" (`SUBMIT_UNCONFIRMED`, `find_order_by_tag`); MA "Failure handling"; design review 2026-10-22 item 3 (HAT-BRK-103).
- **PR:** 13, 28
- **Verification:** TLA `OrderReconciliation!UnconfirmedResolvedByTag`, TLA `OrderReconciliation!AtMostOneEntryPerSignal`, TLA `OrderReconciliation!LostEntryAdopted`, PBT `tags.a_timeout_is_looked_up_before_any_retry`, PBT `tags.idempotent_order_submission`, PBT `router.unresolved_entry_is_alerted_not_resent`
- **Acceptance:**
  - Property: in every behaviour where a submit times out, no new place call with that tag occurs until a tag lookup has returned "absent"; a found order is adopted with its broker order id. Harm: a timed-out order is placed twice.
  - Property: in every behaviour, including an order book that lags the orders that landed, an entry tag reaches the broker at most once: a lost entry is never sent again, and one that landed is adopted once the book shows it. Harm: Fyers takes the same tag twice and the position is doubled at double the risk, or a live entry is never tracked.
- **Implementation:** `submit.send_order` raises `UnresolvedOrderError` for an entry whose lookup finds nothing; the router alerts `entry.unresolved` and leaves the signal to the fill sync and the session end. A crash between the attempt's commit and the broker call cannot be told from a lost answer, so that entry is not sent either. A missed entry costs a trade; a doubled one costs money at double the risk.

#### HAT-BRK-103: Fyers does not enforce tag uniqueness
- **Statement:** The broker may hold two orders under one HT tag: Fyers accepts the same `orderTag` twice, confirmed live (MX-F6, 2026-10-22). Reconciliation reports a tag held more than once as the `duplicate_ht_order` mismatch for the whole account (entries blocked, one alert). Every reader of the book matches a row that holds a broker order id by that id only, and a row without one adopts the first order under its tag that the broker has not ended unfilled, else the first (`submit.TagBook`), so a duplicate's status is never read as the row's own; the fill sync records every fill of such an order, so the shares the broker holds stay tracked and under the stop, and raises the URGENT `fill.excess` alert once, on the fill that first takes an entry's fills above its quantity, after that fill's stop is placed.
- **Source:** Design review 2026-10-22 item 3; the live recording `audits/fyers-live/trading_05_writes.json` (two `place_order` calls with tag `HATTEST2` got two order ids, 26102200316678 and 26102200316686, both shown in the book as `1:HATTEST2`).
- **PR:** 25, 28
- **Verification:** CONF `brk.recorded_writes`, PBT `tags.a_tag_twice_at_the_broker_blocks_the_account`, PBT `manage.a_second_order_under_one_tag_is_tracked`, PBT `manage.a_dead_second_order_never_ends_the_entry`, PBT `tags.a_duplicated_tag_is_read_one_way`
- **Acceptance:**
  - Property: for every order book, each HT tag the broker holds more than one order for is reported once as `duplicate_ht_order` for the account, and a tag held once never is. Harm: one logical order reaches the broker twice and the account keeps opening positions.
  - Property: when a second order under an entry's tag fills, every share is recorded on the position, its stop covers them all, and `fill.excess` is raised once, however many fills make up the excess. Harm: shares bought twice sit at the broker untracked and with no stop, or the owner never learns the position outgrew its risk.
  - Property: a second order under a filled entry's tag that the broker cancels, rejects or expires unfilled, listed before or after the entry's own order, leaves the entry row on its own broker order id and status, its signal unchanged and the position under its stops. Harm: the duplicate's status is read as the entry's, and an open position's signal is ended.
- **Not done:** refusing the excess fill or fencing trades on the broker order id (the review's proposal) would leave the excess shares at the broker untracked and unprotected; a mismatch never auto-closes them either (HAT-BRK-111), so the owner sells them (`/flatten`, or by hand after reconciliation).

## §11 Reconciliation

#### HAT-BRK-110: Reconcile before the first order
- **Statement:** On startup and before the first entry of each trade date, broker orders are matched to `hat_order` by tag and broker positions to `hat_position` by symbol and quantity; no entry is placed on a trade date until a reconciliation of that date succeeds. The gate holds entries only: stops and sales (protective stops, resizes, exits, `/flatten`) go out whatever the reconciliation state, before the day's first success and after a failed run alike (owner decision D118).
- **Source:** MA "Runtime" (Live service reconciles before its first order); MA "Failure handling" (Machine down); owner decision of 2026-10-22 (D118 in the owner's decision log, on audit findings MX-F5 and MX-F8): a failed reconciliation only ever blocks new entries, stops and exits always go out.
- **PR:** 13, 15, 24
- **Verification:** TLA `OrderReconciliation!ReconcileBeforeFirstOrder`, PBT `router.reconcile_before_first_order`, PBT `router.routine_reconcile_keeps_gate`, PBT `router.entry_gate_keyed_by_trade_date`, PBT `router.failed_first_reconcile_alerts_once_per_date`
- **Acceptance:**
  - Property: in every behaviour, no entry place call happens before a successful reconciliation in the process. Harm: after a crash the engine re-enters a position it already holds.
  - Property: the router's first real entry of a trade date runs the reconciliation first; a broker that cannot be read, or a book holding an HT order HAT does not know, places nothing and ends the entry Rejected with an `entry.not_placed` alert. Harm: an order goes out on top of a book HAT has not matched.
  - Property: the gate is keyed by trade date inside `ExecutionGuard` (`reconciled_for`): a success on one date never opens a later date's entries, and a routine reconciliation (the periodic one every 5 minutes) that fails after the date's success never closes the gate. A stop or a sale is never refused by the reconciliation state. Harm: yesterday's success opens today's entries over a book HAT has not matched, an entry placed while a routine pass runs is refused, or an unreadable book keeps a stop or a sale from the broker.
  - Property: a reconciliation that fails before the trade date's first success raises one `reconcile.failed` health alert for that date, however many runs fail. Harm: the owner never learns the account cannot open entries, or the chat is flooded every five minutes.
- **Implementation:** mismatch alerts reach the user through the `Notifier` (`reconcile.notifier_sink`: one `reconcile.mismatch` health alert per new mismatch, carrying its kind, its tag or symbol and the bot, or `account` when the bot cannot be told; Telegram from PR 16, the log until then). `daily/accounts.py:AccountDesk.reconcile` raises `reconcile.failed` (the account id) once per trade date while the date is not reconciled.

#### HAT-BRK-111: Reconciliation fails closed
- **Statement:** A mismatch (broker position without a HAT row, HAT open position missing at the broker, quantity difference, an unknown HT-tagged order, or an HT tag the broker holds more than once, HAT-BRK-103) blocks new entries for the affected bot (the account when the bot cannot be told) and raises an alert; it never auto-closes or auto-adopts a position.
- **Source:** MA "Failure handling"; PLAN PR 13 (fail closed).
- **PR:** 13, 24, 28
- **Verification:** TLA `OrderReconciliation!MismatchBlocksEntries`, PBT `manage.real_exits_never_blocked`, PBT `tags.held_quantity_nets_today`, PBT `router.older_read_never_overrides_newer`
- **Acceptance:**
  - Property: in every reachable state with an unresolved mismatch for a bot, that bot accepts no entry and has raised exactly one alert per mismatch, while its exits and stops keep running. Harm: the engine trades on a false view of its book.
  - Property: the quantity compared per symbol is the broker's holding plus today's signed net CNC position (a buy today adds, a sale today of a holding subtracts, so a holding sold in full reads as 0; an intraday position is not counted) [verify the holdings and positions shapes on a live account]. Harm: a holding a GTT sold today still reads as held, so a HAT book that missed the sale passes, or one that saw it blocks the account.
  - Property: when two reconciliations finish out of order, the result of the later book read stays in force (`ExecutionGuard.begin_read`). Harm: an older clean book clears a newer mismatch and entries open over it.

#### HAT-BRK-112: Terminal orders ignored
- **Statement:** Reconciliation considers only HT-tagged orders and HAT positions; orders tagged by the SmartTrader terminal (`ST`) or untagged are out of scope.
- **Source:** PLAN C12.
- **PR:** 13, 24
- **Verification:** PBT `tags.reconcile_scope`, PBT `tags.unreadable_foreign_rows_skipped`
- **Acceptance:**
  - Property: for every broker order book mixing ST, HT and untagged orders, the reconciliation result depends only on the HT-tagged subset. Harm: manual trades are adopted or cancelled by HAT.
  - Property: an order row without an HT tag that the adapter cannot read is skipped and counted (`ReconcileReport.skipped_rows`), with one `reconcile.unreadable_row` health alert per trade date; an HT-tagged row that cannot be read fails the whole read. Harm: one manual order fails every reconciliation of the day, or an HT order HAT cannot read is silently missed.

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

A split or bonus with factor f (a 1:2 split is 0.5, a 1:1 bonus 0.5, a 1:2 bonus 2/3; 02 §5) puts every price after its ex-date on a new scale. A position opened before the ex-date and still open on it holds prices of the old scale: its stop sits above the new price, so without a rescale `hard_stop` sells it on the ex-date open for nothing but the action, a shadow position books a fake loss and stop-out, `close_below_sma` fires against an unadjusted SMA, and a broker stop re-placed at the old trigger is refused (audit 2026-10-22 F3). Implementation: `engine/rescale.py`. An action that changes no price (a dividend, factor 1) is stored too (HAT-DATA-052) but is never rescaled or flagged; its only effect is the morning GTT check of HAT-EXEC-145, since Fyers cancels a resting GTT before any ex-date. A series change is HAT-EXEC-146 (`execution/series.py`).

#### HAT-EXEC-140: Next-session actions are fetched the night before
- **Statement:** The nightly `ca.adjust` step looks up `get_corporate_actions` with ex-date = the next session for every instrument with an open position or a watchlist row of the day, and stores what it finds (HAT-DATA-052) before `features` runs, so the night's features, scan and the morning's rescale see it. A failed or incomplete lookup (including one with a row that cannot be read), or one that cannot run (no MCP client, a database error), is alerted (`corporate_action.unchecked`, symbol `*` when the whole lookup failed) and never fails the step, so the nightly chain goes on.
- **Source:** audit 2026-10-22 F3.
- **PR:** 21
- **Verification:** PBT `rescale.next_session_lookup`
- **Acceptance:**
  - Property: for every set of open positions, watchlist rows and MCP answers, the symbols looked up are exactly the held and watchlisted ones, each over (next session, next session); every action returned is stored once; and every failed lookup raises one alert while the step still succeeds; with no MCP client the step succeeds and raises one alert. Harm: a split of a held name is first seen after its ex-date close, when the fake stop-out has already happened.

#### HAT-EXEC-141: Open positions are rescaled before the open
- **Statement:** `ca.rescale` (08:40, trading days) applies every stored split or bonus with factor below 1 whose ex-date is after the day a position opened and on or before today, and that the position has not been rescaled for (an `other` action such as a rights issue, or a consolidation, changes no share count HAT can know and is not rescaled: HAT-EXEC-144): quantity ⌊q / f⌋ (a fractional entitlement is the broker's cash), average entry × f (half-even) and both stops × f (rounded down, 4 places), in one transaction with the event `position.rescaled` (`action`, `factor`, `from`, `to`). The stop guard accepts this one downward move (HAT-SCH-081). The entry quantity behind R and the partial exits is scaled the same way, so R and the exit fractions stay on one scale. Each action is applied once per position. The factor is read as the ratio with denominator up to 1000 within 1e-9 of it; a factor that is no such ratio (one sent to 4 places, 0.6667) is refused, so the position stays manage-only and is alerted rather than rescaled by a guessed ratio.
- **Source:** audit 2026-10-22 F3.
- **PR:** 21
- **Verification:** PBT `rescale.preserves_position`, PBT `rescale.applied_once`, PBT `rescale.factor_exact`, DB `governance.rescale_moves_stop`
- **Acceptance:**
  - Property: for every quantity, entry, stops and split or bonus ratio, the rescaled position keeps quantity × entry within one share's value, its open risk (entry − stop) × quantity within rounding, and the stop's distance in R, (current − initial) / (entry − initial), within rounding; `current_stop ≥ initial_stop` still holds; and on the ex-date open at the old close × f no exit rule fires (no `hard_stop`, no sale) from the action alone. Harm: the ex-date open sells the position at a fake stop or books a fake loss.
  - Property: for every sequence of `ca.rescale` runs (repeated, with actions added between), each (position, action) is applied exactly once, a position opened on or after an ex-date is never rescaled for it, and an `other` action or a consolidation is never applied. Harm: a split applied twice halves the position's prices again, or a rights issue adds shares the account does not hold.
  - Property: for every ratio n/d below 1 with d ≤ 1000, the stored 12-place factor reads back as n/d exactly, and a factor sent to 5 places or fewer that is not exactly such a ratio is refused. Harm: 0.0110 read as 11/1000 instead of 1/91 sizes the position at a tenth of its shares.

#### HAT-EXEC-142: Until rescaled, the instrument is manage-only and its exits wait
- **Statement:** While an open position has any stored action with ex-date after the day it opened and on or before today that it was not rescaled for (a split or bonus not yet applied, or an action the rescale cannot handle, HAT-EXEC-144), the exits' `ViewSource` gives it no view (no exit rule runs on its old-scale prices), and any trigger of that instrument is stored skipped with reason `corporate_action_pending`. A failed rescale fails the job (retried every 5 minutes) and alerts `corporate_action.pending`.
- **Source:** audit 2026-10-22 F3.
- **PR:** 21
- **Verification:** PBT `rescale.pending_is_manage_only`
- **Acceptance:**
  - Property: for every position with a pending action and every live price, the view source returns None and a trigger of that instrument is stored Rejected with `corporate_action_pending`; once rescaled, the view returns and the trigger is decided by the risk authority. Harm: an exit or an entry is decided on prices of the wrong scale.

#### HAT-EXEC-143: The broker stop follows the rescale down
- **Statement:** After a rescale, each fill sync first moves every live broker stop of the position resting above the rescaled stop down to it (the stop rounded down to the tick), keeping the quantity the stop already covers: a GTT by `modify_gtt`, a day stop by `modify_order`. Only then does the fill sync resize the stop to the rescaled quantity, so no stop is ever larger than the old size while its trigger is on the old scale, and a refused resize (the new shares not yet in the holdings) leaves the old shares under the new stop and is tried again by the next sync. The move waits while a sale of the position is working. This is the only downward move a broker stop makes, and only for a position with a `position.rescaled` event; a refused move is alerted (`stop.not_moved`) and tried again by the next sync. A GTT the broker cancelled before the ex-date is placed again by the morning check (HAT-EXEC-145) once the position is rescaled. *[verify: whether Fyers cancels or adjusts a resting GTT itself on an ex-date (O2(b), HAT-EXEC-145), and when the new shares reach the holdings; until they do, reconciliation sees a quantity mismatch and blocks the account's entries.]*
- **Source:** audit 2026-10-22 F3.
- **PR:** 21
- **Verification:** PBT `rescale.broker_stop_follows`
- **Acceptance:**
  - Property: for every rescaled real position and resting broker stop, after a sync every live stop rests at the rescaled trigger and quantity, no modify on the way sized the stop above the old size while its trigger was above the rescaled stop, while the broker refuses the larger size the stop rests at the rescaled trigger for the old size, and for a position never rescaled no stop is moved down. Harm: the old GTT at the pre-split trigger fires on the ex-date open and sells part of the position.

#### HAT-EXEC-144: An action the rescale cannot handle leaves the position manage-only until the owner resolves it
- **Statement:** A stored action of a held instrument that is not a split or bonus with factor below 1 (an `other` action such as a rights issue or a demerger, or a consolidation, factor 1 or above) is never applied: HAT cannot know the share count or price scale it leaves. Each position spanning one (ex-date after the day it opened, on or before today) stays manage-only (HAT-EXEC-142: no exit view, the instrument's triggers skipped) for as long as it is open, and its broker stop is left where it is. `ca.rescale` records `corporate_action.unhandled` (`action` = `<ex-date>:<type>`, `factor`, `symbol`, `virtual`) once per (position, action), virtual positions included; the relay sends it as an URGENT health alert (HAT-DEC-T15). The owner resolves the position by `/flatten` (or, from PR 35, the admin CLI); the flatten sells at most the shares the broker holds of the symbol, shared among the positions of that symbol it sells (all of them when the holdings cannot be read: the broker refuses an oversell and a flatten is never blocked). An archive purpose text naming a consolidation is reported unparsed (HAT-DATA-055), never dropped as a dividend is. Real consolidations (a rescale with factor above 1) are a later change (design review 2026-10-22 item 6, option B).
- **Source:** design review 2026-10-22 item 6 (option A).
- **PR:** 31
- **Verification:** PBT `rescale.unhandled_is_manage_only`, PBT `manage.flatten_capped_at_holdings`, PBT `corporate_actions.factor_parse_unparseable_is_reported`, PBT `rescale.price_neutral_changes_nothing`
- **Acceptance:**
  - Property: for every action the rescale cannot handle, ex-date around the entry day, with or without a split beside it, over repeated `ca.rescale` runs: the action is never applied, a position it spans gets no exit view at any price, is manage-only and its instrument awaits a rescale, and exactly one `corporate_action.unhandled` names the (position, action); a split beside it is applied once; an action on or before the entry day changes nothing. Harm: a consolidation keeps ten times the shares on an old-scale stop (R-rules fire at about 90R, a trim sells more than is held), or a rights issue's old stop sits above the ex-date price and `hard_stop` sells at the open.
  - Property: for every position, with an unhandled action, a rescaled bonus or none, and a holdings book of any size or unreadable, `/flatten` sells min(open quantity, shares held) when an unhandled action spans the position and the book reads, two positions of the symbol together sell at most the shares held, and otherwise each sells its open quantity. Harm: an oversell the broker refuses leaves the position unsold, or a sale short of what HAT holds.
  - Property: for every archive purpose text without a usable ratio that names a split, a bonus or a consolidation, the row is reported unparsed and yields no record. Harm: a consolidation in the archive is silently dropped and never alerted.
  - Property (PR 47): for every set of positions and actions, adding price-neutral rows (`dividend`, `notice`, factor 1) on any ex-date changes nothing: the same positions are manage-only, await a rescale, are rescaled and are flagged unhandled as without them. Harm: every dividend of a held stock makes the position manage-only and raises an URGENT alert.


#### HAT-EXEC-145: A GTT the broker cancelled before an ex-date is placed again before the open
- **Statement:** At 08:40 on trading days (`gtt.ex_date:<account>`, registered only while `HAT_EXDATE_GTT_CHECK` is on, default off), every open real position with a stored corporate action of any type (a dividend or another action of factor 1 included, HAT-DATA-052) whose ex-date is today or the next session, or whose instrument changed series (HAT-EXEC-146), has its GTT checked against the broker's GTT book after the fills are read. A live GTT the book does not hold (the broker cancelled it) is ended `cancelled`, alerted (`gtt.missing`, code `gtt_cancelled_by_broker`) and the position protected again: a GTT at the current stop before the open; when the broker refuses it, `stop.not_placed` is alerted and the fill sync places the intraday SL-M stop from 09:15 (HAT-EXEC-060), so the refusal does not fail the run. A GTT still resting is left alone. While the fills or the GTT book cannot be read, or a GTT's lost answer cannot be settled, the position is alerted `gtt.unverified`, nothing is placed and the run fails, so it is tried again a minute later. A position still awaiting a split or bonus rescale gets nothing until `ca.rescale` has put it on the new scale (the run fails meanwhile). A position with no such action is not looked at. The check, the fill sync, the swap and the nightly verify place a position's stops under one lock per position (`StopDesk._serialised`): the job and the fill sync run on their own threads, and neither places a GTT while the other is between ending the old one and placing the new.
- **Source:** research G01 (Fyers cancels GTTs before any ex-date); plan audit 2026-10-22 PR 47 and O2(b).
- **PR:** 47
- **Verification:** PBT `manage.ex_date_gtt_replaced`, PBT `ops.ex_date_job_behind_flag`, PBT `manage.ex_date_check_serialised`
- **Acceptance:**
  - Property: for every stored action (dividend, notice or split) with ex-date before, on, or one or two sessions after the check day, every assumed GTT-book shape of a cancelled GTT (`fyers_gtt_ex_date_assumed.json`: kept with status 1, dropped, or an unknown status) and a GTT the broker accepts or refuses: a position with an action of ex-date today or the next session whose GTT the book no longer holds gets its old row ended `cancelled`, `gtt.missing`, and a new GTT at the current stop (or, refused, `stop.not_placed` and an intraday stop from the first sync after 09:15); one whose GTT still rests sends nothing; an unreadable book sends nothing, alerts `gtt.unverified` and fails the run; one awaiting a split's rescale sends nothing and fails the run; any other position is not read. Harm: Fyers cancels the GTT the night before a dividend and the position opens the ex-date with no stop at the broker.
  - Property: for both assumed shapes of a cancelled GTT, when the fill sync's protect or the nightly verify reaches the position on another thread between the check ending the old GTT and placing the new one, exactly one GTT is placed. Harm: two GTT sales rest over the same shares.
  - Property: for every value of `HAT_EXDATE_GTT_CHECK`, `gtt.ex_date:<account>` is registered (08:40, trading days, retried every minute) exactly when it is a true value, and every other job is unchanged. Harm: the check runs on a book behaviour nobody confirmed live, or the switch turns it on without a job.
- **[verify on a live account]** (O2(b), PR 37's list): how the GTT book shows a GTT Fyers cancelled for an ex-date (status 1, dropped, or another code) and from what time; whether a GTT can be placed before the open on the ex-date; whether Fyers instead keeps or adjusts the GTT (the check then finds it resting and does nothing). The switch stays off until then.

#### HAT-EXEC-146: A series change makes the position manage-only and re-maps its symbol
- **Statement:** An open real or virtual position whose instrument is no longer active while another instrument of the same market and ISIN under another series is (NSE moved the stock from EQ to BE or BZ; HAT-DATA-020 makes that a new instrument) is manage-only: the exits' view gives it none, so no exit rule runs on the old series' stopped prices; its broker stops go out under the successor's NSE symbol, which the adapter maps to the successor's Fyers symbol (`-BE`), and a GTT placed under the old symbol (on or before the old row's `delisted_on`) that the broker still lists is cancelled and placed again under the new one, by the morning check (a refused cancel is alerted `gtt.unverified` and fails the run) or the nightly verify (a refused cancel leaves it refreshed for the next night); and a real one is alerted URGENT `position.series_changed` (`symbol`, `from`, `to`, `broker_symbol`, or `unmapped` when the successor has no Fyers symbol: then no stop is sent, since the adapter would fall back to the old `-EQ` symbol, a placement is alerted `stop.not_placed` code `series_unmapped`, and a GTT still listed under the old symbol is left for the owner) by the morning check (HAT-EXEC-145, which also re-places a GTT the broker cancelled) and by every nightly `gtt.verify`, until the owner resolves it (`/flatten`). A delisted instrument with no active successor of its ISIN is no series change. ASM and GSM are surveillance lists, not series: the instrument master holds no such flag, so they are not detected **[OWNER-PENDING]** (research O7).
- **Source:** research G17, O7; plan audit 2026-10-22 PR 47.
- **PR:** 47
- **Verification:** PBT `manage.series_change_remapped`
- **Acceptance:**
  - Property: for every successor (none, another series of the ISIN with or without a Fyers symbol, under the same or a new NSE symbol, or an active instrument of another ISIN) and every GTT-book shape, a series change gives the position no exit view, one URGENT `position.series_changed` at the morning check naming EQ → BE and the Fyers symbol (or `unmapped`), another at the nightly verify, and a GTT the broker cancelled, or one still listed under the old symbol (cancelled first), placed again under the successor's Fyers symbol, at the morning check or, with the check off, the nightly verify; a refused cancel at the check places nothing and fails the run; a GTT placed after the change is left resting; with no Fyers symbol for the successor nothing is placed or cancelled; without a successor nothing is alerted, read or sent. Harm: the old `-EQ` GTT is gone, every order under it is refused, and exit rules run on prices that stopped.
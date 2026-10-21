---
name: verify-execution
description: How to run, read and update the checks behind Hire a Trader's broker interface, the Fyers adapter, order tags, idempotent order submission, reconciliation, account onboarding, the broker session, the execution router, execution modes and confirm-mode approvals (TLA+ OrderReconciliation, ExecutionRouter, ApprovalToken, tag, adapter, submission, session and router properties, broker CONF checks). Use before and after any change to backend/app/hat/execution (the router, modes, Fyers broker, session, login and token store included), backend/app/hat/engine/approvals.py, backend/app/hat/api/fyers_auth.py, FakeBroker or the OrderReconciliation, ExecutionRouter or ApprovalToken model.
---

# verify-execution

Checks that an order reaches the broker at most once per tag and only after its attempt is persisted, that an unknown outcome is looked up by tag before any retry, that nothing is sent before a reconciliation or while a mismatch blocks it, that an unreadable broker never reads as an empty one, and that the Fyers adapter is a thin, CNC-only wrapper over HAT's own Fyers broker, session and login (K2), with one login and one token store. Since PR 15b it also checks the execution router: one risk check, three routes by the bot's effective mode, confirm cards that expire and re-check max-chase and the live conditions on Take, skips that continue as veto trades, and mode changes that wait for the next session open. Since PR 15c it also checks live trade management on the real code: broker fills into HAT's book, the broker stop under every real fill, the stop following the position's stop up, the 15:25 swap to a GTT, the nightly GTT verify, and real exits that no kill switch blocks (the model of those stops, `StopPlacement`, is in `verify-stops`). Specs: `docs/specs/hat/07-execution.md` §8-§11, `docs/specs/hat/10-ops.md` §2 (HAT-OPS-031, 032, 051), HAT-BRK-080, 091, 100, 112, HAT-DEC-I7, I8, I9, I10, BD2, HAT-SCH-053, HAT-EXEC-062, 07 §1-§3 and §13 (HAT-EXEC-010..012, 020..023, 030..032), HAT-GOV-084, 07 §4 and §6 (HAT-EXEC-060..064), 06 §6, `11-verification.md` (HAT-VER-030..034, 072).

## Properties

- `TLA OrderReconciliation!PersistBeforeCall` A broker call is made only after the attempt is stamped in hat_order.placed_at and committed. Harm: an order reaches the broker, the process dies, and nothing records that it was tried, so a restart cannot tell it happened.
- `TLA OrderReconciliation!AtMostOneEntryPerSignal` One signal has at most one live entry order at the broker; a retry reuses the tag. Harm: a retried entry is a second order and the position is doubled.
- `TLA OrderReconciliation!UnconfirmedResolvedByTag` An order whose outcome is unknown is looked up by tag before any second placement. Harm: a timeout is retried blind and the first order had landed.
- `TLA OrderReconciliation!ReconcileBeforeFirstOrder` No order is called before a reconciliation succeeded in this process lifetime. Harm: orders go out on top of a book HAT has never compared with the broker's.
- `TLA OrderReconciliation!MismatchBlocksEntries` While a mismatch is open, no entry is called for the bot or account it blocks. Harm: HAT keeps buying while its own books disagree with the broker's.
- `TLA OrderReconciliation!AlertOncePerMismatch` One alert per mismatch, however many reconciliations see it. Harm: the owner is flooded and learns to ignore the alert.
- `TLA OrderReconciliation!AccountActiveAfterCheck` An order is called only for an account that passed the capability check. Harm: orders go to an account whose funds, positions or holdings could not be read.
- `TLA OrderReconciliation!OneSessionOwner` At most one service holds an account's live session. Harm: two services rotate or use the same Fyers session and invalidate each other mid-order.
- `TLA OrderReconciliation!ProductIsCNC` Every order and stop is built with product CNC. Harm: an intraday or margin product is sent for a delivery strategy.
- `TLA OrderReconciliation!RetryAtMostOnce` A rejected order is retried at most once, with a new tag. Harm: a persistently rejected order is hammered at the broker.
- `PBT tags.shape_and_roundtrip` `encode_tag` always gives 20 alphanumeric characters `HT` + purpose + sequence + 16 Crockford characters, in the alphabet Fyers accepts, and `decode_tag` returns what was encoded. Harm: a tag the broker truncates or rejects, or a tag that cannot be matched back to its signal.
- `PBT tags.uniqueness_and_sequence` Distinct signals made in one millisecond, and distinct (purpose, sequence) of one signal, never share a tag; the 37th order of one (signal, purpose) is refused; the sequence counts earlier orders. Harm: two logical orders share a tag and a lookup adopts the wrong one.
- `PBT tags.fyers_payload_mapping` Each valid `OrderIntent` becomes exactly the Fyers payload (symbol, side, type, product CNC, tag, price and trigger only when the type needs them), through the existing `FyersBroker`. Harm: the broker receives a different order than the one HAT decided.
- `PBT tags.intent_product_cnc` An intent that is not CNC, has no HT tag, has a non-positive quantity or the wrong price fields raises `InvalidIntentError` before any SDK call. Harm: a malformed or non-delivery order is sent.
- `PBT tags.broker_errors_fail_closed` Every read raises `BrokerUnavailableError` with a fixed code when the SDK raises, returns an error or returns garbage, and a write never returns broker text. Harm: an outage reads as an empty book (nothing held, nothing open) and reconciliation or the entry gate acts on it.
- `PBT tags.reconcile_scope` `find_mismatches` depends on the broker's orders only through the HT-tagged ones, and flags exactly the four mismatch kinds. Harm: a manual order in the same account raises a false mismatch or hides a real one.
- `PBT tags.status_mapping_total` `map_fyers_status` maps every input (any code, any quantity type) to one of the order statuses, unknown or fractional codes to `unconfirmed`. Harm: an unexpected Fyers status crashes the poller or is read as filled.
- `PBT tags.idempotent_submission` On the real `reserve_order` and `send_order`, random calls, lost answers, simulated crashes and restarts leave at most one broker order per tag, never call the broker for an ended row, look up before a retry, never retry a GTT after a lost answer and do not read an unreachable broker as a rejection, and leave no attempt marker on a GTT that was never sent. Harm: a retry or restart double-places an entry or stop.
- `PBT tags.refusals_before_the_broker` A virtual order, an inactive account, an unreconciled or blocked bot, an entry that is not a buy, an exit that is not a sell, or an entry with the session not live is refused before the broker is called. Harm: an order reaches the broker that HAT's own rules forbid.
- `PBT stops.gtt_limit_offset` `gtt_limit_price` is the trigger times 0.99 rounded down to the tick, never above the trigger, never off the tick grid, and raises when the result is not positive. Harm: a stop with a limit above its trigger or off the tick grid is rejected by the exchange, or fills badly.
- `PBT broker.confirm_and_auto_need_gtt` `effective_mode` and `request_mode` allow confirm or auto only for an active account that reports `gtt_delivery`, and the Fyers adapter does not (the MVP is shadow only); a mode outside the three runs shadow and its request is refused as unknown (`UnknownModeError`), whatever the account. Harm: a live mode runs on an account that cannot hold a protective stop.
- `PBT broker.account_active_after_check` `onboard_account` marks an account active only when funds, positions and holdings were all fetched, whichever one fails. Harm: an account that cannot be read is treated as ready.
- `PBT broker.one_session_owner` `SessionRegistry` hands out one owner per account, serialises that owner's calls and refuses a call after release; the adapter's session status never raises and maps (connected, client, store error) to live, expired, missing or unknown; `record_session` writes the morning login. Harm: two owners race for one session, or a broken token store reads as live.
- `PBT ops.session_backoff_alert` The reconnect delay is 1 s doubling to 30 s, and an outage raises exactly one alert after 2 minutes, none after a recovery until the next outage. Harm: a dead session retries in a tight loop or alerts every minute.
- `CONF brk__interface_methods` The `HatBroker` protocol has exactly the 17 methods of 07 §8 and the Fyers adapter implements each with the same parameters. Harm: a method is added to or dropped from the broker boundary without the spec.
- `CONF brk__import_boundary` Engine, risk, data, router and the interface module import nothing from `app.brokers`, `fyers_apiv3` or an OpenAlgo package, directly or transitively (and the checker is shown to catch a synthetic offender). Harm: strategy code starts to depend on one broker's SDK.
- `CONF brk__fyers_capabilities` The Fyers adapter reports tag length 20, a static IP requirement, CNC only and no GTT for delivery. Harm: confirm and auto unlock before the owner verified GTT on a live account.
- `CONF fyers_adapter_wraps_existing` The adapter reaches Fyers only through HAT's `FyersBroker`, `FyersSession` and `FyersAuthService`; the SDK is imported only in `fyers_session.py` and `fyers_auth.py` (and GTT SDK calls only in `fyers_broker.py`), and nothing under `app/hat` imports `app.brokers`. Harm: a second, untested path to the broker.
- `CONF ops__single_fyers_secret_store` Fyers credential words and the token file appear only in `fyers_auth.py`, `fyers_tokens.py` and `fyers_session.py`, and no HAT table holds a broker secret. Harm: a second copy of the Fyers token.
- `CONF mvp_seed_scope` Only the Fyers adapter is registered and onboarding accepts no other broker (PR 8 extends this to the seeded strategies, markets and user). Harm: an unreviewed broker is onboarded.
- `PBT fyers_tokens.expiry_next_ist_midnight` For every instant and zone, `token_expiry` is the first IST midnight after it (strictly later, at most a day, exactly 00:00 IST); a naive time is refused. Harm: a token outlives the Fyers day and orders go out on a dead session, or it expires early and the morning login is lost.
- `PBT fyers_tokens.roundtrip_and_usable` A saved token reads back with its client id, user id and expiry, and is usable exactly until the expiry and never without a client id. Harm: a token is used past its expiry or without the app it belongs to.
- `PBT fyers_tokens.fail_closed_reads` For any file content (garbage bytes, missing or malformed expiry, wrong types, naive times) `load` never raises and never invents a token, and a token without a parseable expiry is not usable. Harm: a corrupt file reads as a good token.
- `PBT fyers_tokens.atomic_write` A write that fails at the write, the fsync or the replace leaves the old file byte-for-byte (or none), no temporary file and a fixed-message `TokenStoreError`; a good write is owner-only (0600). Harm: a half-written token file locks the owner out, or the token is readable by other users.
- `PBT fyers_tokens.token_never_shown` Saving, loading, clearing, a corrupt file and a failed write never put the token in a log line, a repr or an error message. Harm: the daily Fyers token leaks into a log.
- `PBT fyers_session.model_only_for_usable_token` At startup the session holds an SDK model exactly when the file holds a token with a client id that has not expired, and makes no network call. Harm: an expired or client-less token is installed and used.
- `PBT fyers_session.expiry_while_running` A caller that never validates still gets no model once the clock passes the expiry, and the credentials go with it. Harm: orders go out all day on yesterday's token.
- `PBT fyers_session.bad_file_drops_model` After the token file is removed, corrupted, expired or client-less, `validate_token` is false, makes no profile call and leaves no model. Harm: a logout does not log out, and old credentials stay in use.
- `PBT fyers_session.relogin_picked_up` A new token written while the app runs is used by the next validation and the old token is never used again; a reset (logout) that lands while the process-wide session is being built discards that session. Harm: the owner logs in and the process keeps failing with the dead token.
- `PBT fyers_session.expired_validation_no_call` Once the stored expiry passed, `validate_token` is false without any profile call. Harm: a known-dead token is sent to Fyers on every check.
- `PBT fyers_session.validation_retry_bound` A profile call is retried only after a re-login is found on disk and only once: at most two calls, and the last answer decides. Harm: validation hammers Fyers or loops.
- `PBT fyers_session.nested_validation` A validation started from inside a validation returns at once with whether a model exists and makes no call. Harm: a recursive validation loop.
- `PBT fyers_auth.failed_exchange_fails_closed` Whatever Fyers answers or raises (an error, a missing or empty token, a non-mapping, an SDK exception) the exchange raises a fixed-message error, writes no file, runs no hook, and leaves no auth code, secret or echoed text in the error or the log. Harm: a failed login leaves a half state or leaks the code or secret.
- `PBT fyers_auth.good_exchange_order` A good reply saves the token for the app's client id, reports connected with the user id at once, runs `token_change` before `login`, and never logs the token, secret or code. Harm: the feed starts on a stale session or the token reaches a log.
- `PBT fyers_auth.save_failure_runs_no_hook` When the token cannot be saved the old file stays and no hook runs. Harm: the process is told a new token exists when it does not.
- `PBT fyers_auth.logout_wins` A logout cancels a login whose reply is still pending, counted from the start of the logout: even while a slow disconnect hook is running, no token is written and no `login` hook fires. Harm: a logged-out user is logged in again by a slow reply.
- `PBT fyers_auth.hooks_isolated` A hook that raises never fails the login or the logout, never stops the other hooks and its message stays out of the log. Harm: a feed error blocks the daily login or the logout.
- `PBT fyers_auth.disconnect_hooks_before_file` Disconnect hooks run while the token is still on disk, then the file is removed, then `token_change` fires; a second disconnect is harmless. Harm: the feed keeps running on a token that was deleted, or a logout half-completes.
- `PBT fyers_auth.status_cache_model` The login status equals a model of the 30 s cache: recomputed from disk when older than the window, always right after a login or a logout. Harm: the page shows connected after a logout, or not connected after a login.
- `PBT fyers_auth.credentials_precedence` Each credential comes from the first non-empty name in its list (the legacy name first); a missing one raises an error naming the primary variable and carrying no value, and the credentials' repr hides the secret. Harm: the wrong app credentials are used or the secret leaks.
- `PBT fyers_auth.no_sdk_without_credentials` Without app credentials neither the login link nor the exchange builds an SDK session or touches the token file. Harm: a half-configured process calls Fyers with blank credentials.
- `PBT fyers_routes.failed_exchange_fixed_message` Every failure of the Fyers side answers 400 or 500 with one of the fixed messages, the body and the log carry no code, secret or echoed text, and no token file exists. Harm: Fyers text or the auth code reaches the browser or the log.
- `PBT fyers_routes.missing_credentials_fixed` Without app credentials the link and the exchange answer 500 with the fixed message, never the variable text. Harm: configuration detail leaks to the client.
- `PBT fyers_routes.good_exchange_connected` After a good exchange `status` is connected with the user id, disconnect flips it back, and the token never appears in a response or log. Harm: the page shows the wrong login state or the token leaks.
- `PBT fyers_routes.unreadable_or_unsaved_fixed_error` An unreadable token path gives `status` the fixed error text, and a failed save the fixed store message; neither shows a path, the token or the code. Harm: a disk problem leaks paths or secrets.
- `PBT fyers_broker.breaker_model` The circuit breaker is open exactly from the threshold-th failure in a row until the recovery time later, one success closes it, and a second breaker never moves with the first. Harm: the breaker opens late, never closes, or one broker's outage blocks another.
- `PBT fyers_broker.nothing_sent_while_open` With the breaker open no order, GTT, modify or cancel reaches the SDK, and a place says so with a definite rejection. Harm: orders are hammered at a failing broker.
- `PBT fyers_broker.only_explicit_error_rejects` A place is SUBMITTED only with ok and an id, REJECTED only on an explicit error, and SUBMIT_UNCONFIRMED for a lost, garbled, id-less or odd reply. Harm: an order that landed is read as rejected and placed twice.
- `PBT fyers_broker.bad_price_never_sent` A limit or trigger price that is NaN, infinite, zero, negative or not a number is a definite rejection with no SDK call, and a good price is sent exactly. Harm: a non-finite price passes a positivity check and reaches the exchange.
- `PBT fyers_broker.bad_gtt_price_never_sent` A GTT place or modify with a non-finite or non-positive price is refused before the SDK. Harm: a stop is placed with a nonsense price.
- `PBT fyers_broker.breakers_independent` Brokers built without a breaker get their own; two given the same breaker share it. Harm: a failure recorded through one class shadows another's (the legacy class-level state).
- `CONF ops.one_login_one_token_store` The login service and the session default use HAT's one `token_store`; the legacy client and symbol master subclass HAT's; the legacy token-store re-export, the `brokers` package and the login router are gone; the HAT router has exactly the four login routes. Harm: a second login or token copy drifts from the first.
- `TLA TradeLifecycle!OnlyAllowedTransitions` Every step of every trade is a row of the transition table of 07 §4 (a trade is never Open without an entry fill, never Closed without ExitPending). Harm: a trade reaches an impossible state and is mismanaged.
- `TLA TradeLifecycle!EndingsTerminal` No step follows Closed, Rejected, Expired or Cancelled. Harm: a closed or rejected trade re-opens and holds risk budget.
- `TLA TradeLifecycle!EventuallyEnds` Under weak fairness a Watchlisted trade reaches Open or an ending, and a trade whose full exit fired reaches Closed. Harm: pending signals linger and an exit that fired leaves a position open.
- `TLA TradeLifecycle!StateMatchesLog` The stored state is always the fold of the trade's logged steps over the table (every transition appends its event in the same transaction). Harm: a state that cannot be rebuilt from the log hides a missed event.
- `TLA TradeLifecycle!ShadowSameMachine` A virtual trade is entered and closed through the same steps as a real one (order placed, ExitPending, closed). Harm: shadow results come from a different machine than live ones and prove nothing about live.
- `TLA TradeLifecycle!ResultOnClose` A Closed trade always has its trade result (written in the closing transaction). Harm: a closed trade without a result is missing from R, expectancy and the daily-loss and stop-out counters.
- `TLA TradeLifecycle!VersionPinned` The exit rules a trade reads are those of the strategy version it entered under, even after a promotion. Harm: a promotion silently rewrites the exits of positions already held.
- `TLA TradeLifecycle!PerBotCapHolds` With the bot's lock held between the risk decision and the insert, a bot never holds more position slots than its cap. Harm: two entries of one bot evaluated at once both take the last slot.
- `CONF lifecycle_transitions_match_spec` The states, endings and (from, to) pairs of `lifecycle.TRANSITIONS` equal the table in 07 §4, row for row, and `TRADE_STATES` equals the 11 states. Harm: the machine that runs is not the machine the owner approved.
- `PBT exits.replay_reproduces_state` After every step of a shadow trade the state stored on the signal and position equals the fold of its events over the table. Harm: a state that cannot be rebuilt from the log hides a missed event.
- `PBT exits.shadow_rows_virtual` A shadow trade driven from signal to close writes only virtual orders, fills and positions with no broker ids and no fees, the broker records zero calls, the quantities add up and a closed position has one result whose R is its P&L over the initial risk. Harm: a shadow row that looks real can be reconciled against, and sold at, the broker.
- `PBT trades.retry_returns_existing_signal` A repeated trigger for the same (bot, playbook, instrument, day) returns the signal it already made and writes nothing, even after the bot left shadow mode or the instrument left the universe. Harm: a retried job crashes instead of finding the trade it already took.
- `PBT trades.shared_bot_cap` However entries are spread over a bot's playbooks the accepted ones never exceed the bot's position cap, and one is rejected exactly when the cap is full (the cap is the bot's, not the playbook's). Harm: each playbook gets its own cap and the bot holds a multiple of what the policy allows.
- `PBT config.trade_keeps_entry_version` After the bot moves to a newer strategy version an open trade still evaluates the exit rules of the version it entered under, and a trade opened after the move uses the new ones. Harm: a promotion silently rewrites the exits of positions already held.
- `DB runtime.publish_serialised` Two real nightly publishes of one bot and day, the first holding inside its scan after its "already published?" read, write one `watchlist.published`; the second waits on the bot's lock and skips. Harm: duplicate watchlist rows and events.
- `DB runtime.per_bot_serialised` Two real transactions of one bot, the first holding between its risk decision and its insert, end with one accepted entry and one rejected for the cap; the second waits on the bot's advisory lock. Harm: the cap is read before the other entry is visible and exceeded.
- `DB runtime.state_domain` `hat_signal.state` holds one of the 11 lifecycle states and `hat_position.state` one of the four a position can be in; any other word is refused. Harm: a typo or an old lowercase state silently drops a trade out of every state-based count.
- `TLA ExecutionRouter!RouteByMode` Each routed signal takes the route of the mode it was routed by: shadow a virtual position, confirm a card (a real order only after a Take), auto a real order. Harm: a signal is routed under the wrong mode.
- `TLA ExecutionRouter!ShadowNeverCallsBroker` A shadow signal never reaches the broker. Harm: a paper strategy places real orders.
- `TLA ExecutionRouter!RiskBeforeRoute` Nothing is routed without passing evaluate_signal. Harm: an entry skips every cap and kill.
- `TLA ExecutionRouter!EntryHasStop` Every accepted entry carries its stop. Harm: a position with no stop to place at the broker.
- `TLA ExecutionRouter!MaxChaseAlwaysApplied` Max-chase is applied at the trigger and again at a Take. Harm: an entry buys after the price ran away.
- `TLA ExecutionRouter!SkipBecomesShadow` A skipped or expired card continues as a virtual veto trade, never a real order. Harm: veto scoring misses skipped trades.
- `TLA ExecutionRouter!ExitsNeverAsk` No exit waits for the user. Harm: an exit stalls on a card while the price falls.
- `TLA ExecutionRouter!NoOrderOutsideHours` No entry and no order outside the calendar session, a late Take included. Harm: an order on a closed market.
- `TLA ExecutionRouter!NoOrderWhenStale` No entry and no order on a stale feed or a failed quality gate, a Take included. Harm: an order on a frozen feed.
- `TLA ExecutionRouter!GateFreshBeforeFirstEntry` No entry before the session's regime gate is known. Harm: yesterday's gate admits today's entries.
- `TLA ExecutionRouter!GateOffNoEntry` No entry while the regime gate is off, a Take included. Harm: longs bought in a regime the plan forbids.
- `TLA ExecutionRouter!ModeDomain` The mode in force, the queued mode and every signal's mode are one of the three. Harm: an unknown mode routes by accident.
- `TLA ExecutionRouter!LongOnly` Every entry order buys. Harm: a short in a long-only system.
- `TLA ExecutionRouter!AutoNeedsGtt` No real order without GTT for delivery, at the route and at a Take. Harm: a real position goes overnight without a broker stop.
- `TLA ExecutionRouter!NoAddToOpenPosition` At most one of the bot's positions or pending entries on an instrument (a veto trade holds none). Harm: pyramiding into a held name.
- `TLA ExecutionRouter!ModeChangeNextSession` The mode in force changes only at a session open. Harm: a bot switches to real orders mid-session.
- `TLA ExecutionRouter!ApprovalsDecided` Every card is eventually taken, skipped or expired (liveness). Harm: a card hangs and its slot is never freed.
- `TLA ExecutionRouter!ExitsComplete` A due exit is eventually sent, in every mode (liveness). Harm: a real position is never closed.
- `TLA ApprovalToken!NoTakeAfterExpiry` No card is taken at or after its expiry. Harm: an approval opens a position at a price the user never saw.
- `TLA ApprovalToken!ChaseRecheckOnTake` No Take places an order at a price past the max-chase level. Harm: the user approves an entry that already ran away.
- `TLA ApprovalToken!SingleDecision` A card is decided at most once. Harm: a double tap places two orders.
- `TLA ApprovalToken!CardsDecided` Every sent card is eventually decided (liveness). Harm: an undecided card waits forever.
- `PBT router.routes_by_mode` For every mode and GTT capability, one accepted trigger through the real router on `FakeBroker` gives a virtual position and no call (shadow), a 5-minute card and no call (confirm), or one HT-tagged LIMIT BUY CNC at the entry price for the risk-sized quantity after the session's reconciliation (auto), when the live conditions passed to `route` (read at placement) allow it and otherwise no order and the signal Rejected with the reason; confirm and auto without GTT run shadow; a routed signal without a position holds a slot valued at its planned entry price. Harm: a route places an order its mode never allows, or a pending entry's open risk is understated.
- `PBT router.refused_entry_is_not_sent` An auto entry whose bot became manage-only (account, bot, manual or health kill, or a pause) after the signal committed sends nothing: an unsent reserved order is cancelled and the signal ends Rejected with the kill or pause reason; an order that may be at the broker (unconfirmed) is left to reconciliation, neither placed again nor rejected. Harm: a bot buys after its kill switch, or a refusal re-sends an order the broker may hold.
- `PBT router.take_rechecks_max_chase` A Take before expiry places exactly when the live conditions allow an entry and the price is within max-chase; otherwise it records the decision, ends Rejected with the reason and places nothing; a Take at or after expiry expires the card into its veto trade; a second Take does nothing. Harm: a Take buys late, blind or past the plan's price.
- `PBT router.skip_and_expiry_become_one_veto_trade` Any mix of Skips, sweeps and late Takes ends a card Cancelled or Expired with exactly one virtual veto trade at the card's quantity and stop, no broker call, replays that match, no slot held and a stop-out that is not the bot's. Harm: a skip is scored twice or never, or blocks the bot's next entries.
- `PBT router.rejected_entry_is_retried_once` A rejected auto entry is alerted and retried once under the next tag; a second rejection ends Rejected with one `entry.not_placed` alert and no third order. Harm: a retry storm or a silent failure.
- `PBT router.reconcile_before_first_order` The first real entry of a session reconciles first; an unreadable broker or an unknown HT order places nothing and ends the entry Rejected with an alert. Harm: an order on top of an unmatched book.
- `PBT router.mode_change_takes_effect_next_session` A change queued outside hours routes nothing differently before the next open (the evening and the next pre-open included) and everything after; one asked in hours is refused and writes nothing; the open applies it to the bot row once. Harm: a bot switches to real orders mid-session.
- `PBT manage.real_fill_protected` Each entry fill the sync reads (one or several partial fills, at any time of the session or after the close) is followed, before any other broker write, by the position's stop: placed for the open quantity when none rests (an intraday SL-M SELL before 15:25, a GTT from 15:25 on), resized when one does, its trigger on the tick and never above the stop; a stop the broker refuses twice is alerted and placed by the next sync; a resize it refuses is alerted and sent again by the next sync. Harm: a filled position sits at the broker with no stop, or a stop for fewer shares than it holds.
- `PBT manage.broker_stop_only_tightens` As the position's stop rises (any steps, refused modifies among them), its intraday or GTT broker stop is modified up to it rounded down to the tick, never above it and never down; a refused modify leaves it and is alerted. Harm: a loosened broker stop, or one above the price HAT holds, sells on noise.
- `PBT manage.swap_leaves_no_gap` Whatever the broker answers to the GTT (and its retry), `gtt.swap` cancels the intraday stop only after a confirmed GTT; otherwise the intraday stop keeps working, `gtt.swap_failed` is raised and the job fails to be retried; a day stop the broker will not cancel fails the swap too. Harm: the day stop is cancelled first and the position spends the night unprotected.
- `PBT manage.verify_covers` The nightly verify refreshes a GTT the broker confirms to the current stop (`gtt.refreshed`); one it does not confirm is alerted (`gtt.missing`), cancelled at the broker and in HAT's book, and placed again; when the cancel fails, the broker is unreachable, the fills cannot be read or HAT's GTT row was never confirmed, `gtt.unverified` is raised and no GTT is modified or placed. Harm: a GTT that triggered or expired goes unnoticed and the position opens the next day without a stop.
- `PBT manage.real_exits_never_blocked` A real position's live exits come from the shared exit stack and go out as MARKET SELL CNC orders, identical with and without an account or bot kill or a pause; HAT sends nothing at the hard stop while the broker stop rests, and every sale is preceded by the broker stop's cancel or its resize to the shares left. Harm: a kill switch traps a position its exits should sell, or HAT and the broker both sell at the stop.
- `PBT manage.no_sale_after_close` A run that starts at 15:2x and reaches a sale some minutes later sends it only before 15:30 (the time sent is the run's start plus the monotonic time taken, with no clock injected); otherwise `exit.not_placed` is raised and the broker stop keeps resting. Harm: a slow close run sends market orders after the close.
- `PBT manage.entry_ended_by_broker` An entry the broker cancels, expires at the close or rejects after taking it, with nothing filled, ends its signal (Cancelled, or Rejected with `order.rejected` final); one whose send answer was lost and that is first found already ended ends Rejected (`entry_not_filled`) when the sync adopts it; replay agrees, no position opens and no stop is placed. Harm: a dead entry keeps its signal in EntryPlaced and holds one of the bot's slots.
- `PBT manage.gtt_trigger_handled` A GTT that triggers and fills only in part reduces the position and is neither resized nor doubled while its sale may still sell the rest; once that sale ends short, `gtt.unfilled` is raised once and the shares left get a new stop for exactly their quantity. Harm: a second stop under a working sale sells the shares twice, or the rest stays unprotected.
- `CONF exec__default_mode_shadow` Every seed YAML, the strategy config default, the bot row default (model and database) and the entry path start at shadow, `create_bot` refuses another mode, and an unknown mode reads as shadow. Harm: a new bot trades real money before it ran in shadow.

## Run

```bash
export JAVA_HOME=/path/to/jdk   # Java 17+ (Temurin 21 is what CI uses)
verification/tla/run.sh OrderReconciliation            # the model must pass
verification/tla/run.sh --broken OrderReconciliation   # each variant must fail with its EXPECT line
verification/tla/run.sh TradeLifecycle                # the model must pass
verification/tla/run.sh --broken TradeLifecycle       # each variant must fail with its EXPECT line
verification/tla/run.sh ExecutionRouter               # the router model must pass
verification/tla/run.sh --broken ExecutionRouter      # 18 variants, one per property
verification/tla/run.sh ApprovalToken
verification/tla/run.sh --broken ApprovalToken
cd backend
CI=1 python -m pytest tests/hat/properties/test_tags.py tests/hat/properties/test_broker.py \
  tests/hat/properties/test_stops.py tests/hat/properties/test_ops.py -q --no-cov
CI=1 python -m pytest tests/hat/properties/test_fyers_tokens.py tests/hat/properties/test_fyers_session.py \
  tests/hat/properties/test_fyers_auth.py tests/hat/properties/test_fyers_auth_routes.py \
  tests/hat/properties/test_fyers_broker.py -q --no-cov
python -m pytest tests/hat/conformance/test_conf_broker.py -q --no-cov
CI=1 python -m pytest tests/hat/properties/test_exits.py tests/hat/properties/test_trades.py -q --no-cov
CI=1 python -m pytest tests/hat/properties/test_config.py -k trade_keeps_entry_version -q --no-cov
python -m pytest tests/hat/conformance/test_conf_lifecycle.py -q --no-cov
CI=1 python -m pytest tests/hat/properties/test_router.py -q --no-cov   # the router on FakeBroker
CI=1 python -m pytest tests/hat/properties/test_manage.py -q --no-cov   # fills, broker stops, exits
# the DB checks need a throwaway Postgres whose database name ends in _test
SMARTTRADER_PG_TESTS=1 DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test \
  python -m pytest tests/hat/db -q --no-cov -m postgres -k "per_bot_serialised or publish_serialised or state_domain"
```

All adapter checks run on `FakeBroker` or a recording stand-in for the Fyers SDK. No check calls Fyers, and none needs a credential.

## Bug or model error

When a check fails, decide which side is wrong before touching anything.

- TLC prints a counterexample as numbered states. Each action names its function in the `\* code:` comment above it in `OrderReconciliation.tla`. Replay the steps against the real functions (`reserve_order`, `send_order`, `ReconciliationService.run`, `onboard_account`, `SessionRegistry.acquire`). If the code can take them, it is a bug: fix it or record a finding. If it cannot (a guard the model omits), it is a model error: add the guard and say so under "Scope changes". An earlier draft of this model showed an entry placed after a reconcile blocked the bot between stamp and call; the real `send_order` now reads the guard again just before the call, and the model's `PlaceCall` guards `reconciled` and `~blocked`.
- A failing property test prints a falsifying example; a state-machine failure lists the steps. Re-run them by hand against `FakeBroker`.
- `ExecutionRouter` and `ApprovalToken`: replay a counterexample against `ExecutionRouter.route`, `take`, `skip`, `expire`, `place_entry` and `approvals.take`/`skip`/`expire_due`, then against `test_router.py`'s `Rig` (the real router, adapter and `FakeBroker`). The router model merges an auto entry's route and placement into one step and stands a placed entry for the open position; a counterexample that needs a fill, a broker stop or a rejection belongs to `OrderReconciliation` or `StopPlacement`.
- Never weaken a property or shrink a strategy. Narrowing needs a reason under "Scope changes".
- Model bounds: three tags (the original and up to two retries; the protocol allows one), one retry, two broker orders per tag counted, one divergence, two session owners, two restarts. Assumptions: each action is one transaction or one broker call, a crash can fall between any two, a rejection means nothing was placed, and the tag lookup is consistent with the broker's book.
- Known windows, kept visible:
  - Fyers' order book may lag a placement. A lookup that finds nothing and then re-places with the same tag could double-place. The spec mandates lookup-then-retry; the model assumes a consistent lookup.
  - A reconciliation can still land between the guard re-check and the broker call (microseconds). The next order is blocked, the one in flight is not.
  - A GTT has no tag at Fyers, so an unconfirmed GTT cannot be resolved by tag. It is not retried (`UnresolvedOrderError`) and is left to the nightly GTT verify. The GTT reply shape is [verify] on a live account.
  - Reconciliation quantity is the larger of the CNC position and the holding per symbol: [verify] on a live account, and it fails closed on a partial overlap.

## Updating

- A new or changed order rule: edit `verification/tla/OrderReconciliation.tla`, keep a `\* code:` comment on every action, add or adjust a broken variant (`broken/OrderReconciliation__<mode>.cfg` with an `EXPECT:` line and the mode in the model's `ASSUME`), then run both `run.sh` commands and the property checks.
- A new Fyers payload field or status code: update `map_fyers_status` or `_order_payload` and the matching property in `test_tags.py`.
- A change to `HatBroker` needs the spec table in 07 §8 changed first (`brk__interface_methods` compares them).
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-execution` prints the digest.

## Coverage

TLA+ model:
- `verification/tla/OrderReconciliation.tla` and `verification/tla/OrderReconciliation.cfg`
- `verification/tla/broken/OrderReconciliation__no_persist.cfg`, `verification/tla/broken/OrderReconciliation__retry_new_tag.cfg`, `verification/tla/broken/OrderReconciliation__retry_without_lookup.cfg`, `verification/tla/broken/OrderReconciliation__no_reconcile.cfg`, `verification/tla/broken/OrderReconciliation__ignore_mismatch.cfg`, `verification/tla/broken/OrderReconciliation__alert_every_run.cfg`, `verification/tla/broken/OrderReconciliation__inactive_account.cfg`, `verification/tla/broken/OrderReconciliation__two_owners.cfg`, `verification/tla/broken/OrderReconciliation__non_cnc.cfg`, `verification/tla/broken/OrderReconciliation__unlimited_retry.cfg`

Code under check:
- `backend/app/hat/execution/tags.py`
- `backend/app/hat/execution/broker.py`
- `backend/app/hat/execution/gtt.py`
- `backend/app/hat/execution/fyers_adapter.py`
- `backend/app/hat/execution/guard.py`
- `backend/app/hat/execution/submit.py`
- `backend/app/hat/execution/reconcile.py`
- `backend/app/hat/execution/accounts.py`
- `backend/app/hat/execution/session.py`
- `backend/tests/hat/fakes/broker.py`
- `backend/tests/hat/fakes/fyers_sdk.py`
- `backend/app/hat/execution/fyers_broker.py`
- `backend/app/hat/execution/fyers_session.py`
- `backend/app/hat/execution/fyers_auth.py`
- `backend/app/hat/execution/fyers_tokens.py`
- `backend/app/hat/api/fyers_auth.py`

Checks:
- `backend/tests/hat/hat_exec_support.py`
- `backend/tests/hat/properties/test_tags.py`
- `backend/tests/hat/properties/test_broker.py`
- `backend/tests/hat/properties/test_stops.py`
- `backend/tests/hat/properties/test_ops.py`
- `backend/tests/hat/conformance/test_conf_broker.py`
- `backend/tests/hat/hat_fyers_support.py`
- `backend/tests/hat/properties/test_fyers_tokens.py`
- `backend/tests/hat/properties/test_fyers_session.py`
- `backend/tests/hat/properties/test_fyers_auth.py`
- `backend/tests/hat/properties/test_fyers_auth_routes.py`
- `backend/tests/hat/properties/test_fyers_broker.py`

Trade lifecycle (PR 11):
- `verification/tla/TradeLifecycle.tla` and `verification/tla/TradeLifecycle.cfg`
- `verification/tla/broken/TradeLifecycle__no_lock.cfg`, `verification/tla/broken/TradeLifecycle__state_without_event.cfg`, `verification/tla/broken/TradeLifecycle__skip_exit_pending.cfg`, `verification/tla/broken/TradeLifecycle__virtual_skips_exit_pending.cfg`, `verification/tla/broken/TradeLifecycle__reopen.cfg`, `verification/tla/broken/TradeLifecycle__result_late.cfg`, `verification/tla/broken/TradeLifecycle__exits_use_live.cfg`, `verification/tla/broken/TradeLifecycle__exit_stalls.cfg`
- `backend/app/hat/engine/trades.py`
- `backend/app/hat/engine/botlock.py`
- `backend/app/hat/engine/lifecycle.py`
- `backend/app/hat/models/runtime.py`
- `backend/tests/hat/properties/test_trades.py`
- `backend/tests/hat/properties/test_exits.py`
- `backend/tests/hat/conformance/test_conf_lifecycle.py`
- `backend/tests/hat/db/hat_db_runtime.py`
- `backend/tests/hat/db/test_db_broken_variants.py`

Execution router and approvals (PR 15b):
- `verification/tla/ExecutionRouter.tla` and `verification/tla/ExecutionRouter.cfg`
- `verification/tla/broken/ExecutionRouter__any_mode.cfg`, `verification/tla/broken/ExecutionRouter__apply_now.cfg`, `verification/tla/broken/ExecutionRouter__confirm_places_now.cfg`, `verification/tla/broken/ExecutionRouter__exit_asks.cfg`, `verification/tla/broken/ExecutionRouter__exits_wait.cfg`, `verification/tla/broken/ExecutionRouter__gate_unknown_ok.cfg`, `verification/tla/broken/ExecutionRouter__no_expiry_sweep.cfg`, `verification/tla/broken/ExecutionRouter__no_gtt_gate.cfg`, `verification/tla/broken/ExecutionRouter__no_stop_check.cfg`, `verification/tla/broken/ExecutionRouter__place_unchecked.cfg`, `verification/tla/broken/ExecutionRouter__pyramid.cfg`, `verification/tla/broken/ExecutionRouter__route_without_risk.cfg`, `verification/tla/broken/ExecutionRouter__shadow_calls_broker.cfg`, `verification/tla/broken/ExecutionRouter__short_entry.cfg`, `verification/tla/broken/ExecutionRouter__skip_drops_trade.cfg`, `verification/tla/broken/ExecutionRouter__take_ignores_feed.cfg`, `verification/tla/broken/ExecutionRouter__take_ignores_gate.cfg`, `verification/tla/broken/ExecutionRouter__take_ignores_hours.cfg`, `verification/tla/broken/ExecutionRouter__take_skips_chase.cfg`
- `verification/tla/ApprovalToken.tla` and `verification/tla/ApprovalToken.cfg`
- `verification/tla/broken/ApprovalToken__decide_twice.cfg`, `verification/tla/broken/ApprovalToken__no_chase_recheck.cfg`, `verification/tla/broken/ApprovalToken__no_sweep.cfg`, `verification/tla/broken/ApprovalToken__take_after_expiry.cfg`
- `backend/app/hat/execution/router.py`
- `backend/app/hat/execution/modes.py`
- `backend/app/hat/engine/approvals.py`
- `backend/app/hat/notify.py`
- `backend/tests/hat/properties/test_router.py`

Live trade management (PR 15c):
- `backend/app/hat/execution/protect.py`
- `backend/app/hat/execution/fills.py`
- `backend/app/hat/execution/manage.py`
- `backend/app/hat/engine/live_trades.py`
- `backend/tests/hat/hat_manage_support.py`
- `backend/tests/hat/properties/test_manage.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `d0ba323bb8d38329ecc3e61d5208a2ec0db15943`
Covered digest: `136b15947ef2783a4f60e40fa3ec98f530b91c662c6000cc5c561f4983fa2500`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- PR 12: `open_entry` and `close_position` take a fill model (`FillModel`; the default `SHADOW_FILLS` fills at the reference price with no fees, as before), and `position_state` exposes the pinned stack and state `evaluate_position` reads. The backtest passes its own fills (slippage and charges, `verify-backtest`). The shadow checks here were re-run unchanged and pass; no property was narrowed.
- K5b deleted the legacy `services/fyers_client.py`, `symbol_master.py` and `services/fyers/` (their subclass-of-HAT assertions in `CONF ops.one_login_one_token_store` became assertions that the files are gone; the rest of the check is unchanged).
- K4 deleted the legacy `FyersBroker` plugin and `app/brokers`, the `services/fyers/token_store.py` re-export and the DEV_MODE switch (HAT never honoured it), and trimmed `FakeBroker` to the methods the adapter calls (the lenient reads, the tag lookup, `ltp` and `working` state went). `ops.one_login_one_token_store` now asserts the deleted modules stay deleted.
- K2 moved the Fyers broker, session, login and token store into `app/hat/execution` and replaced the legacy suites that tested them (`test_fyers_token_store`, `test_fyers_auth_flow`, `test_fyers_client_validate_token`, `test_auth_status_cache`) with the properties above. `fyers_adapter_wraps_existing` and `ops__single_fyers_secret_store` were rewritten for the new files (the SDK only in `fyers_session.py` and `fyers_auth.py`; credential words only in `fyers_auth.py`, `fyers_tokens.py`, `fyers_session.py`) and keep their synthetic-offender checks.
- An order reply that is neither ok nor an explicit error is now `SUBMIT_UNCONFIRMED` (the legacy order path read it as a rejection): a tightening, as is refusing NaN and infinite prices.
- `fyers_session.expired_validation_no_call` needs both expiry guards removed to fail: the `fyers` property and `validate_token` each stop an expired token, so one alone is redundant by design.
- The order payload uses the broker symbol stored in `hat_broker_symbol` (-BE for a trade-for-trade stock) and falls back to the converted -EQ symbol only when the stored symbols cannot be read (`tags.fyers_payload_mapping`, `test_order_goes_out_under_the_stored_broker_symbol`; found in review).

- `OrderReconciliation` models one signal with one entry purpose: signals are independent rows keyed by tag, so one stands for "any number". Exits and stops are not modelled: they never consult the entry block (covered by `tags.refusals_before_the_broker` on the real code).
- `ExecutionRouter!NoEntryWithoutSession` and `ExecutionRouter!AutoNeedsGtt` belong to PR 15 and 16 and the router model is not built here. Session liveness and the confirm/auto gate are proven on the real code (`tags.refusals_before_the_broker`, `broker.confirm_and_auto_need_gtt`). `AutoNeedsGtt` was added to `KNOWN_GAPS` for that reason. (PR 15b built the model: `AutoNeedsGtt` left `KNOWN_GAPS`; `NoEntryWithoutSession` waits for PR 16, and `tags.refusals_before_the_broker` still checks the session on the real order path.)
- `CONF mvp_seed_scope` covers the broker half only; PR 8 extends it.
- `gtt_limit_price` raises when the rounded price is not positive (a trigger within about a tick of zero): a refinement of the spec property, not a weakening.
- PR 13 updated harness files covered by `verify-scheduler` without narrowing any check: `MERGED_PRS` includes 13, `KNOWN_GAPS` lost `TLA OrderReconciliation!AtMostOneEntryPerSignal` and gained `TLA ExecutionRouter!AutoNeedsGtt`, and `test_ops.py` gained `test_session_backoff_alert`.
- K3 edited `test_conf_broker.py`: `brk.import_boundary` lets `app.hat.data.provider.fyers_websocket` (the data socket module, HAT-OVR-006) import the SDK socket `fyers_apiv3.FyersWebsocket` and nothing else, with a synthetic offender showing any other data module still fails; `fyers_adapter_wraps_existing` adds that module to the SDK importers and `ops.single_fyers_secret_store` to the token readers. The REST model, GTT and secret rules are unchanged for every other module.
- PR 8 extended `CONF mvp_seed_scope` (`test_conf_broker.py`) with the seeded strategies, markets and owner (HAT-OVR-002); the broker half is unchanged. Its second scratch break is recorded in `verify-criteria`.

- PR 11 added `TradeLifecycle` and the shadow engine; `TLA TradeLifecycle!TypeOK` is the one property without a broken variant. The model puts the entry's Triggered and EntryPlaced steps inside one commit (Watchlisted to Open or Rejected), so AwaitingApproval and the confirm and auto paths are checked only as rows of the table (`CONF lifecycle_transitions_match_spec`), not as model actions: they are PR 15.
- `DB runtime.per_bot_serialised` commits real rows and empties `hat_event` and the runtime tables with TRUNCATE afterwards (the event log refuses DELETE). Its broken variants are code breaks (`lock_bot` a no-op, or keyed per call), registered in `test_db_broken_variants.py` with an undo list.
- `evaluate_position` and `open_entry` refuse a real position or a confirm or auto bot (`position_not_virtual`, `mode_not_shadow`); the router (PR 15) owns them. A virtual trade's continuation after a confirm skip is PR 15. (PR 15b: `open_entry` takes the effective mode and routes all three; `evaluate_position` still refuses a real position until 15c, and drives veto trades.)

- PR 15b added `ExecutionRouter` (16 invariants, 2 liveness properties) and `ApprovalToken` (3 invariants, 1 liveness property). `TypeOK` of each is the one property without a broken variant. Bounds and their reasons are in each model's header: two signals on one instrument (enough for `NoAddToOpenPosition`), one card per signal, the quality gate and the feed folded into one input, an auto entry routed and placed in one step, a placed entry standing for the open position (fills, rejections and broker stops are `OrderReconciliation`'s and 15c's `StopPlacement`'s). `ApprovalToken` checks the decision service; the token's signature and single use at the Telegram edge are PR 16's (`SecondConfirmRequired` is not built).
- `ExecutionRouter!UserCannotRaiseRisk`, `RejectNoCard` and `NoEntryWithoutSession` name PR 16 in their README rows and are not in this model yet.
- `router.take_rechecks_max_chase` needed two `@example`s: its first draft drew the over-chase region too rarely to catch a removed re-check in 40 examples (recorded below). `router.mode_change_takes_effect_next_session` gained a pre-open probe after its first draft could not see a change read as effective at its request (recorded below).
- The router properties found two bugs, fixed here: an approval written with an IST timestamp read back on SQLite five and a half hours late (approval times are now stored in UTC), and a veto trade counted as one of the bot's slots and stop-outs (`trades.bot_snapshot` and `portfolio.service.trade_results` now leave veto trades out).
- `order.rejected` drives the lifecycle step to Rejected only with `final: true` (15c sends it); the router ends a twice-rejected entry with `signal.rejected` (stage `route`) from Triggered. `CONF lifecycle_transitions_match_spec` still compares the table.

- PR 15c: the `manage.*` properties run one auto bot with one instrument on SQLite with the tick fixed at 0.05 (the tick rule is `stops.tick_size_slabs`); prices of a sale or a stop come from the real code, fills are written into `FakeBroker`'s trade and order books in the Fyers row format. `manage.real_fill_protected` has an `@example` for a stop refused twice (the generator reaches it, but rarely). `manage.real_exits_never_blocked` compares the broker calls of the same price path with and without a kill or pause rather than an independent oracle of the exit stack (the stack's semantics are `exits.*`). Writing the properties found three bugs, fixed here: a stop placed by a fill after 15:25 was an intraday order that would expire at the close after the swap had run (now a GTT; also found by `StopPlacement!AlwaysProtected`), a stop the broker refused twice was never retried in the session (the sync now protects every open real position without a working stop), and a GTT placed by a fill raised `gtt.swap_failed` instead of `stop.not_placed`. The pre-push review then found and fixed: verify re-placing a GTT over one that may still rest (it now cancels first, or alerts `gtt.unverified`), a swap reported done when a day-stop cancel failed, a partly filled GTT resized under its own working sale, an entry the exchange rejected after placement left in EntryPlaced, a sale sent while the broker stop still covered all the shares (`StopDesk.make_room`), unbounded same-day resends after rejections, and one failing position stopping `exits.close` for the rest. CodeAnt then found (fixed in the review round): a refused resize never sent again, the verify modifying GTTs when the fills could not be read, and an entry first found already ended leaving its signal in Triggered (its `order.cancelled` adoption raised `LifecycleStateError` and stopped the sync, found by `manage.entry_ended_by_broker`; it now ends at adoption). A sale's 15:30 check read the run's start time when no clock was injected (now the start plus the monotonic time taken; `manage.no_sale_after_close`). `lifecycle.step_of`: an `order.cancelled` moves state only for an entry, and an `order.filled` with `exit_triggered` stands for the exit placed (07 §4); the lifecycle and trade properties were re-run and pass.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or model (HAT-VER-072), then the break was removed and the check returned to passing. TLA breaks are the model's `BrokenMode` variants, run by `run.sh --broken`; the state lines are the last steps of TLC's counterexample.

### TLA OrderReconciliation!PersistBeforeCall

Break: broken variant `OrderReconciliation__no_persist.cfg` (the model's `BrokenMode = "no_persist"`): `Stamp` is skipped, so the broker call happens with no persisted attempt.

```
Error: Invariant PersistBeforeCall is violated.
State 3: <Reconcile ...>
State 4: <Stamp ...>
State 5: <PlaceCall ...>
```

### TLA OrderReconciliation!AtMostOneEntryPerSignal

Break: broken variant `OrderReconciliation__retry_new_tag.cfg` (the model's `BrokenMode = "retry_new_tag"`): a retry after a rejection takes a new tag instead of reusing the logical order's.

```
Error: Invariant AtMostOneEntryPerSignal is violated.
State 8: <Reserve ...>
State 9: <Stamp ...>
State 10: <PlaceCall ...>
```

### TLA OrderReconciliation!UnconfirmedResolvedByTag

Break: broken variant `OrderReconciliation__retry_without_lookup.cfg` (the model's `BrokenMode = "retry_without_lookup"`): a restarted unconfirmed order is placed again without a tag lookup.

```
Error: Invariant UnconfirmedResolvedByTag is violated.
State 7: <Reconcile ...>
State 8: <Stamp ...>
State 9: <PlaceCall ...>
```

### TLA OrderReconciliation!ReconcileBeforeFirstOrder

Break: broken variant `OrderReconciliation__no_reconcile.cfg` (the model's `BrokenMode = "no_reconcile"`): `PlaceCall` no longer requires `reconciled`.

```
Error: Invariant ReconcileBeforeFirstOrder is violated.
State 3: <Reserve ...>
State 4: <Stamp ...>
State 5: <PlaceCall ...>
```

### TLA OrderReconciliation!MismatchBlocksEntries

Break: broken variant `OrderReconciliation__ignore_mismatch.cfg` (the model's `BrokenMode = "ignore_mismatch"`): `PlaceCall` ignores an open mismatch.

```
Error: Invariant MismatchBlocksEntries is violated.
State 5: <Reserve ...>
State 6: <Stamp ...>
State 7: <PlaceCall ...>
```

### TLA OrderReconciliation!AlertOncePerMismatch

Break: broken variant `OrderReconciliation__alert_every_run.cfg` (the model's `BrokenMode = "alert_every_run"`): every reconciliation raises its alert again.

```
Error: Invariant AlertOncePerMismatch is violated.
State 2: <Diverge ...>
State 3: <Reconcile ...>
State 4: <Reconcile ...>
```

### TLA OrderReconciliation!AccountActiveAfterCheck

Break: broken variant `OrderReconciliation__inactive_account.cfg` (the model's `BrokenMode = "inactive_account"`): `PlaceCall` no longer requires an active account.

```
Error: Invariant AccountActiveAfterCheck is violated.
State 3: <Reserve ...>
State 4: <Stamp ...>
State 5: <PlaceCall ...>
```

### TLA OrderReconciliation!OneSessionOwner

Break: broken variant `OrderReconciliation__two_owners.cfg` (the model's `BrokenMode = "two_owners"`): `AcquireSession` ignores an existing owner.

```
Error: Invariant OneSessionOwner is violated.
State 1: <Initial ...>
State 2: <AcquireSession ...>
State 3: <AcquireSession ...>
```

### TLA OrderReconciliation!ProductIsCNC

Break: broken variant `OrderReconciliation__non_cnc.cfg` (the model's `BrokenMode = "non_cnc"`): an intent may be built with another product.

```
Error: Invariant ProductIsCNC is violated.
State 4: <Reserve ...>
State 5: <Stamp ...>
State 6: <PlaceCall ...>
```

### TLA OrderReconciliation!RetryAtMostOnce

Break: broken variant `OrderReconciliation__unlimited_retry.cfg` (the model's `BrokenMode = "unlimited_retry"`): the retry count is not capped.

```
Error: Invariant RetryAtMostOnce is violated.
State 8: <Reserve ...>
State 9: <Stamp ...>
State 10: <PlaceCall ...>
```

### PBT tags.shape_and_roundtrip

Break: in `tags.py`, `BASE32_ALPHABET` has `I` in place of `J`.

```
FAILED tests/hat/properties/test_tags.py::test_shape_and_roundtrip - AssertionError("assert None ... fullmatch('HT[EDGPFL][0-9A-Z][0-9A-HJKMNP-TV-Z]{16}', 'HTE0EVB9IF0C3QQS74SK')")
```

### PBT tags.uniqueness_and_sequence

Break: `encode_tag` writes `SEQUENCE_ALPHABET[0]` for every sequence.

```
E   AssertionError: ((UUID('00000000-0000-779f-bb61-a191b008a9e7'), 'entry', 0), (UUID('...'), 'entry', 1))
E   assert 'HTE0EYFVPRD1J6R0HAF7' not in {'HTE0EYFVPRD1J6R0HAF7': (UUID('...'), 'entry', 0)}
```

### PBT tags.fyers_payload_mapping

Break: `_order_payload` always sends type `LIMIT`.

```
FAILED tests/hat/properties/test_tags.py::test_fyers_payload_mapping - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

Second break (stored symbols, `test_order_goes_out_under_the_stored_broker_symbol`): `_fyers_symbol` always converts instead of using the stored symbol.

```
FAILED tests/hat/properties/test_tags.py::test_order_goes_out_under_the_stored_broker_symbol - AssertionError
```

### PBT tags.intent_product_cnc

Break: the CNC check in `_order_payload` is removed (`if False:`).

```
E   Failed: DID NOT RAISE InvalidIntentError
E   Failing test case: test_invalid_intents_raise_before_any_sdk_call(
```

### PBT tags.broker_errors_fail_closed

Break: `_read` returns `[]` when the SDK raises.

```
E   Failed: DID NOT RAISE BrokerUnavailableError
E   Failing test case: test_broker_errors_fail_closed(kind='raises', read='orders')
```

### PBT tags.reconcile_scope

Break: `find_mismatches` considers every broker order, not only HT-tagged ones.

```
E   AssertionError: assert [Mismatch(kind='unknown_ht_order', key='None', bot_id=None)] == []
```

### PBT tags.status_mapping_total

Break: `map_fyers_status` indexes `_FYERS_STATUS[status]` instead of defaulting to `unconfirmed`.

```
E   KeyError: 0
E   Failing test case: test_broker_status_mapping_is_total(code=0, filled=None, qty=None)
```

### PBT tags.idempotent_submission

Break: `send_order` skips the tag lookup before a retry (`found = None`).

```
E   AssertionError: [{'id': 'L-1', 'orderTag': 'HTE0EKRA142SDCB6FHTF', ...}, {'id': 'B-2', 'orderTag': 'HTE0EKRA142SDCB6FHTF', ...}]
E   assert 2 <= 1
```

### PBT tags.refusals_before_the_broker

Break: the session-live check in `_check_allowed` is removed.

```
E   Failed: DID NOT RAISE OrderRefusedError
E   Failing test case: test_refusals_happen_before_the_broker(
```

### PBT stops.gtt_limit_offset

Break: `gtt_limit_price` rounds up (`ROUND_CEILING`) instead of down.

```
E   AssertionError: assert Decimal('0.01') <= Decimal('0.0099')
E   Failing test case: test_gtt_limit_offset(trigger=Decimal('0.01'), tick=Decimal('0.01'))
```

### PBT broker.confirm_and_auto_need_gtt

Break: `_gtt_ready` ignores the `gtt_delivery` capability.

```
E   AssertionError: assert 'confirm' == 'shadow'
```

Second break: `request_mode` drops the unknown-mode check, so a mode outside the three is refused as a GTT problem.

```
E   app.hat.execution.modes.ModeRefusedError: mode needs an active account with GTT for delivery
E   Failing test case: test_confirm_and_auto_need_gtt(
E       mode='live',
```

### PBT broker.account_active_after_check

Break: `onboard_account` marks the account active whatever the check found.

```
E   AssertionError: assert ('active' == 'active') == False
E   Failing test case: test_account_active_after_check(failing='funds',  (truncated)
```

### PBT broker.one_session_owner

Break: `SessionRegistry.acquire` no longer refuses a second owner.

```
E   Failed: DID NOT RAISE SessionOwnedError
```

### PBT ops.session_backoff_alert

Break: `SessionWatch.observe` alerts on every down observation.

```
E   assert 2 <= 1
E   Failing test case: test_session_backoff_alert(
```

### CONF brk__interface_methods

Break: the protocol's `funds` is renamed `funds_now`.

```
E   AssertionError: assert {'cancel_gtt'...} == {'cancel_gtt'...}
E     Extra items in the left set:
E     'funds_now'
E     Extra items in the right set:
E     'funds'
```

### CONF brk__import_boundary

Break: `execution/broker.py` imports `app.brokers.plugins.fyers`.

```
E   Left contains 1 more item:
E   {'app.hat.execution.broker': ['app.hat.execution.broker', 'app.brokers.plugins.fyers']}
```

### CONF brk__fyers_capabilities

Break: the adapter's capabilities report `gtt_delivery=True`.

```
E   AssertionError: assert True is False
E    +  where True = Capabilities(gtt_delivery=True, tag_max_len=20, ...).gtt_delivery
```

### CONF fyers_adapter_wraps_existing

Break: `execution/gtt.py` imports `fyers_apiv3`.

```
E   AssertionError: PosixPath('.../backend/app/hat/execution/gtt.py')
E   assert not <re.Match object; match='\nimport fyers_apiv3'>
```

### CONF ops__single_fyers_secret_store

Break: `execution/gtt.py` reads `os.environ`.

```
E   AssertionError: assert ['execution/gtt.py:11: _X = os.environ'] == []
```

### CONF mvp_seed_scope

Break: a second adapter, `zerodha`, is registered in `ADAPTER_FACTORIES`.

```
E   AssertionError: assert {'fyers', 'zerodha'} == {'fyers'}
```

### PBT fyers_tokens.expiry_next_ist_midnight

Break: `token_expiry` returns the same IST day instead of the next midnight.

```
E   AssertionError: assert datetime.datetime(2001, 1, 1, 0, 0, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) > datetime.datetime(2001, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
E   Failing test case: test_expiry_is_the_next_ist_midnight(
E       now=datetime.datetime(2001, 1, 1, 0, 0, tzinfo=datetime.timezone.utc),  # or any other generated value
E   )
```

### PBT fyers_tokens.roundtrip_and_usable

Break: `StoredToken.is_usable` ignores the client id.

```
E   AssertionError: assert True is False
E    +  where True = is_usable(datetime.datetime(2001, 1, 1, 18, 30, tzinfo=datetime.timezone.utc))
E    +    where is_usable = StoredToken(client_id=None, user_id=None, expires_at=datetime.datetime(2001, 1, 1, 18, 30, tzinfo=datetime.timezone.utc)).is_usable
E    +      where StoredToken(client_id=None, user_id=None, expires_at=datetime.datetime(2001, 1, 1, 18, 30, tzinfo=datetime.timezone.utc)) = StoredToken('TKN000000000000', None, None, datetime.datetime(2001, 1, 1, 18, 30, tzinfo=datetime.timezone.utc))
```

### PBT fyers_tokens.fail_closed_reads

Break: A token without an expiry reads as not expired.

```
E   AssertionError: assert (None is not None)
E    +  where None = StoredToken(client_id='n', user_id=None, expires_at=None).expires_at
E   Failing test case: test_unreadable_or_untrusted_files_fail_closed(
E       content=b'{"access_token":"n","client_id":"n"}',
```

### PBT fyers_tokens.atomic_write

Break: A failed write leaves its temporary file behind.

```
E   AssertionError: assert ['.access_tok...s_token.json'] == ['access_token.json']
E     
E     At index 0 diff: '.access_token.json.5wrna3f6.tmp' != 'access_token.json'
E     Left contains one more item: 'access_token.json'
```

### PBT fyers_tokens.token_never_shown

Break: The unreadable-file warning logs the raw file content.

```
E   assert 'TKN000000000000' not in 'WARNING app... ignoring it'
E     
E     'TKN000000000000' is contained here:
E       _token": "TKN000000000000", ' is not valid UTF-8 JSON; ignoring it
```

### PBT fyers_session.model_only_for_usable_token

Break: `_load_credentials` installs an expired token.

```
FAILED tests/hat/properties/test_fyers_session.py::test_a_model_exists_only_for_a_usable_token - ExceptionGroup: Hypothesis found 2 distinct failures. (2 sub-exceptions)
```

### PBT fyers_session.expiry_while_running

Break: The `fyers` property no longer checks the expiry.

```
E   assert <test_fyers_session.Model object at 0x7f52b5ecf170> is None
E    +  where <test_fyers_session.Model object at 0x7f52b5ecf170> = <app.hat.execution.fyers_session.FyersSession object at 0x7f52b5e08e90>.fyers
E   Failing test case: test_a_token_that_expires_while_running_stops_being_used(
E       run_for=14401,
```

### PBT fyers_session.bad_file_drops_model

Break: A removed token file no longer drops the cached credentials.

```
E   assert True is False
E    +  where True = validate_token()
E    +    where validate_token = <app.hat.execution.fyers_session.FyersSession object at 0x7f202e278050>.validate_token
E   Failing test case: test_validation_never_keeps_a_model_after_the_file_goes_bad(
```

### PBT fyers_session.relogin_picked_up

Break: A reload never reports a new token.

```
E   AssertionError: assert 'tok-A' == 'tok-B'
E     
E     - tok-B
E     ?     ^
```

Second break (`test_a_reset_during_construction_never_installs_the_old_session`): `get_fyers_session` installs the built session without comparing the reset generation.

```
FAILED tests/hat/properties/test_fyers_session.py::test_a_reset_during_construction_never_installs_the_old_session - AssertionError
```

### PBT fyers_session.expired_validation_no_call

Break: The expiry check in the `fyers` property and in `validate_token` are both removed.

```
E   assert True is False
E    +  where True = validate_token()
E    +    where validate_token = <app.hat.execution.fyers_session.FyersSession object at 0x7fb87e30c4d0>.validate_token
E   Failing test case: test_validation_of_an_expired_token_makes_no_call(
```

### PBT fyers_session.validation_retry_bound

Break: The retry after a re-login makes two more profile calls.

```
E   assert (True is False)
E   Failing test case: test_validation_makes_at_most_two_profile_calls(
E       refused=True,
E       relogin=False,
```

### PBT fyers_session.nested_validation

Break: The nested-validation guard is removed.

```
E   assert 1 == 0
E    +  where 1 = sum(<generator object test_nested_validation_does_not_recurse.<locals>.<genexpr> at 0x7f5546176cf0>)
E   Failing test case: test_nested_validation_does_not_recurse(
E       state='valid',
```

### PBT fyers_auth.failed_exchange_fails_closed

Break: The SDK failure is raised with its own text.

```
E   AssertionError: assert ('AUTHCODE0000000000' not in 'login faile...y-fyers-9a1b'
E     
E     'AUTHCODE0000000000' is contained here:
E       -fyers-9a1b and 'SECRETKEY-9f8e7d6c' not in 'login faile...y-fyers-9a1b'
```

### PBT fyers_auth.good_exchange_order

Break: `login` runs before `token_change`.

```
E   AssertionError: assert ['login', 'token_change'] == ['token_change', 'login']
E     
E     At index 0 diff: 'login' != 'token_change'
E     
```

### PBT fyers_auth.save_failure_runs_no_hook

Break: `token_change` runs before the token is saved.

```
E   AssertionError: assert ['token_change'] == []
E     
E     Left contains one more item: 'token_change'
E     
```

### PBT fyers_auth.logout_wins

Break: The logout-during-login guard is removed.

```
E   Failed: DID NOT RAISE LoginCancelledError
E   Failing test case: test_a_logout_during_a_login_wins(
E       # The test always failed when commented parts were varied together.
E       reply={'s': 'ok', 'code': 200, 'access_token': 'TKN0000000000000000', 'fy_id': 'XA1234'},  # or any other generated value
```

Second break (a slow logout, `test_a_logout_that_is_still_running_cancels_a_login`): `disconnect` counts the logout only after its hooks ran.

```
FAILED tests/hat/properties/test_fyers_auth.py::test_a_logout_that_is_still_running_cancels_a_login - AssertionError: assert isinstance(None, LoginCancelledError)
```

### PBT fyers_auth.hooks_isolated

Break: A failing hook's message is logged.

```
E   AssertionError: assert 'echoed-by-fyers-9a1b' not in 'INFO app.ha...y-fyers-9a1b'
E     
E     'echoed-by-fyers-9a1b' is contained here:
E       ok secret echoed-by-fyers-9a1b
```

### PBT fyers_auth.disconnect_hooks_before_file

Break: Disconnect hooks run after the file is removed.

```
E   assert [False] == [True]
E     
E     At index 0 diff: False != True
E     
```

### PBT fyers_auth.status_cache_model

Break: `disconnect` no longer clears the status cache.

```
E   AssertionError: assert True is False
E    +  where True = FyersStatus(connected=True, user_id='XA1').connected
E    +    where FyersStatus(connected=True, user_id='XA1') = status()
E    +      where status = <app.hat.execution.fyers_auth.FyersAuthService object at 0x7f5ab6ac0eb0>.status
```

### PBT fyers_auth.credentials_precedence

Break: The environment names are read in another order.

```
E   AssertionError: assert 'CLIENT_ID' in 'FYERS_APP_ID not found in environment variables. Please add it to .env file.'
E    +  where 'FYERS_APP_ID not found in environment variables. Please add it to .env file.' = str(MissingCredentialsError('FYERS_APP_ID not found in environment variables. Please add it to .env file.'))
E    +    where MissingCredentialsError('FYERS_APP_ID not found in environment variables. Please add it to .env file.') = <ExceptionInfo MissingCredentialsError('FYERS_APP_ID not found in environment variables. Please add it to .env file.') tblen=2>.value
E   Failing test case: test_credentials_from_env_precedence(
```

### PBT fyers_auth.no_sdk_without_credentials

Break: `login_url` builds the SDK session before reading the credentials.

```
E   AssertionError: no SDK session without credentials
E   Failing test case: test_unset_credentials_fail_before_any_sdk_call(
E       code='AUTHCODE0000000000',  # or any other generated value
E   )
```

### PBT fyers_routes.failed_exchange_fixed_message

Break: The 400 answer appends the exception and the auth code.

```
E   assert "Fyers rejected the auth code. Please log in again. TokenRejectedError('Fyers rejected the auth code. Please log in again.') AUTHCODE0000000000" in {'Could not read Fyers login status', 'Failed to disconnect from Fyers', 'Failed to generate authentication URL', 'Fye... check .env file.', 'Fyers login failed. Please try again.', 'Fyers rejected the auth code. Please log in again.', ...}
E   Failing test case: test_a_failed_token_exchange_is_a_fixed_message_and_writes_nothing(
E       reply={'s': 'error', 'code': -413, 'message': 'bad code echoed-text-77ee'},
E       code='AUTHCODE0000000000',  # or any other generated value
```

### PBT fyers_routes.missing_credentials_fixed

Break: The missing-credentials answer shows the exception text.

```
E   AssertionError: assert 'CLIENT_ID mi...oed-text-77ee' == 'Fyers creden...ck .env file.'
E     
E     - Fyers credentials not found in environment variables. Please check .env file.
E     + CLIENT_ID missing echoed-text-77ee
```

### PBT fyers_routes.good_exchange_connected

Break: `status` reports not connected after a good exchange.

```
E   AssertionError: assert {'connected':...'error': None} == {'connected':...'error': None}
E     
E     Omitting 2 identical items, use -vv to show
E     Differing items:
```

### PBT fyers_routes.unreadable_or_unsaved_fixed_error

Break: The save-failure log line includes the auth code.

```
E   assert ('TKN0000000000000000' not in '{"connected...erver Error"'
E     
E     'TKN0000000000000000' is contained here:
E       rver Error" and 'AUTHCODE0000000000' not in '{"connected...erver Error"'
```

### PBT fyers_broker.breaker_model

Break: The breaker opens one failure late.

```
E   assert False == (100.0 < 101.0)
E    +  where False = is_open()
E    +    where is_open = <app.hat.execution.fyers_broker.CircuitBreaker object at 0x7fe5667ee6d0>.is_open
E    +  and   100.0 = <test_fyers_broker.Tick object at 0x7fe566e68fa0>.now
```

### PBT fyers_broker.nothing_sent_while_open

Break: `place_order` no longer checks the breaker before sending.

```
E   AssertionError: assert ('SUBMIT_UNCONFIRMED' == 'REJECTED'
E     
E     - REJECTED
E     + SUBMIT_UNCONFIRMED)
```

### PBT fyers_broker.only_explicit_error_rejects

Break: A lost answer reads as a rejection.

```
E   AssertionError: assert 'REJECTED' == 'SUBMIT_UNCONFIRMED'
E     
E     - SUBMIT_UNCONFIRMED
E     + REJECTED
```

### PBT fyers_broker.bad_price_never_sent

Break: `_float` lets NaN and infinity through.

```
E   AssertionError: assert 'SUBMITTED' == 'REJECTED'
E     
E     - REJECTED
E     + SUBMITTED
```

### PBT fyers_broker.bad_gtt_price_never_sent

Break: `modify_gtt` no longer refuses a non-positive price.

```
E   KeyError: 'status'
E   Failing test case: test_a_gtt_with_a_bad_price_never_reaches_the_sdk(
E       # The test always failed when commented parts were varied together.
E       bad=nan,  # or any other generated value
```

### PBT fyers_broker.breakers_independent

Break: A broker without its own breaker shares the process one.

```
E   assert (True and not True)
E    +  where True = is_open()
E    +    where is_open = <app.hat.execution.fyers_broker.CircuitBreaker object at 0x7f2e643d3cb0>.is_open
E    +      where <app.hat.execution.fyers_broker.CircuitBreaker object at 0x7f2e643d3cb0> = <app.hat.execution.fyers_broker.FyersBroker object at 0x7f2e64154aa0>.breaker
```

### CONF ops.one_login_one_token_store

Break: The legacy `FyersClient` is a class of its own again.

```
tests/hat/conformance/test_conf_broker.py F                              [100%]
tests/hat/conformance/test_conf_broker.py:250: in test_ops__one_login_one_token_store
E   assert None
E    +  where None = <function search at 0x7fad557d8670>('^class FyersClient\\(FyersSession\\)', '"""\nLegacy Fyers client: the process singleton over HAT\'s `FyersSession`.\n\nHAT owns the session (credentials from... a
E    +    where <function search at 0x7fad557d8670> = re.search
E    +    and   '"""\nLegacy Fyers client: the process singleton over HAT\'s `FyersSession`.\n\nHAT owns the session (credentials from... are loaded on next access."""\n    global _fyers_client\n    _fyers_client = None\
E    +    and   re.MULTILINE = re.M
FAILED tests/hat/conformance/test_conf_broker.py::test_ops__one_login_one_token_store
```

Break (K5b, the legacy client files must stay deleted): recreated `app/services/fyers_client.py`.

```
tests/hat/conformance/test_conf_broker.py F
E   AssertionError: assert not True
E    +  where True = exists()
E    +    where exists = ((PosixPath('.../backend/app') / 'services') / 'fyers_client.py').exists
```

### TLA TradeLifecycle!OnlyAllowedTransitions

Break: broken variant `TradeLifecycle__skip_exit_pending.cfg` (`BrokenMode = "skip_exit_pending"`): a full exit closes the trade without placing the exit order.

```
Error: Invariant OnlyAllowedTransitions is violated.
State 4: Commit line 134, col 5 to line 142, col 78 of module TradeLi
State 5: FireExit line 158, col 5 to line 162, col 30 of module Trade
State 6: PlaceExit line 179, col 5 to line 188, col 80 of module Trad
```

### TLA TradeLifecycle!EndingsTerminal

Break: broken variant `TradeLifecycle__reopen.cfg` (`BrokenMode = "reopen"`): a closed trade takes a further step.

```
Error: Invariant EndingsTerminal is violated.
State 5: PlaceExit line 179, col 5 to line 188, col 80 of module Trad
State 6: FillExit line 193, col 5 to line 197, col 91 of module Trade
State 7: Reopen line 212, col 5 to line 216, col 30 of module TradeLi
```

### TLA TradeLifecycle!EventuallyEnds

Break: broken variant `TradeLifecycle__exit_stalls.cfg` (`BrokenMode = "exit_stalls"`): a placed exit order never fills.

```
Error: Temporal properties were violated.
State 6: FireExit line 158, col 5 to line 162, col 30 of module Trade
State 7: PlaceExit line 179, col 5 to line 188, col 80 of module Trad
State 8: Stuttering
```

### TLA TradeLifecycle!StateMatchesLog

Break: broken variant `TradeLifecycle__state_without_event.cfg` (`BrokenMode = "state_without_event"`): the session end changes the state and writes no event.

```
Error: Invariant StateMatchesLog is violated.
State 1: Initial predicate>
State 2: Expire line 147, col 5 to line 154, col 30 of module TradeLi
```

### TLA TradeLifecycle!ShadowSameMachine

Break: broken variant `TradeLifecycle__virtual_skips_exit_pending.cfg` (`BrokenMode = "virtual_skips_exit_pending"`): a virtual trade closes without the exit step a real one takes.

```
Error: Invariant ShadowSameMachine is violated.
State 4: Commit line 134, col 5 to line 142, col 78 of module TradeLi
State 5: FireExit line 158, col 5 to line 162, col 30 of module Trade
State 6: PlaceExit line 179, col 5 to line 188, col 80 of module Trad
```

### TLA TradeLifecycle!ResultOnClose

Break: broken variant `TradeLifecycle__result_late.cfg` (`BrokenMode = "result_late"`): the trade result is written in a later transaction than the close.

```
Error: Invariant ResultOnClose is violated.
State 4: FireExit line 158, col 5 to line 162, col 30 of module Trade
State 5: PlaceExit line 179, col 5 to line 188, col 80 of module Trad
State 6: FillExit line 193, col 5 to line 197, col 91 of module Trade
```

### TLA TradeLifecycle!VersionPinned

Break: broken variant `TradeLifecycle__exits_use_live.cfg` (`BrokenMode = "exits_use_live"`): exit rules are read from the bot's live version after a promotion.

```
Error: Invariant VersionPinned is violated.
State 3: Commit line 134, col 5 to line 142, col 78 of module TradeLi
State 4: Promote line 220, col 5 to line 223, col 30 of module TradeL
State 5: PartialExit line 167, col 5 to line 174, col 78 of module Tr
```

### TLA TradeLifecycle!PerBotCapHolds

Break: broken variant `TradeLifecycle__no_lock.cfg` (`BrokenMode = "no_lock"`): two entries of one bot evaluate without its lock and both take the last slot.

```
Error: Invariant PerBotCapHolds is violated.
State 4: Evaluate line 122, col 5 to line 129, col 83 of module Trade
State 5: Commit line 134, col 5 to line 142, col 78 of module TradeLi
State 6: Commit line 134, col 5 to line 142, col 78 of module TradeLi
```

### CONF lifecycle_transitions_match_spec

Break: in `backend/app/hat/engine/lifecycle.py`: the code lets Open close without ExitPending.

```
E   AssertionError: assert {('AwaitingAp... 'Open'), ...} == {('AwaitingAp... 'Open'), ...}
FAILED tests/hat/conformance/test_conf_lifecycle.py::test_lifecycle_transitions_match_spec
============================== 1 failed in 0.22s ===============================
```

### PBT exits.replay_reproduces_state

Break: in `backend/app/hat/engine/trades.py`: a step is logged but the signal's stored state does not move.

```
FAILED tests/hat/properties/test_exits.py::test_replay_reproduces_state - app [cut]
============================== 1 failed in 26.59s ==============================
```

### PBT exits.shadow_rows_virtual

Break: in `backend/app/hat/engine/trades.py`: the shadow engine writes a real-looking order row.

```
E   assert ([<app.hat.models.runtime.HatOrder object at 0x7f1f83655a90>, <app.hat.models.runtime.HatOrder object at 0x7f1f83681a90>] and False)
FAILED tests/hat/properties/test_exits.py::test_shadow_rows_virtual - assert  [cut]
============================== 1 failed in 29.97s ==============================
```

### PBT trades.shared_bot_cap

Break: in `backend/app/hat/engine/trades.py`: the snapshot the caps read has no open positions.

```
E   AssertionError: assert 5 == 4
FAILED tests/hat/properties/test_trades.py::test_shared_bot_cap - AssertionEr [cut]
============================== 1 failed in 2.16s ===============================
```

### PBT config.trade_keeps_entry_version

Break: in `backend/app/hat/engine/trades.py`: the exit rules are read from the bot's live version, not the trade's.

```
E   AssertionError: assert ['hard_stop'] == ['hard_stop',..., 'time_stop']
FAILED tests/hat/properties/test_config.py::test_trade_keeps_entry_version -  [cut]
============================== 1 failed in 6.74s ===============================
```

### DB runtime.per_bot_serialised

Break: in `backend/app/hat/engine/trades.py`: `lock_bot` takes no lock.

```
E   AssertionError: [True, True]
FAILED tests/hat/db/test_db_runtime.py::test_runtime__per_bot_serialised - As [cut]
============================== 1 failed in 0.52s ===============================
```

### DB runtime.state_domain

Break: in `backend/app/hat/models/runtime.py`: the signal state CHECK also admits the old lowercase `open`.

```
E   AssertionError: the database accepted: signal state 'open'
FAILED tests/hat/db/test_db_runtime.py::test_runtime__state_domain - Assertio [cut]
============================== 1 failed in 0.18s ===============================
```

### PBT trades.retry_returns_existing_signal

Break: in `backend/app/hat/engine/trades.py`: the entry rules (mode, universe, policy) run before the existing-signal lookup.

```
FAILED tests/hat/properties/test_trades.py::test_retry_returns_existing_signal
```

### DB runtime.publish_serialised

Break: in `backend/app/hat/engine/watchlist.py`: `scan_and_publish` takes no bot lock (the `no_publish_lock` variant in `test_db_broken_variants.py`).

```
tests/hat/db/test_db_broken_variants.py::test_check_fails_when_rule_is_broken[runtime.publish_serialised-...] PASSED (the check raised AssertionError: 2 publishes)
```

### TLA ExecutionRouter!RouteByMode

Break: broken variant `ExecutionRouter__confirm_places_now.cfg` (the model's `BrokenMode = "confirm_places_now"`): a confirm signal is placed at the trigger with no card.

```
Error: Invariant RouteByMode is violated.
State 3: <SessionOpen ...>
State 4: <GateRefresh ...>
State 5: <Route ...>
```

### TLA ExecutionRouter!ShadowNeverCallsBroker

Break: broken variant `ExecutionRouter__shadow_calls_broker.cfg` (the model's `BrokenMode = "shadow_calls_broker"`): a shadow route also sends a real order.

```
Error: Invariant ShadowNeverCallsBroker is violated.
State 2: <SessionOpen ...>
State 3: <GateRefresh ...>
State 4: <Route ...>
```

### TLA ExecutionRouter!RiskBeforeRoute

Break: broken variant `ExecutionRouter__route_without_risk.cfg` (the model's `BrokenMode = "route_without_risk"`): `Route` skips the risk check.

```
Error: Invariant RiskBeforeRoute is violated.
State 1: <Initial ...>
State 2: <Route ...>
```

### TLA ExecutionRouter!EntryHasStop

Break: broken variant `ExecutionRouter__no_stop_check.cfg` (the model's `BrokenMode = "no_stop_check"`): the risk check accepts an entry with no stop.

```
Error: Invariant EntryHasStop is violated.
State 2: <SessionOpen ...>
State 3: <GateRefresh ...>
State 4: <Route ...>
```

### TLA ExecutionRouter!MaxChaseAlwaysApplied

Break: broken variant `ExecutionRouter__take_skips_chase.cfg` (the model's `BrokenMode = "take_skips_chase"`): a Take places without the max-chase re-check.

```
Error: Invariant MaxChaseAlwaysApplied is violated.
State 5: <Route ...>
State 6: <PriceMove ...>
State 7: <Take ...>
```

### TLA ExecutionRouter!SkipBecomesShadow

Break: broken variant `ExecutionRouter__skip_drops_trade.cfg` (the model's `BrokenMode = "skip_drops_trade"`): a Skip or an expiry opens no veto trade.

```
Error: Invariant SkipBecomesShadow is violated.
State 4: <GateRefresh ...>
State 5: <Route ...>
State 6: <Skip ...>
```

### TLA ExecutionRouter!ExitsNeverAsk

Break: broken variant `ExecutionRouter__exit_asks.cfg` (the model's `BrokenMode = "exit_asks"`): an exit of a confirm position waits for the user.

```
Error: Invariant ExitsNeverAsk is violated.
State 6: <Take ...>
State 7: <ExitDue ...>
State 8: <ExitSend ...>
```

### TLA ExecutionRouter!NoOrderOutsideHours

Break: broken variant `ExecutionRouter__take_ignores_hours.cfg` (the model's `BrokenMode = "take_ignores_hours"`): a Take places after the session closed.

```
Error: Invariant NoOrderOutsideHours is violated.
State 5: <Route ...>
State 6: <SessionClose ...>
State 7: <Take ...>
```

Second break: broken variant `ExecutionRouter__place_unchecked.cfg` (the model's `BrokenMode = "place_unchecked"`): an auto entry is placed under the conditions its risk check read, after the session closed.

```
Error: Invariant NoOrderOutsideHours is violated.
State 5: <Route ...>
State 6: <SessionClose ...>
State 7: <Place ...>
```

### TLA ExecutionRouter!NoOrderWhenStale

Break: broken variant `ExecutionRouter__take_ignores_feed.cfg` (the model's `BrokenMode = "take_ignores_feed"`): a Take places on a stale feed.

```
Error: Invariant NoOrderWhenStale is violated.
State 5: <Route ...>
State 6: <FeedFlip ...>
State 7: <Take ...>
```

### TLA ExecutionRouter!GateFreshBeforeFirstEntry

Break: broken variant `ExecutionRouter__gate_unknown_ok.cfg` (the model's `BrokenMode = "gate_unknown_ok"`): the risk check reads an unknown regime gate as on.

```
Error: Invariant GateFreshBeforeFirstEntry is violated.
State 1: <Initial ...>
State 2: <SessionOpen ...>
State 3: <Route ...>
```

### TLA ExecutionRouter!GateOffNoEntry

Break: broken variant `ExecutionRouter__take_ignores_gate.cfg` (the model's `BrokenMode = "take_ignores_gate"`): a Take places while the regime gate is off.

```
Error: Invariant GateOffNoEntry is violated.
State 7: <SessionOpen ...>
State 8: <GateRefresh ...>
State 9: <Take ...>
```

### TLA ExecutionRouter!ModeDomain

Break: broken variant `ExecutionRouter__any_mode.cfg` (the model's `BrokenMode = "any_mode"`): a mode request outside the three modes is queued.

```
Error: Invariant ModeDomain is violated.
State 1: <Initial ...>
State 2: <RequestMode ...>
```

### TLA ExecutionRouter!LongOnly

Break: broken variant `ExecutionRouter__short_entry.cfg` (the model's `BrokenMode = "short_entry"`): an auto entry sells.

```
Error: Invariant LongOnly is violated.
State 3: <SessionOpen ...>
State 4: <GateRefresh ...>
State 5: <Route ...>
```

### TLA ExecutionRouter!AutoNeedsGtt

Break: broken variant `ExecutionRouter__no_gtt_gate.cfg` (the model's `BrokenMode = "no_gtt_gate"`): confirm and auto run without GTT for delivery.

```
Error: Invariant AutoNeedsGtt is violated.
State 3: <SessionOpen ...>
State 4: <GateRefresh ...>
State 5: <Route ...>
```

### TLA ExecutionRouter!NoAddToOpenPosition

Break: broken variant `ExecutionRouter__pyramid.cfg` (the model's `BrokenMode = "pyramid"`): the risk check allows a second entry on a held instrument.

```
Error: Invariant NoAddToOpenPosition is violated.
State 3: <GateRefresh ...>
State 4: <Route ...>
State 5: <Route ...>
```

### TLA ExecutionRouter!ModeChangeNextSession

Break: broken variant `ExecutionRouter__apply_now.cfg` (the model's `BrokenMode = "apply_now"`): a mode change applies at once, even in market hours.

```
Error: Invariant ModeChangeNextSession is violated.
State 1: <Initial ...>
State 2: <SessionOpen ...>
State 3: <RequestMode ...>
```

### TLA ExecutionRouter!ApprovalsDecided

Break: broken variant `ExecutionRouter__no_expiry_sweep.cfg` (the model's `BrokenMode = "no_expiry_sweep"`): no sweep expires a card past its window.

```
Error: Temporal properties were violated.
State 10: <SessionOpen ...>
State 11: <GateRefresh ...>
State 12: <PriceMove ...>
Back to state 11: <PriceMove line 288, col 5 to line 290, col 70 of module ExecutionRouter>
```

### TLA ExecutionRouter!ExitsComplete

Break: broken variant `ExecutionRouter__exits_wait.cfg` (the model's `BrokenMode = "exits_wait"`): the exit of a real confirm position is never sent.

```
Error: Temporal properties were violated.
State 14: <GateRefresh ...>
State 15: <SessionClose ...>
State 16: <SessionOpen ...>
Back to state 14: <GateRefresh line 266, col 5 to line 269, col 70 of module ExecutionRouter>
```

### TLA ApprovalToken!NoTakeAfterExpiry

Break: broken variant `ApprovalToken__take_after_expiry.cfg` (the model's `BrokenMode = "take_after_expiry"`): a Take is accepted at or after the card's expiry.

```
Error: Invariant NoTakeAfterExpiry is violated.
State 3: <Tick ...>
State 4: <Tick ...>
State 5: <Take ...>
```

### TLA ApprovalToken!ChaseRecheckOnTake

Break: broken variant `ApprovalToken__no_chase_recheck.cfg` (the model's `BrokenMode = "no_chase_recheck"`): a Take places without the max-chase re-check.

```
Error: Invariant ChaseRecheckOnTake is violated.
State 2: <Tick ...>
State 3: <Send ...>
State 4: <Take ...>
```

### TLA ApprovalToken!SingleDecision

Break: broken variant `ApprovalToken__decide_twice.cfg` (the model's `BrokenMode = "decide_twice"`): a decided card accepts a second decision.

```
Error: Invariant SingleDecision is violated.
State 2: <Send ...>
State 3: <Take ...>
State 4: <Take ...>
```

### TLA ApprovalToken!CardsDecided

Break: broken variant `ApprovalToken__no_sweep.cfg` (the model's `BrokenMode = "no_sweep"`): no sweep expires an undecided card.

```
Error: Temporal properties were violated.
State 8: <Tick ...>
State 9: <Tick ...>
State 10: <PriceMove ...>
Back to state 9: <PriceMove line 108, col 5 to line 109, col 72 of module ApprovalToken>
```

### PBT router.routes_by_mode

Break: `effective_mode` drops the GTT gate, so confirm and auto run on an account without GTT for delivery.

```
E   AssertionError: assert 'confirm' == 'shadow'
E     
E     - shadow
E     + confirm
E   Failing test case: test_routes_by_mode(
E       mode='confirm',
```

Second break: `route` places the auto entry under the trigger's own snapshot and time (`PlacementCheck.of(request.context)`), not the conditions passed for the placement.

```
E   AssertionError: assert [{'symbol': '...'LIMIT', ...}] == []
E     Left contains one more item: {'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}
```

Third break: `bot_snapshot` values a pending signal's slot at its trigger price (the CodeAnt finding fixed here).

```
E   AssertionError: assert Decimal('99.5000') == Decimal('100')
E   Failing test case: test_routes_by_mode(
E       mode='confirm',
```

### PBT router.refused_entry_is_not_sent

Break: `place_entry` drops the kill and pause re-check (`_kill_refusal`).

```
E   AssertionError: assert [{'symbol': '...'LIMIT', ...}] == []
E     Left contains one more item: {'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}
```

Second break: a refusal ignores the open entry tag (neither cancels an unsent order nor leaves a possibly sent one to reconciliation).

```
AssertionError: assert 'Rejected' == 'Triggered'
Failing test case: test_refused_entry_is_not_sent(kill='drawdown_account', reserved='unconfirmed')
AssertionError: assert 'pending' == 'cancelled'
Failing test case: test_refused_entry_is_not_sent(kill='drawdown_account', reserved='pending')
```

### PBT router.take_rechecks_max_chase

Break: `approvals.take` ignores the max-chase re-check, so a Take past the level is placed. (the first draft of the property, without its `@example`s, passed this break; it was strengthened and now fails). Second break: `ExecutionRouter.take` passes no live-condition refusal, and `place_entry` ignores the check (second break).

```
E   AssertionError: assert ('approved' == 'market_closed'
E     
E     - market_closed
E     + approved)
E   Failing test case: test_take_rechecks_max_chase(
E       chase_bp=0,  # or any other generated value
```

```
E   AssertionError: assert ('approved' == 'max_chase'
E     
E     - max_chase
E     + approved)
E   Failing explicit example: test_take_rechecks_max_chase(
E       chase_bp=1000,
```

### PBT router.skip_and_expiry_become_one_veto_trade

Break: `bot_snapshot` counts a veto trade as one of the bot's slots (the bug this PR fixed).

```
E   AssertionError: assert [OpenPosition...virtual=True)] == []
E     
E     Left contains one more item: OpenPosition(bot_id=UUID('1bb2696a-e02c-41e3-ac17-ae824a767836'), instrument_id=UUID('565cfc6b-c007-4b27-8b14-0b39d089...y=500, avg_entry=Decimal('100.0000'), last_price=Decimal('100.0000'), current_stop=Decimal('95.0000'), is_virtual=True)
E     Use -v to get more diff
E   Failing test case: test_skip_and_expiry_become_one_veto_trade(
E       actions=[('skip', 0)],  # or any other generated value
```

### PBT router.rejected_entry_is_retried_once

Break: `MAX_ENTRY_REJECTIONS = 3`: a rejected entry is retried twice.

```
E   AssertionError: assert 3 == 2
E    +  where 3 = len([{'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}, {'symbol': 'NSE:SBIN-EQ', 'quantity'... 'side': 'BUY', 'type': 'LIMIT', ...}, {'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}])
E   Failing test case: test_rejected_entry_is_retried_once(
E       replies=[False, False],
E   )
E
```

### PBT router.reconcile_before_first_order

Break: `find_mismatches` ignores an unknown HT order (the fixture is a valid HT tag, `encode_tag`, so this is caught; the first fixture was not a HAT tag and passed this break).

```
E   AssertionError: assert [{'symbol': '...'LIMIT', ...}] == []
E     Left contains one more item: {'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}
```

Second break: `place_entry` skips the reconciliation before the session's first order.

```
E   assert 0 == 1
E    +  where 0 = len([])
E    +    where [] = places()
E    +      where places = <test_router.Rig object at 0x7f85636f84d0>.places
E   Failing test case: test_reconcile_before_first_order(
E       foreign_ht=False,  # or any other generated value
```

### PBT router.mode_change_takes_effect_next_session

Break: `modes._changes` reads a queued change as effective at its request, not at the next open (the first draft passed this break; the evening and pre-open probes now fail).

```
E   AssertionError: assert 'confirm' == 'shadow'
    |     assert modes.bot_mode(session, bot, PRE_OPEN, CALENDAR) == start
Failing test case: test_mode_change_takes_effect_next_session(start='shadow', target='confirm', minutes=270)
```

### CONF exec__default_mode_shadow

Break: `create_bot` accepts a confirm or auto bot.

```
E   Failed: DID NOT RAISE VersionError
FAILED tests/hat/conformance/test_conf_broker.py::test_exec__default_mode_shadow
```

### PBT manage.real_fill_protected

Break: in `backend/app/hat/execution/fills.py`, `FillSync._apply` records the fill but no longer calls `StopDesk.protect`.

```
E   AssertionError: assert [] == ['place_order', 'place_order']
E     Right contains 2 more items, first extra item: 'place_order'
```

Break: `FillSync.run` no longer protects open positions without a working stop (`_unprotected` skipped).

```
E   ValueError: not enough values to unpack (expected 1, got 0)
E   Failing explicit example: test_real_fill_protected(
```

Break: in `backend/app/hat/execution/protect.py`, a new stop is an intraday order whenever the market is open (the 15:25 cut removed).

```
    | AssertionError: assert ['place_order'] == ['place_gtt', 'place_gtt']
    | AssertionError: assert ('stop_day' == 'stop_day'
```

Break: in `backend/app/hat/execution/fills.py`, the sync no longer resizes a placed stop for another quantity (step 6 checks only a missing stop).

```
E   assert 250 == 500
E   Failing explicit example: test_real_fill_protected(
```

### PBT manage.broker_stop_only_tightens

Break: in `backend/app/hat/execution/protect.py`, `_move` stores the new trigger even when the broker refused the modify.

```
E   AssertionError: assert Decimal('95.0500') == Decimal('95.0000')
E   Failing test case: test_broker_stop_only_tightens(
```

Break: `broker_trigger` adds one tick after rounding down.

```
E   AssertionError: assert Decimal('95.0500') <= Decimal('95.0000')
E   Failing test case: test_broker_stop_only_tightens(
```

### PBT manage.swap_leaves_no_gap

Break: in `backend/app/hat/execution/protect.py`, `swap` cancels the intraday stop before placing the GTT.

```
E   AssertionError: assert 1 < 0
E   Failing test case: test_swap_leaves_no_gap(
```

Break: `StopDesk.swap` returns `SWAPPED` even when a day-stop cancel failed.

```
E   assert [] == [UUID('...')]
```

### PBT manage.verify_covers

Break: in `backend/app/hat/execution/protect.py`, `verify` treats an unreachable broker as a missing GTT.

```
E   AssertionError: assert 'gtt.unverified' in ['gtt.missing']
E   Failing test case: test_verify_covers(
```

Break: `verify` alerts a missing GTT but no longer places a new one.

```
E   ValueError: not enough values to unpack (expected 1, got 0)
E   Failing test case: test_verify_covers(
```

Break: the nightly verify places a new GTT without cancelling the one it could not confirm.

```
E   AssertionError: assert 'gtt.unverified' in ['gtt.missing']
E   assert 'expired' == 'cancelled'
```

Break: in `backend/app/hat/execution/manage.py`, `verify_stops` runs the verify when the fills could not be read.

```
E   AssertionError: assert 'gtt.unverified' in ['gtt.missing']
E   Failing test case: test_verify_covers(
```

### PBT manage.real_exits_never_blocked

Break: in `backend/app/hat/execution/manage.py`, `_sell` returns without sending while any kill switch row exists.

```
E   AssertionError: assert [] == [('MARKET', 165)]
E     Right contains one more item: ('MARKET', 165)
```

Break: `_sell` no longer skips `hard_stop` while a broker stop rests.

```
E   AssertionError: assert 3 == 2
E   Failing test case: test_real_exits_never_blocked(
```

Break: `StopDesk.make_room` returns True without touching the broker stop.

```
E   assert False
E   Failing test case: test_real_exits_never_blocked(
```

### PBT manage.no_sale_after_close

Break: in `backend/app/hat/execution/manage.py`, `_sending_clock` returns the run's start time for every sale.

```
E   AssertionError: assert True == False
E   Failing test case: test_no_sale_after_close(
```

### PBT manage.entry_ended_by_broker

Break: in `backend/app/hat/execution/submit.py`, `record_broker_status` records the exchange rejection without `final=True`.

```
E   AssertionError: assert 'EntryPlaced' == 'Rejected'
E   Failing test case: test_entry_ended_by_broker(
```

Break: in `backend/app/hat/execution/submit.py`, `_end_adopted_entry` records the rejection but no longer ends the signal.

```
E   AssertionError: assert 'Triggered' == 'Rejected'
E   Failing test case: test_entry_ended_by_broker(
```

Break: in `backend/app/hat/execution/submit.py`, `adopt_broker_order` records an entry first found cancelled as `order.cancelled`.

```
E   app.hat.engine.lifecycle.LifecycleStateError: no transition from Triggered on order.cancelled
E   Failing test case: test_entry_ended_by_broker(
```

### PBT manage.gtt_trigger_handled

Break: in `backend/app/hat/execution/protect.py`, `protect` no longer waits on a partially filled GTT.

```
E   assert 250 == (250 - 250)
E   Failing test case: test_gtt_trigger_handled(
```

Break: in `backend/app/hat/execution/fills.py`, a GTT trade marks the GTT `filled` whatever its filled quantity.

```
E   AssertionError: assert [('place_orde...'SL-M', ...})] == []
E     Left contains one more item: ('place_order', {'symbol': 'NSE:SBIN-EQ', 'quantity': 250, 'side': 'SELL', 'type': 'SL-M', ...})
```

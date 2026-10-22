---
name: verify-execution
description: How to run, read and update the checks behind Hire a Trader's broker interface, the Fyers adapter, order tags, idempotent order submission, reconciliation, account onboarding, the broker session, the execution router, execution modes and confirm-mode approvals (TLA+ OrderReconciliation, ExecutionRouter, ApprovalToken, tag, adapter, submission, session and router properties, broker CONF checks). Use before and after any change to backend/app/hat/execution (the router, modes, Fyers broker, session, login and token store included), backend/app/hat/engine/approvals.py, backend/app/hat/api/fyers_auth.py, FakeBroker or the OrderReconciliation, ExecutionRouter or ApprovalToken model.
---

# verify-execution

Checks that an order reaches the broker at most once per tag and only after its attempt is persisted, that an unknown outcome is looked up by tag before any retry, that nothing is sent before a reconciliation or while a mismatch blocks it, that an unreadable broker never reads as an empty one, and that the Fyers adapter is a thin, CNC-only wrapper over HAT's own Fyers broker, session and login (K2), with one login and one token store. Since PR 15b it also checks the execution router: one risk check, three routes by the bot's effective mode, confirm cards that expire and re-check max-chase and the live conditions on Take, skips that continue as veto trades, and mode changes that wait for the next session open. Since PR 15c it also checks live trade management on the real code: broker fills into HAT's book, the broker stop under every real fill, the stop following the position's stop up, the 15:25 swap to a GTT, the nightly GTT verify, and real exits that no kill switch blocks; since PR 18, that a lost broker answer on a stop or a sale is settled and never freezes a position, that a sale the exchange rejects after accepting it is retried once, that an ok reply without its list reads as unreadable, and that a non-Live sleeve routes as shadow (the model of those stops, `StopPlacement`, is in `verify-stops`). Specs: `docs/specs/hat/07-execution.md` §8-§11, `docs/specs/hat/10-ops.md` §2 (HAT-OPS-031, 032, 051), HAT-BRK-080, 091, 100, 112, HAT-DEC-I7, I8, I9, I10, BD2, HAT-SCH-053, HAT-EXEC-062, 07 §1-§3 and §13 (HAT-EXEC-010..012, 020..023, 030..032), HAT-GOV-084, 07 §4 and §6 (HAT-EXEC-060..065, 075), HAT-BRK-081, HAT-PORT-063 (through `bot_mode`), 06 §6, `11-verification.md` (HAT-VER-030..034, 072).

## Properties

- `TLA OrderReconciliation!PersistBeforeCall` A broker call is made only after the attempt is stamped in hat_order.placed_at and committed. Harm: an order reaches the broker, the process dies, and nothing records that it was tried, so a restart cannot tell it happened.
- `TLA OrderReconciliation!AtMostOneEntryPerSignal` One signal has at most one live entry order at the broker; a retry reuses the tag. Harm: a retried entry is a second order and the position is doubled.
- `TLA OrderReconciliation!UnconfirmedResolvedByTag` An order whose outcome is unknown is looked up by tag before any second placement. Harm: a timeout is retried blind and the first order had landed.
- `TLA OrderReconciliation!ReconcileBeforeFirstOrder` No entry is called before a reconciliation succeeded in this process lifetime (the model's orders are entries; a new trade date closing the gate again is `router.entry_gate_keyed_by_trade_date`). Harm: orders go out on top of a book HAT has never compared with the broker's.
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
- `PBT tags.status_mapping_total` `map_fyers_status` maps every input (any code, any quantity type, a non-ASCII digit such as `¹` included) to one of the order statuses, unknown or fractional codes to `unconfirmed`; only an ASCII integer string reads as a number. Harm: an unexpected Fyers status crashes the poller or is read as filled.
- `PBT tags.an_unreachable_broker_is_not_a_rejection` A place the broker did not act on (circuit open, no client, Fyers' -360), once or several times, raises `BrokerUnavailableError` with its code and leaves the order pending, and a later send places it once (`broker.CODES_NOT_SENT`). Harm: a spent request quota burns a sale's retries, raises `exit.not_placed` and leaves rejected rows for an order that never reached the market.
- `PBT tags.idempotent_submission` On the real `reserve_order` and `send_order`, random calls, lost answers, simulated crashes and restarts leave at most one broker order per tag, never call the broker for an ended row, look up before a retry, never send an entry of unknown outcome again (even while the book lags), never retry a GTT after a lost answer and do not read an unreachable broker as a rejection, and leave no attempt marker on a GTT that was never sent. Harm: a retry or restart double-places an entry or stop.
- `PBT router.a_stored_capability_never_enables_a_real_entry` An auto entry reaches the broker only when the stored capabilities and the adapter's both report GTT; a stored `true` the adapter does not report is refused at placement before any order is reserved and ends Rejected `no_gtt_delivery`. Harm: a hand-written or stale account row buys real shares with no overnight stop.
- `PBT broker.session_check_refreshes_capabilities` Every session check, live or not, rewrites the stored capabilities to the adapter's after committing its session row, with one `broker.capabilities_changed` per change (GTT before and after, the changed keys); a refresh that fails (the adapter's answer, or its own event write) leaves them, also at the next commit, and never undoes the row. Harm: a flip of the adapter's GTT value never reaches an existing account, or a failed refresh loses the day's login.
- `PBT router.desks_start_with_the_adapters_capabilities` Building the desks rewrites each account's capabilities to its adapter's before any routing, so the first signal routes auto only when the adapter reports GTT; a capability read never waits behind a broker call holding the session owner. Harm: a stale row sends cards or entries the placement then refuses.
- `PBT tags.refusals_before_the_broker` A virtual order, an inactive account, an entry on an unreconciled trade date or for a blocked bot (a stop or a sale passes both, D118), an entry that is not a buy, an exit that is not a sell, an entry with the session not live, or an entry without GTT for delivery in both the stored capabilities and the adapter's own (a stop or a sale is never refused for it, HAT-EXEC-012) is refused before the broker is called. Harm: an order reaches the broker that HAT's own rules forbid.
- `PBT stops.gtt_limit_offset` `gtt_limit_price` is the trigger times 0.99 rounded down to the tick, never above the trigger, never off the tick grid, and raises when the result is not positive. Harm: a stop with a limit above its trigger or off the tick grid is rejected by the exchange, or fills badly.
- `PBT broker.confirm_and_auto_need_gtt` `effective_mode` and `request_mode` allow confirm or auto only for an active account that reports `gtt_delivery`, and the Fyers adapter does not (the MVP is shadow only); a mode outside the three runs shadow and its request is refused as unknown (`UnknownModeError`), whatever the account. Harm: a live mode runs on an account that cannot hold a protective stop.
- `PBT broker.account_active_after_check` `onboard_account` marks an account active only when funds, positions and holdings were all fetched, whichever one fails. Harm: an account that cannot be read is treated as ready.
- `PBT broker.token_refused_is_session_expired` Over the real `FyersBroker` answering the -16 replies recorded on 2026-10-22, the adapter's session status is EXPIRED though the token file is in date, and every read fails with `broker_session_expired`; any other error reply or SDK failure is an outage: reads fail with `broker_unavailable` and the session stays LIVE; the profile is read once per token per 30 s window. Harm: a revoked or server-expired token reads as an outage, so nobody is asked to log in again while every read fails.
- `PBT broker.token_refused_on_a_read_ends_the_check` A -16 on any read inside the 30 s window of a passed profile check makes the next `session_status` EXPIRED at once (CodeAnt, PR 23). Harm: the alert and login prompt wait up to 30 s while every read already fails.
- `PBT broker.one_session_owner` `SessionRegistry` hands out one owner per account, serialises that owner's calls and refuses a call after release; the adapter's session status never raises and maps (connected, client, store error) to live, expired, missing or unknown; `record_session` writes the morning login. Harm: two owners race for one session, or a broken token store reads as live.
- `PBT ops.session_backoff_alert` The reconnect delay is 1 s doubling to 30 s, and an outage raises exactly one alert after 2 minutes, none after a recovery until the next outage. Harm: a dead session retries in a tight loop or alerts every minute.
- `PBT ops.no_outage_before_the_login_window` An outage is counted from 08:30 IST: checks before it never alert and reset the watch, so a session down since midnight alerts once, 2 minutes after the first check from 08:30, only if it is still down. Harm: a `broker.session_lost` alert every night at 00:02 trains the owner to ignore it.
- `PBT router.broker_health_follows_the_session` After every session check of the desks, each bot of the account has `broker_health` tripped iff the session is not live (a failed read counts) between 09:05 and the 15:30 close IST; a tripped `feed_health` stays, and the account switch and a bot of another account are untouched (checks are at least a minute apart: a trip wins a same-instant tie by design, `kill._latest`). Harm: a missed login leaves the bots looking healthy while their exits are off, or a check clears another reason's trip.
- `CONF brk__interface_methods` The `HatBroker` protocol has exactly the 17 methods of 07 §8 and the Fyers adapter implements each with the same parameters. Harm: a method is added to or dropped from the broker boundary without the spec.
- `CONF brk__import_boundary` Engine, risk, data, router and the interface module import nothing from `app.brokers`, `fyers_apiv3` or an OpenAlgo package, directly or transitively (and the checker is shown to catch a synthetic offender). Harm: strategy code starts to depend on one broker's SDK.
- `CONF brk__fyers_capabilities` The Fyers adapter reports tag length 20, a static IP requirement, CNC only and GTT for delivery (HAT-BRK-091, verified on the live account 2026-10-22, PR 25). Harm: confirm and auto stay locked though GTT works, or unlock on an adapter that cannot place one.
- `PBT broker.the_fyers_adapter_unlocks_modes_on_an_active_account` With the adapter's own capabilities every mode runs as asked on an active account and only shadow on any other; with `gtt_delivery` false only shadow runs anywhere (HAT-BRK-091, HAT-EXEC-012). Harm: a real entry on an inactive account, or one without an overnight stop.
- `CONF brk__recorded_writes` The write replies recorded live on 2026-10-22 (`tests/hat/fixtures/fyers_live_writes.json`) hold: the GTT payload the broker builds is the one Fyers accepted (`orderTag` included); place, modify and cancel replies read as placed and ok; GTT book rows in the live keys (`tran_side`, `product_type`, `ordertag` `1:GTT<tag>` or `2:GTTUntagged`) read as resting GTTs with their tag, cancelled ones left out; the two orders under one tag (MX-F6) both read as rejections; the fixture holds no account id. Harm: HAT reads a shape Fyers never sends, and a real GTT book is unreadable (as it was before PR 25).
- `CONF fyers_adapter_wraps_existing` The adapter reaches Fyers only through HAT's `FyersBroker`, `FyersSession` and `FyersAuthService`; the SDK's HTTP client `fyersModel` is imported only in `fyers_ipv4.py`, after its hook is installed, and session and auth take it from `fyers_ipv4.sdk` (the data socket's `FyersWebsocket` only in `data/provider/fyers_websocket.py`; GTT SDK calls only in `fyers_broker.py`), and nothing under `app/hat` imports `app.brokers`. Harm: a second, untested path to the broker, or an SDK client that reaches Fyers over IPv6.
- `PBT fyers_ipv4.fyers_http_leaves_over_ipv4` Through a real `requests.Session` over a scripted resolver and socket, a request to a Fyers host (`fyers.in` or a subdomain, any case, a trailing dot) opens AF_INET sockets only, to its IPv4 addresses in order until one connects, none when it has no IPv4 address; any other host connects as urllib3 does by itself (HAT-BRK-093). Harm: orders leave over IPv6 and Fyers refuses every write with -50 (not the whitelisted IP), as the live test of 2026-10-22 found.
- `PBT fyers_ipv4.is_fyers_host_exact` Exactly `fyers.in` and its subdomains are Fyers hosts (case and a trailing dot ignored); a look-alike is not. Harm: another host is forced onto IPv4, or a Fyers host escapes the hook.
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
- `PBT fyers_auth.a_good_exchange_saves_once_and_reports_in_order` A good reply (the documented `generate_token` shape, which has no user id) saves the token for the app's client id, reports connected with the user id from the account's profile (the recorded reply) at once and after the cache expires, none (never an empty string) when the profile is refused or fails, runs `token_change` before `login`, and never logs the token, secret, code or profile error text. Harm: the feed starts on a stale session, the token reaches a log, or the login status shows no user (live check 2026-10-22).
- `PBT fyers_auth.save_failure_runs_no_hook` When the token cannot be saved the old file stays and no hook runs. Harm: the process is told a new token exists when it does not.
- `PBT fyers_auth.logout_wins` A logout cancels a login whose reply is still pending, counted from the start of the logout: even while a slow disconnect hook is running, no token is written and no `login` hook fires. Harm: a logged-out user is logged in again by a slow reply.
- `PBT fyers_auth.hooks_isolated` A hook that raises never fails the login or the logout, never stops the other hooks and its message stays out of the log. Harm: a feed error blocks the daily login or the logout.
- `PBT fyers_auth.disconnect_hooks_before_file` Disconnect hooks run while the token is still on disk, then the file is removed, then `token_change` fires; a second disconnect is harmless. Harm: the feed keeps running on a token that was deleted, or a logout half-completes.
- `PBT fyers_auth.status_cache_model` The login status equals a model of the 30 s cache: recomputed from disk when older than the window, always right after a login or a logout. Harm: the page shows connected after a logout, or not connected after a login.
- `PBT fyers_auth.credentials_precedence` Each credential comes from the first non-empty name in its list (the legacy name first); a missing one raises an error naming the primary variable and carrying no value, and the credentials' repr hides the secret. Harm: the wrong app credentials are used or the secret leaks.
- `PBT fyers_auth.no_sdk_without_credentials` Without app credentials neither the login link nor the exchange builds an SDK session or touches the token file. Harm: a half-configured process calls Fyers with blank credentials.
- `PBT fyers_auth_routes.cross_site_write_refused` (HAT-DEC-BD5, PR 38) For every write header (absent, `1`, other values), `Origin` (absent, a frontend origin, a page on another site, `null`), `Sec-Fetch-Site` and with or without the app's origin rule wired, both login POSTs run only when the header is `1` and the request names a frontend origin, or names none and is not marked cross-site or same-site. A refusal is a 403 with `guard.WRITE_REFUSED`, writes or removes no token and runs no hook. Five `@example`s pin the harmful shapes. Harm: any page the owner's browser opens logs HAT out of Fyers mid-session or plants its own token.
- `PBT fyers_auth_routes.token_for_another_account_refused` (HAT-DEC-BD5, PR 38) For every set of broker accounts (broker, `account_ref`, status) and every user a token belongs to (None: the profile failed), `POST /token` saves the token exactly when no Fyers account is active or the user is an active one's `account_ref`; otherwise 403 with `ACCOUNT_UNCONFIRMED` (unknown user) or `WRONG_ACCOUNT`, no file, and neither token nor code in the body or log. With the accounts unreadable (`db_down`: the table is dropped) it answers 503 with `DB_ERROR` and writes nothing. Harm: HAT trades or reconciles on another Fyers account.
- `PBT fyers_routes.failed_exchange_fixed_message` Every failure of the Fyers side answers 400 or 500 with one of the fixed messages, the body and the log carry no code, secret or echoed text, and no token file exists. Harm: Fyers text or the auth code reaches the browser or the log.
- `PBT fyers_routes.missing_credentials_fixed` Without app credentials the link and the exchange answer 500 with the fixed message, never the variable text. Harm: configuration detail leaks to the client.
- `PBT fyers_routes.good_exchange_connected` After a good exchange `status` is connected with the user id, disconnect flips it back, and the token never appears in a response or log. Harm: the page shows the wrong login state or the token leaks.
- `PBT fyers_routes.unreadable_or_unsaved_fixed_error` An unreadable token path gives `status` the fixed error text, and a failed save the fixed store message; neither shows a path, the token or the code. Harm: a disk problem leaks paths or secrets.
- `PBT fyers_broker.breaker_model` The circuit breaker is open exactly from the threshold-th failure in a row until the recovery time later, one success closes it, and a second breaker never moves with the first. Harm: the breaker opens late, never closes, or one broker's outage blocks another.
- `PBT fyers_broker.nothing_sent_while_open` With the breaker open no order, GTT, modify or cancel reaches the SDK, and a place says so with a definite rejection. Harm: orders are hammered at a failing broker.
- `PBT fyers_broker.a_rejection_with_an_id_stays_rejected` An explicit error stays a rejection though it carries the id of the order Fyers created and refused (the recorded RMS refusal): REJECTED with no order id, and the adapter's `rejected` with no broker id (HAT-BRK-081). Harm: a refused order read as placed leaves the signal waiting on an order that never fills, or a refused stop read as resting leaves the position unprotected.
- `PBT broker.rate_limit_is_its_own_code` Fyers' -360 (the plan's daily requests spent) makes every read fail closed with `broker_rate_limited` and every write a refusal with that code (which `send_order` treats as not sent), leaves the session LIVE and carries no broker text (HAT-BRK-082). Harm: a spent quota reads as an outage or a lost login, or a refused write as unknown.
- `PBT fyers_broker.only_explicit_error_rejects` A place is SUBMITTED only with ok and an id, REJECTED only on an explicit error, and SUBMIT_UNCONFIRMED for a lost, garbled, id-less or odd reply. Harm: an order that landed is read as rejected and placed twice.
- `PBT fyers_broker.bad_price_never_sent` A limit or trigger price that is NaN, infinite, zero, negative or not a number is a definite rejection with no SDK call, and a good price is sent exactly. Harm: a non-finite price passes a positivity check and reaches the exchange.
- `PBT fyers_broker.bad_gtt_price_never_sent` A GTT place or modify with a non-finite or non-positive price is refused before the SDK. Harm: a stop is placed with a nonsense price.
- `PBT fyers_broker.an_ok_reply_without_its_list_is_unreadable` An ok reply to the order, trade, position, holding or GTT book read whose list key is missing or not a list raises `FyersUnavailableError`, never an empty list. Harm: a book read as empty re-sends an order or a stop that rests at the broker.
- `PBT fyers_broker.a_circuit_opening_mid_place_is_not_sent` A place or GTT place whose breaker opens between the caller's check and the guard is a definite rejection (`BROKER_CIRCUIT_OPEN`) with no SDK call, never a lost answer. Harm: an order that never left is chased as unconfirmed and its position frozen.
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
- `PBT trades.global_open_risk_across_bots` (HAT-RISK-030; the cap is 1.5% since PR 46, D128) Two bots of one owner on different broker accounts, triggers interleaved in any order through `open_entry`: a trigger is rejected with `global_open_risk` exactly when the open risk of both bots plus its own exceeds 1.5% of total equity, is accepted otherwise, and the owner never holds more. Harm: each bot reads only its own positions, so N bots hold N x 1.5%.
- `DB runtime.owner_serialised` (HAT-RISK-030) Two real transactions of different bots of one owner (on different broker accounts), the first holding between its risk decision and its insert, with global open-risk room for one entry, end with one accepted and one rejected; the second waits on the owner's advisory lock. Harm: two bots' triggers in one instant both fit on one snapshot and together exceed the cap.
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
- `PBT router.entry_needs_live_session` An accepted auto entry is placed only when its broker account has a live `hat_broker_session` for the placement's trade date (written by the runtime's session watch); a missing, expired or unknown session, or a live one of another day, sends nothing and ends the signal Rejected with `broker_session_not_live`. Harm: an entry goes out on an account nobody logged in today (HAT-OPS-023).
- `PBT router.refused_entry_is_not_sent` An auto entry whose bot became manage-only (account, bot, manual or health kill, or a pause) after the signal committed sends nothing: an unsent reserved order is cancelled and the signal ends Rejected with the kill or pause reason; an order that may be at the broker (unconfirmed) is left to reconciliation, neither placed again nor rejected. Harm: a bot buys after its kill switch, or a refusal re-sends an order the broker may hold.
- `PBT router.take_rechecks_max_chase` A Take before expiry places exactly when the live conditions allow an entry and the price is within max-chase; otherwise it records the decision, ends Rejected with the reason and places nothing; a Take at or after expiry expires the card into its veto trade; a second Take does nothing. Harm: a Take buys late, blind or past the plan's price.
- `PBT router.skip_and_expiry_become_one_veto_trade` Any mix of Skips, sweeps and late Takes ends a card Cancelled or Expired with exactly one virtual veto trade at the card's quantity and stop, no broker call, replays that match, no slot held and a stop-out that is not the bot's. Harm: a skip is scored twice or never, or blocks the bot's next entries.
- `PBT router.rejected_entry_is_retried_once` A rejected auto entry is alerted and retried once under the next tag; a second rejection ends Rejected with one `entry.not_placed` alert and no third order. Harm: a retry storm or a silent failure.
- `PBT router.reconcile_before_first_order` The first real entry of a session reconciles first; an unreadable broker or an unknown HT order places nothing and ends the entry Rejected with an alert. Harm: an order on top of an unmatched book.
- `PBT router.mode_gates_hold_bots_in_shadow` A Live bot on an active account with GTT for delivery runs the mode it asked for only while DDPI was confirmed on its account by the session's open (one confirmed during the session waits for the next open; one revoked holds the bot back at once) and the policy in force at the open confirms the base weights; otherwise it runs shadow and calls no broker (HAT-GOV-085). Harm: real orders before DDPI is registered (sales stall on a TPIN prompt) or before the owner set the allocation numbers.
- `PBT router.auto_places_only_high_confidence` An accepted auto signal labelled high sends exactly one entry order; any other label, or none, goes to the owner as in confirm: a card, the signal AwaitingApproval and no broker call (HAT-EXEC-014, D149). Harm: a medium- or low-confidence trade is bought without the owner's approval.
- `PBT router.open_settles_mode_changes` A change queued in the evening while every gate passed is settled at the next open by the gates as they stand then: applied (the bot row takes the mode, one `mode.applied`) or refused with the gate's code (the bot keeps its mode, one `mode.apply_refused`); a second run at the same open changes nothing (HAT-GOV-084, HAT-GOV-085). Harm: a change silently fails, or a refused change takes effect later without a request.
- `PBT router.mode_change_takes_effect_next_session` A change queued outside hours routes nothing differently before the next open (the evening and the next pre-open included) and everything after; one asked in hours is refused and writes nothing; the open applies it to the bot row once. Harm: a bot switches to real orders mid-session.
- `PBT manage.real_fill_protected` Each entry fill the sync reads (one or several partial fills, at any time of the session or after the close) is followed, before any other broker write, by the position's stop: placed for the open quantity when none rests (an intraday SL-M SELL before 15:25, a GTT from 15:25 on), resized when one does, its trigger on the tick and never above the stop; a stop the broker refuses twice is alerted and placed by the next sync; a resize it refuses is alerted and sent again by the next sync. Harm: a filled position sits at the broker with no stop, or a stop for fewer shares than it holds.
- `PBT manage.broker_stop_only_tightens` As the position's stop rises (any steps, refused modifies among them), its intraday or GTT broker stop is modified up to it rounded down to the tick, never above it and never down; a refused modify leaves it and is alerted. Harm: a loosened broker stop, or one above the price HAT holds, sells on noise.
- `PBT manage.swap_leaves_no_gap` Whatever the broker answers to the GTT (and its retry), `gtt.swap` cancels the intraday stop only after a confirmed GTT; otherwise the intraday stop keeps working, `gtt.swap_failed` is raised and the job fails to be retried; a day stop the broker will not cancel fails the swap too. Harm: the day stop is cancelled first and the position spends the night unprotected.
- `PBT manage.entry_remainder_tracked` An entry fills in chunks, interleaved with its day stop filling and a flatten, with the broker taking or refusing the cancel of the entry's rest. Once a sale empties the position the rest of the entry is asked to be cancelled (once); a chunk that fills first adds to the still-open position and gets a stop; after every sync HAT's open quantity equals the shares in the broker's trade book and its live stops cover exactly those, and the position closes only with no share held and no entry working at the broker (audit 2026-10-22 F2). Harm: shares the broker bought after HAT closed the position sit untracked and unprotected.
- `PBT manage.verify_covers` The nightly verify refreshes a GTT the broker confirms to the current stop (`gtt.refreshed`), never below the trigger it rests at when the instrument's tick changed since (a coarser tick floors the stop lower; audit F24), and a refused refresh of a GTT so kept is alerted `gtt.unverified` with nothing placed; one it does not confirm is alerted (`gtt.missing`), cancelled at the broker and in HAT's book, and placed again; when the cancel fails, the broker is unreachable, the fills cannot be read or HAT's GTT row was never confirmed, `gtt.unverified` is raised and no GTT is modified or placed. Harm: a GTT that triggered or expired goes unnoticed and the position opens the next day without a stop.
- `PBT manage.verify_renews_old_gtt` (PR 47, HAT-EXEC-063) For every GTT age around 300 days and cancel reply, the nightly verify cancels a GTT placed 300 days ago or more and places it again at the current stop (`gtt.missing` never raised), and refreshes in place a younger one or one whose cancel is refused. Harm: the broker expires the GTT a year after placement and the position has no stop.
- `PBT manage.ex_date_gtt_replaced` (PR 47, HAT-EXEC-145) For every stored action (dividend, notice, split) with ex-date around the check day, every assumed book shape of a broker-cancelled GTT (`fixtures/fyers_gtt_ex_date_assumed.json`) and a GTT accepted or refused, `check_ex_date_gtts` ends a GTT the book no longer holds (`gtt.missing`, `gtt_cancelled_by_broker`) and places it again (a refusal does not fail the run: the intraday stop goes out from 09:15), leaves a resting one alone, fails with `gtt.unverified` and sends nothing on an unreadable book, waits for a split's rescale, and reads no position without an action today or the next session. Harm: Fyers cancels the GTT before a dividend and the position opens the ex-date unprotected.
- `PBT manage.series_change_remapped` (PR 47, HAT-EXEC-146) For every successor (none, BE mapped or unmapped, renamed, another ISIN), a series change gives no exit view, one URGENT `position.series_changed` at the morning check and at the nightly verify, and a re-placed GTT under the successor's Fyers symbol; otherwise nothing. Harm: every order under the old `-EQ` symbol is refused and exit rules run on stopped prices.
- `PBT manage.real_exits_never_blocked` A real position's live exits come from the shared exit stack and go out as MARKET SELL CNC orders, identical with and without an account or bot kill or a pause, and whatever the reconciliation state (none today, only yesterday's, a failed run, a mismatch; the new fill's stop is placed in each, D118); HAT sends nothing at the hard stop while the broker stop rests, and every sale is preceded by the broker stop's cancel or its resize to the shares left. Harm: a kill switch traps a position its exits should sell, or HAT and the broker both sell at the stop.
- `PBT manage.no_sale_after_close` A run that starts at 15:2x and reaches a sale some minutes later sends it only before 15:30 (the time sent is the run's start plus the monotonic time taken, with no clock injected); otherwise `exit.not_placed` is raised and the broker stop keeps resting. Harm: a slow close run sends market orders after the close.
- `PBT manage.entry_ended_by_broker` An entry the broker cancels, expires at the close or rejects after taking it, with nothing filled, ends its signal (Cancelled, or Rejected with `order.rejected` final); one whose send answer was lost and that is first found already ended ends Rejected (`entry_not_filled`) when the sync adopts it; replay agrees, no position opens and no stop is placed. Harm: a dead entry keeps its signal in EntryPlaced and holds one of the bot's slots.
- `PBT manage.gtt_trigger_handled` A GTT whose child sale (untagged or carrying the GTT's tag) fills only in part reduces the position and is neither resized nor doubled while its sale may still sell the rest; once that sale ends short, `gtt.unfilled` is raised once and the shares left get a new stop for exactly their quantity; another untagged sale on the symbol that ended (not at the GTT's quantity) changes nothing. Harm: a second stop under a working sale sells the shares twice, the rest stays unprotected, or an unrelated cancelled sale ends the GTT while it still rests.
- `PBT manage.flatten_capped_at_holdings` For one or two positions of a symbol, with an action the rescale cannot handle, a bonus (an action it handles; the property runs no rescale) or none, and a holdings book of any size or unreadable, `/flatten` sells min(open quantity, shares held) in all when an unhandled action spans the positions and the book reads, else the open quantity of each (HAT-EXEC-144). Harm: an oversell the broker refuses leaves the position unsold, or a sale short of what HAT holds.
- `PBT manage.gtt_resting_marketed` A GTT that triggered with its limit sale resting below the limit (the order book shows its CNC SELL at the GTT's limit price and quantity, part filled or none: untagged, or carrying the GTT's tag bare or `GTT`-prefixed, when it is attributed by the tag whatever its price) is left a minute to fill, then recorded `gtt.triggered` and alerted `gtt.unfilled` (code `gtt_triggered_resting`) once; an untagged sale at another price, and one under another HAT tag, is never touched; every sync asks for exactly `{order_type: MARKET}` on that order until the book shows it MARKET; a refused change is alerted once (`gtt_sale_not_marketed`) and asked again; a flatten and a raised stop send nothing while the sale works, once it fills the position closes with no stop left, and the next trade date (the sale gone from the book unseen) a flatten is sent again (HAT-EXEC-066). Harm: the stop triggered and the shares sit unsold below the limit all session, or a second sale or a stop move goes over the working sale.
- `PBT manage.lost_answers_never_freeze` For a stop path (lost day stop, lost GTT, refused stop, late fill) and a sale answer (lost and landed, lost and absent, refused, invalid), with the books readable or not and the answer young or settled: an exit or hard stop due is sold once the books can be read and the answer settled, a stop sent with a lost answer is adopted or ended and never doubled, a sale already resting is never sent twice, and an unreadable book alerts instead of acting. Harm: a stop-loss exit is frozen behind a lost answer, or two orders sell the same shares.
- `PBT manage.async_sale_rejection_retried` A sale the broker accepts and then rejects, cancels or expires short (in full or after a part fill) records `order.rejected`, un-fires its rules unless a partial exit sold some shares, sends one new sale on the next evaluation and alerts `exit.not_placed` on the second rejection with no third sale. Harm: a rule marked fired on a sale that never sold leaves the position open with no alert.
- `PBT manage.a_settled_answer_never_sells_twice` A lost GTT that landed under a position a sale then closed is cancelled by the first sync after the settle wait; a lost sale the broker filled is left to the fill sync and no second sale goes out before its fills are recorded; a lost sale of an earlier session that today's book cannot show is left unsure with its rule fired, alerted (`sale_unresolved`), and nothing more is sold while the broker stop rests. Harm: a GTT rests under shares already sold, or a position is sold twice.
- `PBT manage.a_resting_sale_is_never_stopped_over` While a full sale the broker accepted still works (resting, or part filled over one to three syncs), no sync places or grows a stop over its shares, and once it ends short the next sync protects the position again. Harm: a stop rests under shares a working sale is selling, so the sale is refused or the position sold twice.
- `PBT manage.the_close_ends_only_settled_answers` After the close, a lost entry or sale the book does not hold and that is older than the settle wait ends (entry signal Rejected, sale `expired`); one the book holds, or a young one, is left unsure. Harm: an order that reached the broker is written off.
- `TLA OrderReconciliation!LostEntryAdopted` An entry of unknown outcome that landed is adopted by tag once the order book shows it, though it is never sent again (liveness, fairness on `BookCatchesUp` and `Adopt`). Harm: a live entry is never tracked and its shares get no stop.
- `PBT router.unresolved_entry_is_alerted_not_resent` An auto entry whose answer was lost (landed behind a lagging book, or not landed) is placed once; each later attempt sends nothing and raises one `entry.unresolved`, and the signal is not rejected. Harm: a lagging book doubles the position, or a stuck entry is silent until the close.
- `PBT tags.a_timeout_is_looked_up_before_any_retry` An entry or a day stop whose answer was lost is looked up by tag before anything is sent again; an entry is placed at most once even when the order book lags its placement (the lookup finds nothing), and a day stop found by the lookup is not sent again. Harm: a lagging book doubles the position.
- `PBT tags.a_tag_twice_at_the_broker_blocks_the_account` Two broker orders under one HT tag (Fyers does not enforce tag uniqueness) are a `duplicate_ht_order` mismatch on that tag, whatever HAT knows. Harm: a second order under HAT's tag goes unnoticed and entries continue.
- `PBT manage.a_second_order_under_one_tag_is_tracked` A second filled order under an entry's tag (the DUP-1 case) has every fill recorded: the open quantity is the sum, the live stops cover all of it, and one `fill.excess` is raised however many partial fills make up the excess, only once the stop covers every share. Harm: real shares are left untracked and without a stop.
- `PBT tags.a_duplicated_tag_is_read_one_way` Of the broker orders under one HT tag, a row with a broker order id reads that order only, and a row without one reads the first order not ended unfilled, else the first (`submit.TagBook`, also `resolve_by_tag`). Harm: a dead duplicate's status ends a live entry's signal.
- `PBT manage.a_dead_second_order_never_ends_the_entry` A second order under a filled entry's tag that the broker cancels, rejects or expires unfilled, listed before or after the entry's own, leaves the row on its own broker order id and status, the signal unchanged and the position open under its stops (`submit.TagBook`). Harm: the duplicate's status ends an open position's signal.
- `PBT tags.a_reserved_tag_is_sent_by_one_sender` Two senders that read the same order before either stamped a new attempt (never sent, or a lost answer the lookup did not find): one calls the broker with a new attempt time, the other sends nothing and reports what the first left; a lost answer the fill sync adopted between a sender's lookup and its stamp is not sent again. Harm: two stops or two entries for one order, or a resend in flight settled as absent.
- `PBT tags.gtt_payloads` The GTT place, modify and cancel payloads are exact, CNC and single-leg, the place carries the stop's HT tag as `orderTag`, and a GTT without a stop_gtt HT tag is refused before any SDK call (HAT-BRK-090). Harm: a stop is sent with fields HAT did not decide, or a lost GTT answer cannot be found by its tag.
- `PBT tags.a_lost_gtt_is_found_by_its_tag` The GTT a lost stop_gtt answer settles to is one no other row holds on the symbol: the one under the row's own tag (any quantity or trigger), else one shown untagged of exactly the row's side, quantity and trigger; never one under another HAT GTT's tag or a foreign tag (`BrokerGtt.foreign`) (HAT-EXEC-065). Harm: another position's stop, or a GTT HAT did not place, is adopted and later cancelled or moved, leaving that position unprotected.
- `PBT tags.the_gtt_book_reads_only_resting_stops` `gtts()` lists exactly the single-leg CNC GTTs resting or triggered (`ord_status` 2, 4 or 6, or none) from the flat book fields (the live keys `tran_side`, `product_type`, `ordertag`), `orderInfo.leg1` or `leg1`, each with its HAT tag read from `ordertag` (`N:GTT<tag>`; `GTTUntagged` or a foreign tag is no tag); a cancelled, rejected or expired GTT, an OCO or another product (whatever its status) is left out, and a single-leg CNC row with an unknown status code raises. Harm: a lost GTT is matched to one that no longer rests, or one still resting is read as gone.
- `PBT tags.a_keyless_book_never_reads_as_absent` An unconfirmed order whose order-book read answers ok without its list is not sent again (the lookup raises `BrokerUnavailableError`). Harm: a keyless reply reads as "not found" and the order is placed twice.
- `PBT router.non_live_sleeve_routes_as_shadow` For every lifecycle state, queued mode and GTT capability, `bot_mode` answers shadow unless the state routes live, and the router places no real order for a non-Live sleeve. Harm: a Probation or Retired sleeve keeps trading real money.
- `CONF exec__default_mode_confirm` A bot asks for confirm unless the owner changes it and runs shadow until every gate passes: the bot row defaults to confirm (model and database), `create_bot` refuses another mode, the entry path routes shadow unless told otherwise, an unknown mode reads as shadow, and confirm with any gate missing reads as shadow. The seed YAMLs and the strategy config default still say shadow; the bot's mode never reads them (PR 45). Harm: a new bot trades real money before its gates pass.
- `PBT router.routine_reconcile_keeps_gate` A routine reconciliation (the periodic pass) of an account already reconciled for the trade date leaves the entry gate open while it reads the book and after it, readable or not. Harm: entries are refused while a routine pass runs, or wait on a pass that already succeeded.
- `PBT router.older_read_never_overrides_newer` When a reconciliation that read the book earlier finishes after one that read it later (`ExecutionGuard.begin_read` tickets), the later read's result stays in force. Harm: the router's run and the periodic run race, and an older clean book clears a newer mismatch so entries open over an unknown HT order.
- `PBT router.entry_gate_keyed_by_trade_date` Over any sequence of reconciliations that read the book or cannot, with the clock moving on 0..2 trade dates between them, `ExecutionGuard` accepts an entry exactly on a trade date that has had a success, and never refuses a stop or a sale (D118, MX-F5). Harm: yesterday's success opens today's entries over an unmatched book, or an unreadable book keeps a stop or a sale from the broker.
- `PBT router.failed_first_reconcile_alerts_once_per_date` Through `Desks`, a reconciliation that fails before the trade date's first success raises exactly one `reconcile.failed` health alert for that date, and a failure after the success raises none. Harm: the owner never learns the account cannot open entries, or is flooded every five minutes.
- `PBT tags.unreadable_foreign_rows_skipped` An order row without an HT tag that the adapter cannot read (a missing key, an unreadable side or quantity, not a mapping) is skipped and counted (`OrderBook.skipped`, `ReconcileReport.skipped_rows`) and the rest of the book is read, with one `reconcile.unreadable_row` alert per trade date; an HT-tagged row that cannot be read fails the read and the reconciliation. Harm: one manual order fails every reconciliation of the day, or an HT order HAT cannot read is missed.
- `PBT tags.held_quantity_nets_today` Through the adapter, the quantity reconciled per symbol is the holding plus today's signed net CNC position, an intraday position not counted; `@example`s for a buy today, a buy and sale today, a holding sold in full and in part (MX-F8). Harm: a holding a GTT sold today reads as held (fail open), or a correct HAT book blocks the account.
- `PBT router.resumed_entry_waits_for_feed` An entry left Triggered by the previous process is placed after a restart only once the feed has heard a fresh tick and the account is reconciled, at most once, and never on a later trade date than its own (`signal_expired`); the restart runs the fill sync whether or not its reconciliation succeeded (D118). Harm: an entry goes out on a stale price, twice, or a day late.
- `PBT router.fill_sync_cadence` `Desks.sync_due` reads an account's fills at every periodic step while `fills.watch_closely` holds (a real entry of the day's signals may still fill, or a stop of an open real position is pending or unconfirmed), and otherwise at the first pass and then once a minute; an entry of an earlier session still shown working and a stop the broker refused do not speed it up, and a sync whose read failed is tried again at the next step. The test reads the expected state row by row (`_watched`). Harm: an entry filled just after a sync sits up to a minute at the broker with no stop, or a refused stop is re-sent every 5 s until its tag's sequence runs out.
- `PBT router.desk_calls_serialised` Every broker call of an account's desk goes through its session owner, so no two overlap, and a session check reads `session_status` once and records that same status. Harm: concurrent sends race the reconciliation gate, and the recorded session status disagrees with the one that raised the alert.
- `PBT router.one_login_per_broker` Two active accounts of one broker refuse the start with `HatConfigError`; one per broker builds. Harm: two accounts share one login and the same funds count twice in the allocation equity.

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
  python -m pytest tests/hat/db -q --no-cov -m postgres -k "per_bot_serialised or owner_serialised or publish_serialised or state_domain"
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
  - Fyers' order book may lag a placement and Fyers does not enforce tag uniqueness (HAT-BRK-103). Since PR 28 an entry of unknown outcome is never sent again (the model's `Lookup` reads a lagging `seen` book). A lost day stop is still re-placed under its tag once the lookup finds nothing after `LOST_ANSWER_SETTLE`: if the book lagged longer than that, two stops rest for one position; the sync records both fills and `fill.excess` does not cover stops. Sales always take a new tag.
  - A reconciliation can still land between the guard re-check and the broker call (microseconds). The next order is blocked, the one in flight is not.
  - An unconfirmed GTT is not retried (`UnresolvedOrderError`): Fyers does not enforce tag uniqueness. Since PR 18 it is settled against the GTT book (`StopDesk.settle_lost`) once older than `LOST_ANSWER_SETTLE` (60 s, so an SDK request still in flight is not read as absent). Since PR 25 a GTT carries its HT tag (`orderTag`, recorded live 2026-10-22), so the settle matches it by tag first, else an untagged GTT by symbol, side, quantity and trigger. Unverified: a 20-character tag on a GTT (the recording used an 8-character one), and whether a triggered GTT's child order carries the tag and in which form (the fill sync accepts it bare or `GTT`-prefixed, else falls back to the untagged shape). Two GTTs on one symbol with a tagged child are not driven with two positions.
  - The IPv4 hook covers urllib3 connections only: an HTTPS proxy (the connection goes to the proxy) and the data websocket are not forced onto IPv4.
  - Fyers' -360 reply was not recorded (only its message, from the brief); the code is matched on `code` alone.
  - Reconciliation quantity is the larger of the CNC position and the holding per symbol: [verify] on a live account, and it fails closed on a partial overlap.

## Updating

- A new or changed order rule: edit `verification/tla/OrderReconciliation.tla`, keep a `\* code:` comment on every action, add or adjust a broken variant (`broken/OrderReconciliation__<mode>.cfg` with an `EXPECT:` line and the mode in the model's `ASSUME`), then run both `run.sh` commands and the property checks.
- A new Fyers payload field or status code: update `map_fyers_status` or `_order_payload` and the matching property in `test_tags.py`.
- A change to `HatBroker` needs the spec table in 07 §8 changed first (`brk__interface_methods` compares them).
- After re-confirming everything, update the sync commit and digest below: `python backend/tests/hat/conformance/hat_conf_skills.py verify-execution` prints the digest.

## Coverage

TLA+ model:
- `verification/tla/OrderReconciliation.tla` and `verification/tla/OrderReconciliation.cfg`
- `verification/tla/broken/OrderReconciliation__no_persist.cfg`, `verification/tla/broken/OrderReconciliation__retry_new_tag.cfg`, `verification/tla/broken/OrderReconciliation__retry_without_lookup.cfg`, `verification/tla/broken/OrderReconciliation__retry_on_lagging_lookup.cfg`, `verification/tla/broken/OrderReconciliation__no_late_adoption.cfg`, `verification/tla/broken/OrderReconciliation__no_reconcile.cfg`, `verification/tla/broken/OrderReconciliation__ignore_mismatch.cfg`, `verification/tla/broken/OrderReconciliation__alert_every_run.cfg`, `verification/tla/broken/OrderReconciliation__inactive_account.cfg`, `verification/tla/broken/OrderReconciliation__two_owners.cfg`, `verification/tla/broken/OrderReconciliation__non_cnc.cfg`, `verification/tla/broken/OrderReconciliation__unlimited_retry.cfg`

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
- `backend/app/hat/execution/fyers_ipv4.py`
- `backend/app/hat/api/fyers_auth.py`
- `backend/app/hat/api/guard.py`

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
- `backend/tests/hat/properties/test_fyers_ipv4.py`
- `backend/tests/hat/fixtures/fyers_live_writes.json`

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
- `backend/app/hat/execution/mode_changes.py`
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
- `backend/app/hat/execution/series.py`
- `backend/tests/hat/fixtures/fyers_gtt_ex_date_assumed.json`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `d0ba323bb8d38329ecc3e61d5208a2ec0db15943`
Covered digest: `5741ff3946b31341990ca45f146133c77713ba2464ea76d5e393fb03e9e1e54f`

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
- `DB runtime.per_bot_serialised` commits real rows and empties `hat_event` and the runtime tables with TRUNCATE afterwards (the event log refuses DELETE). Its broken variants are code breaks (`lock_bot` a no-op, or keyed per call), registered in `test_db_broken_variants.py` with an undo list. Since PR 19 the owner lock `open_entry` takes after the bot's also serialises one bot's entries, so these breaks remove `lock_owner` too.
- `evaluate_position` and `open_entry` refuse a real position or a confirm or auto bot (`position_not_virtual`, `mode_not_shadow`); the router (PR 15) owns them. A virtual trade's continuation after a confirm skip is PR 15. (PR 15b: `open_entry` takes the effective mode and routes all three; `evaluate_position` still refuses a real position until 15c, and drives veto trades.)

- PR 15b added `ExecutionRouter` (16 invariants, 2 liveness properties) and `ApprovalToken` (3 invariants, 1 liveness property). `TypeOK` of each is the one property without a broken variant. Bounds and their reasons are in each model's header: two signals on one instrument (enough for `NoAddToOpenPosition`), one card per signal, the quality gate and the feed folded into one input, an auto entry routed and placed in one step, a placed entry standing for the open position (fills, rejections and broker stops are `OrderReconciliation`'s and 15c's `StopPlacement`'s). `ApprovalToken` checks the decision service; the token's signature and single use at the Telegram edge are PR 16's (`SecondConfirmRequired` is not built).
- `ExecutionRouter!UserCannotRaiseRisk`, `RejectNoCard` and `NoEntryWithoutSession` name PR 16 in their README rows and are not in this model yet.
- `router.take_rechecks_max_chase` needed two `@example`s: its first draft drew the over-chase region too rarely to catch a removed re-check in 40 examples (recorded below). `router.mode_change_takes_effect_next_session` gained a pre-open probe after its first draft could not see a change read as effective at its request (recorded below).
- The router properties found two bugs, fixed here: an approval written with an IST timestamp read back on SQLite five and a half hours late (approval times are now stored in UTC), and a veto trade counted as one of the bot's slots and stop-outs (`trades.bot_snapshot` and `portfolio.service.trade_results` now leave veto trades out).
- `order.rejected` drives the lifecycle step to Rejected only with `final: true` (15c sends it); the router ends a twice-rejected entry with `signal.rejected` (stage `route`) from Triggered. `CONF lifecycle_transitions_match_spec` still compares the table.

- PR 15c: the `manage.*` properties run one auto bot with one instrument on SQLite with the tick fixed at 0.05 (the tick rule is `stops.tick_size_slabs`); prices of a sale or a stop come from the real code, fills are written into `FakeBroker`'s trade and order books in the Fyers row format. `manage.real_fill_protected` has an `@example` for a stop refused twice (the generator reaches it, but rarely). `manage.real_exits_never_blocked` compares the broker calls of the same price path with and without a kill or pause rather than an independent oracle of the exit stack (the stack's semantics are `exits.*`). Writing the properties found three bugs, fixed here: a stop placed by a fill after 15:25 was an intraday order that would expire at the close after the swap had run (now a GTT; also found by `StopPlacement!AlwaysProtected`), a stop the broker refused twice was never retried in the session (the sync now protects every open real position without a working stop), and a GTT placed by a fill raised `gtt.swap_failed` instead of `stop.not_placed`. The pre-push review then found and fixed: verify re-placing a GTT over one that may still rest (it now cancels first, or alerts `gtt.unverified`), a swap reported done when a day-stop cancel failed, a partly filled GTT resized under its own working sale, an entry the exchange rejected after placement left in EntryPlaced, a sale sent while the broker stop still covered all the shares (`StopDesk.make_room`), unbounded same-day resends after rejections, and one failing position stopping `exits.close` for the rest. CodeAnt then found (fixed in the review round): a refused resize never sent again, the verify modifying GTTs when the fills could not be read, and an entry first found already ended leaving its signal in Triggered (its `order.cancelled` adoption raised `LifecycleStateError` and stopped the sync, found by `manage.entry_ended_by_broker`; it now ends at adoption). A sale's 15:30 check read the run's start time when no clock was injected (now the start plus the monotonic time taken; `manage.no_sale_after_close`). `lifecycle.step_of`: an `order.cancelled` moves state only for an entry, and an `order.filled` with `exit_triggered` stands for the exit placed (07 §4); the lifecycle and trade properties were re-run and pass.
- PR 16 built the Telegram half of `ExecutionRouter` (`SessionFlip`, `Relay`, `UserEntry`, a user size on `Take`; `NoEntryWithoutSession`, `RejectNoCard`, `UserCannotRaiseRisk`) and of `ApprovalToken` (`Ask`, `Confirm`; `SecondConfirmRequired`). Those properties, their broken variants and their scratch breaks are recorded in `verify-telegram`; the properties above were re-run against the extended models and pass. `open_entry` now stores the sha256 of the token the router's `issuer` signs (`telegram/tokens.py`) and `EntryOutcome` no longer carries a raw token; `TradeManager.flatten` sells every open real position through the exit path.
- PR 20 (audit 2026-10-22 F2, F24): `ManageRig.tick` replaces the fixed tick, so `manage.verify_covers` changes the tick between the follow and the verify. `manage.entry_remainder_tracked` keeps every step before 15:25 (day stops only; the GTT path of a late fill is the model's, `StopPlacement!SharesTracked`), and a refused cancel of the entry's rest is scripted by replacing `FakeBroker.cancel_order` for the entry's broker id only.
- PR 16 review round: `TradeManager` serialises the sales of one position (`_sale_lock`) and `flatten` continues past a position that fails (`FlattenReport.failed`); `orders_allowed` is public for the Telegram flattener. `test_manage.py` passes unchanged.
- PR 18 (audit 2026-10-22 money-safety-execution F1-F3, engine-ops F1, tests-hat F3): `HatBroker.gtts()` reads the GTT book (07 §8, so `brk__interface_methods` changed with the spec table); `FakeBroker` keeps GTT book rows in the Fyers shape and `RecordingSdk`'s default read replies now carry their list keys (a reply without one is the case `fyers_broker.an_ok_reply_without_its_list_is_unreadable` drives). `ManageRig` takes `settle_after` (default 0: the rig settles a lost answer at once; `manage.lost_answers_never_freeze` also draws a young answer that must wait) and moves the settle's wall clock on by `elapsed`. `FakeBroker`'s GTT book rows take the flat Fyers book shape (`price_trigger`, `qty`, `ord_status`, `gtt_oco_ind`, as OpenAlgo's Fyers mapping reads them) and a cancelled GTT stays in the book with `ord_status` 1 (`resting_gtts()` lists the active ones). The pre-push review found and fixed: a lost GTT left resting under a position a sale closed (the sync now settles and cancels it), two threads sending one reserved stop (`send_order` claims the first attempt), a lost sale found filled followed by a second sale, a lost sale of an earlier session read as never sent, ended or foreign GTT book rows, and a repeated alert while a lost answer is still young. A second review of those fixes found and fixed: a retry kept its first attempt's time (the settle wait then ran from the wrong send), two senders of a retry both sending (every attempt is now a conditional stamp), a triggered GTT dropped from the book read, an odd status on someone else's GTT failing the whole read, and an earlier session's lost sale followed by a new sale for a quantity it may already have sold (it now holds further sales). `manage.async_sale_rejection_retried` patches `submit.utc_now` because `rejected_sales` counts the sales placed on the trade day of the wall clock. The hard-stop fallback now counts only a broker-confirmed stop (an unsure one is settled first, and one still unsure no longer holds the sale back: the previous rule froze positions behind lost answers); `manage.real_exits_never_blocked` passes unchanged. Nothing was narrowed.
- PR 19 (audit 2026-10-22 money-safety-execution F4, tests-hat F1): `open_entry` hands the decision `owner_slots`, the slots of every bot of the owner on every broker account (each slot carries its own bot, so the per-bot caps in `decide` keep filtering to the bot's own), and takes `lock_owner` after `lock_bot` and after the duplicate check. The scope is the owner, not one broker account, because total equity sums every bot's allocation (`daily/live.py`). Only an entry adds open risk: the other slot writers (fills, Take, skip, expiry, stop raises, exits) hold only the bot's lock and can only keep or shrink a slot, so a stale read over-counts; `_slots` reads the pending signals before the positions so that a fill landing between the two reads is counted twice, never missed (CodeAnt). `bot_snapshot` (the bot's own slots) is unchanged for its other callers. `hat_engine_support.add_bot` adds a second bot of the owner on another broker account. The per-bot DB breaks now also remove the owner lock (above). Nothing was narrowed.
- PR 23 (live checks 2026-10-22): Fyers code -16 raises `FyersSessionExpiredError` (a `FyersUnavailableError`, so every read still fails closed); `session_status` makes one profile read when the token file says live (only -16 turns it EXPIRED; an outage leaves it LIVE as before); `FyersAuthService` reads the profile after a login (on its own small pool, 5 s timeout, checked by the shared `profile_data`) only with a `model_factory` (the process instance passes the SDK model, tests a `RecordingSdk`, so no test can reach Fyers). The login fixtures lost the `fy_id` they had put in the token reply, which Fyers does not send. `FakeBroker` gained `profile` and `get_profile`.
- PR 24 (audit 2026-10-22 money-safety-execution F5, F8; D118): `ExecutionGuard` keys the gate by trade date (`reconciled_for`, `today`; `begin_session` and the boolean `reconciled` are gone) and gates entries only; `TradeManager` no longer takes the reconciler (its sales, `gtt.swap` and the fill sync never wait for a reconciliation). The rigs build their guard with `hat_exec_support.guard_on(day)`, a fixed trade date. `tags.refusals_before_the_broker` now expects a stop or a sale to pass while the date is not reconciled, and `tags.broker_errors_fail_closed`'s garbage order row carries an HT tag (an untagged unreadable row is now skipped, not a failure). `OrderReconciliation` is unchanged: its orders are entries, and a new trade date is the router property. Nothing was narrowed.
- PR 26 (design review 2026-10-22 item 1): the periodic `sync` step runs every pass and `Desks.sync_due` decides per account (every step while `fills.watch_closely`, else `FILL_SYNC_EVERY`, one minute; a sync that read nothing is tried at the next step; one account's failure is logged and never stops another's); `recover` stamps `synced_at` too and isolates each account (a failure is logged; the start goes on; CodeAnt), and `Desks.sync_all` is gone (`router.desk_calls_serialised` drives `sync_due`). A refused stop keeps the one-minute retry, so the 36 sequence characters of its tag last. The models still take a broker fill and HAT reading it as one step; `router.fill_sync_cadence` checks the time between them. An order socket was not added (HAT-OVR-006 allows one broker socket, the data socket). The per-account isolation has no property (the rig has one account). Nothing was narrowed.
- PR 27 (design review 2026-10-22 item 2): a GTT that triggers with its sale resting below the limit is seen by `FillSync` step 5b (`_resting_trigger`: untagged SELL, working, short of its quantity, the GTT's quantity) and its sale made a market sale (`StopDesk.market_sale`, `modify_order` with `order_type` MARKET only). `BrokerOrder.order_type` is new (the adapter maps the Fyers `type` code; an unknown code is None, so the sync asks again). `FakeBroker.modify_order` now shows an accepted type change in its book. The GTT row stays live while the sale works, and `StopDesk.triggered_gtts` (a live GTT with a `gtt.triggered` event) holds back exit sales and `follow`. The review's proposals (a `triggered` status, a one-shot 09:15 job) were not taken: the sync already runs every minute and the live row keeps every other path off the shares. That Fyers accepts the change on a GTT's child is `[verify on a live account]`. Review round (/code-review): the sale is identified by its limit price (`BrokerOrder.price`, the adapter's `limitPrice`) and CNC product as well as quantity, and by its broker order id once marked; a sale is acted on only after `fills.RESTING_AFTER` (60 s, an in-memory first sight: a restart waits again); `triggered_gtts` counts only today's triggers (the sale is a DAY order) and filters on `aggregate_type`; `rescaled` waits on a triggered GTT too. That wait in `rescaled` has no property (a rescale and a trigger on one day need an ex-date intraday, which cannot happen: the rescale runs before the open). Declined: a `triggered` status (every LIVE-based path would then treat the shares as unprotected and place a stop over the working sale); bounding the conversion retries (once a minute while the sale works, and only in the session); persisting the refused-conversion alert (a restart may alert it once more); merging the resting and the ended-unfilled alerts (two different events). Nothing was narrowed.
- PR 28 (design review 2026-10-22 item 3, HAT-BRK-102, -103): `_lookup_before_retry` raises `UnresolvedOrderError` for an entry the lookup did not find, so an unknown-outcome entry is never placed again; the fill sync adopts it by tag or `end_session` ends the signal `entry_not_found`. `find_mismatches` adds `duplicate_ht_order` for a tag the broker holds twice; `FillSync` records every fill and raises URGENT `fill.excess` for a fill that leaves an entry above its quantity. `FakeBroker.lagging` hides the newest book rows. The model gains `seen` (the lagging book), `BookCatchesUp`, `Adopt` (the later sync) and the liveness `LostEntryAdopted`. Review round (/code-review): the router alerts `entry.unresolved` (a crash between the attempt's commit and the call is not sent again either); `submit.TagBook` is the one rule for a duplicated tag in the send path, the fill sync and reconciliation (a row's own broker id, else the first order); `fill.excess` is alerted on the crossing fill only; `tags.a_reserved_tag_is_sent_by_one_sender` keeps entries in its retry case (both senders refuse; one the sync adopted meanwhile is reported placed). CodeAnt round: `fill.excess` is sent after the fill's stop (a slow Telegram call no longer delays the stop); `TagBook` skips a duplicate the broker ended unfilled when a row has no broker id. Declined: `FakeBroker.fetch_orderbook` appending `book` to a scripted read (unchanged behaviour the rigs rely on). Not taken from the review: the early `entry_unresolved` rejection (on a lagging book it rejects a signal whose order is live), the fill fence by broker order id and the refusal of excess fills (both leave real shares untracked and unstopped). Stops keep the same-tag re-place (above). Nothing was narrowed.
- PR 29 (design review 2026-10-22 item 4, HAT-EXEC-012, HAT-BRK-092): `session.refresh_capabilities` rewrites `hat_broker_account.capabilities` from the adapter when the desks start and after every session check's row commit (`broker.capabilities_changed` on a change, relayed as a health alert); `modes.real_orders_allowed` (stored and the adapter's GTT) gates `router._gtt_refusal` and `submit._check_allowed` for an entry. `FyersHatBroker` takes `capabilities` (default `FYERS_CAPABILITIES`), and the rigs that place real entries build it and store it as `hat_exec_support.GTT_READY` (`exec_env` takes `stored_gtt` and `live_gtt`; the router `Rig` takes `live_gtt`). `tags.refusals_before_the_broker` gains both flags (re-pinned: stronger). `OwnedBroker.capabilities` reads the local value without the session owner, and `OverlapSpy` does not count it as a broker call, so `router.desk_calls_serialised` is unchanged. The triage asked for the first live check of a trade date; the desk start and every session check are used (the value is local, and a deploy that flips the constant reaches the account at once). Review round (/code-review): the refresh at start and on every check, outside the owner, after the row's commit, the health alert, the changed keys in the payload. `ExecutionRouter.tla` still has one GTT value per account: the stored/live split is checked by the properties only. Nothing was narrowed.
- PR 30 (design review 2026-10-22 item 5, HAT-RISK-063, HAT-OPS-031, HAT-TG-021): `SessionWatch.observe` resets before `WATCH_FROM` (08:30 IST), which ends the nightly 00:02 `broker.session_lost`; `AccountDesk.check_session` syncs `broker_health` per bot of the account (`session.broker_health_failing`: not live from 09:05 until 15:30 IST; a failed status read counts as not live). `ops.session_backoff_alert` is unchanged (its runs start at 14:30 IST).
- PR 31 (design review 2026-10-22 item 6, option A, HAT-EXEC-144): `TradeManager.flatten` caps the sale of a position spanning an action the rescale cannot handle at the broker's holdings of the symbol (`_held_shares`, read once per flatten and only when such a position exists; an unreadable book leaves the cap off, since the broker refuses an oversell and a flatten is never blocked). Holdings are per account through the manager's own broker, so the cap is per account. CodeAnt round: the snapshot is spent as each capped sale is sized (`_capped`), so two positions of one symbol never sell more than is held together (a refused sale's shares are not offered to the next position in that flatten; `/flatten` again sells them); the property's `twin` adds a second position with a cloned signal one day earlier (one signal per bot, playbook, name and day). Nothing was narrowed.
- PR 41 only reworded the comment on `brk.import_boundary`'s data-socket exception (`test_conf_broker.py`): the module also hands the minute-history download the session's REST `history` (HAT-DATA-119). The graph check itself is unchanged.
- PR 38 (design review 2026-10-22 item 17, HAT-DEC-BD5): the two login POSTs depend on `app/hat/api/guard.py`'s `frontend_write`. `FyersAuthService.exchange` takes an optional `accept_user` that sees the profile's user id before the save and raises `WrongAccountError` (a minimal change to `execution/fyers_auth.py`, which revfix's PR 30 may also touch). `POST /token` passes the active Fyers `account_ref`s through it and now opens a request session (`get_db`), so a login fails closed (503) while the database is down. The route properties' `client_for` sends the write header, wires a test origin rule and serves the broker accounts from SQLite. `fyers_auth_routes.cross_site_write_refused` needed `@example`s: 100 random draws missed the cross-site-without-Origin shape (about 1 in 87) when `Sec-Fetch-Site` was ignored (recorded below).
- PR 38 review round: `_account_check` reads the accounts inside `accept_user`, once Fyers has answered (no connection held while Fyers answers, and an account onboarded meanwhile counts); an unknown user gets `ACCOUNT_UNCONFIRMED`; unreadable accounts answer 503 `DB_ERROR`. Not covered: the read-after-answer ordering itself (no property orders the read against the exchange). Left as follow-ups: `FyersHatBroker.complete_login` (`fyers_adapter.py`, no production caller) still calls `exchange` without `accept_user`; `ACCOUNT_ACTIVE` and `FYERS` are local strings in `api/fyers_auth.py`, because importing them from `execution.submit` or `execution.accounts` pulls the mode code into the API (CONF `auto_mode_single_user`), as `dashboard.py` and `daily/accounts.py` already do.
- PR 25 (Fyers live write test 2026-10-22, HAT-BRK-081, -082, -090, -091, -093, -103, HAT-EXEC-012, -065, -066; the brief assigns these `execution/` files to this PR, overriding "revfix owns execution"): `fyers_ipv4.py` installs a urllib3 hook so Fyers HTTP leaves over IPv4 and is the one importer of `fyersModel` (`fyers_adapter_wraps_existing`'s `SDK_IMPORTERS` is now a map of file to allowed submodule). Fyers -360 is `FyersRateLimitedError` (a `FyersUnavailableError`, so reads still fail closed) and the adapter's `broker_rate_limited`. A GTT carries its HT tag (`GttRequest.tag`, sent as `orderTag`); the GTT book reads the live keys (`tran_side`, `product_type`, `ordertag`: the old `side` and `productType` made every non-empty live book unreadable); an order or trade row tagged with a stop_gtt tag (bare or `GTT`-prefixed) is that GTT's child (`gtt_tag`). `find_gtt` matches by tag first; the fill sync attributes a child by `gtt_tag`, else the untagged shape. `FakeBroker`'s GTT rows take the live shape with `ordertag`. `gtt_delivery` is now true (`broker.the_fyers_adapter_never_unlocks_confirm_or_auto` became `the_fyers_adapter_unlocks_modes_on_an_active_account`; `brk__fyers_capabilities` asserts true). `tags.status_mapping_total` found that `'¹'` passed `str.isdigit()` and crashed `int()` (`_whole_number` now matches ASCII digits; `@example`s added). `fyers_broker.only_explicit_error_rejects` (pinned) is unchanged; the recorded rejection with an id is a new property. `manage.gtt_trigger_handled` and `gtt_resting_marketed` draw the child's tag (none, `GTTUntagged`, bare, `GTT`-prefixed); nothing was narrowed. Review round (/code-review): a -360 write is not sent, like an unavailable broker (`broker.CODES_NOT_SENT`: `send_order` leaves the order pending and burns no retry; `StopDesk` retries an entry-remainder cancel and does not cancel a GTT whose resize was refused for it); `tags.an_unreachable_broker_is_not_a_rejection` became a property over circuit, client and -360. The two `StopDesk` branches have no property of their own (no rig drives a rate-limited cancel or GTT modify). The GTT book also reads the placement keys `side` and `productType`, and `GTTUntagged` on an order or trade row is no tag. Declined: a quantity check on a tagged child (the tag is the GTT's, which identifies it; a partial child keeps the GTT's quantity), and importing `rate_limited` into `minute_history` (it keeps its own -360 constant as it keeps -50). CodeAnt round: a GTT shown under a foreign tag (not `GTTUntagged`, not one of HAT's GTT tags) is `BrokerGtt.foreign` and never matched by shape; an untagged sale that ended ends a GTT only at the GTT's quantity (`_unfilled_trigger`). `tags.a_lost_gtt_is_found_by_its_tag` missed its break in 100 draws (about 1 book in 75 had a foreign GTT first in the shape match), so `gtt_books` now draws the row's quantity, SBIN and unclaimed GTTs more often (about 1 in 10) and has an `@example`. Declined: the claim that the IPv4 hook misses connections once `requests` is imported (urllib3 2.x looks `create_connection` up on its module at each connect, and `fyers_ipv4.fyers_http_leaves_over_ipv4` imports `requests` first); an untagged SELL trade on a symbol whose one GTT is working is still attributed to it (a trade carries no order quantity to compare).
- PR 33 edited `test_ops.py` (covered here): `ops.chain_carries_past_midnight` also draws an unchained job marked `carry` (`snapshot.bots`); its checks are `verify-scheduler`'s, and the property runs in this skill's command too. Nothing was narrowed.
- PR 45 (HAT-GOV-085, HAT-EXEC-014): the DDPI and base-weights gates and the high-confidence rule are proved on the real code (`router.mode_gates_hold_bots_in_shadow`, `router.open_settles_mode_changes`, `router.auto_places_only_high_confidence`); `ExecutionRouter.tla` models only the GTT gate and was not extended. No live caller passes a confidence label yet (PR 71), so every auto signal goes to approval. Nothing was narrowed.
- PR 47 (research G01, G17, G24; HAT-EXEC-063, -145, -146): `TradeManager.check_ex_date_gtts` (job `gtt.ex_date`, behind `HAT_EXDATE_GTT_CHECK`, default off) and `StopDesk.check_gtt`; `StopDesk._renewed` (300-day renewal in `verify`); `execution/series.py` (successor by market and ISIN). The broker-cancelled GTT's book shape is ASSUMED (`fyers_gtt_ex_date_assumed.json`, three shapes) until the O2(b) live check; the property draws all three. ASM and GSM are not detectable (no source) and are not checked. A BZ successor has no Fyers symbol (`fyers_symbol` maps only BE) and is alerted `unmapped`; its re-place is refused by the adapter, which the property does not draw separately. `manage.verify_covers` passes unchanged. Nothing was narrowed.

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

Break: broken variant `OrderReconciliation__retry_on_lagging_lookup.cfg` (`BrokenMode = "retry_on_lagging_lookup"`): an attempted entry is stamped again once its lookup, reading the lagging book, finds nothing. With `Lookup` reading the true book the variant passes, so the lag is the cause. (PR 28)

```
Error: Invariant AtMostOneEntryPerSignal is violated.
Init, CheckAccount, Reconcile, Reserve, Stamp, PlaceCall, Record, Lookup, Stamp, PlaceCall
```

### TLA OrderReconciliation!LostEntryAdopted

Break: broken variant `OrderReconciliation__no_late_adoption.cfg` (`BrokenMode = "no_late_adoption"`): only the lookup adopts; the later sync never does. (PR 28)

```
Error: Temporal properties were violated.
State 15: <PlaceCall ...>
State 16: <Record ...>
State 17: <BookCatchesUp ...>
State 18: <ReleaseSession ...>
Back to state 17: <AcquireSession ...>
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

### PBT tags.unreadable_foreign_rows_skipped

Break: `_order_book` re-raises for every unreadable row (main before PR 24: the whole book failed).

```
E   app.hat.execution.broker.BrokerUnavailableError: broker_response_invalid
E   Failing explicit example: test_unreadable_foreign_rows_skipped(
E       rows=[('bad', None, 'side')],
```

Break: `_order_book` skips an unreadable HT-tagged row too.

```
E   Failed: DID NOT RAISE BrokerUnavailableError
E   Failing explicit example: test_unreadable_foreign_rows_skipped(
E       rows=[('good', None, '')],
E       ht_broken=('HTE00000000000000001', 'qty'),
```

Break: `_alert_unreadable` drops the once-per-date check.

```
E   AssertionError: assert [('reconcile.... {'rows': 1})] == [('reconcile.... {'rows': 1})]
E     Left contains one more item: ('reconcile.unreadable_row', datetime.date(2026, 10, 21), {'rows': 1})
```

### PBT tags.held_quantity_nets_today

Break: `broker_quantities` takes the larger of the CNC position and the holding (main before PR 24).

```
E   AssertionError: assert 100 == ((100 + 0) - 40)
E   Failing explicit example: test_held_quantity_nets_today(
E       holding=100,
E       bought=0,
E       sold=40,
```

### PBT tags.status_mapping_total

Break: `map_fyers_status` indexes `_FYERS_STATUS[status]` instead of defaulting to `unconfirmed`.

```
E   KeyError: 0
E   Failing test case: test_broker_status_mapping_is_total(code=0, filled=None, qty=None)
```

Break (PR 25): `_whole_number` goes back to `value.strip().lstrip("-").isdigit()`, which accepts `'¹'`.

```
E   ValueError: invalid literal for int() with base 10: '¹'
E   Failing explicit example: test_broker_status_mapping_is_total(
E       code='¹',
```

### PBT tags.an_unreachable_broker_is_not_a_rejection

Break: (PR 25 review round) `send_order` treats only `broker_unavailable` as not sent, so a -360 refusal is stored as a rejection.

```
E   Failed: DID NOT RAISE BrokerUnavailableError
E   Failing test case: test_an_unreachable_broker_is_not_a_rejection(
E       kind='rate_limited',
E       times=1,  # or any other generated value
```

### PBT tags.idempotent_submission

Break: `send_order` skips the tag lookup before a retry (`found = None`).

```
E   AssertionError: [{'id': 'L-1', 'orderTag': 'HTE0EKRA142SDCB6FHTF', ...}, {'id': 'B-2', 'orderTag': 'HTE0EKRA142SDCB6FHTF', ...}]
E   assert 2 <= 1
```

Break: `_lookup_before_retry` re-sends an entry the lookup did not find, with the book lagging (the state machine's `the_book_lags`). (PR 28)

```
E   AssertionError: [{'id': 'L-1', 'orderTag': 'HTE0E8MRA22G4F68YCQH', 'symbol': 'NSE:SBIN-EQ', 'side': 1, ...}, {'id': 'B-2', 'orderTag': 'HTE0E8MRA22G4F68YCQH', 'symbol': 'NSE:SBIN-EQ', 'side': 1, ...}]
E   assert 2 <= 1
E   state.at_most_one_broker_order_per_tag()
```

### PBT tags.a_timeout_is_looked_up_before_any_retry

Break: in `submit.py`, `_lookup_before_retry` returns None for an entry the lookup did not find (the PR 13 behaviour). (PR 28)

```
E   assert 2 == 1
E    +  where 2 = len([1, 3])
E   Failing explicit example: test_a_timeout_is_looked_up_before_any_retry(
E       outcomes=['landed', 'ok'],
E       purpose='entry',
E       lagging=True,
```

### PBT router.unresolved_entry_is_alerted_not_resent

Break: in `router.py`, `_send_once` swallows `UnresolvedOrderError` without the alert. (PR 28)

```
E   AssertionError: assert 0 == 1
E    +  where 0 = <built-in method count of list object at 0x7f3d041852c0>('entry.unresolved')
E   Failing explicit example: test_unresolved_entry_is_alerted_not_resent(
```

### PBT tags.a_duplicated_tag_is_read_one_way

Break: `TagBook.of` keeps the first order under a tag even when the broker ended it unfilled. (PR 28)

```
E   AssertionError: assert BrokerOrder(order_id='D-0', ..., filled_qty=0, status='cancelled', ...) is BrokerOrder(order_id='D-1', ..., filled_qty=0, status='placed', ...)
E   Failing test case: test_a_duplicated_tag_is_read_one_way(
E       orders=[('cancelled', 0), ('placed', 0)],
```

### PBT manage.a_dead_second_order_never_ends_the_entry

Break: `TagBook` keeps the last order per tag and ignores the row's broker order id (the `by_tag` dicts before the review round). (PR 28)

```
E   AssertionError: assert ('B-1', 'rejected') == ('B-1', 'filled')
E   Failing explicit example: test_a_dead_second_order_never_ends_the_entry(
E       status=5,
```

### PBT tags.a_tag_twice_at_the_broker_blocks_the_account

Break: `find_mismatches` flags a tag only when it is held more than twice. (PR 28)

```
E   AssertionError: assert [] == ['HTE00000000000000000']
E   Failing test case: test_a_tag_twice_at_the_broker_blocks_the_account(
```

### PBT manage.a_second_order_under_one_tag_is_tracked

Break: `_record` does not call `_check_excess`. (PR 28)

```
E   AssertionError: assert 0 == 1
E   Failing explicit example: test_a_second_order_under_one_tag_is_tracked(
```

Break: the review's fence, a fill recorded only for the order's stored broker order id. (PR 28)

```
E   assert 500 == (500 + 1)
E   Failing explicit example: test_a_second_order_under_one_tag_is_tracked(
```

Break: `_check_excess` alerts every fill above the quantity, not the crossing one (review round). (PR 28)

```
E   AssertionError: assert 3 == 1
E    +    where <built-in method count of list object> = ['fill.excess', 'fill.excess', 'fill.excess'].count
E   Failing explicit example: test_a_second_order_under_one_tag_is_tracked(
E       extra=3,
E       parts=3,
```

Break: `_apply` checks the excess before `_protect` (CodeAnt). (PR 28)

```
E   assert [False] == [True]
```

### PBT router.a_stored_capability_never_enables_a_real_entry

Break: `router._gtt_refusal` passes `True` for the adapter's value (stored only, before PR 29). (PR 29)

```
E   assert 1 == 0
E    +  where 1 = scalar(<sqlalchemy.sql.selectable.Select object>)
E    +    and   <sqlalchemy.sql.selectable.Select object> = select_from(HatOrder)
```

### PBT broker.session_check_refreshes_capabilities

Break: the refresh runs on live checks only (the first version of PR 29). (PR 29)

```
E   AssertionError: assert {} == {'gtt_deliver... 'SL-M'], ...}
```

Break: a failing adapter's exception escapes the refresh. (PR 29)

```
E   RuntimeError: adapter down
E   Failing test case: test_session_check_refreshes_capabilities(
```

Break: the refresh runs before the session row's commit, so its rollback undoes the row. (PR 29)

```
E   AttributeError: 'NoneType' object has no attribute 'status'
E   Failing test case: test_session_check_refreshes_capabilities(
```

Break: a refresh whose event write fails is not rolled back, so the next commit stores the new capabilities without their event (PR 29, CodeAnt review). The property reads the stored value with a query, which flushes first; `session.refresh` would discard the pending change and hide this break.

```
E   AssertionError: assert {'gtt_deliver... 'SL-M'], ...} == {}
```

### PBT router.desks_start_with_the_adapters_capabilities

Break: `Desks` does not refresh at start. (PR 29)

```
E   assert False is True
E   Failing test case: test_desks_start_with_the_adapters_capabilities(
E       stored=False,
E       live=True,
```

Break: `OwnedBroker.capabilities` removed, so the read goes through the session owner. (PR 29)

```
E   AssertionError: the capability read waited for a broker call in flight
```

### PBT tags.refusals_before_the_broker

Break: `_check_allowed` passes `True` for the adapter's GTT (stored only). (PR 29)

```
E   Failed: DID NOT RAISE OrderRefusedError
E   Failing test case: test_refusals_happen_before_the_broker(
```

Break: `_check_allowed` has no GTT check for an entry. (PR 29)

```
E   Failed: DID NOT RAISE OrderRefusedError
E   Failing test case: test_refusals_happen_before_the_broker(
```

Break: the session-live check in `_check_allowed` is removed.

```
E   Failed: DID NOT RAISE OrderRefusedError
E   Failing test case: test_refusals_happen_before_the_broker(
```

Break: `ExecutionGuard.refusal` refuses a stop or a sale too while the date is not reconciled (main before PR 24).

```
E   app.hat.execution.submit.OrderRefusedError: not_reconciled
E       'reconciled': False,
E       'purpose': 'stop_day',
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

### PBT ops.no_outage_before_the_login_window

Break: `SessionWatch.observe` has no 08:30 window (the code before PR 30). (PR 30)

```
E   assert not True
E    +  where True = WatchStep(retry_in=2, alert=True).alert
E   Failing test case: test_no_outage_before_the_login_window(
```

### PBT router.broker_health_follows_the_session

Break: `check_session` does not sync `broker_health` (the code before PR 30). (PR 30)

```
E   AssertionError: assert 'armed' == 'tripped'
```

Break: `broker_health_failing` ignores the 09:05-15:30 window (`return not live`). (PR 30)

```
E   AssertionError: assert 'tripped' == 'armed'
```

Break: a failed status read returns before the sync, so it does not count as not live. (PR 30)

```
E   AssertionError: assert 'armed' == 'tripped'
```

Break: the sync trips the account switch instead of each bot. (PR 30)

```
E   AssertionError: assert 'armed' == 'tripped'
```

Break: the sync covers only the account's first bot (CodeAnt review: the property now has a second bot on the account). (PR 30)

```
E   AssertionError: assert 'armed' == 'tripped'
```

Break: the sync covers every bot, not only the account's (a bot on another account is in the property). (PR 30)

```
E   AssertionError: assert 'tripped' == 'armed'
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

Break: (PR 25, the property now asserts true) `FYERS_CAPABILITIES` reports `gtt_delivery=False`.

```
E   AssertionError: assert False is True
E    +  where False = Capabilities(gtt_delivery=False, tag_max_len=20, tag_alphabet='A-Za-z0-9', order_types=('MARKET', 'LIMIT', 'SL', 'SL-M'), products=('CNC',), static_
```

Before PR 25 the break was the reverse (`gtt_delivery=True` while the property asserted false).

### CONF fyers_adapter_wraps_existing

Break: `execution/gtt.py` imports `fyers_apiv3`.

```
E   AssertionError: PosixPath('.../backend/app/hat/execution/gtt.py')
E   assert not <re.Match object; match='\nimport fyers_apiv3'>
```

Break (PR 25): `fyers_session.py` imports `fyersModel` from the SDK itself instead of `from .fyers_ipv4 import sdk`.

```
E   AssertionError: assert ['execution/f...orts the SDK'] == []
E     Left contains one more item: 'execution/fyers_session.py: imports the SDK'
```

Second break (PR 25): `fyers_ipv4.sdk()` imports the SDK without calling `install()` first (the check locates `install()` before the import and fails when it is absent).

```
E   ValueError: substring not found
```

### PBT fyers_ipv4.fyers_http_leaves_over_ipv4

Break: `connect_ipv4` resolves a Fyers host with family 0 (any) instead of `AF_INET`.

```
E   assert [0] == [<AddressFamily.AF_INET: 2>]
E     At index 0 diff: 0 != <AddressFamily.AF_INET: 2>
E   Failing test case: test_fyers_http_leaves_over_ipv4(
```

Second break: `connect_ipv4` passes every host straight through (`if True: return connect(...)`).

```
E   assert [<AddressFamily.AF_UNSPEC: 0>] == [<AddressFamily.AF_INET: 2>]
E     At index 0 diff: <AddressFamily.AF_UNSPEC: 0> != <AddressFamily.AF_INET: 2>
E   Failing test case: test_fyers_http_leaves_over_ipv4(
```

Third break: the first refused IPv4 address raises at once instead of trying the next.

```
E   AssertionError: assert ['10.0.0.1'] == ['10.0.0.1', '10.0.0.2']
E     Right contains one more item: '10.0.0.2'
E   Failing test case: test_fyers_http_leaves_over_ipv4(
```

### PBT fyers_ipv4.is_fyers_host_exact

Break: `is_fyers_host` matches `.endswith("fyers.in")` (no dot), so a look-alike counts.

```
E   AssertionError: assert True == (('notfyers.in' == 'fyers.in'
E    +  where True = <function is_fyers_host at 0x7f67ad813950>('notfyers.in')
E   Failing test case: test_is_fyers_host_exact(
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

### PBT fyers_auth_routes.cross_site_write_refused

Break: (PR 38) the guard skips the write-header check.

```
E   assert 200 == 403
E   Failing explicit example: test_cross_site_write_refused(
E       header=None,
E       origin='http://localhost:3000',
E       fetch_site='same-origin',
```

Break: (PR 38) the guard accepts any `Origin`.

```
E   assert 200 == 403
E   Failing explicit example: test_cross_site_write_refused(
E       header='1',
E       origin='https://evil.example',
```

Break: (PR 38) the guard ignores `Sec-Fetch-Site` when no `Origin` is sent. Before the `@example`s were added, this break passed: 100 random draws missed the shape.

```
E   assert 200 == 403
E   Failing explicit example: test_cross_site_write_refused(
E       header='1',
E       origin=None,
```

Break: (PR 38) `POST /disconnect` without `Depends(frontend_write)` (also fails `CONF hat_writes_guarded`, in `verify-scheduler`).

```
E   assert 200 == 403
E       header=None,
```

### PBT fyers_auth_routes.token_for_another_account_refused

Break: (PR 38) `POST /token` does not pass `accept_user` to the exchange.

```
tests/hat/properties/test_fyers_auth_routes.py:374: in test_token_for_another_account_refused
E   assert 200 == 403
E   Failing test case: test_token_for_another_account_refused(
```

Break: (PR 38) the account check counts every Fyers account, not only active ones.

```
tests/hat/properties/test_fyers_auth_routes.py:371: in test_token_for_another_account_refused
E   assert 403 == 200
```

Break: (PR 38) the account check lets a user it could not read (`None`) through.

```
tests/hat/properties/test_fyers_auth_routes.py:374: in test_token_for_another_account_refused
E   assert 200 == 403
```

Break: (PR 38 review) the route has no `except SQLAlchemyError`, so unreadable accounts fall to the generic 500.

```
tests/hat/properties/test_fyers_auth_routes.py:367: in test_token_for_another_account_refused
E   assert 500 == 503
```

Break: (PR 38 review) `exchange` answers `WRONG_ACCOUNT` for an unknown user too.

```
tests/hat/properties/test_fyers_auth_routes.py:375: in test_token_for_another_account_refused
E   AssertionError: assert 'This Fyers l...that account.' == 'Could not co...log in again.'
```

### PBT fyers_auth.failed_exchange_fails_closed

Break: The SDK failure is raised with its own text.

```
E   AssertionError: assert ('AUTHCODE0000000000' not in 'login faile...y-fyers-9a1b'
E     
E     'AUTHCODE0000000000' is contained here:
E       -fyers-9a1b and 'SECRETKEY-9f8e7d6c' not in 'login faile...y-fyers-9a1b'
```

### PBT broker.token_refused_on_a_read_ends_the_check

Break: (PR 23) a -16 on a read leaves the profile check standing.

```
1 failed: assert <SessionStatus.LIVE: 'live'> is <SessionStatus.EXPIRED: 'expired'>
```

### PBT broker.token_refused_is_session_expired

Break: (PR 23, each tried alone) `_read_ok` without the -16 branch; `session_status` without the profile probe; the adapter's `_read` maps -16 to `broker_unavailable`; main's broker and adapter.

Also: no check cache (`assert 2 == 1`); the cache ignores a new SDK model, i.e. a new token (`assert 1 == 2`).

```
no -16 branch:  E   AssertionError: assert <SessionStatus.LIVE: 'live'> is <SessionStatus.EXPIRED: 'expired'>
no probe:       E   AssertionError: assert <SessionStatus.LIVE: 'live'> is <SessionStatus.EXPIRED: 'expired'>
read code:      E   AssertionError: assert 'broker_unavailable' == 'broker_session_expired'
main:           E   AssertionError: assert <SessionStatus.LIVE: 'live'> is <SessionStatus.EXPIRED: 'expired'>
```

### PBT fyers_auth.a_good_exchange_saves_once_and_reports_in_order

Break: (PR 23, each tried alone) `status()` reports `token.user_id or ""`; the profile's user id is not saved.

```
empty id:     E   AssertionError: assert '' == None
not saved:    E   AssertionError: assert None == 'XA1234'
```


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

### PBT fyers_broker.a_rejection_with_an_id_stays_rejected

Break: `FyersBroker` reads an error reply that carries an `id` as SUBMITTED with that id.

```
E   AssertionError: assert ('SUBMITTED',...102200316678') == ('REJECTED', '')
E     At index 0 diff: 'SUBMITTED' != 'REJECTED'
E   Failing explicit example: test_a_rejection_with_an_id_stays_rejected(
```

### PBT broker.rate_limit_is_its_own_code

Break: the adapter's read raises `BrokerUnavailableError(CODE_UNAVAILABLE)` for a rate-limited read.

```
E   AssertionError: assert 'broker_unavailable' == 'broker_rate_limited'
E   Failing test case: test_rate_limit_is_its_own_code(
```

Second break: `checked_reply` no longer raises `FyersRateLimitedError` for -360 (it falls through to the generic unavailable error).

```
E   AssertionError: assert 'broker_unavailable' == 'broker_rate_limited'
E   Failing test case: test_rate_limit_is_its_own_code(
```

Third break: a rate-limited place answers `CODE_REJECTED`.

```
E   AssertionError: assert 'broker_rejected' == 'broker_rate_limited'
E   Failing test case: test_rate_limit_is_its_own_code(
```

Fourth break: a rate-limited modify or cancel (`_ack`) answers `CODE_REJECTED`.

```
E   AssertionError: assert 'broker_rejected' == 'broker_rate_limited'
E   Failing test case: test_rate_limit_is_its_own_code(
```

### CONF brk__recorded_writes

Break: an unmasked PAN-shaped id (`ABCDE1234F`) added to the fixture's `_note`.

```
E   AssertionError: assert ['ABCDE1234F'... 'XXXXX0000X'] == ['XXXXX0000X'... 'XXXXX0000X']
E     At index 0 diff: 'ABCDE1234F' != 'XXXXX0000X'
```

Second break: the GTT book row reads `side` (the pre-PR 25 key) instead of `tran_side`.

```
E   app.hat.execution.broker.BrokerUnavailableError: broker_response_invalid
```

Third break: `FyersBroker.place_gtt` drops `orderTag` from the payload.

```
E   AssertionError: assert [('place_gtt_... 'qty': 1}}})] == [('place_gtt_...': 1}}, ...})]
E     At index 0 diff: ('place_gtt_order', {'side': -1, 'symbol': 'NSE:SBIN-EQ', 'productType': 'CNC', 'orderInfo': {'leg1': {'price': 578.1, 'triggerPrice': 587.75, 'qty'
```

### PBT broker.the_fyers_adapter_unlocks_modes_on_an_active_account

Break: `FYERS_CAPABILITIES` reports `gtt_delivery=False` (also fails `brk__fyers_capabilities`).

```
E   assert False is True
E   Failing test case: test_the_fyers_adapter_unlocks_modes_on_an_active_account(
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

### PBT trades.global_open_risk_across_bots

Break: `backend/app/hat/engine/trades.py` as on main (the snapshot is `bot_snapshot` of the calling bot only, no owner lock).

```
E   AssertionError: assert UUID('01a16196-0875-7104-835c-0c3e3a4a95a1') is None
E   Failing test case: test_global_open_risk_across_bots(
E       order=[0, 1],
E       room=1,
E       spare=1,
FAILED tests/hat/properties/test_trades.py::test_global_open_risk_across_bots
```

Break: (PR 46) the seed's `global_open_risk_pct` back to 0.03: the second bot's entry fits under the old cap.

```
E   AssertionError: assert UUID('01a16469-36fd-729b-8546-f2c41c2f730a') is None
E   Failing test case: test_global_open_risk_across_bots(
E       order=[1, 1],
```

### DB runtime.owner_serialised

Break: in `backend/app/hat/engine/trades.py`: `open_entry` takes no `lock_owner`.

```
E   AssertionError: [True, True]
FAILED tests/hat/db/test_db_runtime.py::test_runtime__owner_serialised - As [cut]
======================= 1 failed, 12 deselected in 0.49s =======================
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

### PBT router.entry_needs_live_session

Break: `_preflight` drops the live-session refusal (`_session_refusal`).

```
FAILED tests/hat/properties/test_router.py::test_entry_needs_live_session
AssertionError: assert 1 == 0
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

### CONF exec__default_mode_confirm

Break: `create_bot` accepts a confirm or auto bot (before PR 45, as `exec__default_mode_shadow`).

```
E   Failed: DID NOT RAISE VersionError
FAILED tests/hat/conformance/test_conf_broker.py::test_exec__default_mode_shadow
```

Break: (PR 45) the bot row's default and server default go back to shadow (`models/strategy.py`).

```
E   AssertionError: assert 'shadow' == 'confirm'
E     - confirm
E     + shadow
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

### PBT manage.entry_remainder_tracked

Break: in `backend/app/hat/engine/live_trades.py`, `apply_exit_fill` closes the emptied position while its entry still works (the pre-PR 20 code; the late fill is then refused).

```
E   assert 6 != 6
E   Failing explicit example: test_entry_remainder_tracked(
E       first=1,
E       steps=[('stop', 0), ('entry', 3)],
```

Break: in `backend/app/hat/execution/protect.py`, `StopDesk.cancel_entry` cancels nothing.

```
E   AssertionError: assert ('B-1' in [] or 'B-1' in ['B-2'])
E    +  where ['B-2'] = calls_named('cancel_order')
```

### PBT manage.verify_covers

Break: in `backend/app/hat/execution/protect.py`, `verify` never keeps the resting trigger (`keep = False`): it modifies the GTT to the stop floored to the new tick, below the trigger it rests at, and replaces a refused one lower (the pre-PR 20 code).

```
ExceptionGroup: Hypothesis found 2 distinct failures in explicit examples. (2 sub-exceptions)
    | AssertionError: assert Decimal('95.0000') == Decimal('95.0300')
    | AssertionError: assert 'gtt.unverified' in ['gtt.missing']
```

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

Break: `_sell_locked` returns before sending while the trade date is not reconciled (main before PR 24 ran `_reconciled` there).

```
E   AssertionError: assert [] == [('MARKET', 500)]
E   Failing explicit example: test_real_exits_never_blocked(
E       kill='none',
E       reconciled='never',
```

Break: `ExecutionGuard.refusal` refuses a stop while the date is not reconciled: the fill sync places no stop.

```
E   ValueError: not enough values to unpack (expected 1, got 0)
E   Failing explicit example: test_real_exits_never_blocked(
E       reconciled='never',
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

Break (PR 27): in `backend/app/hat/execution/fills.py`, `FillSync.run` has no resting step (`elif False and self._resting_trigger(...)`).

```
E   AssertionError: assert [] == [('modify_ord...: 'MARKET'}))]
E   Failing test case: test_gtt_trigger_handled(
```

Break (PR 27): the GTT row is ended when its trigger is first marked (the review's proposal: `gtt.status = 'expired'` in `_gtt_resting`), so the sync protects again over the working sale.

```
E   AssertionError: assert [('modify_ord...'SL-M', ...})] == [('modify_ord...: 'MARKET'}))]
E   Failing test case: test_gtt_trigger_handled(
```

Break (PR 25): the adapter reads an order row under a stop_gtt tag as an ordinary order (`tag` kept, no `gtt_tag`), so the tagged child is not attributed to its GTT.

```
E   assert 500 == (500 - 250)
E    +  where 500 = <app.hat.models.runtime.HatPosition object at 0x7f5b45cef2f0>.qty_open
E   Failing test case: test_gtt_trigger_handled(
```

Second break (PR 25): `_build_request` drops the GTT's tag, so the rig's child has no tag to carry.

```
E   ValueError: not enough values to unpack (expected 1, got 0)
E   Failing test case: test_gtt_trigger_handled(
E       part=0.5,
```

Third break (PR 25 review round): `_row_tags` keeps Fyers' `GTTUntagged` as the row's tag, so the sale is never attributed (also fails `manage.gtt_resting_marketed`).

```
E   assert 500 == (500 - 250)
E    +  where 500 = <app.hat.models.runtime.HatPosition object at 0x7effb5eec460>.qty_open
E   Failing test case: test_gtt_trigger_handled(
E       part=0.5,  # or any other generated value
E       ended=False,  # or any other generated value
E       child='untagged',
```

Fourth break (PR 25 CodeAnt round): an untagged sale that ended ends the GTT at any quantity (`stray`).

```
E   AssertionError: assert [('place_orde...'SL-M', ...})] == [('modify_ord...: 'MARKET'}))]
E   Failing explicit example: test_gtt_trigger_handled(
E       part=0.5,
E       ended=True,
```

### PBT manage.gtt_resting_marketed

Break: in `backend/app/hat/execution/fills.py`, `_gtt_resting` asks again after the book shows the sale MARKET (`if order.order_type is OrderType.MARKET: return` to `if False:`).

```
E   AssertionError: assert [('modify_ord...: 'MARKET'}))] == [('modify_ord...: 'MARKET'}))]
E   Failing explicit example: test_gtt_resting_marketed(
```

Break: `_gtt_resting` treats every sync as a first sight (`if not self._trigger_seen(...)` to `if True:`).

```
E   AssertionError: assert [('modify_ord...: 'MARKET'}))] == [('modify_ord...: 'MARKET'}))]
E   Failing explicit example: test_gtt_resting_marketed(
```

Break: `_gtt_resting` never asks for the market sale (`if self._desk.market_sale(...).ok or ...:` to `if True:`).

```
E   AssertionError: assert [] == [('modify_ord...: 'MARKET'}))]
E   Failing test case: test_gtt_trigger_handled(
```

Break: `_resting_trigger` takes any untagged CNC SELL of the GTT's quantity (`return (limit is not None and order.price == limit) or ...` to `return True or ...`), so a sale at another price is dumped at the market.

```
E   AssertionError: assert [('modify_ord...: 'MARKET'}))] == []
E   Failing explicit example: test_gtt_resting_marketed(
```

Break: no wait on a first sight (`if now - since < RESTING_AFTER:` to `if False:`), so a sale on its way to a fill is made a market sale.

```
E   AssertionError: assert [('modify_ord...: 'MARKET'}))] == []
E   Failing explicit example: test_gtt_resting_marketed(
```

Break: in `backend/app/hat/execution/protect.py`, `triggered_gtts` without the trade-date bound (`HatEvent.occurred_at >= start` removed), so a trigger of an earlier day whose end the sync never saw holds every exit back.

```
E   AssertionError: assert 'place_order' in []
E   Failing explicit example: test_gtt_resting_marketed(
```

Break: in `backend/app/hat/execution/manage.py`, `_sell_qty` no longer waits on `StopDesk.triggered_gtts`, so a flatten sends a sale over the working one.

```
E   AssertionError: assert [('modify_ord...ARKET', ...})] == [('modify_ord...: 'MARKET'}))]
E   Failing explicit example: test_gtt_resting_marketed(
```

Break: in `backend/app/hat/execution/protect.py`, `follow` no longer skips a triggered GTT (`if row.id in triggered:` to `if False:`).

```
E   AssertionError: assert [('modify_ord...ice': 94.1}))] == [('modify_ord...: 'MARKET'}))]
E   Failing explicit example: test_gtt_resting_marketed(
```

Break (PR 25): a tagged child is attributed to any GTT on its symbol (`by_symbol`), whatever its tag.

```
E   AssertionError: assert [('modify_ord...: 'MARKET'}))] == []
E     Left contains one more item: ('modify_order', ('X-1', {'order_type': 'MARKET'}))
E   Failing explicit example: test_gtt_resting_marketed(
```

Second break (PR 25): a tagged child must also match the GTT's limit price (the bypass in `_resting_trigger` removed).

```
E   AssertionError: assert [] == [('modify_ord...: 'MARKET'}))]
E     Right contains one more item: ('modify_order', ('X-1', {'order_type': 'MARKET'}))
E   Failing explicit example: test_gtt_resting_marketed(
```

Third break (PR 25): the tagged child read as an ordinary order (also fails `manage.gtt_trigger_handled`, above).

```
E   Failing explicit example: test_gtt_resting_marketed(
```

Fourth break (PR 25 review round): `GTTUntagged` kept as a tag.

```
E   AssertionError: assert [] == [('modify_ord...: 'MARKET'}))]
E     Right contains one more item: ('modify_order', ('X-1', {'order_type': 'MARKET'}))
```

### PBT manage.lost_answers_never_freeze

Break: in `backend/app/hat/execution/protect.py`, `settle_lost` treats no row as ready, so a lost answer is never settled.

```
    | AssertionError: assert {'stop_day', 'stop_gtt'} == {'stop_gtt'}
```

Break: in `backend/app/hat/execution/fills.py`, `_unprotected` counts an unsure stop as working, so a lost stop that never landed is never replaced.

```
E   assert False
E    +  where False = _resting(<hat_manage_support.ManageRig ...>, <HatPosition ...>)
```

Break: in `backend/app/hat/execution/manage.py`, `_settle_sales` treats every sale as settled, so a lost sale is never looked up.

```
E   assert ([] or False or False)
E    +  where [] = _sold(<hat_manage_support.ManageRig ...>, 13)
```

Break: in `backend/app/hat/execution/submit.py`, `send_order` no longer catches the adapter's `InvalidIntentError`, so a refused intent stays unconfirmed.

```
E   AssertionError: assert ([] or 'invalid' == 'unconfirmed'
```

Break: in `backend/app/hat/execution/protect.py`, `protect` alerts a lost stop whose answer is still young.

```
E   AssertionError: assert 4 == 2
E    +  where 4 = <built-in method count of list object at 0x...>('stop.not_placed')
```

### PBT manage.async_sale_rejection_retried

Break: in `backend/app/hat/execution/submit.py`, a sale the broker ends short is not treated as a rejection.

```
E   AssertionError: assert 0 == 1
E    +  where 0 = _event_count(<Session ...>, <HatOrder ...>, 'order.rejected')
```

Break: in `backend/app/hat/execution/fills.py`, the sync counts a position as protected when `protect` only waited for its working sale, so a sale that part filled and then ended in the same sync leaves the position without a stop (found by the CI profile in the PR 18 review round; pinned by an `@example`).

```
E   assert False
E    +  where False = _resting(<hat_manage_support.ManageRig object at 0x...>, <app.hat.models.runtime.HatPosition object at 0x...>)
E   Failing explicit example: test_async_sale_rejection_retried(
E       ending='cancelled',
```

### PBT fyers_broker.an_ok_reply_without_its_list_is_unreadable

Break: in `backend/app/hat/execution/fyers_broker.py`, `_read_list` turns a missing or non-list key into `[]`.

```
E   Failed: DID NOT RAISE FyersUnavailableError
E   Failing test case: test_an_ok_reply_without_its_list_is_unreadable(
E       method='fetch_gtt_orders',
```

### PBT tags.a_keyless_book_never_reads_as_absent

Break: the same `_read_list` change.

```
E   Failed: DID NOT RAISE BrokerUnavailableError
E   Failing test case: test_a_keyless_book_never_reads_as_absent(
E       book={'s': 'ok', 'code': 200},
```

### PBT fyers_broker.a_circuit_opening_mid_place_is_not_sent

Break: in `backend/app/hat/execution/fyers_broker.py`, `_call_with_guard` returns the timeout fallback when the breaker opened, as before PR 18.

```
E   AssertionError: assert 'SUBMIT_UNCONFIRMED' == 'REJECTED'
```

### PBT router.non_live_sleeve_routes_as_shadow

Break: in `backend/app/hat/execution/modes.py`, `bot_mode` ignores `lifecycle_state`.

```
E   AssertionError: assert 'confirm' == 'shadow'
```

### PBT manage.a_settled_answer_never_sells_twice

Break: in `backend/app/hat/execution/fills.py`, the sync no longer sweeps closed positions with an unsure stop.

```
E   AssertionError: assert [{'id': 'GL-1...: 'CNC', ...}] == []
E   Failing test case: test_a_settled_answer_never_sells_twice(
E       case='closed_sweep',
```

Break: in `backend/app/hat/execution/manage.py`, `_settle_sales` adopts a lost sale found with shares sold.

```
E   AssertionError: assert [{'symbol': '...MARKET', ...}] == []
E       case='landed_filled',
```

Break: `_settle_sales` ends an earlier session's lost sale that today's book does not hold as never sent.

```
E   AssertionError: assert 'expired' == 'unconfirmed'
E     - unconfirmed
```

### PBT tags.a_reserved_tag_is_sent_by_one_sender

Break: in `backend/app/hat/execution/submit.py`, `send_order` stamps the attempt without the conditional claim.

```
E   AssertionError: assert 1 == 0
E    +  where 1 = SendOutcome(tag='HTE0E...', status='placed', broker_order_id='B-2', code='ok', broker_calls=1).broker_calls
```

Break: the conditional claim covers only the first attempt, not a retry after a lost answer.

```
E   AssertionError: assert 1 == 0
E    +  where 1 = SendOutcome(tag='HTE0E...', status='placed', broker_order_id='B-3', code='ok', broker_calls=1).broker_calls
```

Break: the conditional claim checks only the attempt time, not the status and broker id (CodeAnt, PR 18 review).

```
E   AssertionError: assert 1 == 0
E    +  where 1 = SendOutcome(tag='HTE0E...', status='placed', broker_order_id='B-1', code='ok', broker_calls=1).broker_calls
E   Failing explicit example: test_a_reserved_tag_is_sent_by_one_sender(
E       answer='accepted',
E       retry=True,
E       adopted=True,
```

Break: `_lookup_before_retry` returns None for an entry the lookup did not find (review round: entries back in the retry case). (PR 28)

```
E   Failed: DID NOT RAISE UnresolvedOrderError
E   Failing explicit example: test_a_reserved_tag_is_sent_by_one_sender(
E       answer='accepted',
E       retry=True,
E       adopted=False,
E       purpose='entry',
```

Break: `send_order` does not return early for a row the sync already adopted (`if False and row.status in _ENDED`). (PR 28)

```
E   app.hat.execution.submit.UnresolvedOrderError: an entry of unknown outcome is never sent again
E   Failing test case: test_a_reserved_tag_is_sent_by_one_sender(
E       retry=True,
E       adopted=True,
E       purpose='entry',
```

### PBT manage.a_resting_sale_is_never_stopped_over

Break: in `backend/app/hat/execution/protect.py`, `protect` no longer waits for a sale the broker holds (CodeAnt, PR 18 review).

```
E   AssertionError: assert ['place_order'] == []
```

### PBT manage.the_close_ends_only_settled_answers

Break: in `backend/app/hat/execution/manage.py`, `end_session` ends a lost order sent less than the settle wait ago (CodeAnt, PR 18 review).

```
E   AssertionError: assert 'expired' == 'unconfirmed'
E     - unconfirmed
E     + expired
```

Break: `end_session` ends a lost order the book holds.

```
E   AssertionError: assert 'expired' == 'unconfirmed'
```

### PBT tags.the_gtt_book_reads_only_resting_stops

Break: in `backend/app/hat/execution/fyers_adapter.py`, `_gtt_row` keeps an ended GTT.

```
E   app.hat.execution.broker.BrokerUnavailableError: broker_response_invalid
E   Failing test case: test_the_gtt_book_reads_only_resting_stops(
```

Break: `_gtt_row` drops a triggered GTT (`ord_status` 2).

```
E   AssertionError: assert [BrokerGtt(gt...imal('0.05'))] == [BrokerGtt(gt...imal('0.05'))]
E     Right contains one more item: BrokerGtt(gtt_id='G1', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('0.05'))
```

Break: `_gtt_row` checks the status before leaving out someone else's OCO or intraday GTT.

```
E   app.hat.execution.broker.BrokerUnavailableError: broker_response_invalid
```

Break: `_gtt_row` keeps an OCO.

```
E   AssertionError: assert [BrokerGtt(gt...imal('0.05'))] == [BrokerGtt(gt...imal('0.05'))]
E     Left contains one more item: BrokerGtt(gtt_id='G1', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('0.05'))

Break (PR 25): the GTT row reads `side` instead of the live `tran_side`.

```
E   app.hat.execution.broker.BrokerUnavailableError: broker_response_invalid
E   Failing test case: test_the_gtt_book_reads_only_resting_stops(
E       rows=[({'id': 'G1',
E          'symbol': 'NSE:SBIN-EQ',
E          'tran_side': -1,
E          'product_type': 'CNC',
E          'ordertag': '1:GTTHTG00000000000000000',
```

Second break (PR 25): the GTT row reads `productType` instead of the live `product_type`.

```
E   app.hat.execution.broker.BrokerUnavailableError: broker_response_invalid
E   Failing test case: test_the_gtt_book_reads_only_resting_stops(
```

Third break (PR 25): `_gtt_tag` keeps the `GTT` prefix (also fails `manage.gtt_trigger_handled`).

```
E     At index 0 diff: BrokerGtt(gtt_id='G1', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('0.05'), tag=None) != BrokerGtt(gtt_id='G1', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('0.05'), tag='HTG00000000000000000')
E   Failing test case: test_the_gtt_book_reads_only_resting_stops(
```

Fourth break (PR 25): `_gtt_tag` accepts any HAT tag, not only a stop_gtt one.

```
E     At index 1 diff: BrokerGtt(gtt_id='G1', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('0.05'), tag='HTE00000000000000003') != BrokerGtt(gtt_id='G1', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('0.05'), tag=None)
E   Failing test case: test_the_gtt_book_reads_only_resting_stops(
```

Fifth break (PR 25): the row's `tag` is not read from `ordertag`.

```
E     At index 0 diff: BrokerGtt(gtt_id='G1', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('0.05'), tag=None) != BrokerGtt(gtt_id='G1', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('0.05'), tag='HTG00000000000000000')
E   Failing test case: test_the_gtt_book_reads_only_resting_stops(
```

Sixth break (PR 25 review round): a row in the placement keys has its `productType` ignored.

```
| AssertionError: assert [BrokerGtt(gt...00000000000')] == [BrokerGtt(gt...00000000000')]
    | Failing test case: test_the_gtt_book_reads_only_resting_stops(
```

Seventh break (PR 25 review round): the side is read from `tran_side` only.

```
E   app.hat.execution.broker.BrokerUnavailableError: broker_response_invalid
E   Failing test case: test_the_gtt_book_reads_only_resting_stops(
E       rows=[({'id': 'G1',
E          'symbol': 'NSE:SBIN-EQ',
E          'side': -1,
E          'productType': 'CNC',
```

Eighth break (PR 25 CodeAnt round): the row's `foreign` is never set.

```
    | AssertionError: assert [BrokerGtt(gt...oreign=False)] == [BrokerGtt(gt...foreign=True)]
    | Failing test case: test_the_gtt_book_reads_only_resting_stops(
```

### PBT tags.gtt_payloads

Break: the adapter's `place_gtt_stop` no longer passes `order_tag`.

```
E   AssertionError: assert [('place_gtt_... {'id': '0'})] == [('place_gtt_... {'id': '0'})]
E     At index 0 diff: ('place_gtt_order', {'side': -1, 'symbol': 'NSE:M&M-EQ', 'productType': 'CNC', 'orderInfo': {'leg1': {'price': 0.05, 'triggerPrice': 0.06, 'qty': 1
E   Failing test case: test_gtt_payloads(
```

Second break: `place_gtt_stop` accepts a request without a stop_gtt tag.

```
E   Failed: DID NOT RAISE InvalidIntentError
E   Failing test case: test_gtt_payloads(
E       symbol='M&M',
```

Third break: `FyersBroker.place_gtt` drops the tag fields from the payload.

```
E   AssertionError: assert [('place_gtt_... {'id': '0'})] == [('place_gtt_... {'id': '0'})]
E   Failing test case: test_gtt_payloads(
```

### PBT tags.a_lost_gtt_is_found_by_its_tag

Break: `find_gtt` ignores the GTT under the row's own tag (shape only).

```
E   AssertionError: assert None == BrokerGtt(gtt_id='G0', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('100'), tag='HTG00000000000000000')
E   Failing test case: test_a_lost_gtt_is_found_by_its_tag(
```

Second break: the shape match also takes a GTT under another HAT tag.

```
E   AssertionError: assert BrokerGtt(gtt_id='G0', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('100'), tag='HTG10000000000000000') == None
E   Failing test case: test_a_lost_gtt_is_found_by_its_tag(
```

Third break: `find_gtt` ignores the GTTs other rows already claim.

```
E   AssertionError: assert BrokerGtt(gtt...000000000000') == BrokerGtt(gtt...000000000000')
E     Differing attributes:
E     ['gtt_id']
E   Failing test case: test_a_lost_gtt_is_found_by_its_tag(
```

Fourth break (PR 25 CodeAnt round): the shape match also takes a GTT under a foreign tag (`and not gtt.foreign` removed). Before `gtt_books` was reweighted the property passed this break; now the generated case fails, as does the `@example`.

```
E   AssertionError: assert BrokerGtt(gtt_id='G0', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('100'), tag=None, foreign=True) == None
E   Failing test case: test_a_lost_gtt_is_found_by_its_tag(
```

### PBT router.routine_reconcile_keeps_gate

Break: `ReconciliationService.run` clears `guard.reconciled_for` at its top (re-checked in PR 24; before PR 17 it called `begin_session()` there).

```
E   AssertionError: assert {'not_reconciled'} == {None}
```

### PBT router.older_read_never_overrides_newer

Break: `ExecutionGuard.update` applies every result, whatever its read ticket (main before PR 24).

```
E   AssertionError: assert (None == 'reconcile_mismatch') == True
E   Failing test case: test_older_read_never_overrides_newer(
E       first_unknown=False,
E       second_unknown=True,
```

### PBT router.entry_gate_keyed_by_trade_date

Break: `ExecutionGuard.refusal` checks `reconciled_for is None` instead of `!= today()` (main before PR 24: a boolean gate).

```
E   assert (None is None) == (datetime.date(2026, 10, 22) in {datetime.date(2026, 10, 21)})
E   Failing explicit example: test_entry_gate_keyed_by_trade_date(
E       runs=[(0, True), (1, False)],
```

### PBT router.failed_first_reconcile_alerts_once_per_date

Break: `AccountDesk.reconcile` drops the `failed_alerted_on != day` check.

```
E   assert [datetime.dat...2026, 10, 21)] == [datetime.date(2026, 10, 21)]
E     Left contains one more item: datetime.date(2026, 10, 21)
```

Break: `AccountDesk.reconcile` never alerts (main before PR 24: it only logged).

```
E   assert [] == [datetime.date(2026, 10, 21)]
E   Failing test case: test_failed_first_reconcile_alerts_once_per_date(
E       runs=[(0, False), (0, False)],
```

### PBT router.resumed_entry_waits_for_feed

Break: `daily/live.py:stale_feed` returns the cached `staleness.stale` flag, which is false until the first check after a start.

```
E   AssertionError: assert [{'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}] == []
E     Left contains one more item: {'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}
    assert rig.places() == []  # no tick heard since the start
```

Break: `ExecutionRouter._preflight` skips the trade-date check (`if signal.trade_date != trade_date_of(now):` to `if False:`).

```
E   AssertionError: assert [{'symbol': 'NSE:SBIN-EQ', ... 'type': 'LIMIT', ...}] == []
E   Failing test case: test_resumed_entry_waits_for_feed(
E       earlier=True,
```

Break: `Desks.recover` runs the fill sync only after a successful reconciliation (main before PR 24): a restart over an unreadable book leaves a fill without its stop.

```
E   AssertionError: assert 1 == 2
E    +  where 1 = <built-in method count of list object at 0x7f8a01ce9900>('fetch_orderbook')
E       late=True,
```

Break: `Desks.resume_entries` does not wait for the reconciliation (`if not desk.guard.reconciled_today():` to `if False:`).

```
E   AssertionError: assert [{'symbol': 'NSE:SBIN-EQ', ... 'type': 'LIMIT', ...}] == []
    assert rig.places() == []  # the account has not reconciled yet
```

### PBT router.desk_calls_serialised

Break: `OwnedBroker` calls the adapter directly (`return attribute(*args, **kwargs)`, no `owner.call`).

```
E   assert 4 == 1
E    +  where 4 = <test_router.OverlapSpy object at 0x...>.most
```

Break: `AccountDesk.check_session` drops `status=status`, so `record_session` reads the status a second time.

```
E   AssertionError: assert ['session_status', 'session_status'] == ['session_status']
```

### PBT router.fill_sync_cadence

Break: `Desks.sync_due` ignores `watch_closely` (`if quiet:`), the pre-PR 26 cadence.

```
E   AssertionError: assert (1 > 1) == True
E   (note: 3 other explicit examples also failed with this error; use Verbosity.verbose to view)
E   Failing explicit example: test_fill_sync_cadence(
E       placed=2,
E       filled=10,
```

Break: `watch_closely` checks the entry only (no unsure stop).

```
E   AssertionError: assert (3 > 3) == True
```

Break: `watch_closely` also watches a refused stop (`status.in_((*UNSURE, "rejected"))`).

```
E   AssertionError: assert (4 > 3) == False
```

Break: `watch_closely` drops the trade-date filter, so an entry of the previous session still shown working syncs every step.

```
E   AssertionError: assert (2 > 1) == False
E   Failing explicit example: test_fill_sync_cadence(
E       placed=0,
E       filled=29,
E       earlier=True,
```

Break: `sync_due` stamps `synced_at` before the sync, so a failed read waits its minute.

```
E   AssertionError: assert (1 > 1) == True
E   Failing explicit example: test_fill_sync_cadence(
E       placed=19,
E       down=frozenset({0}),
```

### PBT router.one_login_per_broker

Break: `production.one_login_each` never refuses (`if account.broker in built:` to `if False:`).

```
E   Failed: DID NOT RAISE <class 'app.hat.errors.HatConfigError'>
E   Falsifying example: test_one_login_per_broker(
E       brokers=['fyers', 'fyers'],
```

### PBT manage.flatten_capped_at_holdings

Break: in `backend/app/hat/execution/manage.py`: `flatten` reads the holdings but does not cap the sale (main before PR 31).

```
E   assert 500 == 1
E    +  where 1 = min(500, 1)
E   Failing explicit example: test_flatten_capped_at_holdings(
```

Break: the cap applies to every position, not only one spanning an unhandled action.

```
E   assert 0 == 500
E   Failing test case: test_flatten_capped_at_holdings(
E       action=None,
```

Break (CodeAnt round): `_capped` does not spend the snapshot, so each of two positions of the symbol is capped at all of it.

```
E   assert 1000 == 600
E    +  where 600 = min(1000, 600)
E   Failing explicit example: test_flatten_capped_at_holdings(
```

### PBT router.mode_gates_hold_bots_in_shadow

Break: (PR 45) `gate_refusal` skips the DDPI gate (`if gates.ddpi_confirmed is not True:` to `if False:`). The same break fails `telegram.mode_names_a_bot_and_its_gate` (`verify-telegram`).

```
E   AssertionError: assert 'auto' == 'shadow'
E     - shadow
E     + auto
```

Break: (PR 45) `gate_refusal` skips the weights gate.

```
E   AssertionError: assert 'confirm' == 'shadow'
E     - shadow
E     + confirm
```

Break: (PR 45) `account_gates` counts DDPI without comparing `ddpi_confirmed_at` with the instant (one confirmed during the session opens it at once). `router.open_settles_mode_changes` does not catch this one: it confirms DDPI before the evening it queues in.

```
E   AssertionError: assert 'auto' == 'shadow'
E     - shadow
E     + auto
```

### PBT router.auto_places_only_high_confidence

Break: (PR 45) `route_mode` lets a medium signal place in auto (`confidence not in (HIGH, "medium")`).

```
E   AssertionError: assert 'auto' == 'confirm'
E     - confirm
E     + auto
```

### PBT router.open_settles_mode_changes

Break: (PR 45) `apply_mode_changes` applies every due change without reading the gates (`refusal = None`).

```
E   AssertionError: assert 1 == 0
E    +  where 0 = int('shadow' != 'shadow')
E   Failing explicit example: test_open_settles_mode_changes(
E       start='shadow',
```

### PBT manage.verify_renews_old_gtt

Break: (PR 47) in `backend/app/hat/execution/protect.py`, `verify` never calls `_renewed` (main before PR 47).

```
E   AssertionError: assert 'placed' == 'cancelled'
E     - cancelled
E     + placed
```

Break: (PR 47) `_renewed` places the new GTT without cancelling the old one.

```
E   ValueError: too many values to unpack (expected 1, got 2)
E   Failing explicit example: test_verify_renews_old_gtt(
E       age=300,
```

Break: (PR 47) `GTT_RENEW_AFTER` is 365 days.

```
E   AssertionError: assert 'placed' == 'cancelled'
E     - cancelled
E     + placed
```

### PBT manage.ex_date_gtt_replaced

Break: (PR 47) in `backend/app/hat/execution/manage.py`, `_near_ex_date` takes today only (not the next session).

```
E   AssertionError: assert 'placed' == 'cancelled'
E     - cancelled
E     + placed
```

Break: (PR 47) `StopDesk.check_gtt` trusts HAT's own book (a live row counts as resting without the broker's book).

```
======================= 1 failed, 20 deselected in 1.05s =======================
```

Break: (PR 47) `check_gtt` does not wait for a pending rescale.

```
E   AssertionError: assert ([('place_gtt...', ...})], []) == ([], [UUID('0...33958d4f95')])
E     At index 0 diff: [('place_gtt', {'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'SELL', 'product': 'CNC', 'trigger_price': 95.0, 'price': 94.05, 'order_tag': 'HTG1EWNRPW5QNGNZ1SRE'})] != []
```

Break: (PR 47) an unreadable GTT book does not fail the run.

```
E   AssertionError: assert [] == [UUID('01a164...ecaa14ad32f')]
E     Right contains one more item: UUID('01a164ce-c79b-73cf-91d5-4ecaa14ad32f')
```

Break: (PR 47) a refused GTT fails the run.

```
E   AssertionError: assert [UUID('01a164...0c502d3de4c')] == []
E     Left contains one more item: UUID('01a164ce-d1c1-72f7-803a-30c502d3de4c')
```

Break: (PR 47) a GTT gone from the book is not ended `cancelled`.

```
E   AssertionError: assert 'placed' == 'cancelled'
E     - cancelled
E     + placed
```

### PBT manage.series_change_remapped

Break: (PR 47) in `backend/app/hat/daily/live.py`, `ExitViews` ignores the series change.

```
E   assert (<object object at 0x7f2ff5d71420> is None) == True
E    +  where <object object at 0x7f2ff5d71420> = <app.hat.daily.live.ExitViews object at 0x7f2ff5971450>(...)
E   Failing explicit example: test_series_change_remapped(
```

Break: (PR 47) `StopDesk._target` keeps the old instrument's symbol.

```
E   AssertionError: assert 'NSE:SBIN-EQ' == 'NSE:SBINBE-BE'
E     - NSE:SBINBE-BE
E     ?         ^ ^^^
```

Break: (PR 47) `series.successor` takes an active instrument of any ISIN.

```
E   AssertionError: assert [{'bot_id': '...': 'EQ', ...}] == []
E     Left contains one more item: {'bot_id': '3da1d362-351e-43fc-b6e5-d5c369f06058', 'position_id': '01a164cf-3758-7550-a0c9-2970007b2135', 'symbol': 'SBIN', 'from': 'EQ', ...}
```

Break: (PR 47) `verify_stops` does not alert the series change.

```
E   assert 0 == 1
E    +  where 0 = len([])
E   Failing explicit example: test_series_change_remapped(
```

Break: (PR 47) in `backend/app/hat/telegram/render.py`, `position.series_changed` is not in `URGENT_ALERTS`.

```
E   AssertionError: assert 'position.series_changed' in frozenset({'corporate_action.unhandled', 'fill.excess', 'gtt.unfilled', 'gtt.unverified'})
E   Failing explicit example: test_series_change_remapped(
E       successor='mapped',
```

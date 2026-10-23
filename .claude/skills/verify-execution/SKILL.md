---
name: verify-execution
description: How to run, read and update the checks behind Hire a Trader's broker interface, the Fyers adapter, order tags, idempotent order submission, reconciliation, account onboarding, the broker session, the execution router, execution modes and confirm-mode approvals (TLA+ OrderReconciliation, ExecutionRouter, ApprovalToken, tag, adapter, submission, session and router properties, broker CONF checks). Use before and after any change to backend/app/hat/execution (the router, modes, Fyers broker, session, login and token store included), backend/app/hat/engine/approvals.py, backend/app/hat/api/fyers_auth.py, FakeBroker or the OrderReconciliation, ExecutionRouter or ApprovalToken model.
---

# verify-execution

Checks that an order reaches the broker at most once per tag and only after its attempt is persisted, that an unknown outcome is looked up by tag before any retry, that nothing is sent before a reconciliation or while a mismatch blocks it, that an unreadable broker never reads as an empty one, and that the Fyers adapter is a thin, CNC-only wrapper over HAT's own Fyers broker, session and login, with one login and one token store. Since it also checks the execution router: one risk check, three routes by the bot's effective mode, confirm cards that expire and re-check max-chase and the live conditions on Take, skips that continue as veto trades, and mode changes that wait for the next session open. Since it also checks live trade management on the real code: broker fills into HAT's book, the broker stop under every real fill, the stop following the position's stop up, the 15:25 swap to a GTT, the nightly GTT verify, and real exits that no kill switch blocks;, that a lost broker answer on a stop or a sale is settled and never freezes a position, that a sale the exchange rejects after accepting it is retried once, that an ok reply without its list reads as unreadable, and that a non-Live sleeve routes as shadow (the model of those stops, `StopPlacement`, is in `verify-stops`);, that an entry whose process died (or whose placement raised) between its commit and its placement is placed or ended exactly once after the restart, that the approval sweep is idempotent and ends an earlier trade date's stranded entry, and that no restart sends a third entry order. Requirement IDs (index: `docs/invariants.md`):, (..023, 030..032),, (through `bot_mode`),.

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
- `PBT tags.refusals_before_the_broker` A virtual order, an inactive account, an entry on an unreconciled trade date or for a blocked bot (a stop or a sale passes both, D118), an entry that is not a buy, an exit that is not a sell, an entry with the session not live, or an entry without GTT for delivery in both the stored capabilities and the adapter's own (a stop or a sale is never refused for it) is refused before the broker is called. Harm: an order reaches the broker that HAT's own rules forbid.
- `PBT stops.gtt_limit_offset` `gtt_limit_price` is the trigger times 0.99 rounded down to the tick, never above the trigger, never off the tick grid, and raises when the result is not positive. Harm: a stop with a limit above its trigger or off the tick grid is rejected by the exchange, or fills badly.
- `PBT broker.confirm_and_auto_need_gtt` `effective_mode` and `request_mode` allow confirm or auto only for an active account that reports `gtt_delivery`, and the Fyers adapter does not (the MVP is shadow only); a mode outside the three runs shadow and its request is refused as unknown (`UnknownModeError`), whatever the account. Harm: a live mode runs on an account that cannot hold a protective stop.
- `PBT broker.account_active_after_check` `onboard_account` marks an account active only when funds, positions and holdings were all fetched, whichever one fails. Harm: an account that cannot be read is treated as ready.
- `PBT broker.token_refused_is_session_expired` Over the real `FyersBroker` answering the -16 replies recorded on 2026-10-22, the adapter's session status is EXPIRED though the token file is in date, and every read fails with `broker_session_expired`; any other error reply or SDK failure is an outage: reads fail with `broker_unavailable` and the session stays LIVE; the profile is read once per token per 30 s window. Harm: a revoked or server-expired token reads as an outage, so nobody is asked to log in again while every read fails.
- `PBT broker.token_refused_on_a_read_ends_the_check` A -16 on any read inside the 30 s window of a passed profile check makes the next `session_status` EXPIRED at once (CodeAnt). Harm: the alert and login prompt wait up to 30 s while every read already fails.
- `PBT broker.one_session_owner` `SessionRegistry` hands out one owner per account, serialises that owner's calls and refuses a call after release; the adapter's session status never raises and maps (connected, client, store error) to live, expired, missing or unknown; `record_session` writes the morning login. Harm: two owners race for one session, or a broken token store reads as live.
- `PBT ops.session_backoff_alert` The reconnect delay is 1 s doubling to 30 s, and an outage raises exactly one alert after 2 minutes, none after a recovery until the next outage. Harm: a dead session retries in a tight loop or alerts every minute.
- `PBT ops.no_outage_before_the_login_window` An outage is counted from 08:30 IST: checks before it never alert and reset the watch, so a session down since midnight alerts once, 2 minutes after the first check from 08:30, only if it is still down. Harm: a `broker.session_lost` alert every night at 00:02 trains the owner to ignore it.
- `PBT router.broker_health_follows_the_session` After every session check of the desks, each bot of the account has `broker_health` tripped iff the session is not live (a failed read counts) between 09:05 and the 15:30 close IST; a tripped `feed_health` stays, and the account switch and a bot of another account are untouched (checks are at least a minute apart: a trip wins a same-instant tie by design, `kill._latest`). Harm: a missed login leaves the bots looking healthy while their exits are off, or a check clears another reason's trip.
- `CONF brk__interface_methods` The `HatBroker` protocol has exactly the 18 methods of `BROKER_METHODS` and the Fyers adapter implements each with the same parameters. Harm: a method is added to or dropped from the broker boundary without the spec.
- `CONF brk__import_boundary` Engine, risk, data, router and the interface module import nothing from `app.brokers`, `fyers_apiv3` or an OpenAlgo package, directly or transitively (and the checker is shown to catch a synthetic offender). Harm: strategy code starts to depend on one broker's SDK.
- `CONF brk__fyers_capabilities` The Fyers adapter reports tag length 20, a static IP requirement, CNC only and GTT for delivery (verified on the live account 2026-10-22). Harm: confirm and auto stay locked though GTT works, or unlock on an adapter that cannot place one.
- `PBT broker.the_fyers_adapter_unlocks_modes_on_an_active_account` With the adapter's own capabilities every mode runs as asked on an active account and only shadow on any other; with `gtt_delivery` false only shadow runs anywhere. Harm: a real entry on an inactive account, or one without an overnight stop.
- `CONF brk__recorded_writes` The write replies recorded live on 2026-10-22 (`tests/hat/fixtures/fyers_live_writes.json`) hold: the GTT payload the broker builds is the one Fyers accepted (`orderTag` included); place, modify and cancel replies read as placed and ok; GTT book rows in the live keys (`tran_side`, `product_type`, `ordertag` `1:GTT<tag>` or `2:GTTUntagged`) read as resting GTTs with their tag, cancelled ones left out; the two orders under one tag (MX-F6) both read as rejections; the fixture holds no account id. Harm: HAT reads a shape Fyers never sends, and a real GTT book is unreadable (as it was before).
- `CONF fyers_adapter_wraps_existing` The adapter reaches Fyers only through HAT's `FyersBroker`, `FyersSession` and `FyersAuthService`; the SDK's HTTP client `fyersModel` is imported only in `fyers_ipv4.py`, after its hook is installed, and session and auth take it from `fyers_ipv4.sdk` (the data socket's `FyersWebsocket` only in `data/provider/fyers_websocket.py`; GTT SDK calls only in `fyers_broker.py`), and nothing under `app/hat` imports `app.brokers`. Harm: a second, untested path to the broker, or an SDK client that reaches Fyers over IPv6.
- `PBT fyers_ipv4.fyers_http_leaves_over_ipv4` Through a real `requests.Session` over a scripted resolver and socket, a request to a Fyers host (`fyers.in` or a subdomain, any case, a trailing dot) opens AF_INET sockets only, to its IPv4 addresses in order until one connects, none when it has no IPv4 address; any other host connects as urllib3 does by itself. Harm: orders leave over IPv6 and Fyers refuses every write with -50 (not the whitelisted IP), as the live test of 2026-10-22 found.
- `PBT fyers_ipv4.is_fyers_host_exact` Exactly `fyers.in` and its subdomains are Fyers hosts (case and a trailing dot ignored); a look-alike is not. Harm: another host is forced onto IPv4, or a Fyers host escapes the hook.
- `PBT fyers_ipv4.no_proxy_carries_fyers` For every environment of proxy variables (either case, set or empty) and `NO_PROXY` values, whenever `require_direct_fyers` lets HAT start, neither real `requests` (`get_environ_proxies`, `select_proxy`) nor urllib (`getproxies_environment`, `proxy_bypass_environment`) uses a proxy for any Fyers host HAT reaches; when it refuses, `production_parts` and `sdk()` fail with `fyers_proxy_set: <variable>`, never the value. Harm: a proxy carries orders to Fyers over IPv6 past the pin, and Fyers refuses every write (-50).
- `CONF brk__account_constants_one_copy` No module but `models/strategy.py` defines an `ACCOUNT_*` status constant, and none compares an account's `status` or `broker` with a string (the checker is shown to catch a synthetic offender). Harm: a stale copy of a status survives a rename and a login, gate or view reads accounts by a value no row has.
- `CONF ops__single_fyers_secret_store` Fyers credential words and the token file appear only in `fyers_auth.py`, `fyers_tokens.py` and `fyers_session.py`, and no HAT table holds a broker secret. Harm: a second copy of the Fyers token.
- `CONF mvp_seed_scope` Only the Fyers adapter is registered and onboarding accepts no other broker. Harm: an unreviewed broker is onboarded.
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
- `PBT fyers_auth_routes.cross_site_write_refused` For every write header (absent, `1`, other values), `Origin` (absent, a frontend origin, a page on another site, `null`), `Sec-Fetch-Site` and with or without the app's origin rule wired, both login POSTs run only when the header is `1` and the request names a frontend origin, or names none and is not marked cross-site or same-site. A refusal is a 403 with `guard.WRITE_REFUSED`, writes or removes no token and runs no hook. Five `@example`s pin the harmful shapes. Harm: any page the owner's browser opens logs HAT out of Fyers mid-session or plants its own token.
- `PBT fyers_auth_routes.token_for_another_account_refused` For every set of broker accounts (broker, `account_ref`, status) and every user a token belongs to (None: the profile failed), `POST /token` saves the token exactly when no Fyers account is active or the user is an active one's `account_ref`; otherwise 403 with `ACCOUNT_UNCONFIRMED` (unknown user) or `WRONG_ACCOUNT`, no file, and neither token nor code in the body or log. With the accounts unreadable (`db_down`: the table is dropped) it answers 503 with `ACCOUNTS_UNREADABLE` and writes nothing. Harm: HAT trades or reconciles on another Fyers account. Since the check is the service's (`FyersAuthService(account_refs=...)`), and the rig hands it `fyers_account_refs` over the SQLite accounts.
- `PBT fyers_auth.accounts_read_in_order` Every exchange reads the accounts once, after Fyers has answered and the profile was read and just before the save (traced: exchange, profile, accounts, save); an account onboarded during the profile read counts, a rejected code reads none, a refused user saves nothing (`ACCOUNT_UNCONFIRMED` or `WRONG_ACCOUNT`). Harm: a login plants another account's token, or the read holds a connection while Fyers answers.
- `PBT fyers_auth.complete_login_checks_the_account` The adapter's `complete_login` saves the token exactly when no Fyers account is active or the user is one; another user is `wrong_account`, a user Fyers did not name `account_unconfirmed`, unreadable accounts `accounts_unreadable`, and none writes a file. Harm: a Telegram or job login saves a token for any Fyers user.
- `PBT fyers_routes.failed_exchange_fixed_message` Every failure of the Fyers side answers 400 or 500 with one of the fixed messages, the body and the log carry no code, secret or echoed text, and no token file exists. Harm: Fyers text or the auth code reaches the browser or the log.
- `PBT fyers_routes.missing_credentials_fixed` Without app credentials the link and the exchange answer 500 with the fixed message, never the variable text. Harm: configuration detail leaks to the client.
- `PBT fyers_routes.good_exchange_connected` After a good exchange `status` is connected with the user id, disconnect flips it back, and the token never appears in a response or log. Harm: the page shows the wrong login state or the token leaks.
- `PBT fyers_routes.unreadable_or_unsaved_fixed_error` An unreadable token path gives `status` the fixed error text, and a failed save the fixed store message; neither shows a path, the token or the code. Harm: a disk problem leaks paths or secrets.
- `PBT fyers_broker.breaker_model` The circuit breaker is open exactly from the threshold-th failure in a row until the recovery time later, one success closes it, and a second breaker never moves with the first. Harm: the breaker opens late, never closes, or one broker's outage blocks another.
- `PBT fyers_broker.nothing_sent_while_open` With the breaker open no order, GTT, modify or cancel reaches the SDK, and a place says so with a definite rejection. Harm: orders are hammered at a failing broker.
- `PBT fyers_broker.a_rejection_with_an_id_stays_rejected` An explicit error stays a rejection though it carries the id of the order Fyers created and refused (the recorded RMS refusal): REJECTED with no order id, and the adapter's `rejected` with no broker id. Harm: a refused order read as placed leaves the signal waiting on an order that never fills, or a refused stop read as resting leaves the position unprotected.
- `PBT broker.rate_limit_is_its_own_code` Fyers' -360 (the plan's daily requests spent) makes every read fail closed with `broker_rate_limited` and every write a refusal with that code (which `send_order` treats as not sent), leaves the session LIVE and carries no broker text. Harm: a spent quota reads as an outage or a lost login, or a refused write as unknown.
- `PBT fyers_broker.only_explicit_error_rejects` A place is SUBMITTED only with ok and an id, REJECTED only on an explicit error, and SUBMIT_UNCONFIRMED for a lost, garbled, id-less or odd reply. Harm: an order that landed is read as rejected and placed twice.
- `PBT fyers_broker.bad_price_never_sent` A limit or trigger price that is NaN, infinite, zero, negative or not a number is a definite rejection with no SDK call, and a good price is sent exactly. Harm: a non-finite price passes a positivity check and reaches the exchange.
- `PBT fyers_broker.bad_gtt_price_never_sent` A GTT place or modify with a non-finite or non-positive price is refused before the SDK. Harm: a stop is placed with a nonsense price.
- `PBT fyers_broker.an_ok_reply_without_its_list_is_unreadable` An ok reply to the order, trade, position, holding or GTT book read whose list key is missing or not a list raises `FyersUnavailableError`, never an empty list. Harm: a book read as empty re-sends an order or a stop that rests at the broker.
- `PBT fyers_broker.a_circuit_opening_mid_place_is_not_sent` A place or GTT place whose breaker opens between the caller's check and the guard is a definite rejection (`BROKER_CIRCUIT_OPEN`) with no SDK call, never a lost answer. Harm: an order that never left is chased as unconfirmed and its position frozen.
- `PBT fyers_broker.breakers_independent` Brokers built without a breaker get their own; two given the same breaker share it. Harm: a failure recorded through one class shadows another's (the legacy class-level state).
- `CONF ops.one_login_one_token_store` The login service and the session default use HAT's one `token_store`; the legacy client and symbol master subclass HAT's; the legacy token-store re-export, the `brokers` package and the login router are gone; the HAT router has exactly the four login routes. Harm: a second login or token copy drifts from the first.
- `TLA TradeLifecycle!OnlyAllowedTransitions` Every step of every trade is a row of the transition table of `lifecycle.TRANSITIONS` (a trade is never Open without an entry fill, never Closed without ExitPending). Harm: a trade reaches an impossible state and is mismanaged.
- `TLA TradeLifecycle!EndingsTerminal` No step follows Closed, Rejected, Expired or Cancelled. Harm: a closed or rejected trade re-opens and holds risk budget.
- `TLA TradeLifecycle!EventuallyEnds` Under weak fairness a Watchlisted trade reaches Open or an ending, and a trade whose full exit fired reaches Closed. Harm: pending signals linger and an exit that fired leaves a position open.
- `TLA TradeLifecycle!StateMatchesLog` The stored state is always the fold of the trade's logged steps over the table (every transition appends its event in the same transaction). Harm: a state that cannot be rebuilt from the log hides a missed event.
- `TLA TradeLifecycle!ShadowSameMachine` A virtual trade is entered and closed through the same steps as a real one (order placed, ExitPending, closed). Harm: shadow results come from a different machine than live ones and prove nothing about live.
- `TLA TradeLifecycle!ResultOnClose` A Closed trade always has its trade result (written in the closing transaction). Harm: a closed trade without a result is missing from R, expectancy and the daily-loss and stop-out counters.
- `TLA TradeLifecycle!VersionPinned` The exit rules a trade reads are those of the strategy version it entered under, even after a promotion. Harm: a promotion silently rewrites the exits of positions already held.
- `TLA TradeLifecycle!PerBotCapHolds` With the bot's lock held between the risk decision and the insert, a bot never holds more position slots than its cap. Harm: two entries of one bot evaluated at once both take the last slot.
- `PBT exits.replay_reproduces_state` After every step of a shadow trade the state stored on the signal and position equals the fold of its events over the table. Harm: a state that cannot be rebuilt from the log hides a missed event.
- `CONF journal_only_writers` No module of `app/hat` other than `engine/journal.py` assigns `.state` on a signal or a position (an attribute, `setattr(x, "state", ...)` or an UPDATE's `.values(state=...)` on HatSignal or HatPosition), and the scan sees the journal's own writes. Harm: a writer that skips the table check leaves the stored state off the fold of the log.
- `PBT journal.validates_before_writing` For every sequence of lifecycle events recorded through `journal.record` (or `approval_sent`) as engine decisions or broker facts, an allowed step moves the signal (and its position) and writes one event, a forbidden decision raises before writing and leaves the state, a forbidden broker fact writes one event marked `no_step` and leaves the state, and after every step the stored state is the fold of the log. Harm: a refused step leaves an event the state never took and the log no longer folds.
- `PBT journal.check_names_mismatches` `journal.mismatches` names exactly the trades not yet ended, each as `trade` or `veto`, whose stored state differs from the fold of their events (an active signal, its own position, a signal stored as ended over its own position not yet Closed, an open veto trade) or whose events do not fold (`unfoldable`, the check going on). Harm: a drifted trade is acted on all night with nobody told, or one bad log stops the check.
- `PBT journal.check_alerts` The nightly `journal.check` alerts URGENT `journal.mismatch` once per drifted trade, at most `JOURNAL_ALERTS` (10) a night, each naming the signal, which of its trades (`trade` or `veto`), the stored and replayed states and the night's count in plain codes, records the count as its rows and repairs nothing. Harm: the owner never hears of a drift, or one bad night floods the chat.
- `PBT exits.shadow_rows_virtual` A shadow trade driven from signal to close writes only virtual orders, fills and positions with no broker ids and no fees, the broker records zero calls, the quantities add up and a closed position has one result whose R is its P&L over the initial risk. Harm: a shadow row that looks real can be reconciled against, and sold at, the broker.
- `PBT trades.retry_returns_existing_signal` A repeated trigger for the same (bot, playbook, instrument, day) returns the signal it already made and writes nothing, even after the bot left shadow mode or the instrument left the universe. Harm: a retried job crashes instead of finding the trade it already took.
- `PBT trades.shared_bot_cap` However entries are spread over a bot's playbooks the accepted ones never exceed the bot's position cap, and one is rejected exactly when the cap is full (the cap is the bot's, not the playbook's). Harm: each playbook gets its own cap and the bot holds a multiple of what the policy allows.
- `PBT config.trade_keeps_entry_version` After the bot moves to a newer strategy version an open trade still evaluates the exit rules of the version it entered under, and a trade opened after the move uses the new ones. Harm: a promotion silently rewrites the exits of positions already held.
- `DB runtime.publish_serialised` Two real nightly publishes of one bot and day, the first holding inside its scan after its "already published?" read, write one `watchlist.published`; the second waits on the bot's lock and skips. Harm: duplicate watchlist rows and events.
- `DB runtime.per_bot_serialised` Two real transactions of one bot, the first holding between its risk decision and its insert, end with one accepted entry and one rejected for the cap; the second waits on the bot's advisory lock. Harm: the cap is read before the other entry is visible and exceeded.
- `PBT trades.global_open_risk_across_bots` Two bots of one owner on different broker accounts, triggers interleaved in any order through `open_entry`: a trigger is rejected with `global_open_risk` exactly when the open risk of both bots plus its own exceeds 1.5% of total equity, is accepted otherwise, and the owner never holds more. Harm: each bot reads only its own positions, so N bots hold N x 1.5%.
- `PBT trades.last_hard_stop` Positions opened through `open_entry` and closed by the exit stack's hard stop or by another reason, on the bot or another bot of the owner, in the instrument or another, on later days: the cooldown's history is the trade date of the bot's latest `hard_stop` close of the instrument at or before the trigger, and nothing else. Harm: the cooldown bars the wrong name, misses the stop that should bar it, or reads a stop the bot has not had yet.
- `PBT trades.day_counts` Positions opened through `open_entry` and closed by the exit stack's hard stop or by another reason, on the bot or another bot of the owner, on later days: `snapshot.day_counts` reads the bot's entered signals of the trade date and its `hard_stop` closes of any instrument closed on that date, nothing of another reason, day or bot. Harm: the stop-out cap bars a bot for a stop it did not have, or misses the stops that should bar it.
- `DB runtime.owner_serialised` Two real transactions of different bots of one owner (on different broker accounts), the first holding between its risk decision and its insert, with global open-risk room for one entry, end with one accepted and one rejected; the second waits on the owner's advisory lock. Harm: two bots' triggers in one instant both fit on one snapshot and together exceed the cap.
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
- `TLA ExecutionRouter!EntriesSettled` An auto entry in Triggered or a passed Take waiting for its placement (`taken`) is eventually placed or ended, even when a crash fell between its commit and its placement (liveness; `Crash` loses the in-process placement, the restart's resume is `Place`/`PlaceTaken`). Harm: a stranded signal is never bought and holds its slot.
- `TLA ApprovalToken!NoTakeAfterExpiry` No card is taken at or after its expiry. Harm: an approval opens a position at a price the user never saw.
- `TLA ApprovalToken!ChaseRecheckOnTake` No Take places an order at a price past the max-chase level. Harm: the user approves an entry that already ran away.
- `TLA ApprovalToken!SingleDecision` A card is decided at most once. Harm: a double tap places two orders.
- `TLA ApprovalToken!CardsDecided` Every sent card is eventually decided (liveness). Harm: an undecided card waits forever.
- `PBT router.routes_by_mode` For every mode and GTT capability, one accepted trigger through the real router on `FakeBroker` gives a virtual position and no call (shadow), a 5-minute card and no call (confirm), or one HT-tagged LIMIT BUY CNC at the entry price for the risk-sized quantity after the session's reconciliation (auto), when the live conditions passed to `route` (read at placement, with the dated live price since none, stale, out of band or at or below the stop refuses) allow it and otherwise (auto) no order and the signal Rejected with the reason; confirm and auto without GTT run shadow; a routed signal without a position holds a slot valued at its planned entry price; the outcome `route` returns carries the signal's state after placement and the shadow position's id. Harm: a route places an order its mode never allows, a pending entry's open risk is understated, or the caller acts on a stale outcome.
- `PBT router.entry_needs_live_session` An accepted auto entry is placed only when its broker account has a live `hat_broker_session` for the placement's trade date (written by the runtime's session watch); a missing, expired or unknown session, or a live one of another day, sends nothing and ends the signal Rejected with `broker_session_not_live`. Harm: an entry goes out on an account nobody logged in today.
- `PBT router.refused_entry_is_not_sent` An auto entry whose bot became manage-only (account, bot, manual or health kill, or a pause) after the signal committed sends nothing: an unsent reserved order is cancelled and the signal ends Rejected with the kill or pause reason; an order that may be at the broker (unconfirmed) is left to reconciliation, neither placed again nor rejected. Harm: a bot buys after its kill switch, or a refusal re-sends an order the broker may hold.
- `PBT router.take_rechecks_max_chase` A Take before expiry places exactly when the live conditions allow an entry (the dated live price included) and the price is within max-chase; otherwise it records the decision, ends Rejected with the reason and places nothing; a Take at or after expiry expires the card into its veto trade; a second Take does nothing. Harm: a Take buys late, blind or past the plan's price.
- `PBT router.skip_and_expiry_become_one_veto_trade` Any mix of Skips, sweeps and late Takes ends a card Cancelled or Expired with exactly one virtual veto trade at the card's quantity and stop, no broker call, replays that match, no slot held and a stop-out that is not the bot's. Harm: a skip is scored twice or never, or blocks the bot's next entries.
- `PBT router.rejected_entry_is_retried_once` A rejected auto entry is alerted and retried once under the next tag; a second rejection ends Rejected with one `entry.not_placed` alert and no third order. Harm: a retry storm or a silent failure.
- `PBT router.reconcile_before_first_order` The first real entry of a session reconciles first; an unreadable broker or an unknown HT order places nothing and ends the entry Rejected with an alert. Harm: an order on top of an unmatched book.
- `PBT router.mode_gates_hold_bots_in_shadow` A Live bot on an active account with GTT for delivery runs the mode it asked for only while DDPI was confirmed on its account by the session's open (one confirmed during the session waits for the next open; one revoked holds the bot back at once) and the policy in force at the open confirms the base weights; otherwise it runs shadow and calls no broker. Harm: real orders before DDPI is registered (sales stall on a TPIN prompt) or before the owner set the allocation numbers.
- `PBT router.auto_places_only_high_confidence` An accepted auto signal labelled high (by its real evidence: the rig seeds a run, a bucket and a watchlist score, or none) sends exactly one entry order; any other label, or none, goes to the owner as in confirm: a card, the signal AwaitingApproval and no broker call. Harm: a medium- or low-confidence trade is bought without the owner's approval.
- `PBT router.placement_rechecks_confidence` Placement reads the stored label again: an auto signal stored without a high label is not sent and ends Rejected with `not_high_confidence`. Harm: the auto rule rests on one write, and a medium- or low-confidence trade is bought without the owner's approval.
- `PBT router.open_settles_mode_changes` A change queued in the evening while every gate passed is settled at the next open by the gates as they stand then: applied (the bot row takes the mode, one `mode.applied`) or refused with the gate's code (the bot keeps its mode, one `mode.apply_refused`); a second run at the same open changes nothing. Harm: a change silently fails, or a refused change takes effect later without a request.
- `PBT router.mode_change_takes_effect_next_session` A change queued outside hours routes nothing differently before the next open (the evening and the next pre-open included) and everything after; one asked in hours is refused and writes nothing; the open applies it to the bot row once. Harm: a bot switches to real orders mid-session.
- `PBT manage.real_fill_protected` Each entry fill the sync reads (one or several partial fills, at any time of the session or after the close) is followed, before any other broker write, by the position's stop: placed for the open quantity when none rests (an intraday SL-M SELL before 15:25, a GTT from 15:25 on), resized when one does, its trigger on the tick and never above the stop; a stop the broker refuses twice is alerted and placed by the next sync; a resize it refuses is alerted and sent again by the next sync. Harm: a filled position sits at the broker with no stop, or a stop for fewer shares than it holds.
- `PBT manage.broker_stop_only_tightens` As the position's stop rises (any steps, refused modifies among them), its intraday or GTT broker stop is modified up to it rounded down to the tick, never above it and never down; a refused modify leaves it and is alerted. Harm: a loosened broker stop, or one above the price HAT holds, sells on noise.
- `PBT manage.swap_leaves_no_gap` Whatever the broker answers to the GTT (and its retry), `gtt.swap` cancels the intraday stop only after a confirmed GTT; otherwise the intraday stop keeps working, `gtt.swap_failed` is raised and the job fails to be retried; a day stop the broker will not cancel fails the swap too. Harm: the day stop is cancelled first and the position spends the night unprotected.
- `PBT manage.unfilled_entry_cancelled` Through `EntryWatch` (final bars fed as they end), the real `StopDesk` and the trade manager on `FakeBroker`, a real entry nothing of which filled is asked to be cancelled exactly once, at the first check where a final bar after the trigger bar closed below the level or `HAT_ENTRY_TIMEOUT_MINUTES` passed since placement; the fill sync then ends the signal Cancelled with no position; the trigger bar's own close never counts; the timeout runs from the working order, not an earlier attempt the broker rejected; once anything filled, or the order alone shows a part fill (the book read before the fill sync), `due` names no entry and `cancel_unfilled_entry` refuses it, whoever asks. Harm: a failed break's LIMIT rests all day and fills as the price falls, or a part-filled entry's remainder is cut short.
- `PBT manage.entry_remainder_tracked` An entry fills in chunks, interleaved with its day stop filling and a flatten, with the broker taking or refusing the cancel of the entry's rest. Once a sale empties the position the rest of the entry is asked to be cancelled (once); a chunk that fills first adds to the still-open position and gets a stop; after every sync HAT's open quantity equals the shares in the broker's trade book and its live stops cover exactly those, and the position closes only with no share held and no entry working at the broker. Harm: shares the broker bought after HAT closed the position sit untracked and unprotected.
- `PBT manage.verify_covers` The nightly verify refreshes a GTT the broker confirms to the current stop (`gtt.refreshed`), never below the trigger it rests at when the instrument's tick changed since (a coarser tick floors the stop lower; audit F24), and a refused refresh of a GTT so kept is alerted `gtt.unverified` with nothing placed; one it does not confirm is alerted (`gtt.missing`), cancelled at the broker and in HAT's book, and placed again; when the cancel fails, the broker is unreachable, the fills cannot be read or HAT's GTT row was never confirmed, `gtt.unverified` is raised and no GTT is modified or placed. Harm: a GTT that triggered or expired goes unnoticed and the position opens the next day without a stop.
- `PBT manage.verify_renews_old_gtt` For every GTT age around 300 days and cancel reply, the nightly verify cancels a GTT placed 300 days ago or more and places it again at the current stop (`gtt.missing` never raised), and refreshes in place a younger one or one whose cancel is refused. Harm: the broker expires the GTT a year after placement and the position has no stop.
- `PBT manage.ex_date_gtt_replaced` For every stored action (dividend, notice, split) with ex-date around the check day, every assumed book shape of a broker-cancelled GTT (`fixtures/fyers_gtt_ex_date_assumed.json`) and a GTT accepted or refused, `check_ex_date_gtts` ends a GTT the book no longer holds (`gtt.missing`, `gtt_cancelled_by_broker`) and places it again (a refusal does not fail the run: the intraday stop goes out from 09:15), leaves a resting one alone, fails with `gtt.unverified` and sends nothing on an unreadable book, waits for a split's rescale, and reads no position without an action today or the next session. Harm: Fyers cancels the GTT before a dividend and the position opens the ex-date unprotected.
- `PBT manage.ex_date_check_serialised` For both assumed shapes of a cancelled GTT, the fill sync's protect or the nightly verify reaching the position on another thread between the check ending the old GTT and placing the new waits on the position's stop lock: exactly one GTT is placed. Harm: two GTT sales over the same shares.
- `PBT manage.series_change_remapped` For every successor (none, BE mapped or unmapped, renamed, another ISIN), a series change gives no exit view, one URGENT `position.series_changed` at the morning check and at the nightly verify, and a re-placed GTT under the successor's Fyers symbol: one the broker cancelled, and one still listed under the old symbol, cancelled first (at the morning check, or at the nightly verify when the check is off; a refused cancel at the check fails the run with `gtt.unverified`, at the verify it is refreshed); a GTT placed after the change is left resting; an unmapped successor gets no stop sent (`series_unmapped`) and its old GTT left; otherwise nothing. Harm: every order under the old `-EQ` symbol is refused and exit rules run on stopped prices.
- `PBT mpp.resting_market_sale_resent` For every assumed order-book shape of a protected market sale (`fixtures/fyers_mpp_sale_assumed.json`), number of misses (1 to 4), part fills and `resend` on or off, the fill sync records the book's price on the sale's row; with `resend` on a market sale still resting a minute after its send (not before) is cancelled with `order.cancelled` (code `sale_resend`, the book's type and price), and once the book shows it ended one fresh market sale of what is open goes out under the next tag; no miss records `order.rejected`, raises `exit.not_placed` or counts in `rejected_sales`; URGENT `sale.unfilled` (`sale_resting`) is raised once, at the `page_after`th miss; with `resend` off nothing is cancelled. Harm: a stop-loss sale sits below a stale protection band all session, or a re-send sells the shares twice.
- `PBT mpp.lower_circuit_lock_holds_the_sale` For every run of lower-circuit lock readings and book shape, a resting market sale is not cancelled while the band reads locked, `sale.unfilled` (`lower_circuit_locked`) is raised once, and it is cancelled at the first reading that is not a lock. Harm: a locked sale loses its place in the queue every minute, or the owner is not told.
- `PBT mpp.rejected_twice_goes_at_the_band` For a sale refused twice (at once, or by the broker after it took it), with the lower band known or not, and the band sale accepted or refused, exactly one LIMIT SELL at the band for the open quantity follows when the band is known, with one `sale.unfilled` (`band_limit`); after it nothing more goes that session; without a band nothing follows. Harm: a stop-loss sale refused twice leaves the position unsold all session.
- `PBT mpp.gtt_market_sale_asked_again` For every number of syncs and lock readings, a triggered GTT's sale the book already shows at the market but still resting is asked `{order_type: MARKET}` again at each sync it is not locked when `resend` is on (paged once at the `page_after`th ask the broker accepts; a refused ask is not counted, a lock alerted once), and not at all when off. Harm: the triggered stop's sale sits below a stale protection band all session.
- `PBT mpp.no_cancel_without_a_resend` For every sync time from 15:28 to 15:30:30, a resting market sale is cancelled only when its re-send a minute later still falls before the 15:30 order cutoff. Harm: a sale cancelled at 15:29:30 cannot be sent again and the exit is lost for the session.
- `PBT mpp.resend_flag_parsed` `HAT_MPP_RESEND` is on exactly for a true value, and `HAT_MPP_PAGE_AFTER` is a positive whole number, else 3. Harm: the re-send of an unconfirmed book shape is on by default, or a typo never pages.
- `PBT fyers_broker.budget_windows_hold` For every per-second, per-minute and per-day limit and every sequence of reads, writes, 429 pauses and day changes on a fake clock, `CallBudget.take` grants no more calls than each limit in any window, grants a read only within `READ_SHARE` of each window (at least one call) and of the day (no minimum: a day cap of 1 is a write's), grants nothing inside a pause (at most `MAX_PAUSE`), refuses only for one of those reasons, and grants a refused call asked again after the wait it was told (none once the day is spent). Harm: a burst of syncs and the 15:25 swap exceed Fyers' limits and every write, stops included, is refused.
- `PBT fyers_broker.throttled_call_not_sent` For every adapter operation (an SL-M stop, GTT place, modify and cancel, the order and GTT books), a call the budget cannot grant is not sent, and one Fyers answers HTTP 429 (JSON body or not, any `Retry-After`) is sent once: a read fails closed and a write is refused with `broker_rate_limited`, the breaker counts nothing, and a 429 pauses the budget for its Retry-After (`THROTTLE_PAUSE` when absent or unreadable). Harm: a throttled stop reads as a rejection and burns its tag's sequence, or a busy broker opens the circuit.
- `PBT fyers_broker.writes_go_ahead_of_reads` For every adapter operation, once reads have spent their share of the minute a write is still sent and a read is refused unsent; and while a write waits for a call (`acquire`), a read is not granted the call the window frees. Harm: the fill sync's reads take the calls a stop placement waits for.
- `PBT fyers_broker.limits_from_settings` For every value of `HAT_BROKER_CALLS_PER_SECOND`, `_PER_MINUTE` and `_PER_DAY`, `limits_from` takes a positive whole number as given, the default (8, 180, 5,000) for anything unset, unreadable or not positive, and `0` or `off` lifts the day cap only. Harm: Fyers Prime's limits need a code change, or a typo removes the limit.
- `PBT fyers_broker.late_429_still_pauses` A write Fyers answers HTTP 429 only after the 1.5 s guard gave up is `SUBMIT_UNCONFIRMED` with one breaker failure (a timeout), and the worker still pauses the budget for the 429's Retry-After when it lands. Harm: a slow 429 is lost and HAT keeps calling into the limit.
- `PBT fyers_broker.breaker_opened_while_waiting_sends_nothing` For every adapter operation, a call whose budget wait ended with the circuit open (another call opened it) sends nothing: a read fails closed, a write is not accepted. Harm: calls queued behind the budget reach a broker declared down.
- `PBT manage.real_exits_never_blocked` A real position's live exits come from the shared exit stack and go out as MARKET SELL CNC orders, identical with and without an account or bot kill or a pause, and whatever the reconciliation state (none today, only yesterday's, a failed run, a mismatch; the new fill's stop is placed in each, D118); HAT sends nothing at the hard stop while the broker stop rests, and every sale is preceded by the broker stop's cancel or its resize to the shares left. Harm: a kill switch traps a position its exits should sell, or HAT and the broker both sell at the stop.
- `PBT manage.no_sale_after_close` A run that starts at 15:2x and reaches a sale some minutes later sends it only before 15:30 (the time sent is the run's start plus the monotonic time taken, with no clock injected); otherwise `exit.not_placed` is raised and the broker stop keeps resting. Harm: a slow close run sends market orders after the close.
- `PBT manage.entry_ended_by_broker` An entry the broker cancels, expires at the close or rejects after taking it, with nothing filled, ends its signal (Cancelled, or Rejected with `order.rejected` final); one whose send answer was lost and that is first found already ended ends Rejected (`entry_not_filled`) when the sync adopts it; replay agrees, no position opens and no stop is placed. Harm: a dead entry keeps its signal in EntryPlaced and holds one of the bot's slots.
- `PBT manage.gtt_trigger_handled` A GTT whose child sale (untagged or carrying the GTT's tag) fills only in part reduces the position and is neither resized nor doubled while its sale may still sell the rest; once that sale ends short, `gtt.unfilled` is raised once and the shares left get a new stop for exactly their quantity; another untagged sale on the symbol that ended (not at the GTT's quantity) changes nothing. Harm: a second stop under a working sale sells the shares twice, the rest stays unprotected, or an unrelated cancelled sale ends the GTT while it still rests.
- `PBT manage.flatten_capped_at_holdings` For one or two positions of a symbol, with an action the rescale cannot handle, a bonus (an action it handles; the property runs no rescale) or none, and a holdings book of any size or unreadable, `/flatten` sells min(open quantity, shares held) in all when an unhandled action spans the positions and the book reads, else the open quantity of each. Harm: an oversell the broker refuses leaves the position unsold, or a sale short of what HAT holds.
- `PBT manage.gtt_resting_marketed` A GTT that triggered with its limit sale resting below the limit (the order book shows its CNC SELL at the GTT's limit price and quantity, part filled or none: untagged, or carrying the GTT's tag bare or `GTT`-prefixed, when it is attributed by the tag whatever its price) is left a minute to fill, then recorded `gtt.triggered` and alerted `gtt.unfilled` (code `gtt_triggered_resting`) once; an untagged sale at another price, and one under another HAT tag, is never touched; every sync asks for exactly `{order_type: MARKET}` on that order until the book shows it MARKET; a refused change is alerted once (`gtt_sale_not_marketed`) and asked again; a flatten and a raised stop send nothing while the sale works, once it fills the position closes with no stop left, and the next trade date (the sale gone from the book unseen) a flatten is sent again. Harm: the stop triggered and the shares sit unsold below the limit all session, or a second sale or a stop move goes over the working sale.
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
- `PBT tags.gtt_payloads` The GTT place, modify and cancel payloads are exact, CNC and single-leg, the place carries the stop's HT tag as `orderTag`, and a GTT without a stop_gtt HT tag is refused before any SDK call. Harm: a stop is sent with fields HAT did not decide, or a lost GTT answer cannot be found by its tag.
- `PBT tags.a_lost_gtt_is_found_by_its_tag` The GTT a lost stop_gtt answer settles to is one no other row holds on the symbol: the one under the row's own tag (any quantity or trigger), else one shown untagged of exactly the row's side, quantity and trigger; never one under another HAT GTT's tag or a foreign tag (`BrokerGtt.foreign`). Harm: another position's stop, or a GTT HAT did not place, is adopted and later cancelled or moved, leaving that position unprotected.
- `PBT tags.the_gtt_book_reads_only_resting_stops` `gtts()` lists exactly the single-leg CNC GTTs resting or triggered (`ord_status` 2, 4 or 6, or none) from the flat book fields (the live keys `tran_side`, `product_type`, `ordertag`), `orderInfo.leg1` or `leg1`, each with its HAT tag read from `ordertag` (`N:GTT<tag>`; `GTTUntagged` or a foreign tag is no tag); a cancelled, rejected or expired GTT, an OCO or another product (whatever its status) is left out, and a single-leg CNC row with an unknown status code raises. Harm: a lost GTT is matched to one that no longer rests, or one still resting is read as gone.
- `PBT tags.a_keyless_book_never_reads_as_absent` An unconfirmed order whose order-book read answers ok without its list is not sent again (the lookup raises `BrokerUnavailableError`). Harm: a keyless reply reads as "not found" and the order is placed twice.
- `PBT router.non_live_sleeve_routes_as_shadow` For every lifecycle state, queued mode and GTT capability, `bot_mode` answers shadow unless the state routes live, and the router places no real order for a non-Live sleeve. Harm: a Probation or Retired sleeve keeps trading real money.
- `CONF exec__default_mode_confirm` A bot asks for confirm unless the owner changes it and runs shadow until every gate passes: the bot row defaults to confirm (model and database), `create_bot` refuses another mode, the entry path routes shadow unless told otherwise, an unknown mode reads as shadow, and confirm with any gate missing reads as shadow. The seed YAMLs and the strategy config default still say shadow; the bot's mode never reads them. Harm: a new bot trades real money before its gates pass.
- `PBT router.routine_reconcile_keeps_gate` A routine reconciliation (the periodic pass) of an account already reconciled for the trade date leaves the entry gate open while it reads the book and after it, readable or not. Harm: entries are refused while a routine pass runs, or wait on a pass that already succeeded.
- `PBT router.older_read_never_overrides_newer` When a reconciliation that read the book earlier finishes after one that read it later (`ExecutionGuard.begin_read` tickets), the later read's result stays in force. Harm: the router's run and the periodic run race, and an older clean book clears a newer mismatch so entries open over an unknown HT order.
- `PBT router.entry_gate_keyed_by_trade_date` Over any sequence of reconciliations that read the book or cannot, with the clock moving on 0..2 trade dates between them, `ExecutionGuard` accepts an entry exactly on a trade date that has had a success, and never refuses a stop or a sale (D118, MX-F5). Harm: yesterday's success opens today's entries over an unmatched book, or an unreadable book keeps a stop or a sale from the broker.
- `PBT router.failed_first_reconcile_alerts_once_per_date` Through `Desks`, a reconciliation that fails before the trade date's first success raises exactly one `reconcile.failed` health alert for that date, and a failure after the success raises none. Harm: the owner never learns the account cannot open entries, or is flooded every five minutes.
- `PBT tags.unreadable_foreign_rows_skipped` An order row without an HT tag that the adapter cannot read (a missing key, an unreadable side or quantity, not a mapping) is skipped and counted (`OrderBook.skipped`, `ReconcileReport.skipped_rows`) and the rest of the book is read, with one `reconcile.unreadable_row` alert per trade date; an HT-tagged row that cannot be read fails the read and the reconciliation. Harm: one manual order fails every reconciliation of the day, or an HT order HAT cannot read is missed.
- `PBT tags.held_quantity_nets_today` Through the adapter, the quantity reconciled per symbol is the holding plus today's signed net CNC position, an intraday position not counted; `@example`s for a buy today, a buy and sale today, a holding sold in full and in part (MX-F8). Harm: a holding a GTT sold today reads as held (fail open), or a correct HAT book blocks the account.
- `PBT router.resumed_entry_waits_for_feed` An entry left Triggered by the previous process is placed after a restart only once the feed has heard a fresh tick and the account is reconciled, at most once, and never on a later trade date than its own (`signal_expired`); the restart runs the fill sync whether or not its reconciliation succeeded (D118). Harm: an entry goes out on a stale price, twice, or a day late.
- `PBT router.crash_resumes_entry_once` For an auto entry or a Take, every reply sequence, a process that dies (`Crash`, a `BaseException`) or a placement that raises (`PlacementError`) right after its k-th commit or broker call (k 1-12, `hat_router_support.crash_after`), then a restart's recovery and two resume passes (or the next resume in the same process): at most two entry orders under distinct tags, at most one the broker took, the signal not left waiting with no order that may have reached the broker, and its stored state equal to its events' replay; when the first resume pass itself raises after its `again`-th step (0-3), a later pass places it (`Desks._resume_one` gives it back to the router, `remember_stranded`). `@example`s: auto after the second rejection (k=9), a Take right after its decision, auto right after the trigger's commit. Harm: a Take committed before a crash is never bought and holds its slot, or a restart sends a third entry or buys twice.
- `PBT router.sweep_idempotent` Over any interleaving of sweeps, Takes, Skips, placements and Takes whose placement crashed, on two cards: a sweep repeated at the same time leaves an identical snapshot (signals, approvals, veto trades, orders, event count), each signal has at most one entry order, an undecided card is never placed, every card is decided after its 5 minutes (a taken one is placed or still awaits today's resume), and the next trade date's sweep leaves nothing waiting. Harm: two sweeps open two veto trades or record an expiry twice, or an undecided card is bought.
- `PBT router.stranded_entry_ends_next_day` An auto entry or a Take whose process died after its k-th step (k 1-6) and did not come back that day: on the next trade date the sweep and the restart, in any order and number, place nothing more and leave it not waiting with no order that may have reached the broker (an order reserved and never sent does not count); one ended is Rejected `signal_expired`; a further sweep changes nothing. Harm: yesterday's price, size and risk decision are bought today, or a stranded signal holds its slot for good.
- `PBT router.sweep_survives_a_bad_signal` Two earlier-day stranded auto entries, ending one of them raising (`trades.reject_signal` patched to fail for it): the sweep still ends the other Rejected `signal_expired`, leaves the failing one in Triggered with no order, and the next sweep ends it. Harm: one bad row stops the sweep every minute and every stranded signal after it holds its slot.
- `Postgres test_db_crash_safe_routing.py::test_db_concurrent_placements_send_one` An auto entry or a Take left unplaced, placed by 2-6 threads at once through the account's one router, each on its own connection: exactly one entry order reaches the broker and the signal ends EntryPlaced. Harm: two placements that both read no open tag reserve two tags and buy twice.
- `Postgres test_db_crash_safe_routing.py::test_db_crash_resumes_entry_once` `router.crash_resumes_entry_once` on PostgreSQL: the real router, desks and recovery commit to a real database and a new process restarts on it. Harm: the in-memory SQLite run hides a commit or lock the database orders differently.
- `Postgres test_db_crash_safe_routing.py::test_db_concurrent_sweeps_settle_once` 2-6 sweeps at once, each on its own connection, over two cards past their 5 minutes (one, when drawn, a Take whose placement crashed), then again, then twice on the next trade date: each untaken card Expired with exactly one expiry event and one veto trade, the stranded Take Rejected `signal_expired` with no order, no order sent, and a repeated round leaves the same snapshot. Harm: overlapping sweeps (the periodic job and a Telegram tap's sweep) open two veto trades or record an expiry twice.
- `PBT router.fill_sync_cadence` `Desks.sync_due` reads an account's fills at every periodic step while `fills.watch_closely` holds (a real entry of the day's signals may still fill, or a stop of an open real position is pending or unconfirmed), and otherwise at the first pass and then once a minute; an entry of an earlier session still shown working and a stop the broker refused do not speed it up, and a sync whose read failed (an outage, or the call budget refusing it, `FyersRateLimitedError`) is tried again at the next step. The test reads the expected state row by row (`_watched`). Harm: an entry filled just after a sync sits up to a minute at the broker with no stop, or a refused stop is re-sent every 5 s until its tag's sequence runs out.
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
verification/tla/run.sh --broken ExecutionRouter      # 24 variants, at least one per property
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
CI=1 python -m pytest tests/hat/properties/test_journal.py -q --no-cov   # the trade journal and its nightly check
CI=1 python -m pytest tests/hat/properties/test_router.py -q --no-cov   # the router on FakeBroker
CI=1 python -m pytest tests/hat/properties/test_manage.py -q --no-cov   # fills, broker stops, exits
CI=1 python -m pytest tests/hat/properties/test_mpp.py -q --no-cov      # sales under market price protection
# the DB checks need a throwaway Postgres whose database name ends in _test
SMARTTRADER_PG_TESTS=1 DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test \
  python -m pytest tests/hat/db -q --no-cov -m postgres -k "per_bot_serialised or owner_serialised or publish_serialised or state_domain"
SMARTTRADER_PG_TESTS=1 DB_HOST=localhost DB_PORT=5432 DB_USER=postgres DB_NAME=smarttrader_test \
  python -m pytest tests/hat/db/test_db_crash_safe_routing.py -q --no-cov -m postgres   #
```

All adapter checks run on `FakeBroker` or a recording stand-in for the Fyers SDK. No check calls Fyers, and none needs a credential.

## Bug or model error

When a check fails, decide which side is wrong before touching anything.

- TLC prints a counterexample as numbered states. Each action names its function in the `\* code:` comment above it in `OrderReconciliation.tla`. Replay the steps against the real functions (`reserve_order`, `send_order`, `ReconciliationService.run`, `onboard_account`, `SessionRegistry.acquire`). If the code can take them, it is a bug: fix it or record a finding. If it cannot (a guard the model omits), it is a model error: add the guard and say so under "Scope changes". An earlier draft of this model showed an entry placed after a reconcile blocked the bot between stamp and call; the real `send_order` now reads the guard again just before the call, and the model's `PlaceCall` guards `reconciled` and `~blocked`.
- A failing property test prints a falsifying example; a state-machine failure lists the steps. Re-run them by hand against `FakeBroker`.
- `ExecutionRouter` and `ApprovalToken`: replay a counterexample against `ExecutionRouter.route`, `take`, `skip`, `expire`, `place_entry` and `approvals.take`/`skip`/`expire_due`, then against `test_router.py`'s `Rig` (the real router, adapter and `FakeBroker`). The router model routes an auto entry to Triggered and places it in a separate step (`Place`; a Take decides to `taken`, then `PlaceTaken`), with a `Crash` between that loses the in-process placement, and stands a placed entry for the open position; a counterexample that needs a fill, a broker stop or a rejection belongs to `OrderReconciliation` or `StopPlacement`.
- Never weaken a property or shrink a strategy. Narrowing needs a reason under "Scope changes".
- Model bounds: three tags (the original and up to two retries; the protocol allows one), one retry, two broker orders per tag counted, one divergence, two session owners, two restarts. Assumptions: each action is one transaction or one broker call, a crash can fall between any two, a rejection means nothing was placed, and the tag lookup is consistent with the broker's book.
- Known windows, kept visible:
  - Fyers' order book may lag a placement and Fyers does not enforce tag uniqueness. Since an entry of unknown outcome is never sent again (the model's `Lookup` reads a lagging `seen` book). A lost day stop is still re-placed under its tag once the lookup finds nothing after `LOST_ANSWER_SETTLE`: if the book lagged longer than that, two stops rest for one position; the sync records both fills and `fill.excess` does not cover stops. Sales always take a new tag.
  - A reconciliation can still land between the guard re-check and the broker call (microseconds). The next order is blocked, the one in flight is not.
  - An unconfirmed GTT is not retried (`UnresolvedOrderError`): Fyers does not enforce tag uniqueness. Since it is settled against the GTT book (`StopDesk.settle_lost`) once older than `LOST_ANSWER_SETTLE` (60 s, so an SDK request still in flight is not read as absent). Since a GTT carries its HT tag (`orderTag`, recorded live 2026-10-22), so the settle matches it by tag first, else an untagged GTT by symbol, side, quantity and trigger. Unverified: a 20-character tag on a GTT (the recording used an 8-character one), and whether a triggered GTT's child order carries the tag and in which form (the fill sync accepts it bare or `GTT`-prefixed, else falls back to the untagged shape). Two GTTs on one symbol with a tagged child are not driven with two positions.
  - The IPv4 hook covers urllib3 connections only: an HTTPS proxy (the connection goes to the proxy) and the data websocket are not forced onto IPv4.
  - Fyers' -360 reply was not recorded (only its message, from the brief); the code is matched on `code` alone.
  - Reconciliation quantity is the larger of the CNC position and the holding per symbol: [verify] on a live account, and it fails closed on a partial overlap.

## Updating

- A new or changed order rule: edit `verification/tla/OrderReconciliation.tla`, keep a `\* code:` comment on every action, add or adjust a broken variant (`broken/OrderReconciliation__<mode>.cfg` with an `EXPECT:` line and the mode in the model's `ASSUME`), then run both `run.sh` commands and the property checks.
- A new Fyers payload field or status code: update `map_fyers_status` or `_order_payload` and the matching property in `test_tags.py`.
- A change to `HatBroker` needs `BROKER_METHODS` in `test_conf_broker.py` changed with it.
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

Trade lifecycle:
- `verification/tla/TradeLifecycle.tla` and `verification/tla/TradeLifecycle.cfg`
- `verification/tla/broken/TradeLifecycle__no_lock.cfg`, `verification/tla/broken/TradeLifecycle__state_without_event.cfg`, `verification/tla/broken/TradeLifecycle__skip_exit_pending.cfg`, `verification/tla/broken/TradeLifecycle__virtual_skips_exit_pending.cfg`, `verification/tla/broken/TradeLifecycle__reopen.cfg`, `verification/tla/broken/TradeLifecycle__result_late.cfg`, `verification/tla/broken/TradeLifecycle__exits_use_live.cfg`, `verification/tla/broken/TradeLifecycle__exit_stalls.cfg`
- `backend/app/hat/engine/trades.py`
- `backend/app/hat/engine/snapshot.py`
- `backend/app/hat/engine/positions.py`
- `backend/app/hat/engine/botlock.py`
- `backend/app/hat/engine/lifecycle.py`
- `backend/app/hat/models/runtime.py`
- `backend/tests/hat/properties/test_trades.py`
- `backend/tests/hat/properties/test_exits.py`
- `backend/tests/hat/conformance/test_conf_lifecycle.py`
- `backend/tests/hat/db/hat_db_runtime.py`
- `backend/tests/hat/db/test_db_broken_variants.py`

Execution router and approvals:
- `verification/tla/ExecutionRouter.tla` and `verification/tla/ExecutionRouter.cfg`
- `verification/tla/broken/ExecutionRouter__any_mode.cfg`, `verification/tla/broken/ExecutionRouter__apply_now.cfg`, `verification/tla/broken/ExecutionRouter__confirm_places_now.cfg`, `verification/tla/broken/ExecutionRouter__exit_asks.cfg`, `verification/tla/broken/ExecutionRouter__exits_wait.cfg`, `verification/tla/broken/ExecutionRouter__gate_unknown_ok.cfg`, `verification/tla/broken/ExecutionRouter__no_expiry_sweep.cfg`, `verification/tla/broken/ExecutionRouter__no_gtt_gate.cfg`, `verification/tla/broken/ExecutionRouter__no_resume.cfg`, `verification/tla/broken/ExecutionRouter__no_stop_check.cfg`, `verification/tla/broken/ExecutionRouter__place_unchecked.cfg`, `verification/tla/broken/ExecutionRouter__pyramid.cfg`, `verification/tla/broken/ExecutionRouter__route_without_risk.cfg`, `verification/tla/broken/ExecutionRouter__shadow_calls_broker.cfg`, `verification/tla/broken/ExecutionRouter__short_entry.cfg`, `verification/tla/broken/ExecutionRouter__skip_drops_trade.cfg`, `verification/tla/broken/ExecutionRouter__take_ignores_feed.cfg`, `verification/tla/broken/ExecutionRouter__take_ignores_gate.cfg`, `verification/tla/broken/ExecutionRouter__take_ignores_hours.cfg`, `verification/tla/broken/ExecutionRouter__take_skips_chase.cfg`
- `verification/tla/ApprovalToken.tla` and `verification/tla/ApprovalToken.cfg`
- `verification/tla/broken/ApprovalToken__decide_twice.cfg`, `verification/tla/broken/ApprovalToken__no_chase_recheck.cfg`, `verification/tla/broken/ApprovalToken__no_sweep.cfg`, `verification/tla/broken/ApprovalToken__take_after_expiry.cfg`
- `backend/app/hat/execution/router.py`
- `backend/app/hat/execution/modes.py`
- `backend/app/hat/execution/mode_changes.py`
- `backend/app/hat/engine/approvals.py`
- `backend/app/hat/engine/confidence.py`
- `backend/app/hat/backtest/runs.py`: `signal_confidence`
- `backend/app/hat/notify.py`
- `backend/tests/hat/properties/test_router.py`
- `backend/tests/hat/hat_router_support.py`
- `backend/tests/hat/db/test_db_crash_safe_routing.py`

Live trade management:
- `backend/app/hat/execution/protect.py`
- `backend/app/hat/execution/fills.py`
- `backend/app/hat/execution/manage.py`
- `backend/app/hat/execution/entry_timeout.py`
- `backend/app/hat/engine/live_trades.py`
- `backend/tests/hat/hat_manage_support.py`
- `backend/tests/hat/properties/test_manage.py`
- `backend/app/hat/execution/series.py`
- `backend/tests/hat/fixtures/fyers_gtt_ex_date_assumed.json`
- `backend/tests/hat/properties/test_mpp.py`
- `backend/tests/hat/fixtures/fyers_mpp_sale_assumed.json`
- `backend/app/hat/engine/journal.py`
- `backend/tests/hat/properties/test_journal.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `d0ba323bb8d38329ecc3e61d5208a2ec0db15943`
Covered digest: `538c50dba16756fe9455cb8108ea0bb7f7a1292600c785fcefffbdb000b40d3c`

## Scratch breaks

Each check was shown to catch a deliberate break in the real code or model, then the break was removed and the check returned to passing. TLA breaks are the model's `BrokenMode` variants, run by `run.sh --broken`; the state lines are the last steps of TLC's counterexample.

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

### TLA OrderReconciliation!LostEntryAdopted

Break: broken variant `OrderReconciliation__no_late_adoption.cfg` (`BrokenMode = "no_late_adoption"`): only the lookup adopts; the later sync never does.

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

Break: `_order_book` re-raises for every unreadable row (main before the whole book failed).

```
E   app.hat.execution.broker.BrokerUnavailableError: broker_response_invalid
E   Failing explicit example: test_unreadable_foreign_rows_skipped(
E       rows=[('bad', None, 'side')],
```

### PBT tags.held_quantity_nets_today

Break: `broker_quantities` takes the larger of the CNC position and the holding (main before).

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

Break: `_whole_number` goes back to `value.strip().lstrip("-").isdigit()`, which accepts `'¹'`.

```
E   ValueError: invalid literal for int() with base 10: '¹'
E   Failing explicit example: test_broker_status_mapping_is_total(
E       code='¹',
```

### PBT tags.an_unreachable_broker_is_not_a_rejection

Break: `send_order` treats only `broker_unavailable` as not sent, so a -360 refusal is stored as a rejection.

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

### PBT tags.a_timeout_is_looked_up_before_any_retry

Break: in `submit.py`, `_lookup_before_retry` returns None for an entry the lookup did not find (the behaviour).

```
E   assert 2 == 1
E    +  where 2 = len([1, 3])
E   Failing explicit example: test_a_timeout_is_looked_up_before_any_retry(
E       outcomes=['landed', 'ok'],
E       purpose='entry',
E       lagging=True,
```

### PBT router.unresolved_entry_is_alerted_not_resent

Break: in `router.py`, `_send_once` swallows `UnresolvedOrderError` without the alert.

```
E   AssertionError: assert 0 == 1
E    +  where 0 = <built-in method count of list object at 0x7f3d041852c0>('entry.unresolved')
E   Failing explicit example: test_unresolved_entry_is_alerted_not_resent(
```

### PBT tags.a_duplicated_tag_is_read_one_way

Break: `TagBook.of` keeps the first order under a tag even when the broker ended it unfilled.

```
E   AssertionError: assert BrokerOrder(order_id='D-0', ..., filled_qty=0, status='cancelled', ...) is BrokerOrder(order_id='D-1', ..., filled_qty=0, status='placed', ...)
E   Failing test case: test_a_duplicated_tag_is_read_one_way(
E       orders=[('cancelled', 0), ('placed', 0)],
```

### PBT manage.a_dead_second_order_never_ends_the_entry

Break: `TagBook` keeps the last order per tag and ignores the row's broker order id (the `by_tag` dicts before the review round).

```
E   AssertionError: assert ('B-1', 'rejected') == ('B-1', 'filled')
E   Failing explicit example: test_a_dead_second_order_never_ends_the_entry(
E       status=5,
```

### PBT tags.a_tag_twice_at_the_broker_blocks_the_account

Break: `find_mismatches` flags a tag only when it is held more than twice.

```
E   AssertionError: assert [] == ['HTE00000000000000000']
E   Failing test case: test_a_tag_twice_at_the_broker_blocks_the_account(
```

### PBT manage.a_second_order_under_one_tag_is_tracked

Break: `_record` does not call `_check_excess`.

```
E   AssertionError: assert 0 == 1
E   Failing explicit example: test_a_second_order_under_one_tag_is_tracked(
```

### PBT router.a_stored_capability_never_enables_a_real_entry

Break: `router._gtt_refusal` passes `True` for the adapter's value (stored only, before).

```
E   assert 1 == 0
E    +  where 1 = scalar(<sqlalchemy.sql.selectable.Select object>)
E    +    and   <sqlalchemy.sql.selectable.Select object> = select_from(HatOrder)
```

### PBT broker.session_check_refreshes_capabilities

Break: the refresh runs on live checks only (the first version of).

```
E   AssertionError: assert {} == {'gtt_deliver... 'SL-M'], ...}
```

### PBT router.desks_start_with_the_adapters_capabilities

Break: `Desks` does not refresh at start.

```
E   assert False is True
E   Failing test case: test_desks_start_with_the_adapters_capabilities(
E       stored=False,
E       live=True,
```

### PBT tags.refusals_before_the_broker

Break: `_check_allowed` passes `True` for the adapter's GTT (stored only).

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

### PBT ops.no_outage_before_the_login_window

Break: `SessionWatch.observe` has no 08:30 window (the code before).

```
E   assert not True
E    +  where True = WatchStep(retry_in=2, alert=True).alert
E   Failing test case: test_no_outage_before_the_login_window(
```

### PBT router.broker_health_follows_the_session

Break: `check_session` does not sync `broker_health` (the code before).

```
E   AssertionError: assert 'armed' == 'tripped'
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

Break: `FYERS_CAPABILITIES` reports `gtt_delivery=False`.

```
E   AssertionError: assert False is True
E    +  where False = Capabilities(gtt_delivery=False, tag_max_len=20, tag_alphabet='A-Za-z0-9', order_types=('MARKET', 'LIMIT', 'SL', 'SL-M'), products=('CNC',), static_
```

Before the break was the reverse (`gtt_delivery=True` while the property asserted false).

### CONF fyers_adapter_wraps_existing

Break: `execution/gtt.py` imports `fyers_apiv3`.

```
E   AssertionError: PosixPath('.../backend/app/hat/execution/gtt.py')
E   assert not <re.Match object; match='\nimport fyers_apiv3'>
```

Break: `fyers_session.py` imports `fyersModel` from the SDK itself instead of `from .fyers_ipv4 import sdk`.

```
E   AssertionError: assert ['execution/f...orts the SDK'] == []
E     Left contains one more item: 'execution/fyers_session.py: imports the SDK'
```

Second break: `fyers_ipv4.sdk()` imports the SDK without calling `install()` first (the check locates `install()` before the import and fails when it is absent).

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

Break: the guard skips the write-header check.

```
E   assert 200 == 403
E   Failing explicit example: test_cross_site_write_refused(
E       header=None,
E       origin='http://localhost:3000',
E       fetch_site='same-origin',
```

### PBT fyers_auth_routes.token_for_another_account_refused

Break: `POST /token` does not pass `accept_user` to the exchange.

```
tests/hat/properties/test_fyers_auth_routes.py:374: in test_token_for_another_account_refused
E   assert 200 == 403
E   Failing test case: test_token_for_another_account_refused(
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

Break: a -16 on a read leaves the profile check standing.

```
1 failed: assert <SessionStatus.LIVE: 'live'> is <SessionStatus.EXPIRED: 'expired'>
```

### PBT broker.token_refused_is_session_expired

Break: `_read_ok` without the -16 branch; `session_status` without the profile probe; the adapter's `_read` maps -16 to `broker_unavailable`; main's broker and adapter.

Also: no check cache (`assert 2 == 1`); the cache ignores a new SDK model, i.e. a new token (`assert 1 == 2`).

```
no -16 branch:  E   AssertionError: assert <SessionStatus.LIVE: 'live'> is <SessionStatus.EXPIRED: 'expired'>
no probe:       E   AssertionError: assert <SessionStatus.LIVE: 'live'> is <SessionStatus.EXPIRED: 'expired'>
read code:      E   AssertionError: assert 'broker_unavailable' == 'broker_session_expired'
main:           E   AssertionError: assert <SessionStatus.LIVE: 'live'> is <SessionStatus.EXPIRED: 'expired'>
```

### PBT fyers_auth.a_good_exchange_saves_once_and_reports_in_order

Break: `status()` reports `token.user_id or ""`; the profile's user id is not saved.

```
empty id:     E   AssertionError: assert '' == None
not saved:    E   AssertionError: assert None == 'XA1234'
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

Second break: the GTT book row reads `side` instead of `tran_side`.

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

Break: recreated `app/services/fyers_client.py`.

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

### PBT trades.last_hard_stop

Break: in `backend/app/hat/engine/trades.py`: the instrument filter dropped, so another instrument's hard stop counts.

```
tests/hat/properties/test_trades.py F
tests/hat/properties/test_trades.py:220: in test_last_hard_stop
    assert snapshot.last_hard_stop(session, world.bot_id, world.instruments[0], at) == want
E   assert (datetime.date(2026, 10, 21),) == ()
```

### PBT trades.day_counts

Break: the day window swapped in `snapshot.day_counts` (`start, end = day_bounds(trade_date)` became `end, start = ...`), so no stop-out is counted. It passed every other property and DB check (`test_trades`, `test_router`, `test_journal`, `test_lifecycle`, `test_caps`, `test_backtest`, `test_db_broken_variants`): the gap this property closes.

```
E   assert (1, 0) == (1, 1)
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

Break: in `backend/app/hat/execution/router.py`: `placement_refusal` ignores the signal's stop (`if stop is not None and check.ltp <= stop:` became `if False:`), so an auto entry is placed at or below its stop.

```
E   AssertionError: assert [{'symbol': '...'LIMIT', ...}] == []
E     Left contains one more item: {'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}
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

Break: in `backend/app/hat/execution/router.py`: `take` does not pass the signal's stop (`stop = None`), so a Take at or below the stop is approved.

```
E   AssertionError: assert 'approved' == 'price_at_or_below_stop'
E   Failing explicit example: test_take_rechecks_max_chase(
E       chase_bp=0,
E       after_s=60,
E       conditions=('price_at_or_below_stop',
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

Break: `create_bot` accepts a confirm or auto bot (before, as `exec__default_mode_shadow`).

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

### PBT manage.broker_stop_only_tightens

Break: in `backend/app/hat/execution/protect.py`, `_move` stores the new trigger even when the broker refused the modify.

```
E   AssertionError: assert Decimal('95.0500') == Decimal('95.0000')
E   Failing test case: test_broker_stop_only_tightens(
```

### PBT manage.swap_leaves_no_gap

Break: in `backend/app/hat/execution/protect.py`, `swap` cancels the intraday stop before placing the GTT.

```
E   AssertionError: assert 1 < 0
E   Failing test case: test_swap_leaves_no_gap(
```

### PBT manage.unfilled_entry_cancelled

Break: `cancel_reason` counts the trigger bar's own close (`triggered_at < at` became `<=`).

```
E   AssertionError: (1, False)
```

### PBT manage.entry_remainder_tracked

Break: in `backend/app/hat/engine/live_trades.py`, `apply_exit_fill` closes the emptied position while its entry still works.

```
E   assert 6 != 6
E   Failing explicit example: test_entry_remainder_tracked(
E       first=1,
E       steps=[('stop', 0), ('entry', 3)],
```

### PBT manage.verify_covers

Break: in `backend/app/hat/execution/protect.py`, `verify` never keeps the resting trigger (`keep = False`): it modifies the GTT to the stop floored to the new tick, below the trigger it rests at, and replaces a refused one lower.

```
ExceptionGroup: Hypothesis found 2 distinct failures in explicit examples. (2 sub-exceptions)
    | AssertionError: assert Decimal('95.0000') == Decimal('95.0300')
    | AssertionError: assert 'gtt.unverified' in ['gtt.missing']
```

### PBT manage.real_exits_never_blocked

Break: in `backend/app/hat/execution/manage.py`, `_sell` returns without sending while any kill switch row exists.

```
E   AssertionError: assert [] == [('MARKET', 165)]
E     Right contains one more item: ('MARKET', 165)
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

### PBT manage.gtt_trigger_handled

Break: in `backend/app/hat/execution/protect.py`, `protect` no longer waits on a partially filled GTT.

```
E   assert 250 == (250 - 250)
E   Failing test case: test_gtt_trigger_handled(
```

### PBT manage.gtt_resting_marketed

Break: in `backend/app/hat/execution/fills.py`, `_gtt_resting` asks again after the book shows the sale MARKET (`if order.order_type is OrderType.MARKET: return` to `if False:`).

```
E   AssertionError: assert [('modify_ord...: 'MARKET'}))] == [('modify_ord...: 'MARKET'}))]
E   Failing explicit example: test_gtt_resting_marketed(
```

### PBT manage.lost_answers_never_freeze

Break: in `backend/app/hat/execution/protect.py`, `settle_lost` treats no row as ready, so a lost answer is never settled.

```
    | AssertionError: assert {'stop_day', 'stop_gtt'} == {'stop_gtt'}
```

### PBT manage.async_sale_rejection_retried

Break: in `backend/app/hat/execution/submit.py`, a sale the broker ends short is not treated as a rejection.

```
E   AssertionError: assert 0 == 1
E    +  where 0 = _event_count(<Session ...>, <HatOrder ...>, 'order.rejected')
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

Break: in `backend/app/hat/execution/fyers_broker.py`, `_call_with_guard` returns the timeout fallback when the breaker opened, as before.

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

### PBT tags.a_reserved_tag_is_sent_by_one_sender

Break: in `backend/app/hat/execution/submit.py`, `send_order` stamps the attempt without the conditional claim.

```
E   AssertionError: assert 1 == 0
E    +  where 1 = SendOutcome(tag='HTE0E...', status='placed', broker_order_id='B-2', code='ok', broker_calls=1).broker_calls
```

### PBT manage.a_resting_sale_is_never_stopped_over

Break: in `backend/app/hat/execution/protect.py`, `protect` no longer waits for a sale the broker holds (CodeAnt review).

```
E   AssertionError: assert ['place_order'] == []
```

### PBT manage.the_close_ends_only_settled_answers

Break: in `backend/app/hat/execution/manage.py`, `end_session` ends a lost order sent less than the settle wait ago (CodeAnt review).

```
E   AssertionError: assert 'expired' == 'unconfirmed'
E     - unconfirmed
E     + expired
```

### PBT tags.the_gtt_book_reads_only_resting_stops

Break: in `backend/app/hat/execution/fyers_adapter.py`, `_gtt_row` keeps an ended GTT.

```
E   app.hat.execution.broker.BrokerUnavailableError: broker_response_invalid
E   Failing test case: test_the_gtt_book_reads_only_resting_stops(
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

Fourth break: the shape match also takes a GTT under a foreign tag (`and not gtt.foreign` removed). Before `gtt_books` was reweighted the property passed this break; now the generated case fails, as does the `@example`.

```
E   AssertionError: assert BrokerGtt(gtt_id='G0', symbol='SBIN', side=<Side.SELL: 'sell'>, qty=1, trigger_price=Decimal('100'), tag=None, foreign=True) == None
E   Failing test case: test_a_lost_gtt_is_found_by_its_tag(
```

### PBT router.routine_reconcile_keeps_gate

Break: `ReconciliationService.run` clears `guard.reconciled_for` at its top (re-checked; before it called `begin_session()` there).

```
E   AssertionError: assert {'not_reconciled'} == {None}
```

### PBT router.older_read_never_overrides_newer

Break: `ExecutionGuard.update` applies every result, whatever its read ticket (main before).

```
E   AssertionError: assert (None == 'reconcile_mismatch') == True
E   Failing test case: test_older_read_never_overrides_newer(
E       first_unknown=False,
E       second_unknown=True,
```

### PBT router.entry_gate_keyed_by_trade_date

Break: `ExecutionGuard.refusal` checks `reconciled_for is None` instead of `!= today()` (main before a boolean gate).

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

### TLA ExecutionRouter!EntriesSettled

Break: broken variant `ExecutionRouter__no_resume.cfg` (the model's `BrokenMode = "no_resume"`): a crash loses the placement and no restart resumes it.

```
Error: Temporal properties were violated.
Error: The following behavior constitutes a counter-example:
State 11: <Next line 432, col 9 to line 432, col 36 of module ExecutionRouter>
State 12: <Crash line 223, col 5 to line 225, col 27 of module ExecutionRouter>
State 13: Stuttering
```

### PBT router.crash_resumes_entry_once

Break: `ExecutionRouter._send_entry` skips the stored rejection count before a new tag (`if self._entry_rejections(session, signal.id) >= MAX_ENTRY_REJECTIONS:` to `if False:`).

```
    assert len(sent) <= MAX_ENTRY_REJECTIONS
E   AssertionError: assert 3 <= 2
E   Failing explicit example: test_crash_resumes_entry_once(
E       path='auto',
E       replies=[False, False],
```

### PBT router.sweep_idempotent

Break: `place_entry` places an undecided card (the `_taken` guard to `if False:`).

```
    assert len(rig.places()) == before
E   AssertionError: assert 1 == 0
E   Failing explicit example: test_sweep_idempotent(
```

### PBT router.stranded_entry_ends_next_day

Break: `expire` does not end an earlier trade date's stranded entry (`self._not_placed(session, signal, SIGNAL_EXPIRED, now)` to `pass`).

```
    assert not _left_stranded(session, signal_id)
E   AssertionError: assert not True
E   Failing explicit example: test_stranded_entry_ends_next_day(
E       k=1,
E       order=('sweep',),
```

### PBT router.sweep_survives_a_bad_signal

Break: the sweep's per-signal `except Exception` narrowed to `except ZeroDivisionError`.

```
    raise RuntimeError("database error")
E   RuntimeError: database error
E   Failing explicit example: test_sweep_survives_a_bad_signal(
E       bad=0,
```

### Postgres test_db_crash_safe_routing.py::test_db_concurrent_placements_send_one

Break: `place_entry` without the router's `_placing` lock (`with self._placing:` to `if True:`).

```
    assert len(sent) == 1
E   AssertionError: assert 2 == 1
E   Failing explicit example: test_db_concurrent_placements_send_one(
E       threads=2,
```

### Postgres test_db_crash_safe_routing.py::test_db_crash_resumes_entry_once

Break: the `_entry_rejections` check before a new tag removed (as above), on PostgreSQL.

```
    assert len(sent) <= MAX_ENTRY_REJECTIONS
E   AssertionError: assert 3 <= 2
E   Failing explicit example: test_db_crash_resumes_entry_once(
E       path='auto',
```

### Postgres test_db_crash_safe_routing.py::test_db_concurrent_sweeps_settle_once

Break: `expire` does not end an earlier trade date's stranded entry.

```
    assert expiries == 1
E   assert 0 == 1
E   Failing explicit example: test_db_concurrent_sweeps_settle_once(
E       threads=4,
E       taken=True,
```

### PBT router.resumed_entry_waits_for_feed

Break: `daily/live.py:stale_feed` returns the cached `staleness.stale` flag, which is false until the first check after a start.

```
E   AssertionError: assert [{'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}] == []
E     Left contains one more item: {'symbol': 'NSE:SBIN-EQ', 'quantity': 500, 'side': 'BUY', 'type': 'LIMIT', ...}
    assert rig.places() == []  # no tick heard since the start
```

### PBT router.desk_calls_serialised

Break: `OwnedBroker` calls the adapter directly (`return attribute(*args, **kwargs)`, no `owner.call`).

```
E   assert 4 == 1
E    +  where 4 = <test_router.OverlapSpy object at 0x...>.most
```

### PBT router.fill_sync_cadence

Break: `Desks.sync_due` ignores `watch_closely` (`if quiet:`).

```
E   AssertionError: assert (1 > 1) == True
E   (note: 3 other explicit examples also failed with this error; use Verbosity.verbose to view)
E   Failing explicit example: test_fill_sync_cadence(
E       placed=2,
E       filled=10,
```

### PBT router.one_login_per_broker

Break: `production.one_login_each` never refuses (`if account.broker in built:` to `if False:`).

```
E   Failed: DID NOT RAISE <class 'app.hat.errors.HatConfigError'>
E   Falsifying example: test_one_login_per_broker(
E       brokers=['fyers', 'fyers'],
```

### PBT manage.flatten_capped_at_holdings

Break: in `backend/app/hat/execution/manage.py`: `flatten` reads the holdings but does not cap the sale (main before).

```
E   assert 500 == 1
E    +  where 1 = min(500, 1)
E   Failing explicit example: test_flatten_capped_at_holdings(
```

### PBT router.mode_gates_hold_bots_in_shadow

Break: `gate_refusal` skips the DDPI gate (`if gates.ddpi_confirmed is not True:` to `if False:`). The same break fails `telegram.mode_names_a_bot_and_its_gate` (`verify-telegram`).

```
E   AssertionError: assert 'auto' == 'shadow'
E     - shadow
E     + auto
```

### PBT router.auto_places_only_high_confidence

Break: `route_mode` lets a medium signal place in auto (`confidence not in (HIGH, "medium")`).

```
E   AssertionError: assert 'auto' == 'confirm'
E     - confirm
E     + auto
```

### PBT router.placement_rechecks_confidence

Break: in `backend/app/hat/execution/router.py`: placement no longer reads the stored label.

```
tests/hat/properties/test_router.py F
tests/hat/properties/test_router.py:1344: in test_placement_rechecks_confidence
    assert rig.fake.calls == []
E   AssertionError: assert [('get_profil [cut]LIMIT',  [cut]})] == []
E     Left contains 2 more items, first extra item: ('get_profile', None)
E     Full diff:
E     - []
E     + [
E     +     (
E     +         'get_profile',
E     +         None,
```

### PBT router.open_settles_mode_changes

Break: `apply_mode_changes` applies every due change without reading the gates (`refusal = None`).

```
E   AssertionError: assert 1 == 0
E    +  where 0 = int('shadow' != 'shadow')
E   Failing explicit example: test_open_settles_mode_changes(
E       start='shadow',
```

### PBT manage.verify_renews_old_gtt

Break: in `backend/app/hat/execution/protect.py`, `verify` never calls `_renewed` (main before).

```
E   AssertionError: assert 'placed' == 'cancelled'
E     - cancelled
E     + placed
```

### PBT manage.ex_date_gtt_replaced

Break: in `backend/app/hat/execution/manage.py`, `_near_ex_date` takes today only (not the next session).

```
E   AssertionError: assert 'placed' == 'cancelled'
E     - cancelled
E     + placed
```

### PBT manage.series_change_remapped

Break: in `backend/app/hat/daily/live.py`, `ExitViews` ignores the series change.

```
E   assert (<object object at 0x7f2ff5d71420> is None) == True
E    +  where <object object at 0x7f2ff5d71420> = <app.hat.daily.live.ExitViews object at 0x7f2ff5971450>(...)
E   Failing explicit example: test_series_change_remapped(
```

### PBT manage.ex_date_check_serialised

Break: `_serialised` calls the method without the position's stop lock.

```
E   AssertionError: assert 2 == 1
E    +  where 2 = len(['place_gtt', 'place_gtt'])
E   Failing explicit example: test_ex_date_check_serialised(
E       shape='cancelled',
E       other='protect',
```

### PBT fyers_broker.budget_windows_hold

Break: in `backend/app/hat/execution/fyers_broker.py`, `_read_limit` returns the whole limit (reads use writes' share).

```
E   assert not True
E    +  where True = any((False, True, False, False))
E   Failing explicit example: test_budget_windows_hold(
E       per_second=2,
E       per_minute=3,
```

### PBT fyers_broker.throttled_call_not_sent

Break: `rate_limited` matches -360 only, not 429.

```
    | AssertionError: assert 'broker_rejected' == 'broker_rate_limited'
    | AssertionError: assert 'broker_unavailable' == 'broker_rate_limited'
```

### PBT fyers_broker.writes_go_ahead_of_reads

Break: `acquire` lets a read through while a write waits.

```
    assert not read_granted
E   assert not True
E   Failing test case: test_writes_go_ahead_of_reads(
E       op='place_order',  # or any other generated value
```

### PBT fyers_broker.limits_from_settings

Break: in `backend/app/hat/settings.py`, `broker_calls_per_second` ignores the environment.

```
E   AssertionError: assert 8 == 1
E    +  where 8 = CallLimits(per_second=8, per_minute=180, per_day=5000).per_second
E   Failing test case: test_limits_from_settings(
E       per_second='1',
```

### PBT fyers_broker.late_429_still_pauses

Break: the 429 pause back in the caller only (the worker does not pause).

```
E   AssertionError: assert 0.0 > 0
E    +  where 0.0 = take('write', ((100.0 + 1.0) - 0.01), datetime.date(2026, 10, 23))
E   Failing test case: test_late_429_still_pauses(
E       retry_after=None,  # or any other generated value
```

### PBT fyers_broker.breaker_opened_while_waiting_sends_nothing

Break: no breaker check after the budget wait.

```
    | Failed: DID NOT RAISE BrokerUnavailableError
    | AssertionError: assert True is False
    | AssertionError: assert [('place_orde...e': -1, ...})] == []
```

### PBT mpp.resting_market_sale_resent

Break: in `backend/app/hat/execution/submit.py`, `rejected_sales` counts a sale HAT cancelled to send again (`~_resend_event()` removed), so the third miss hits the two-rejection limit.

```
E   assert [] == [499]
E     
E     Right contains one more item: 499
E     
E     Full diff:
```

### PBT mpp.lower_circuit_lock_holds_the_sale

Break: in `manage.py`, `_cancel_resting` cancels a sale while its instrument is locked at the lower circuit.

```
E   AssertionError: assert ['B-3'] == []
E     
E     Left contains one more item: 'B-3'
E     
E     Full diff:
```

### PBT mpp.rejected_twice_goes_at_the_band

Break: in `manage.py`, `_band_sale` sends the limit at the request's price, not the lower band.

```
E   AssertionError: assert [] == [('SELL', 500...mal('80.00'))]
E     
E     Right contains one more item: ('SELL', 500, Decimal('80.00'))
E     
E     Full diff:
```

### PBT mpp.gtt_market_sale_asked_again

Break: in `fills.py`, a triggered GTT's market sale still resting is never asked again (`if self._resend:` became `if False:`).

```
E   AssertionError: assert [] == [('modify_ord...: 'MARKET'}))]
E     
E     Right contains one more item: ('modify_order', ('X-1', {'order_type': 'MARKET'}))
E     
E     Full diff:
```

### PBT mpp.resend_flag_parsed

Break: in `backend/app/hat/settings.py`, `HAT_MPP_RESEND` is on unless `0`.

```
E   AssertionError: assert True == ((None is not None))
E    +  where True = HatSettings(enabled=False, user_id=UUID('00000000-0000-7000-8000-000000000001'), data_root=PosixPath('/data/workspace/..., broker_calls_per_second=8, broker_calls_per_minute=180, broker_calls_per_day=5000, mpp_resend=True, mpp_page_after=3).mpp_resend
E   Failing explicit example: test_resend_flag_parsed(
E       resend=None,
E       page=None,
```

### PBT mpp.no_cancel_without_a_resend

Break: in `backend/app/hat/execution/manage.py`, `_cancel_resting` ignores the order cutoff (`if not self._orders_allowed(now + RESTING_AFTER):` became `if False:`).

```
E   AssertionError: assert ['B-3'] == []
E   Failing explicit example: test_no_cancel_without_a_resend(
E       seconds=60,
E       shape='market_protected',
```

### CONF journal_only_writers

Break: main's `backend/app/hat/execution/submit.py` (`_advance` assigns `signal.state` and `position.state`), restored into the branch.

```
E   AssertionError: assert ['execution/submit.py:532', 'execution/submit.py:538'] == []
```

### PBT journal.validates_before_writing

Break: in `backend/app/hat/engine/journal.py`, `record` never refuses (`if not broker_fact: raise` became `if False: raise`), so a forbidden decision is written as a `no_step` event.

```
    with pytest.raises(lifecycle.LifecycleStateError):
E   Failed: DID NOT RAISE LifecycleStateError
E   Failing explicit example: test_validates_before_writing(
E       steps=[(0, False), (13, False)],
E   )
```

### PBT journal.check_names_mismatches

Break: in `backend/app/hat/engine/journal.py`, `mismatches` does not compare the signal's own position.

```
    assert {signal_id for signal_id, _, _ in found} == want
E   AssertionError: assert set() == {UUID('adc01e9f-9183-4af4-b3ca-063102378f54')}
E   Failing explicit example: test_check_names_mismatches(
E       trades=[('position', True, 'ExitPending'), ('veto', False, 'Open')],
E   )
```

### PBT journal.check_alerts

Break: in `backend/app/hat/daily/nightly.py`, `journal_check` alerts every mismatch (no `JOURNAL_ALERTS` cap).

```
    assert len(notifier.alerts) == min(drifted, JOURNAL_ALERTS)
E   AssertionError: assert 11 == 10
E   Failing explicit example: test_check_alerts(
E       drifted=11,
E       sound=1,
E   )
```

### PBT fyers_auth.accounts_read_in_order

Break: `exchange` skips the account check (`if not self._accepts(user_id):` became `if False:`).

```
    | AssertionError: assert True == False
    |     assert trace == read + (["save"] if accepted else [])
    | AssertionError: assert ['exchange', ...file', 'save'] == ['exchange', ...unts', 'save']
```

### PBT fyers_auth.complete_login_checks_the_account

Break: `complete_login` maps a refused user to `login_failed` (`raise BrokerLoginError("wrong_account")` became `"login_failed"`).

```
E   AssertionError: assert 'login_failed' == 'wrong_account'
```

### PBT fyers_ipv4.no_proxy_carries_fyers

Break: `*` among other `NO_PROXY` entries read as a bypass.

```
E   AssertionError: api.fyers.in
E   assert 'http://user:pw-77ee@proxy.example:3128' is None
```

### CONF brk__account_constants_one_copy

Break: `execution/submit.py` defines its own `ACCOUNT_ACTIVE = "active"` again.

```
E   AssertionError: assert {'execution/s...OUNT_ACTIVE']} == {}
```


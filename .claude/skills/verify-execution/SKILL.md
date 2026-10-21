---
name: verify-execution
description: How to run, read and update the checks behind Hire a Trader's broker interface, the Fyers adapter, order tags, idempotent order submission, reconciliation, account onboarding and the broker session (TLA+ OrderReconciliation, tag, adapter, submission and session properties, broker CONF checks). Use before and after any change to backend/app/hat/execution (the Fyers broker, session, login and token store included), backend/app/hat/api/fyers_auth.py, FakeBroker or the OrderReconciliation model.
---

# verify-execution

Checks that an order reaches the broker at most once per tag and only after its attempt is persisted, that an unknown outcome is looked up by tag before any retry, that nothing is sent before a reconciliation or while a mismatch blocks it, that an unreadable broker never reads as an empty one, and that the Fyers adapter is a thin, CNC-only wrapper over HAT's own Fyers broker, session and login (K2), with one login and one token store. Specs: `docs/specs/hat/07-execution.md` §8-§11, `docs/specs/hat/10-ops.md` §2 (HAT-OPS-031, 032, 051), HAT-BRK-080, 091, 100, 112, HAT-DEC-I7, I8, I9, I10, BD2, HAT-SCH-053, HAT-EXEC-062, `11-verification.md` (HAT-VER-030..034, 072).

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
- `PBT broker.confirm_and_auto_need_gtt` `effective_mode` and `request_mode` allow confirm or auto only for an active account that reports `gtt_delivery`, and the Fyers adapter does not (the MVP is shadow only). Harm: a live mode runs on an account that cannot hold a protective stop.
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
- `CONF ops.one_login_one_token_store` The login service, the session default and the legacy re-export use HAT's one `token_store`; the legacy client, broker and symbol master subclass HAT's; the legacy login router is gone; the HAT router has exactly the four login routes. Harm: a second login or token copy drifts from the first.

## Run

```bash
export JAVA_HOME=/path/to/jdk   # Java 17+ (Temurin 21 is what CI uses)
verification/tla/run.sh OrderReconciliation            # the model must pass
verification/tla/run.sh --broken OrderReconciliation   # each variant must fail with its EXPECT line
cd backend
CI=1 python -m pytest tests/hat/properties/test_tags.py tests/hat/properties/test_broker.py \
  tests/hat/properties/test_stops.py tests/hat/properties/test_ops.py -q --no-cov
CI=1 python -m pytest tests/hat/properties/test_fyers_tokens.py tests/hat/properties/test_fyers_session.py \
  tests/hat/properties/test_fyers_auth.py tests/hat/properties/test_fyers_auth_routes.py \
  tests/hat/properties/test_fyers_broker.py -q --no-cov
python -m pytest tests/hat/conformance/test_conf_broker.py -q --no-cov
```

All adapter checks run on `FakeBroker` or a recording stand-in for the Fyers SDK. No check calls Fyers, and none needs a credential.

## Bug or model error

When a check fails, decide which side is wrong before touching anything.

- TLC prints a counterexample as numbered states. Each action names its function in the `\* code:` comment above it in `OrderReconciliation.tla`. Replay the steps against the real functions (`reserve_order`, `send_order`, `ReconciliationService.run`, `onboard_account`, `SessionRegistry.acquire`). If the code can take them, it is a bug: fix it or record a finding. If it cannot (a guard the model omits), it is a model error: add the guard and say so under "Scope changes". An earlier draft of this model showed an entry placed after a reconcile blocked the bot between stamp and call; the real `send_order` now reads the guard again just before the call, and the model's `PlaceCall` guards `reconciled` and `~blocked`.
- A failing property test prints a falsifying example; a state-machine failure lists the steps. Re-run them by hand against `FakeBroker`.
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

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `ddae500467150a6b8d8f5007b3ffc7df58a64fe7`
Covered digest: `ded95bd98eb5674b02424982a56e0256424f44b8d0b1b1292c3525fcf0b4535a`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- K2 moved the Fyers broker, session, login and token store into `app/hat/execution` and replaced the legacy suites that tested them (`test_fyers_token_store`, `test_fyers_auth_flow`, `test_fyers_client_validate_token`, `test_auth_status_cache`) with the properties above. `fyers_adapter_wraps_existing` and `ops__single_fyers_secret_store` were rewritten for the new files (the SDK only in `fyers_session.py` and `fyers_auth.py`; credential words only in `fyers_auth.py`, `fyers_tokens.py`, `fyers_session.py`) and keep their synthetic-offender checks.
- An order reply that is neither ok nor an explicit error is now `SUBMIT_UNCONFIRMED` (the legacy order path read it as a rejection): a tightening, as is refusing NaN and infinite prices.
- `fyers_session.expired_validation_no_call` needs both expiry guards removed to fail: the `fyers` property and `validate_token` each stop an expired token, so one alone is redundant by design.
- The order payload uses the broker symbol stored in `hat_broker_symbol` (-BE for a trade-for-trade stock) and falls back to the converted -EQ symbol only when the stored symbols cannot be read (`tags.fyers_payload_mapping`, `test_order_goes_out_under_the_stored_broker_symbol`; found in review).

- `OrderReconciliation` models one signal with one entry purpose: signals are independent rows keyed by tag, so one stands for "any number". Exits and stops are not modelled: they never consult the entry block (covered by `tags.refusals_before_the_broker` on the real code).
- `ExecutionRouter!NoEntryWithoutSession` and `ExecutionRouter!AutoNeedsGtt` belong to PR 15 and 16 and the router model is not built here. Session liveness and the confirm/auto gate are proven on the real code (`tags.refusals_before_the_broker`, `broker.confirm_and_auto_need_gtt`). `AutoNeedsGtt` was added to `KNOWN_GAPS` for that reason.
- `CONF mvp_seed_scope` covers the broker half only; PR 8 extends it.
- `gtt_limit_price` raises when the rounded price is not positive (a trigger within about a tick of zero): a refinement of the spec property, not a weakening.
- PR 13 updated harness files covered by `verify-scheduler` without narrowing any check: `MERGED_PRS` includes 13, `KNOWN_GAPS` lost `TLA OrderReconciliation!AtMostOneEntryPerSignal` and gained `TLA ExecutionRouter!AutoNeedsGtt`, and `test_ops.py` gained `test_session_backoff_alert`.

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
E   assert None
E    +  where None = <function search at 0x7f77df10c670>('^class FyersClient\\(FyersSession\\)', '"""\nLegacy Fyers client: the process singleton over HAT\'s `FyersSession`.\n\nHAT owns the session (credentials from... are loaded on next access."""\n    global _fyers_client\n    _fyers_client = None\n    FyersClient._instance = None\n', re.MULTILINE)
E    +    where <function search at 0x7f77df10c670> = re.search
E    +    and   '"""\nLegacy Fyers client: the process singleton over HAT\'s `FyersSession`.\n\nHAT owns the session (credentials from... are loaded on next access."""\n    global _fyers_client\n    _fyers_client = None\n    FyersClient._instance = None\n' = <function test_ops__one_login_one_token_store.<locals>.source at 0x7f77db5421f0>('services/fyers_client.py')
```

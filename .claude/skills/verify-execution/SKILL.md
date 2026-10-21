---
name: verify-execution
description: How to run, read and update the checks behind Hire a Trader's broker interface, the Fyers adapter, order tags, idempotent order submission, reconciliation, account onboarding and the broker session (TLA+ OrderReconciliation, tag, adapter, submission and session properties, broker CONF checks). Use before and after any change to backend/app/hat/execution, backend/app/brokers/plugins/fyers.py, FakeBroker or the OrderReconciliation model.
---

# verify-execution

Checks that an order reaches the broker at most once per tag and only after its attempt is persisted, that an unknown outcome is looked up by tag before any retry, that nothing is sent before a reconciliation or while a mismatch blocks it, that an unreadable broker never reads as an empty one, and that the Fyers adapter is a thin, CNC-only wrapper over the existing client. Specs: `docs/specs/hat/07-execution.md` §8-§11, `docs/specs/hat/10-ops.md` §2 (HAT-OPS-031, 032, 051), HAT-BRK-080, 091, 100, 112, HAT-DEC-I7, I8, I9, I10, BD2, HAT-SCH-053, HAT-EXEC-062, `11-verification.md` (HAT-VER-030..034, 072).

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
- `CONF fyers_adapter_wraps_existing` The adapter reaches Fyers only through `FyersBroker`, `FyersClient` and `FyersAuthService`; nothing under `app/hat` imports the SDK. Harm: a second, untested path to the broker.
- `CONF ops__single_fyers_secret_store` Nothing under `app/hat` reads a Fyers credential or environment variable, and no HAT table holds a broker secret. Harm: a second copy of the Fyers token.
- `CONF mvp_seed_scope` Only the Fyers adapter is registered and onboarding accepts no other broker (PR 8 extends this to the seeded strategies, markets and user). Harm: an unreviewed broker is onboarded.

## Run

```bash
export JAVA_HOME=/path/to/jdk   # Java 17+ (Temurin 21 is what CI uses)
verification/tla/run.sh OrderReconciliation            # the model must pass
verification/tla/run.sh --broken OrderReconciliation   # each variant must fail with its EXPECT line
cd backend
CI=1 python -m pytest tests/hat/properties/test_tags.py tests/hat/properties/test_broker.py \
  tests/hat/properties/test_stops.py tests/hat/properties/test_ops.py -q --no-cov
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
- `backend/app/brokers/plugins/fyers.py`
- `backend/tests/hat/fakes/broker.py`
- `backend/tests/hat/fakes/fyers_sdk.py`

Checks:
- `backend/tests/hat/hat_exec_support.py`
- `backend/tests/hat/properties/test_tags.py`
- `backend/tests/hat/properties/test_broker.py`
- `backend/tests/hat/properties/test_stops.py`
- `backend/tests/hat/properties/test_ops.py`
- `backend/tests/hat/conformance/test_conf_broker.py`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `742e0f09885017e4b63ac0050f2ac4dc9f7a6cff`
Covered digest: `a4495575cb5eb3693f006c5675c4360310ebee60ac562bb1b50269e40c91a6f4`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

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

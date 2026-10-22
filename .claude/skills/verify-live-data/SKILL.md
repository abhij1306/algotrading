---
name: verify-live-data
description: How to run, read and update the checks behind Hire a Trader's live data - the live feed (one provider socket, tick listeners, subscription registry, login hooks), 5-minute bars, opening range, volume pace, feed staleness, pre-open gap capture, the 09:16 gap scan, the "hat" subscription owner, bars_5m Parquet and the NSE announcement feeds (Hypothesis properties feed, bars, gapscan, announcements; CONF single-feed import rule). Use before and after any change to backend/app/hat/data/bars.py, live.py, staleness.py, subscriptions.py, preopen.py, gapscan.py, announcements.py, bars_store.py, minute_history.py, feed.py or provider/*.py.
---

# verify-live-data

Checks that HAT owns the one provider socket and feeds every listener without a bad listener breaking it, that the socket follows the Fyers login and market hours, that every wanted symbol reaches the provider, that 5-minute bars are aligned to 09:15 IST and never change once final, that a silent feed is flagged exactly when the threshold passes, that the 09:16 gap scan selects only candidates that can really gap 10%, and that announcements are stored once and matched by instrument and time only. Specs: `docs/specs/hat/02-data.md` §10-§12 (HAT-DATA-100..104, 110..118, 120..127), `00-overview.md` (HAT-OVR-006), `01-decisions.md` (HAT-DEC-I3, I6, B8..B10), `04-criteria-and-bots.md` (HAT-BOT-052), `06-risk-and-governance.md` (HAT-RISK-063, the health side).

No TLA+ model: `11-verification.md` §3 assigns none to this area. The ordering promises that involve it are `ExecutionRouter!NoOrderWhenStale` and `JobScheduler!IntradayOrder`, which arrive with PR 15 and PR 17.

## Properties

- `PBT bars.alignment` For every tick sequence, in any arrival order, every in-session tick is in exactly the bar starting at 09:15 + 5 min x floor((t - 09:15) / 5 min) in IST, and each bar's OHLCV is the first, max, min and last price and summed volume of its ticks (by exchange time); a tick outside [09:15, 15:30) is in no bar. Harm: an opening-range high or a pivot break is judged on the wrong bar.
- `PBT bars.alignment_finality` No bar is reported before its end plus 2 s, each bar once, and a final bar never changes: a tick arriving at or after the final instant is dropped and counted, so reported bars equal the bars of the on-time ticks. Harm: a trigger fires on a bar that later changes, so live and replay disagree.
- `PBT bars.alignment_boundary_instants` A bar is not final one microsecond before its end plus 2 s and is final at that instant. Harm: the 2-second lag is off by one tick of the clock.
- `PBT bars.opening_range_fixed` Before 09:20:02 there is no opening range; from then it equals the first bar's high and low and never changes, even when first-bar ticks arrive later. Harm: the ORB trigger level drifts and entries are late or never fire.
- `PBT bars.volume_pace` For t in (09:15, 15:30] pace(t) x (elapsed / 375 min) = V exactly, and there is no pace outside that range. Harm: an early trickle projects to a huge pace and fires false breakouts.
- `PBT bars.volume_pace_builder` The builder gives no pace before a symbol's first tick of the session, and afterwards the pace of the day volume of every in-session tick. Harm: pace is read before any data exists.
- `PBT bars.tick_volume_delta` A tick's volume is the change in cumulative day volume, never negative; the first tick contributes only its own print. Harm: bar volume and pace count volume traded before HAT subscribed, or go negative.
- `PBT bars.tick_volume_fallbacks` A tick with no usable cumulative volume contributes its own last traded quantity, and the first tick of a new trade date contributes its own print rather than its total minus the previous day's total. Harm: a print is lost from bar and day volume, or a new session's opening bar is charged yesterday's volume.
- `PBT bars.feed_connection_window` The live feed may connect on a trading day from 09:00 IST to the 15:30 close and never on a weekend. Harm: the 09:00 to 09:08 gap capture receives no ticks, or the feed stays up all night.
- `PBT bars.staleness_trip` The feed is stale at t exactly when t is in market hours and no tick arrived in (t - 30 s, t]; each transition into stale reaches the health sink once and each recovery once. Harm: orders are placed on a frozen tape, or a transient gap leaves a bot disabled.
- `PBT bars.staleness_boundary` Silence of exactly 30 s is stale and 30 s minus one microsecond is not. Harm: the threshold is off by one.
- `PBT bars.staleness_events_recorded` Each transition into stale writes exactly one `feed.stale` event and calls the health kill once; recovery clears the kill and writes nothing. Harm: the owner is never alerted, or is alerted repeatedly.
- `PBT bars.listener_isolation` With any set of listeners on `LiveFeed`, any subset of which raises, every listener that did not raise for a tick got it, in order, and a removed listener gets none. Harm: one bad listener freezes live quotes for every consumer.
- `PBT bars.subscription_set` The `"hat"` owner's set equals watchlist, open positions and candidates; positions are always in it; the provider holds exactly the symbols some owner holds, and releasing HAT never unsubscribes a symbol another owner holds; a set at or above the limit alerts. Harm: an open position stops receiving prices so its exits never fire.
- `PBT bars.subscription_inputs` The inputs are today's watchlist symbols, every open position's symbol (not closed ones, not other days' watchlists) and the candidates. Harm: stale symbols hold feed budget, or an open position is missed.
- `PBT bars.no_store_keeps_no_bars` Without a bar store, final bars reach the listeners and are then forgotten; nothing accumulates. Harm: the long-running loop grows memory all session.
- `PBT bars.persisted_equals_live` The `bars_5m` rows equal the final bars the bar listener saw, one row per (instrument, bar_start), exactly the spec's columns, however writes failed and re-ran. Harm: replays and audits cannot reproduce a live decision.
- `PBT bars.gap_pct` The recorded gap is 100 x (o / c - 1), from the indicative price inside 09:00-09:08 else the first tick's open, and the record names its source; a later first tick never replaces a record. Harm: EP gap filters use a wrong gap.
- `PBT bars.candidates_never_overwrite_refresh` A watchlist refresh that arrives while a candidate add is in progress is not lost: the `"hat"` set holds the refreshed watchlist after both finish. Harm: a refreshed watchlist symbol or an open position stops receiving prices.
- `PBT bars.history_matches_builder` Aggregating a 1-minute series to 5-minute bars (`minute_history.aggregate`) gives exactly the bars the live builder makes of the same prints (open with the volume, high, low, close): same windows, OHLC and volume, nothing outside the session. Harm: EP backtests read bars built differently from live and mislead promotion decisions.
- `PBT bars.minute_history_aggregates_to_5m` The loader asks the injected history call for 1-minute candles in consecutive chunks of at most 100 days covering exactly the period, writes the builder's bars of them to `bars_5m`, and on a failed response for any symbol (a later one included) writes nothing and raises `history_failed` without the broker's text. Harm: a history gap or a broker message reaches the store or the logs.
- `PBT gapscan.covers_universe` The scan evaluates every EQ symbol exactly once with the exact gap, and no other series. Harm: a gapper is missed because a page was truncated.
- `PBT gapscan.filter` The selected set equals {gap >= 10 and universe passes and (band 10 or 20, or F&O)}; a stock with no price-band row is never selected; every selected symbol is handed on to be subscribed. Harm: impossible gaps are chased or valid EP candidates never watched.
- `PBT gapscan.timing_recorded` Every gap-scan run, successful or failed, has a `hat_job_run` row with start and finish, and a run finishing at or after 09:20:00 IST raises an alert. Harm: the EP playbook silently never sees its candidates in time.
- `PBT announcements.poll_schedule` The next poll is 2 minutes after t from 07:30 (inclusive) to 15:30 (exclusive) IST and 15 minutes otherwise. Harm: a 09:10 catalyst is missed by the 09:20 trigger.
- `PBT announcements.poll_schedule_edges` The window edges 07:29:59, 07:30:00, 15:29:59 and 15:30:00. Harm: the cadence changes at the wrong second.
- `PBT announcements.dedupe` For every sequence of polls with overlapping items, stored rows equal distinct GUIDs. Harm: one announcement is attached and shown several times.
- `PBT announcements.dedupe_recorded_fixtures` The recorded feeds: a GUID listed twice stores once, repeated polls add nothing, and an item with no GUID gets a stable derived one. Harm: the real feed shape breaks the dedupe.
- `PBT announcements.unmatched_kept` Every polled item has a row, `raw` equals the item as received, an unmatched item has a null instrument until a later poll finds it. Harm: a catalyst for a renamed stock is lost.
- `PBT announcements.match_window` An item is attached iff its instrument is the candidate's and t - within_hours <= published_at <= t. Harm: a stale or unrelated headline is shown as the reason to take a trade.
- `PBT announcements.parse_size_in_bytes` The feed size limit counts encoded bytes: a document is refused exactly when its UTF-8 size is over the limit. Harm: a multibyte feed far over the memory budget passes as fewer characters.
- `PBT announcements.fetch_bounded` The feed fetcher returns at most the first limit + 1 bytes of a body and stops reading once it has them. Harm: an oversized feed exhausts the process memory before it is refused.
- `PBT feed.session_one_socket` For every interleaving of start, stop, close and SDK reconnect, the session holds at most one open socket, a replaced socket is closed, and every wanted symbol is replayed on each open. Harm: two sockets break the broker session limit.
- `PBT feed.session_gives_up` A connect that keeps failing (an error or a timeout) makes exactly the retry budget of attempts and then ends FAILED. Harm: a dead feed retries for ever, or never tries enough.
- `PBT feed.session_close_stops_a_backing_off_runner` After close or stop a runner that is backing off builds no further socket, however late its retry fires. Harm: a logout is undone by a retry.
- `PBT feed.tick_normalised` A normalised tick keeps the message fields, is keyed by the database symbol, follows `chp` or derives `change_pct` from the previous close, and gets volume from `volume`, `v` or `vol_traded_today`; the input is never modified. Harm: a mis-parsed price or volume triggers a wrong entry or exit.
- `PBT feed.tick_flow` Every provider message with a symbol reaches every listener once as a normalised tick on the loop thread, in order; messages with no symbol or after the loop closed are dropped without raising. Harm: ticks are lost, doubled or raise into the SDK thread.
- `PBT feed.registry_model` The registry's set is the union of the owners' sets, and each delta is exactly the symbols whose owner count went from zero to one or one to zero, in any symbol format. Harm: releasing HAT's symbols unsubscribes one a websocket client still holds.
- `PBT feed.registry_concurrent` With many threads adding and removing the same symbols, the registry ends empty and every symbol was returned as needed as often as released. Harm: a symbol is stuck subscribed for ever or released while held.
- `PBT feed.provider_converges` Whatever order subscribe, unsubscribe, connect, disconnect and SDK reconnect run in, including a change that lands while a provider call is in flight, the provider's net subscribed set equals the registry's once the feed is idle and connected. Harm: a held symbol silently stops ticking, or an unwanted one eats the symbol limit.
- `PBT feed.auth_hooks` For every order of login, logout, token change, monitor reconnect and market-hours change, at most one socket exists, built from the token on file; a login with the market open leaves it connected, a logout closes it and nothing reconnects until the next login, and each hook runs once however often it is registered. Harm: after a re-login the feed streams on a dead token, or after a logout it reconnects on its own.
- `PBT feed.off_hours_stops_a_backing_off_runner` A health check outside market hours stops a runner that is still connecting or backing off, so no socket appears off-hours. Harm: the feed stays up all night.
- `PBT feed.monitor_survives_errors` The monitor loop continues after any exception in a check and counts it. Harm: one transient error ends reconnecting for the day.
- `PBT feed.token_check_off_loop` The Fyers token check of an async connect runs on a worker thread, never the event loop, and an invalid or failing check gives `token_invalid`. Harm: a slow broker call freezes every live quote.
- `PBT feed.socket_factory` The factory builds the SDK socket with the given token, the SDK auto-reconnect on, no log files and exactly the session's four callbacks, and raises `FyersWebSocketError` without the SDK. Harm: a reconnect storm, files written next to the code, or events that never reach the session.
- `PBT feed.calendar_closed` With HAT's trading calendar the feed never connects on a day it lists as a holiday and connects on other weekdays only from 09:00 IST to the 15:30 close; with the calendar missing, unreadable or not wired it connects on no day. Harm: the Fyers socket opens on an exchange holiday, or on a day nobody knows is a trading day.
- `PBT feed.token_source` The provider token is `client_id:access_token` exactly when the Fyers session holds a model, a client id and an access token, and None otherwise. Harm: the socket is built with half a token, or an expired token is retried for ever.
- `PBT feed.traceback_throttle` A failing site writes a full traceback first and then at most once per interval; the other failures are one counted debug line. Harm: a failing tick handler writes a traceback per tick and fills the disk.
- `PBT announcements.text_never_gates` Items identical except title, category text and URL give the same `catalyst_attached` outcome. Harm: a text heuristic becomes an unreviewed trading rule.
- `CONF imports.no_second_websocket` Only `app/hat/data/provider/fyers_websocket.py` imports or names the Fyers data-socket SDK or any other WebSocket library (statically, or by name through `importlib` or `__import__`), nothing outside `app/hat` imports `fyers_apiv3`, and `LiveFeed.add_tick_listener` is the only tick input `live.py` uses. Harm: a second socket breaks the broker session and drops the feed.
- `CONF single_feed_import_rule` Only `feed.py` builds a `ProviderSession` or imports the provider modules, only `main.py` builds a `LiveFeed` and only inside `if is_enabled()`, and only in the body of `if is_enabled()` (not under a negated or nested `if not is_enabled()`); the only importer of `feed.py` is `main.py` (K5b deleted the legacy read side). Harm: a second feed or session could open a socket HAT does not control, or open one with HAT off.
- `CONF no_duplicate_fakes_or_authorities` Across `backend/` HAT has one `FakeBroker` (`tests/hat/fakes/broker.py`) and one `evaluate_signal` (`app/hat/risk/service.py`); no legacy copy is left (K4 deleted the legacy `FakeBroker` re-export and `RiskManager`, K5b the legacy `LiveMarketService`; a class named `RiskManager` or `LiveMarketService` fails the check), and no other class is named as a risk authority or live-tick service. Harm: two risk authorities or two fakes drift apart and a check passes against the wrong one.
- `PBT bars.trigger_only_on_final_bars` With prints arriving late or out of order through the real bar builder, a pivot breakout fires once, at the final instant (end + 2 s) of the first final bar in [09:20, 15:20] whose high is above the pivot, never from a print in a bar still open or a late print the bar never took, and never at an instant before the bar was handed over; the elapsed share of the session is read at that same final instant, where the day volume is read; and (`test_failed_signal_write_retries`) a failed signal write releases the key so the next final bar fires again, while a stored signal closes it for the day. Harm: an entry fires on a print the 5-minute bar later disowns, or before the bar that justifies it exists.
- `PBT bars.stale_feed_trips_health_kill` Wired as in production (staleness monitor, `EventFeedHealth`, `DataHealthKill`), the account's data-health kill is tripped exactly while the feed is stale and clears by itself when ticks resume, even when the trip and the recovery fall on one instant and (`test_health_kill_writes_out_of_order`) whatever order the worker threads write the changes in; a trigger fired while stale is stored Rejected with `stale_feed` and opens nothing, one on a live feed opens its virtual position. Harm: entries keep being taken on prices that stopped updating, or the account stays manage-only after the feed is back.
- `PBT bars.restore_matches_live` After a mid-session restart, a bar builder given the day's stored final bars and then the rest of the ticks has the same opening ranges and day volumes as one that saw every tick, and a restored bar never replaces a live one. Harm: after a restart the opening range is lost and no ORB entry fires that day.

## Run

```bash
cd backend
CI=1 python -m pytest tests/hat/properties/test_feed.py tests/hat/properties/test_bars.py tests/hat/properties/test_gapscan.py \
  tests/hat/properties/test_announcements.py -q --no-cov     # about 40 s under the ci profile
python -m pytest tests/hat/conformance/test_conf_imports.py -q --no-cov
```

No network, broker or database server is touched: ticks come from generated payloads, MCP rows from `PricedListingServer`, feeds from `rss_item`/`rss_document` and two recorded fixtures (`rss_announcements.xml`, `rss_financial_results.xml`, `mcp_cm_equity_stocks_reli.json`), and the database is in-memory SQLite.

## Bug or model error

A failing property prints the falsifying example and a `reproduce_failure` blob. Replay the example by hand with the real functions first.

- Bars: feed the printed ticks to `BarBuilder.on_tick(tick, now)` and `advance(now)`. If the bar the code returns disagrees with the spec rule (bar start by integer arithmetic on seconds of the IST day, open and close by exchange time, a tick arriving at or after end + 2 s is late), it is a bug in `bars.py`. The oracle `expected_bars` is the spec rule, not the code: change it only if the spec changes.
- Staleness: the rule is the property's, "no tick in (t - 30 s, t]". The statement in 02 §11 says "more than 30 seconds"; the two disagree at exactly 30 s and the acceptance wins (recorded under Scope changes).
- Listener isolation and the feed: a failure means an exception reached another listener or the SDK thread, or the socket, replay or hooks disagree with the model. Fix `LiveFeed` (`feed.py`) or `ProviderSession`, never the property. The state-machine properties print the failing rule sequence; replay it against `make_feed` in `tests/hat/fakes/data_socket.py`.
- Gap scan: the F&O rule is `[OWNER-PENDING]` (a known price-band row with a null `band_pct`). If the owner changes it, change the spec first, then `Gapper` and the property together.
- Announcements: the feed field names (`symbol`, `isin`, `company`, `category`, the `Company - Subject` title, `pubDate`) and URLs are `[A]`, built from recorded fixtures only. A live poll that shows different names is a model error in the parser and the fixtures, not a failure of these properties.
- Never weaken a property or shrink a strategy to pass; record a narrowing under "Scope changes".

## Updating

- A new tick rule or bar rule: change `docs/specs/hat/02-data.md` first, then `bars.py`, the oracle in `test_bars.py` and this skill.
- A new feed field or gap rule: add a recorded fixture and extend the matching property; never read live feeds in a test.
- Every new check needs a recorded break under "Scratch breaks" (HAT-VER-072) and a Properties bullet naming its harm.
- After re-confirming everything, run `python backend/tests/hat/conformance/hat_conf_skills.py verify-live-data` and update the sync commit and digest below.

## Coverage

Code under check:
- `backend/app/hat/data/bars.py`: `BarBuilder`, `Tick`, `Bar`, `volume_pace`
- `backend/app/hat/data/bars_store.py`: `BarStore`
- `backend/app/hat/data/staleness.py`: `StalenessMonitor`
- `backend/app/hat/data/subscriptions.py`: `LiveSubscriptions`, `load_inputs`
- `backend/app/hat/data/preopen.py`: `PreopenGaps`, `gap_pct`, `meets_gap`
- `backend/app/hat/data/gapscan.py`: `scan_rows`, `select_gappers`, `run_gap_scan`
- `backend/app/hat/data/announcements.py`: `parse_feed`, `store_items`, `attached_announcements`, `catalyst_attached`, `next_poll_at`, `poll_feed`
- `backend/app/hat/data/live.py`: `LiveData`, `EventFeedHealth`, `tick_from_payload`
- `backend/app/hat/engine/trigger.py`: `TriggerMonitor`, `in_window`
- `backend/app/hat/engine/live_entry.py`: `TriggerService`, `DataHealthKill`
- `backend/app/hat/data/sessions.py`: `MarketHoursPolicy`
- `backend/app/hat/clock.py`: `FixedClock`, `require_aware`
- `backend/app/hat/data/feed.py`: `LiveFeed`, `connect_status_message`
- `backend/app/hat/data/provider/data_socket.py`: `DataSocket`, `SocketCallbacks`
- `backend/app/hat/data/provider/provider_session.py`: `ProviderSession`, `RetryPolicy`
- `backend/app/hat/data/provider/ticks.py`: `normalize_tick`
- `backend/app/hat/data/provider/metrics.py`: `FeedMetrics`, `log_exception_throttled`
- `backend/app/hat/data/provider/fyers_websocket.py`: `fyers_socket_factory`, `fyers_token`

- `backend/app/hat/data/minute_history.py`: `parse_candles`, `candle_prints`, `aggregate`, `load_minute_history`

Checks:
- `backend/tests/hat/properties/test_feed.py`
- `backend/tests/hat/properties/test_bars.py`
- `backend/tests/hat/properties/test_gapscan.py`
- `backend/tests/hat/properties/test_announcements.py`
- `backend/tests/hat/conformance/test_conf_imports.py`
- `backend/tests/hat/conformance/test_conf_authorities.py`
- `backend/tests/hat/hat_live_support.py`
- `backend/tests/hat/hat_trigger_support.py`
- `backend/tests/hat/fakes/data_socket.py`
- `backend/tests/hat/fixtures/rss_announcements.xml`
- `backend/tests/hat/fixtures/rss_financial_results.xml`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `d0ba323bb8d38329ecc3e61d5208a2ec0db15943`
Covered digest: `91b626d09cbc64870b86ec658634f9cc750029242f9071e72b46af8a1514e3d7`

## Scope changes

- PR 12 added the minute-history loader (HAT-DATA-119). `FyersClient.get_historical_data` named by the spec does not exist; the loader takes the history call as an injected function, so the properties fake it and no check reaches Fyers.

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `bars.staleness_trip` and `bars.staleness_boundary`: HAT-DATA-116's statement said "more than 30 seconds" and its acceptance said no tick in (t - 30 s, t], which disagree at exactly 30 s. The acceptance wins; the statement in `02-data.md` was edited to "30 seconds or more".
- The session is the half-open interval [09:15, 15:30) IST; a tick at 15:30:00 or later is in no bar.
- A late tick (at or after its bar's end + 2 s) is left out of bars but still counts in the day volume, so volume pace stays right.
- `bars.persisted_equals_live` uses a reduced `max_examples` and bounded sequences, because each example builds a Parquet directory.
- `announcements.text_never_gates` draws text of at most 10 characters, at most 8 stored items, 40 examples and has no shrink phase: a broken variant fails on almost every example and shrinking over arbitrary text grew without bound. The failing example is still printed, unminimised. Database engines are disposed as examples go.
- Pre-open field names (`indicative_open_price`, `pre_open_price`) and the RSS field names and URLs are `[A]`, built from recorded fixtures only.
- `bars.history_matches_builder` (HAT-DATA-119) belongs to PR 12 and is not checked here.
- K3 moved the data socket, the session, the tick normaliser and the subscription registry into HAT (`app/hat/data/feed.py`, `provider/`, `subscriptions.py`) and deleted their legacy tests; each is a `feed.*` property here. The session, registry, convergence and login-hook checks are Hypothesis state machines, not TLA+ models, because the promises are about one process and the fake socket is the whole environment. The fake provider forgets its subscriptions when the SDK reconnects (`FakeDataSocket.sdk_reconnect` clears them), which is the assumption the replay is checked against; a live check that Fyers keeps them would make the replay redundant but harmless.
- `bars.listener_isolation` moved from `LiveMarketService._notify_tick_listeners` to `LiveFeed._deliver`; it no longer checks the legacy `latest_values` and broadcast batch, which are the legacy read side's own tests in `test_live_market_regressions.py`. `bars.feed_connection_window` moved the same way. `CONF imports.no_second_websocket` now allows the SDK socket in exactly one HAT module instead of none.
- K5b deleted the legacy `LiveMarketService`, `ws_manager`, the websocket and market routes and their tests (`test_live_market_regressions.py` and the stream tests). `CONF no_duplicate_fakes_or_authorities` lost its only `LEGACY_COPIES` entry (`LiveMarketService`; the list is empty and the lookalike check still refuses a class by that name) and `CONF single_feed_import_rule` lost `services/live_market_service.py` from `FEED_IMPORTERS`. Both are stricter, not narrower. `subscriptions.API_OWNER` (the legacy REST owner) was deleted; the registry property now draws a third client owner instead.
- Review found `main.py` built the feed with no calendar, so weekday exchange holidays were connectable. The feed now takes its hours policy from HAT's trading calendar (`calendar_policy_source`, read at most every 10 minutes) and is closed while the calendar is missing, unreadable or not wired; `bars.feed_connection_window` keeps checking the weekday rule with a fixed empty-holiday policy and `feed.calendar_closed` checks the calendar cases.
- `feed.registry_concurrent` raises the thread switch frequency for its run so an unguarded update shows; it draws at most 6 examples.
- K2 edited `test_conf_imports.py` (the `fyers_apiv3.fyersModel` REST import is allowed under `app/hat/execution`; sockets stay forbidden, with synthetic-offender checks) and `tests/test_subscription_registry.py` (`canonicalize` is now a required argument, passed `symbol_master.to_db`): no check narrowed, and the existing live and stream tests above stay green.
- PR 15b widened the live trigger path (`engine/live_entry.py`): `watchlist_candidates` and `ep_bot_candidates` take every active bot with a live version and its effective mode (with an optional `calendar`), and `submit_trigger` and `TriggerService` take a `route`. The bar and trigger properties pass unchanged; none was narrowed.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code (HAT-VER-072), then the break was removed and the check returned to passing.

### PBT bars.alignment

Break: in `app/hat/data/bars.py`, replaced `if key < self.open_key:` with `if key > self.open_key:`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:118: in test_alignment
tests/hat/properties/test_bars.py:131: in test_alignment
E   AssertionError: assert {('AAA', date...'), volume=0)} == {('AAA', date...'), volume=0)}
E     
E     Differing items:
```

### PBT bars.alignment_finality

Break: in `app/hat/data/bars.py`, replaced `if start + BAR_WIDTH + BAR_FINAL_LAG <= now: self.late_ticks += 1 return TickOutcome.LATE` with `(removed)`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:163: in test_alignment_finality
tests/hat/properties/test_bars.py:184: in test_alignment_finality
E   AssertionError: assert <TickOutcome.ACCEPTED: 'accepted'> is <TickOutcome.LATE: 'late'>
E    +  where <TickOutcome.LATE: 'late'> = TickOutcome.LATE
E   Failing test case: test_alignment_finality(
```

### PBT bars.alignment_boundary_instants

Break: in `app/hat/data/bars.py`, replaced `BAR_FINAL_LAG = timedelta(seconds=2)` with `BAR_FINAL_LAG = timedelta(seconds=1)`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:197: in test_alignment_boundary_instants
tests/hat/properties/test_bars.py:209: in test_alignment_boundary_instants
E   AssertionError: assert [Bar(symbol='...'), volume=1)] == []
E     
E     Left contains one more item: Bar(symbol='AAA', start=datetime.datetime(2026, 10, 21, 9, 15, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')), open=
```

### PBT bars.opening_range_fixed

Break: in `app/hat/data/bars.py`, replaced `if start + BAR_WIDTH + BAR_FINAL_LAG <= now: self.late_ticks += 1` with `if start + BAR_WIDTH + BAR_FINAL_LAG <= now: self.late_ticks += 1 fixed = self._opening.ge`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:217: in test_opening_range_fixed
tests/hat/properties/test_bars.py:260: in test_opening_range_fixed
E   AssertionError: assert (Decimal('0.0...mal('0.0001')) == (Decimal('0.0...mal('0.0002'))
E     
E     At index 1 diff: Decimal('0.0001') != Decimal('0.0002')
```

### PBT bars.volume_pace

Break: in `app/hat/data/bars.py`, replaced `return Fraction(cum_volume * ((closes - opens) // micro), (local - opens) // micro)` with `return Fraction(cum_volume * ((local - opens) // micro), (closes - opens) // micro)`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:267: in test_volume_pace
tests/hat/properties/test_bars.py:276: in test_volume_pace
E   assert (Fraction(1, 22500000000) * Fraction(1, 22500000000)) == 1
E    +  where Fraction(1, 22500000000) = Fraction((33300000001 - (33300 * 1000000)), ((375 * 60) * 1000000))
E   Failing test case: test_volume_pace(
```

### PBT bars.volume_pace_builder

Break: in `app/hat/data/bars.py`, replaced `if state is None or state.ticks == 0: return None return volume_pace(` with `state = state or _DayState() return volume_pace(`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:281: in test_volume_pace_builder
tests/hat/properties/test_bars.py:290: in test_volume_pace_builder
E   AssertionError: assert Fraction(0, 1) is None
E    +  where Fraction(0, 1) = volume_pace('AAA', datetime.datetime(2026, 10, 21, 9, 15, 0, 1, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')))
E    +    where volume_pace = BarBuilder(session_open=datetime.time(9, 15), session_close=datetime.time(15, 30), late_ticks=0, _open_bars={}, _opening
```

### PBT bars.tick_volume_delta

Break: in `app/hat/data/live.py`, replaced `qty = total - previous` with `qty = total`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:303: in test_tick_volume_delta
tests/hat/properties/test_bars.py:326: in test_tick_volume_delta
E   assert 1 == (1 - 1)
E    +  where 1 = sum([1])
E   Failing test case: test_tick_volume_delta(
```

### PBT bars.staleness_trip

Break: in `app/hat/data/staleness.py`, replaced `self.check(now) if self._last_tick is None or now > self._last_tick:` with `if self._last_tick is None or now > self._last_tick:`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:349: in test_staleness_trip
tests/hat/properties/test_bars.py:378: in test_staleness_trip
E   assert 0 == 1
E    +  where 0 = len([])
E    +    where [] = <test_bars.Sink object at 0x7f61ad3c0fb0>.stale
```

### PBT bars.staleness_boundary

Break: in `app/hat/data/staleness.py`, replaced `now - self._last_tick >= self._stale_after` with `now - self._last_tick > self._stale_after`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:390: in test_staleness_boundary
tests/hat/properties/test_bars.py:400: in test_staleness_boundary
E   AssertionError: assert False == (30000000 >= (30 * 1000000))
E    +  where False = is_stale((datetime.datetime(2026, 10, 21, 10, 0, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) + datetime.timedelta(seconds=30))
E    +    where is_stale = <app.hat.data.staleness.StalenessMonitor object at 0x7f32ba382cf0>.is_stale
```

### PBT bars.staleness_events_recorded

Break: in `app/hat/data/live.py`, replaced `self._notify(True) silent` with `silent`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:402: in test_staleness_events_recorded
tests/hat/properties/test_bars.py:434: in test_staleness_events_recorded
E   assert 0 == 1
E    +  where 0 = <built-in method count of list object at 0x7ff8da05aa00>(True)
E    +    where <built-in method count of list object at 0x7ff8da05aa00> = [False].count
```

### PBT bars.listener_isolation

Break: in `app/hat/data/feed.py`, replaced `try: listener(tick) except Exception as exc: # isolate: never break the stream for a listener's bug feed_metrics.increment("feed.listener.errors") log_exception_thrott...` with `listener(tick)`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:504: in test_listener_isolation
tests/hat/properties/test_bars.py:531: in test_listener_isolation
tests/hat/properties/test_bars.py:500: in __call__
E   RuntimeError: listener bug
E   Failing test case: test_listener_isolation(
E       # The test always failed when commented parts were varied together.
E       symbols=['AAA'],  # or any other generated value
E       raising=[{0}],
E       always_raise=False,  # or any other generated value
E   )
E   Explanation:
E       These lines were always and only run by failing test cases:
E           tests/hat/properties/test_bars.py:500
```

### PBT bars.subscription_set

Break: in `app/hat/data/subscriptions.py`, replaced `return inputs.watchlist | inputs.positions | inputs.candidates` with `return inputs.watchlist | inputs.candidates`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:520: in test_subscription_set
tests/hat/properties/test_bars.py:570: in test_subscription_set
E   AssertionError: assert set() == {'S0'}
E     
E     Extra items in the right set:
```

### PBT bars.subscription_inputs

Break: in `app/hat/data/subscriptions.py`, replaced `.where(HatPosition.closed_at.is_(None))` with `(removed)`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:581: in test_subscription_inputs_from_database
tests/hat/properties/test_bars.py:656: in test_subscription_inputs_from_database
E   AssertionError: assert frozenset({'SYM0'}) == set()
E     
E     Extra items in the left set:
```

### PBT bars.persisted_equals_live

Break: in `app/hat/data/live.py`, replaced `pending = list(self._unsaved) try:` with `pending = list(self._unsaved) self._unsaved.clear() try:`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:687: in test_persisted_equals_live
tests/hat/properties/test_bars.py:750: in test_persisted_equals_live
E   AssertionError: assert {} == {('189e0082-a...'0.0001'), 0)}
E     
E     Right contains 1 more item:
```

### PBT bars.gap_pct

Break: in `app/hat/data/preopen.py`, replaced `return Decimal(100) * (open_price / prev_close - 1)` with `return Decimal(100) * (open_price - prev_close) / open_price`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:766: in test_gap_pct
tests/hat/properties/test_bars.py:798: in test_gap_pct
E   AssertionError: assert Fraction(50, 1) < Fraction(1, 100000000000000000000)
E    +  where Fraction(50, 1) = abs((Fraction(50, 1) - Fraction(100, 1)))
E    +    where Fraction(50, 1) = Fraction(Decimal('50'))
```

### PBT gapscan.covers_universe

Break: in `app/hat/data/gapscan.py`, replaced `pct = gap_pct(opened, prev) if opened is not None and prev is not None else None` with `pct = gap_pct(prev, opened) if opened is not None and prev is not None else None`.

```
tests/hat/properties/test_gapscan.py F
tests/hat/properties/test_gapscan.py:58: in test_covers_universe
tests/hat/properties/test_gapscan.py:78: in test_covers_universe
E   AssertionError: assert Fraction(9996699669966996699669967299, 75700000000000000000000000000) < Fraction(1, 100000000000000000000)
E    +  where Fraction(9996699669966996699669967299, 75700000000000000000000000000) = abs((Fraction(-6600660066006600660066007, 1000000000000000000000
E    +    where Fraction(-6600660066006600660066007, 100000000000000000000000000) = Fraction(Decimal('-0.0660066006600660066006600700'))
```

### PBT gapscan.filter

Break: in `app/hat/data/gapscan.py`, replaced `EP_BANDS = frozenset({Decimal(10), Decimal(20)})` with `EP_BANDS = frozenset({Decimal(5), Decimal(10), Decimal(20)})`.

```
tests/hat/properties/test_gapscan.py F
tests/hat/properties/test_gapscan.py:99: in test_filter
tests/hat/properties/test_gapscan.py:148: in test_filter
E   AssertionError: assert {'A'} == set()
E     
E     Extra items in the left set:
```

### PBT gapscan.timing_recorded

Break: in `app/hat/data/gapscan.py`, replaced `GAP_SCAN_DEADLINE = time(9, 20)` with `GAP_SCAN_DEADLINE = time(9, 21)`.

```
tests/hat/properties/test_gapscan.py F
tests/hat/properties/test_gapscan.py:155: in test_timing_recorded
tests/hat/properties/test_gapscan.py:186: in test_timing_recorded
E   AssertionError: assert [] == ['gapscan_late']
E     
E     Right contains one more item: 'gapscan_late'
```

### PBT announcements.poll_schedule

Break: in `app/hat/data/announcements.py`, replaced `FAST_FROM = time(7, 30)` with `FAST_FROM = time(7, 0)`.

```
tests/hat/properties/test_announcements.py F
tests/hat/properties/test_announcements.py:42: in test_poll_schedule
tests/hat/properties/test_announcements.py:52: in test_poll_schedule
E   assert (datetime.datetime(2026, 1, 1, 1, 32, tzinfo=datetime.timezone.utc) - datetime.datetime(2026, 1, 1, 1, 30, tzinfo=datetime.timezone.utc)) =
E    +  where datetime.datetime(2026, 1, 1, 1, 32, tzinfo=datetime.timezone.utc) = next_poll_at(datetime.datetime(2026, 1, 1, 1, 30, tzinfo=datetime.t
E    +  and   datetime.timedelta(seconds=900) = timedelta(minutes=15)
```

### PBT announcements.poll_schedule_edges

Break: in `app/hat/data/announcements.py`, replaced `FAST_FROM <= local < FAST_UNTIL` with `FAST_FROM <= local <= FAST_UNTIL`.

```
tests/hat/properties/test_announcements.py F
tests/hat/properties/test_announcements.py:66: in test_poll_schedule_edges
E   AssertionError: assert (datetime.datetime(2026, 10, 21, 15, 32, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) - datetime.datetime(2026, 10, 21, 15
E    +  where datetime.datetime(2026, 10, 21, 15, 32, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) = next_poll_at(datetime.datetime(2026, 10, 21, 15,
E    +  and   datetime.timedelta(seconds=900) = timedelta(minutes=15)
FAILED tests/hat/properties/test_announcements.py::test_poll_schedule_edges - AssertionError: assert (datetime.datetime(2026, 10, 21, 15, 32, tzinfo=z
```

### PBT announcements.dedupe

Break: in `app/hat/data/announcements.py`, replaced `guid=guid, feed=item.feed,` with `guid=guid + str(uuid.uuid4()), feed=item.feed,`.

```
tests/hat/properties/test_announcements.py F
tests/hat/properties/test_announcements.py:101: in test_dedupe
tests/hat/properties/test_announcements.py:112: in test_dedupe
E   AssertionError: assert 2 == 1
E    +  where 2 = scalar(<sqlalchemy.sql.selectable.Select object at 0x7f7d45f03f00>)
E    +    where scalar = <sqlalchemy.orm.session.Session object at 0x7f7d47b02e50>.scalar
```

### PBT announcements.dedupe_recorded_fixtures

Break: in `app/hat/data/announcements.py`, replaced `guid=guid, feed=item.feed,` with `guid=guid + str(uuid.uuid4()), feed=item.feed,`.

```
tests/hat/properties/test_announcements.py F
tests/hat/properties/test_announcements.py:134: in test_dedupe_recorded_fixtures
E   AssertionError: assert {'announcemen...l_results': 6} == {'announcemen...l_results': 2}
E     
E     Differing items:
E     {'financial_results': 6} != {'financial_results': 2}
```

### PBT announcements.unmatched_kept

Break: in `app/hat/data/announcements.py`, replaced `row = HatAnnouncement(` with `if instrument_id is None: continue row = HatAnnouncement(`.

```
tests/hat/properties/test_announcements.py F
tests/hat/properties/test_announcements.py:143: in test_unmatched_kept
tests/hat/properties/test_announcements.py:167: in test_unmatched_kept
E   AssertionError: assert set() == {'g0'}
E     
E     Extra items in the right set:
```

### PBT announcements.match_window

Break: in `app/hat/data/announcements.py`, replaced `HatAnnouncement.published_at <= at,` with `(removed)`.

```
tests/hat/properties/test_announcements.py F
tests/hat/properties/test_announcements.py:195: in test_match_window
tests/hat/properties/test_announcements.py:220: in test_match_window
E   AssertionError: assert {'g0'} == set()
E     
E     Extra items in the left set:
```

### PBT announcements.text_never_gates

Break: in `app/hat/data/announcements.py`, replaced `HatAnnouncement.published_at <= at,` with `HatAnnouncement.published_at <= at, HatAnnouncement.title.like("%e%"),`.

```
tests/hat/properties/test_announcements.py F
tests/hat/properties/test_announcements.py:247: in test_text_never_gates
tests/hat/properties/test_announcements.py:299: in test_text_never_gates
E   AssertionError: assert {'AAA': (True...: (False, [])} == {'AAA': (Fals...True, ['g1'])}
E     
E     Omitting 1 identical items, use -vv to show
```

### CONF imports.no_second_websocket

Break: in `app/hat/data/live.py`, replaced `import asyncio` with `import asyncio from fyers_apiv3.FyersWebsocket import data_ws # noqa`.

```
tests/hat/conformance/test_conf_imports.py F
tests/hat/conformance/test_conf_imports.py:179: in test_imports__no_second_websocket
E   AssertionError: assert [('live.py', ...', 'data_ws')] == []
E     
E     Left contains 3 more items, first extra item: ('live.py', 'fyers_apiv3.FyersWebsocket')
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_imports.py::test_imports__no_second_websocket
```

### CONF single_feed_import_rule

Break: in `app/hat/data/subscriptions.py`, replaced `import logging import threading` with `import logging import threading from .provider.provider_session import ProviderSession # noqa`.

```
tests/hat/conformance/test_conf_imports.py F
tests/hat/conformance/test_conf_imports.py:250: in test_single_feed_import_rule
E   AssertionError: hat/data/subscriptions.py
E   assert not True
E    +  where True = any(<generator object test_single_feed_import_rule.<locals>.<genexpr> at 0x7f67b304de00>)
FAILED tests/hat/conformance/test_conf_imports.py::test_single_feed_import_rule
```

Second break (nested negated guard): in `app/main.py`, replaced `if is_enabled(): _start_hat_feed(app)` with `if is_enabled(): if not is_enabled(): _start_hat_feed(app)`; the output is `AssertionError: assert False where False = _guarded_by_hat_enabled(Module(body=[...`.

Break (K5b, `FEED_IMPORTERS` is `main.py` only): recreated `app/services/live_market_service.py` containing `from app.hat.data import feed`.


```
tests/hat/conformance/test_conf_imports.py:274: in test_single_feed_import_rule
E   AssertionError: services/live_market_service.py
E   assert 'services/live_market_service.py' in {'main.py'}
FAILED tests/hat/conformance/test_conf_imports.py::test_single_feed_import_rule
```

### CONF no_duplicate_fakes_or_authorities

Break: appended `class FakeBroker: pass` to `tests/hat/fakes/fyers_sdk.py`.

```
tests/hat/conformance/test_conf_authorities.py F                         [100%]
tests/hat/conformance/test_conf_authorities.py:42: in test_no_duplicate_fakes_or_authorities
E   AssertionError: ('FakeBroker', ['tests/hat/fakes/broker.py', 'tests/hat/fakes/fyers_sdk.py'])
E   assert ['tests/hat/f...fyers_sdk.py'] == ['tests/hat/fakes/broker.py']
E     
E     Left contains one more item: 'tests/hat/fakes/fyers_sdk.py'
E     Use -v to get more diff
FAILED tests/hat/conformance/test_conf_authorities.py::test_no_duplicate_fakes_or_authorities
```

Break (K5b, `LEGACY_COPIES` emptied): recreated `app/services/live_market_service.py` with `class LiveMarketService: pass`.


```
tests/hat/conformance/test_conf_authorities.py F
E   AssertionError: [('LiveMarketService', 'app/services/live_market_service.py')]
E   assert [('LiveMarket..._service.py')] == []
```

### PBT bars.candidates_never_overwrite_refresh

Break: in `app/hat/data/subscriptions.py`, `add_candidates` reads the current inputs without the update lock.

```
tests/hat/properties/test_bars.py:575: in test_candidates_never_overwrite_refresh
E   AssertionError: assert {'S0', 'S1'} <= {'S0'}
E     Extra items in the left set:
E     'S1'
```

### PBT announcements.fetch_bounded

Break: in `app/hat/data/announcements.py`, the loop in `_take` no longer stops once the limit is reached.

```
tests/hat/properties/test_announcements.py:89: in test_fetch_bounded
E   AssertionError: assert b'   ' == b'  '
```

### PBT bars.tick_volume_fallbacks

Break: in `app/hat/data/live.py`, the same-trade-date test was removed from the delta rule (yesterday's total is subtracted), and, second break, the fallback to the print's own quantity was set to 0.

```
tests/hat/properties/test_bars.py:374: in test_tick_volume_fallbacks
E   AssertionError: assert 0 == 1
E    +  where 0 = Tick(symbol='AAA', ... qty=0)   (second break: line 370, the same assertion)
```

### PBT bars.no_store_keeps_no_bars

Break: in `app/hat/data/live.py`, `step` appends final bars to the unsaved list even with no store.

```
tests/hat/properties/test_bars.py:347: in test_no_store_keeps_no_bars
E   assert 1 == 0
E    +  where 1 = <LiveData object>.pending_bars
E   Failing test case: test_no_store_keeps_no_bars(offsets=[33_300_000_000])
```

### PBT announcements.parse_size_in_bytes

Break: in `app/hat/data/announcements.py`, the size check counts characters instead of encoded bytes.

```
tests/hat/properties/test_announcements.py:104: in test_parse_size_in_bytes
E   Failed: DID NOT RAISE FeedError
E   Failing test case: test_parse_size_in_bytes(words=1, extra=-1)
```

### PBT bars.feed_connection_window

Break: in `app/hat/data/feed.py`, replaced `PREOPEN_FROM = clock_time(9, 0)` with `PREOPEN_FROM = clock_time(9, 15)`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:563: in test_feed_connection_window
tests/hat/properties/test_bars.py:578: in test_feed_connection_window
E   assert False == True
E    +  where False = connection_allowed()
E    +    where connection_allowed = <app.hat.data.feed.LiveFeed object at 0x7f9a5a116710>.connection_allowed
E   (note: 1 other explicit example also failed with this error; use Verbosity.verbose to view)
E   Failing explicit example: test_feed_connection_window(
E       second=32400,
E       weekend=False,
E   )
FAILED tests/hat/properties/test_bars.py::test_feed_connection_window - asser [cut]
```

### PBT feed.session_one_socket

Break: in `app/hat/data/provider/provider_session.py`, replaced `self._close_quietly(old)` with `pass`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:238: in test_session_one_socket
tests/hat/properties/test_feed.py:218: in at_most_one_open_socket_and_none_leaked
E   assert 2 == 1
E    +  where 2 = len([<hat.fakes.data_socket.FakeDataSocket object at 0x7f2579458cb0>, <hat.fakes.data_socket.FakeDataSocket object at 0x7f25797dd860>])
E   Failing test case: <state machine>
E   state = SessionMachine()
E   state.at_most_one_open_socket_and_none_leaked()
E   state.state_agrees_with_the_socket()
E   state.start()
E   state.at_most_one_open_socket_and_none_leaked()
E   state.state_agrees_with_the_socket()
E   state.drop()
E   state.at_most_one_open_socket_and_none_leaked()
```

### PBT feed.session_gives_up

Break: in `app/hat/data/provider/provider_session.py`, replaced `if failures >= self.retry.max_attempts:` with `if failures > self.retry.max_attempts:`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:245: in test_session_gives_up
tests/hat/properties/test_feed.py:273: in test_session_gives_up
E   assert 2 == 1
E    +  where 2 = len([1, 1])
E   Failing test case: test_session_gives_up(
E       # The test always failed when commented parts were varied together.
E       mode='never_up',  # or any other generated value
E       attempts=1,  # or any other generated value
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEAQQE=') as a decorator on your test function
FAILED tests/hat/properties/test_feed.py::test_session_gives_up - assert 2 == 1
```

### PBT feed.session_close_stops_a_backing_off_runner

Break: in `app/hat/data/provider/provider_session.py`, replaced `self._stop.set() self._state = final_state` with `self._state = final_state`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:280: in test_session_close_stops_a_backing_off_runner
tests/hat/properties/test_feed.py:301: in test_session_close_stops_a_backing_off_runner
E   assert not True
E    +  where True = is_alive()
E    +    where is_alive = <Thread(Thread-6 (<lambda>), started daemon 140689279010496)>.is_alive
E   Failing test case: test_session_close_stops_a_backing_off_runner(
E       late_reconnects=0,  # or any other generated value
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEA') as a decorator on your test function
FAILED tests/hat/properties/test_feed.py::test_session_close_stops_a_backing_off_runner
```

### PBT feed.tick_normalised

Break: in `app/hat/data/provider/ticks.py`, replaced `tick_db["change_pct"] = tick_db["chp"]` with `tick_db["change_pct"] = -tick_db["chp"] - 1`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:330: in test_tick_normalised
tests/hat/properties/test_feed.py:348: in test_tick_normalised
E   assert -1 == 0
E   Failing test case: test_tick_normalised(
E       tick={'symbol': 'NSE:SBIN-EQ', 'chp': 0},
E   )
E   Explanation:
E       These lines were always and only run by failing test cases:
E           app/hat/data/provider/ticks.py:21
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AAFBAUEAQQAA') as a decorator on your test function
FAILED tests/hat/properties/test_feed.py::test_tick_normalised - assert -1 == 0
```

### PBT feed.tick_flow

Break: in `app/hat/data/feed.py`, replaced `normalize_tick(message, self._symbols.to_db(provider_symbol))` with `normalize_tick(message, provider_symbol)`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:377: in test_tick_flow
tests/hat/properties/test_feed.py:407: in test_tick_flow
tests/hat/properties/test_feed.py:403: in scenario
E   AssertionError: assert [{'symbol': '...seq': 0, ...}] == [{'symbol': '...seq': 0, ...}]
E     
E     At index 0 diff: {'symbol': 'NSE:SBIN-EQ', 'ltp': 1.0, 'v': 0, 'seq': 0, 'volume': 0} != {'symbol': 'SBIN', 'ltp': 1.0, 'v': 0, 'seq': 0, 'volume': 0}
E     Use -v to get more diff
E   Failing test case: test_tick_flow(
E       # The test always failed when commented parts were varied together.
E       messages=[(lambda symbol, ltp, seq: {'symbol': symbol, 'ltp': ltp, 'v': seq, 'seq': seq})(
E            'NSE:SBIN-EQ',
E            1.0,  # or any other generated value
E            0,  # or any other generated value
```

### PBT feed.registry_model

Break: in `app/hat/data/subscriptions.py`, replaced `if count == 0: newly_needed.add(symbol)` with `if True: newly_needed.add(symbol)`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:486: in test_registry_model
tests/hat/properties/test_feed.py:439: in add
E   AssertionError: assert {'SBIN'} == set()
E     
E     Extra items in the left set:
E     'SBIN'
E     Use -v to get more diff
E   Failing test case: <state machine>
E   state = RegistryMachine()
E   state.registry_equals_the_model()
E   state.add(owner='ws-1', symbols=['SBIN'])
E   state.registry_equals_the_model()
E   state.add(owner='ws-2', symbols=['SBIN'])
```

### PBT feed.registry_concurrent

Break: in `app/hat/data/subscriptions.py`, replaced `self._lock = threading.Lock() self._by_owner` with `self._lock = __import__("contextlib").nullcontext() self._by_owner`.

```
tests/hat/properties/test_feed.py F
FAILED tests/hat/properties/test_feed.py::test_registry_concurrent - Exceptio [cut]
```

### PBT feed.provider_converges

Break: in `app/hat/data/feed.py`, replaced `if regained: await self.subscribe(regained)` with `if regained: pass`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:627: in test_provider_converges
tests/hat/properties/test_feed.py:618: in the_provider_streams_what_the_registry_holds
E   AssertionError: assert set() == {'NSE:SBIN-EQ'}
E     
E     Extra items in the right set:
E     'NSE:SBIN-EQ'
E     Use -v to get more diff
E   Failing test case: <state machine>
E   state = ConvergeMachine()
E   state.the_provider_streams_what_the_registry_holds()
E   state.add(owner='ws-1', symbols=['SBIN'])
E   state.the_provider_streams_what_the_registry_holds()
E   state.arm_race(during='unsubscribe', owner='ws-1', release=False, symbols=['SBIN'])
```

### PBT feed.auth_hooks

Break: in `app/hat/data/feed.py`, replaced `service.add_hook("disconnect", self.disconnect)` with `pass`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:730: in test_auth_hooks
tests/hat/properties/test_feed.py:692: in logout
E   assert False is True
E    +  where False = <app.hat.data.feed.LiveFeed object at 0x7f7cdb8b4410>.explicitly_disconnected
E    +    where <app.hat.data.feed.LiveFeed object at 0x7f7cdb8b4410> = AuthMachine({}).feed
E   Failing test case: <state machine>
E   state = AuthMachine()
E   state.one_socket_on_the_current_token_and_none_after_a_logout()
E   state.logout()
E   state.teardown()
E   Explanation:
E       These lines were always and only run by failing test cases:
E           app/hat/execution/fyers_auth.py:263
```

### PBT feed.off_hours_stops_a_backing_off_runner

Break: in `app/hat/data/feed.py`, replaced `if state in (SessionState.CONNECTING, SessionState.CONNECTED, SessionState.DROPPED):` with `if state in (SessionState.CONNECTED, SessionState.DROPPED):`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:733: in test_off_hours_stops_a_backing_off_runner
tests/hat/properties/test_feed.py:755: in test_off_hours_stops_a_backing_off_runner
E   assert not True
E    +  where True = is_alive()
E    +    where is_alive = <Thread(Thread-5 (<lambda>), started daemon 140422835844800)>.is_alive
E   Failing test case: test_off_hours_stops_a_backing_off_runner(
E       late_reconnects=0,  # or any other generated value
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEEA') as a decorator on your test function
FAILED tests/hat/properties/test_feed.py::test_off_hours_stops_a_backing_off_runner
```

### PBT feed.monitor_survives_errors

Break: in `app/hat/data/feed.py`, replaced `except Exception: logger.exception("Error in the feed monitor; continuing") feed_metrics.increment("feed.monitor.loop_errors") await asyncio.sleep(LOOP_ERROR_PAUSE_S)` with `except Exception: raise`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:765: in test_monitor_survives_errors
tests/hat/properties/test_feed.py:800: in test_monitor_survives_errors
E   assert (False)
E   Failing test case: test_monitor_survives_errors(
E       failing={0},
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AAFBAAA=') as a decorator on your test function
FAILED tests/hat/properties/test_feed.py::test_monitor_survives_errors - asse [cut]
```

### PBT feed.token_check_off_loop

Break: in `app/hat/data/feed.py`, replaced `prevalidated_token = await asyncio.to_thread(self._token_valid)` with `prevalidated_token = self._token_valid()`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:804: in test_token_check_off_loop
tests/hat/properties/test_feed.py:824: in test_token_check_off_loop
E   assert ([140042434537344] and {140042434537344} == {140042434537344}
E     
E     Use -v to get more diff and 140042434537344 != 140042434537344)
E   Failing test case: test_token_check_off_loop(
E       # The test always failed when commented parts were varied together.
E       valid=False,  # or any other generated value
E       is_open=False,  # or any other generated value
E       explicit=False,  # or any other generated value
E       raises=False,  # or any other generated value
E   )
E   
```

### PBT feed.socket_factory

Break: in `app/hat/data/provider/fyers_websocket.py`, replaced `reconnect=True,` with `reconnect=False,`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:843: in test_socket_factory
tests/hat/properties/test_feed.py:870: in test_socket_factory
E   assert (False is True)
E   Failing test case: test_socket_factory(
E       token='000:00000',  # or any other generated value
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AXicY2w0YAQjBkYYC8oHAEvQBZE=') as a decorator on your test function
FAILED tests/hat/properties/test_feed.py::test_socket_factory - assert (False [cut]
```

### PBT feed.token_source

Break: in `app/hat/data/provider/fyers_websocket.py`, replaced `if session.fyers is None: # also drops a token past its expiry return None` with `(nothing)`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:886: in test_token_source
tests/hat/properties/test_feed.py:901: in test_token_source
E   AssertionError: assert '0:0' == None
E   Failing test case: test_token_source(
E       client_id='0',
E       access_token='0',
E       has_model=False,
E   )
E   Explanation:
E       These lines were always and only run by failing test cases:
E           tests/hat/properties/test_feed.py:903
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AEECgTBBAoEwAA==') as a decorator on your test function
```

### PBT feed.traceback_throttle

Break: in `app/hat/data/provider/metrics.py`, replaced `full = last is None or now - last >= TRACEBACK_INTERVAL_S` with `full = True`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:907: in test_traceback_throttle
tests/hat/properties/test_feed.py:933: in test_traceback_throttle
E   assert True == False
E   Failing test case: test_traceback_throttle(
E       times=[0.0, 0.0],
E   )
E   
E   You can reproduce this test case by temporarily adding @reproduce_failure('6.170.5', b'AXicY9RggAJGOIsBAATlAFM=') as a decorator on your test function
FAILED tests/hat/properties/test_feed.py::test_traceback_throttle - assert Tr [cut]
```

### PBT feed.calendar_closed

Break: in `app/hat/data/feed.py`, replaced `policy = calendar.policy(clock)` with `policy = MarketHoursPolicy(clock=clock)`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:951: in test_calendar_closed
tests/hat/properties/test_feed.py:1000: in test_calendar_closed
E   assert True == ((0 < 5 and datetime.date(2026, 10, 19) not in {datetime.date(2026, 10, 19)}))
E    +  where 0 = <built-in method weekday of datetime.date object at 0x7ff6ab52abd0>()
E    +    where <built-in method weekday of datetime.date object at 0x7ff6ab52abd0> = datetime.date(2026, 10, 19).weekday
E   Failing explicit example: test_calendar_closed(
E       offset=0,
E       holidays=set(),
E       today_is_a_holiday=True,
```

Second break (fail open): Break: in `app/hat/data/feed.py`, replaced `if calendar is None: return None` with `if calendar is None: return MarketHoursPolicy(clock=clock)`.

```
tests/hat/properties/test_feed.py F
tests/hat/properties/test_feed.py:951: in test_calendar_closed
tests/hat/properties/test_feed.py:1002: in test_calendar_closed
E   assert True is False
E   Failing explicit example: test_calendar_closed(
E       offset=1,
E       holidays=set(),
E       today_is_a_holiday=False,
E       second=36000,
E       calendar='missing',
```

### PBT bars.trigger_only_on_final_bars

Break: in `backend/app/hat/engine/trigger.py`: a trigger is stamped with the bar's end instead of its final instant (end + 2 s).

```
E   AssertionError: assert datetime.datetime(2026, 10, 21, 9, 20, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) == datetime.datetime(2026, 10, 21, 9,  [cut]
E    +  where datetime.datetime(2026, 10, 21, 9, 20, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) = Trigger(candidate=Candidate(bot_id=UUID('00000000 [cut]
```

Break: in `backend/app/hat/engine/trigger.py`: the context's elapsed share is taken at the bar's end, not its final instant.

```
E   AssertionError: assert Fraction(1, 75) == Fraction(151, 11250)
```

Break: in `backend/app/hat/engine/trigger.py`: `TriggerMonitor` consumes the key when the trigger fires (before the write), so a failed write is never retried.

```
E   assert [1, 0] == [1, 1]
E     At index 1 diff: 0 != 1
```

Break: in `backend/app/hat/engine/live_entry.py`: `TriggerService.write` does not release the key after a failed write.

```
E   assert [1, 0] == [1, 1]
E     At index 1 diff: 0 != 1
```

### PBT bars.stale_feed_trips_health_kill

Break: in `backend/app/hat/engine/live_entry.py`: `DataHealthKill` no longer moves a change to an instant after the previous one, so a recovery at the trip's own instant leaves the kill tripped.

```
E   AssertionError: assert True == False
E    +  where True = KillState(account_tripped=False, bot_tripped=False, health_tripped=True, paused=False).health_tripped
```

Break: in `backend/app/hat/engine/live_entry.py`: `DataHealthKill.write` no longer drops a change older than the last one written, so a trip written after its recovery wins.

```
E   AssertionError: assert True == False
E    +  where True = KillState(account_tripped=False, bot_tripped=False, health_tripped=True, paused=True).health_tripped
```

### PBT bars.history_matches_builder

Break: in `backend/app/hat/data/minute_history.py`: a 5-minute bar closes at its first candle's close.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:947: in test_history_matches_builder
    @example(raw=[])
                   ^^
tests/hat/properties/test_bars.py:953: in test_history_matches_builder
    assert aggregate("AAA", candles) == _builder_bars("AAA", candles)
E   AssertionError: assert [Bar(symbol='...'), volume=0)] == [Bar(symbol='...'), volume=0)]
```

### PBT bars.minute_history_aggregates_to_5m

Break: in `backend/app/hat/data/minute_history.py`: chunks of 101 days.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:957: in test_minute_history_aggregates_to_5m
    @settings(deadline=None)
                   ^^^
tests/hat/properties/test_bars.py:981: in test_minute_history_aggregates_to_5m
    assert all((hi - lo).days < CHUNK_DAYS for lo, hi in spans)
E   assert False
```

Break (CodeAnt review, all-or-nothing): in `backend/app/hat/data/minute_history.py`: each symbol written to `bars_5m` as soon as it loads, so a later symbol's failure leaves the earlier one stored.

```
tests/hat/properties/test_bars.py:1004: in test_minute_history_aggregates_to_5m
E   AssertionError: assert not True
E    +  where True = exists()
E    +    where exists = (PosixPath('/tmp/tmpirbze0gm') / 'other').exists
E   Failing test case: test_minute_history_aggregates_to_5m(
E       raw=[[1_792_554_300, 1.0, 1.0, 1.0, 1.0, 0]],
```

### PBT bars.restore_matches_live

Break: `BarBuilder.restore` skips the known-key guard (`if key in known:` to `if False:`).

```
E   AssertionError: assert 1 == 0
E    +  where 1 = restore([Bar(symbol='AAA', start=datetime.datetime(2026, 10, 21, 9, 15, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')), open=Decimal('0.0001'), ...)])
```

Break: `BarBuilder.restore` does not seed the opening range (`self._opening.setdefault(...)` to `pass`).

```
E   AssertionError: assert None == (Decimal('0.0001'), Decimal('0.0001'))
E    +  where None = opening_range('AAA', datetime.date(2026, 10, 21))
```

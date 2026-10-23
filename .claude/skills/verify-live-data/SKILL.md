---
name: verify-live-data
description: How to run, read and update the checks behind Hire a Trader's live data - the live feed (one provider socket, tick listeners, subscription registry, login hooks), 5-minute bars, opening range, volume pace, feed staleness, pre-open gap capture, the 09:16 gap scan, the "hat" subscription owner, bars_5m Parquet and the NSE announcement feeds (Hypothesis properties feed, bars, gapscan, announcements; CONF single-feed import rule). Use before and after any change to backend/app/hat/data/bars.py, live.py, staleness.py, subscriptions.py, preopen.py, gapscan.py, announcements.py, bars_store.py, minute_history.py, feed.py or provider/*.py.
---

# verify-live-data

Checks that HAT owns the one provider socket and feeds every listener without a bad listener breaking it, that the socket follows the Fyers login and market hours, that every wanted symbol reaches the provider, that 5-minute bars are aligned to 09:15 IST and never change once final, that a silent feed is flagged exactly when the threshold passes, that the 09:16 gap scan selects only candidates that can really gap 10%, and that announcements are stored once and matched by instrument and time only. Requirement IDs (index: `docs/invariants.md`): (HAT-DATA-100..104, 110..118, 120..127), (HAT-OVR-006), (HAT-DEC-I3, I6, B8..B10), (HAT-BOT-052), (HAT-RISK-063, the health side).

No TLA+ model: `docs/architecture.md` lists none for this area. The ordering promises that involve it are `ExecutionRouter!NoOrderWhenStale` and `JobScheduler!IntradayOrder`, which arrive with PR 15 and PR 17.

## Properties

- `PBT bars.alignment` For every tick sequence, in any arrival order, every in-session tick is in exactly the bar starting at 09:15 + 5 min x floor((t - 09:15) / 5 min) in IST, and each bar's OHLCV is the first, max, min and last price and summed volume of its ticks (by exchange time); a tick outside [09:15, 15:30) is in no bar. Harm: an opening-range high or a pivot break is judged on the wrong bar.
- `PBT bars.alignment_finality` No bar is reported before its end plus 2 s, each bar once, and a final bar never changes: a tick arriving at or after the final instant is dropped and counted, so reported bars equal the bars of the on-time ticks. Harm: a trigger fires on a bar that later changes, so live and replay disagree.
- `PBT bars.alignment_boundary_instants` A bar is not final one microsecond before its end plus 2 s and is final at that instant. Harm: the 2-second lag is off by one tick of the clock.
- `PBT bars.opening_range_fixed` For a symbol watched from the open, before 09:20:02 there is no opening range; from then it equals the first bar's high and low and never changes, even when first-bar ticks arrive later. Harm: the ORB trigger level drifts and entries are late or never fire.
- `PBT bars.volume_pace` For t in (09:15, 15:30] pace(t) x (elapsed / 375 min) = V exactly, and there is no pace outside that range. Harm: an early trickle projects to a huge pace and fires false breakouts.
- `PBT bars.volume_pace_builder` The builder gives no pace before a symbol's first tick of the session, and afterwards the pace of the day volume of every in-session tick. Harm: pace is read before any data exists.
- `PBT bars.tick_volume_delta` A tick's volume is the change in cumulative day volume, never negative; the first tick contributes only its own print. Harm: bar volume and pace count volume traded before HAT subscribed, or go negative.
- `PBT bars.tick_volume_fallbacks` A tick with no usable cumulative volume contributes its own last traded quantity, and the first tick of a new trade date contributes its own print rather than its total minus the previous day's total. Harm: a print is lost from bar and day volume, or a new session's opening bar is charged yesterday's volume.
- `PBT bars.feed_connection_window` The live feed may connect on a trading day from 09:00 IST to the 15:30 close and never on a weekend. Harm: the 09:00 to 09:08 gap capture receives no ticks, or the feed stays up all night.
- `PBT bars.staleness_trip` The feed is stale at t exactly when t is in market hours and no tick arrived in (t - 30 s, t]; each transition into stale reaches the health sink once and each recovery once. Harm: orders are placed on a frozen tape, or a transient gap leaves a bot disabled.
- `PBT bars.staleness_boundary` Silence of exactly 30 s is stale and 30 s minus one microsecond is not. Harm: the threshold is off by one.
- `PBT bars.staleness_events_recorded` Each transition into stale writes exactly one `feed.stale` event and calls the health kill once; recovery clears the kill and writes nothing. Harm: the owner is never alerted, or is alerted repeatedly.
- `PBT bars.listener_isolation` With any set of listeners on `LiveFeed`, any subset of which raises, every listener that did not raise for a tick got it, in order, and a removed listener gets none. Harm: one bad listener freezes live quotes for every consumer.
- `PBT bars.subscription_set` The `"hat"` owner's set equals watchlist, open positions and candidates, plus the heartbeat index `NIFTY50` from the first sync until HAT releases its symbols (PR 39; it is also in the drawn pool, so other owners and the inputs hold it too); positions are always in it; the provider holds exactly the symbols some owner holds, and releasing HAT never unsubscribes a symbol another owner holds; a set at or above the limit alerts. Harm: an open position stops receiving prices so its exits never fire.
- `PBT bars.subscription_inputs` The inputs are today's watchlist symbols, every open position's symbol (not closed ones, not other days' watchlists) and the candidates. Harm: stale symbols hold feed budget, or an open position is missed.
- `PBT bars.no_store_keeps_no_bars` Without a bar store, final bars reach the listeners and are then forgotten; nothing accumulates. Harm: the long-running loop grows memory all session.
- `PBT bars.persisted_equals_live` The `bars_5m` rows equal the final bars the bar listener saw, one row per (instrument, bar_start), exactly the spec's columns, however writes failed and re-ran. Harm: replays and audits cannot reproduce a live decision.
- `PBT bars.gap_pct` The recorded gap is 100 x (o / c - 1), from the indicative price inside 09:00-09:08 else the open of the first tick at or after 09:15, and the record names its source and trade date; a later first tick never replaces a record. Harm: EP gap filters use a wrong gap.
- `PBT bars.gap_record_from_its_own_day` In a process running across days, every gap record of trade date d holds d's open: its own indicative open, or the feed's open from a tick at or after 09:15 (a 09:00 snapshot carries yesterday's open), and a later day's record drops the earlier days' records. Harm: `gap_up` runs on yesterday's open (PR 22, audit F13).
- `PBT bars.candidates_never_overwrite_refresh` A watchlist refresh that arrives while a candidate add is in progress is not lost: the `"hat"` set holds the refreshed watchlist after both finish. Harm: a refreshed watchlist symbol or an open position stops receiving prices.
- `PBT bars.history_matches_builder` Aggregating a 1-minute series to 5-minute bars (`minute_history.aggregate`) gives exactly the bars the live builder makes of the same prints (open with the volume, high, low, close): same windows, OHLC and volume, nothing outside the session. Harm: EP backtests read bars built differently from live and mislead promotion decisions.
- `PBT bars.minute_history_aggregates_to_5m` The loader asks the injected history call for each symbol's 1-minute candles in consecutive chunks of at most `chunk_days` (drawn 1..100) covering exactly the period and writes each symbol's builder bars to `bars_5m` as soon as it loads; a symbol whose calls are refused (whichever of the three symbols: the first, middle or last one; every call of that symbol is refused, so it fails on its first chunk) or whose answer holds more candles than the cap, or whose candle rows are garbage (`None`, a dict, a string, too short, an impossible epoch, a price too large for a decimal: `history_bad_candle`), or whose answer is not a mapping at all or an "ok" without candles (`history_bad_answer`, which never counts toward the refused-in-a-row stop, CodeAnt), is reported with a fixed code (no broker text) and has nothing written, while every other symbol is stored (PR 41, D131). Harm: one bad symbol throws away hours of loaded history, or a broker message reaches the store or the logs.
- `PBT bars.minute_history_budget` With a fake clock and calls taking 0..40 s, whatever the day already spent (saved progress), and across a second run started right after the first with a fresh pacer on the saved progress, no 60 s window holds more than `per_minute` calls and no IST day more than `per_day`; a spent day stops the run with `history_daily_limit` and the saved count equals what was spent plus the calls made. Harm: Fyers throttles or blocks the app, and live trading loses its session.
- `PBT bars.minute_history_resume` A run stopped by the day's budget (0..12 calls) or by failing symbols, then resumed the next day with `--resume` (and given the next day's shifted period, which the resume replaces with the stopped run's), leaves `bars_5m` equal to one uninterrupted run, skips exactly the symbols the progress records and never asks for them again. Harm: a multi-day download wastes its budget or leaves gaps.
- `PBT bars.minute_history_plan_clamp` A fake plan that refuses (-50, `data.range_from` "exceeds the historical data available for your plan") any range before a floor 0..2 days after `today - plan_days`: a start 0..3 days before the floor is clamped to exactly the floor (saved in the progress), within `max_clamps`, and every symbol is loaded from there; a -50 with another reason is a symbol failure, never a clamp. Harm: the download crashes on its first call, or silently skips history it could have had.
- `PBT bars.minute_history_rate_limit_stops_run` Once Fyers answers -360 (the plan's daily requests spent), the run stops at that call with `history_rate_limited`, not `history_failed`, and asks nothing more: the symbol it stopped on is neither failed nor done (a resume asks for it again), every symbol loaded before it is stored and done, and no broker text is kept (HAT-BRK-082). Harm: a spent quota marks every remaining symbol failed and the run keeps spending calls the plan refuses.
- `PBT minute_history_cli.root_and_report` Driving `main` with `HAT_DATA_ROOT` set only in `.env`, only in the environment, in both, or blank in the environment over a `.env` value, 0 to 3 symbols, every call refused or answered `no_data`, and any count of members without a Fyers symbol: the lock, progress and report land only under the root that rule gives, `minute_history/report-YYYY-MM-DD.txt` ends with exactly the coverage report printed, `main` exits 1 exactly when the run stopped or a symbol failed (with a budget that never runs out, only refused symbols do either), and nothing reaches the network. Harm: a three-day download writes to the repo's `data_system/hat/` instead of the data volume, or its coverage is lost with the terminal.
- `PBT gapscan.covers_universe` The scan evaluates every EQ symbol exactly once with the exact gap, and no other series. Harm: a gapper is missed because a page was truncated.
- `PBT gapscan.filter` The selected set equals {gap >= 10 and universe passes and (band 10 or 20, or F&O)}; a stock with no price-band row is never selected; every selected symbol is handed on to be subscribed, with the listing's open (PR 22: the gapper is not subscribed yet, so the feed has no open of it). Harm: impossible gaps are chased or valid EP candidates never watched.
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
- `PBT feed.calendar_closed` With HAT's trading calendar the feed never connects on a day it lists as a holiday and connects on its other trading days only from 09:00 IST to the 15:30 close; with the calendar missing, unreadable or not wired it connects on no day. Harm: the Fyers socket opens on an exchange holiday, or on a day nobody knows is a trading day.
- `PBT feed.token_source` The provider token is `client_id:access_token` exactly when the Fyers session holds a model, a client id and an access token, and None otherwise. Harm: the socket is built with half a token, or an expired token is retried for ever.
- `PBT feed.traceback_throttle` A failing site writes a full traceback first and then at most once per interval; the other failures are one counted debug line. Harm: a failing tick handler writes a traceback per tick and fills the disk.
- `PBT announcements.text_never_gates` Items identical except title, category text and URL give the same `catalyst_attached` outcome. Harm: a text heuristic becomes an unreviewed trading rule.
- `CONF imports.no_second_websocket` Only `app/hat/data/provider/fyers_websocket.py` imports or names the Fyers data-socket SDK or any other WebSocket library (statically, or by name through `importlib` or `__import__`), nothing outside `app/hat` imports `fyers_apiv3`, and `LiveFeed.add_tick_listener` is the only tick input `live.py` uses. Harm: a second socket breaks the broker session and drops the feed.
- `CONF single_feed_import_rule` Only `feed.py` builds a `ProviderSession` or imports the provider modules, only `main.py` builds a `LiveFeed` and only inside `if is_enabled()`, and only in the body of `if is_enabled()` (not under a negated or nested `if not is_enabled()`); the only importer of `feed.py` is `main.py` (K5b deleted the legacy read side). Harm: a second feed or session could open a socket HAT does not control, or open one with HAT off.
- `CONF no_duplicate_fakes_or_authorities` Across `backend/` HAT has one `FakeBroker` (`tests/hat/fakes/broker.py`) and one `evaluate_signal` (`app/hat/risk/service.py`); no legacy copy is left (K4 deleted the legacy `FakeBroker` re-export and `RiskManager`, K5b the legacy `LiveMarketService`; a class named `RiskManager` or `LiveMarketService` fails the check), and no other class is named as a risk authority or live-tick service. Harm: two risk authorities or two fakes drift apart and a check passes against the wrong one.
- `PBT bars.trigger_only_on_final_bars` With prints arriving late or out of order through the real bar builder, a pivot breakout fires once, at the final instant (end + 2 s) of the first final bar in [09:20, 15:20] whose high is above the pivot and whose close is at or above it (`confirm: close`, PR 49), never from a print in a bar still open or a late print the bar never took, and never at an instant before the bar was handed over; the elapsed share of the session is read at that same final instant, where the day volume is read; and (`test_failed_signal_write_retries`) a failed signal write releases the key so the next final bar fires again, while a stored signal closes it for the day. Harm: an entry fires on a print the 5-minute bar later disowns, or before the bar that justifies it exists.
- `PBT bars.stale_feed_trips_health_kill` Wired as in production (staleness monitor, `EventFeedHealth`, `DataHealthKill`), the account's data-health kill is tripped exactly while the feed is stale and clears by itself when ticks resume, even when the trip and the recovery fall on one instant and (`test_health_kill_writes_out_of_order`) whatever order the worker threads write the changes in; a trigger fired while stale is stored Rejected with `stale_feed` and opens nothing, one on a live feed opens its virtual position. Harm: entries keep being taken on prices that stopped updating, or the account stays manage-only after the feed is back.
- `PBT bars.restore_matches_live` After a mid-session restart, a bar builder given the day's stored final bars and coverage has the same opening ranges and day volumes as one that saw every tick, a restored bar never replaces a live one, and it has no day low (the restart is a gap); the opening range comes back only when the stopped process saw the feed past the first bar (`coverage.json` `seen_until`). Harm: after a restart the opening range is lost and no ORB entry fires that day.
- `PBT bars.session_state_never_partial` Through `LiveData` with a `BarStore`, whatever HAT missed of the session (AAA first seen after the open, a feed-wide silence of 30 s or more, also while it is still under 30 s, a restart and its downtime, with a BBB heartbeat as the rest of the feed; the process keeps stepping during a silence; `@example`s: a gap found after the first bar was stored then a restart before the next bar, and a restart while silent from 09:19:50), a published opening range is the session's real one and a published day low the real low of the day's final bars; watched from the open with no gap both are published (the opening range also after a restart once the first bar was stored). PR 39 adds `seeded`: the exchange range is on and each AAA tick carries the session's high and low so far (`high_price`, `low_price`); the same holds, and a symbol first seen late, after a restart or after a gap, with no gap after, has the opening range when it ticked inside the first bar and the day low once that tick's bar is final (`@example`s: the 09:17:30 subscriber, a seed after a restart, a gap then a new seed, a seed before a gap whose low traded unseen, and a restart whose next tick knows a high from after 09:20). Harm: EP buys under its true opening-range high with a too-tight, oversized stop (PR 22, audit F4); or EP never fires on a gap-scan name (PR 39).
- `PBT bars.trigger_never_uses_a_partial_session` Through the real builder and trigger monitor, for a candidate first seen late an opening-range trigger fires only at the real opening-range high and a `low_of_day` stop is the real low less a tick or `stop_input_missing`; watched from the open both are exactly the session's. With `seeded` (PR 39) the late candidate's ticks carry the exchange's high and low, and its stop is exactly the real low less a tick. The audit's probe (an unseen 09:15 spike to 110, ticks from 09:17:30) is an `@example`. Harm: a breakout bought under its own level, with an oversized position.
- `PBT bars.symbol_silence_resubscribes_and_alerts` Through `LiveData` with a `SilenceWatch` (threshold 60 s, checks every 5 s, the heartbeat ticking every 10 s), a watched symbol is subscribed again exactly when the threshold has passed since its last tick, its last resubscribe, or the last check at which it was not yet watched, the market closed or the feed stale; each check makes at most one resubscribe (every symbol found) and one alert (those not alerted since the market last opened); a failing alert or resubscribe is logged and never reaches the feed, and a name whose alert failed is named again at the next report; and no quiet symbol ever makes the feed stale (only a feed-wide outage does). `@example`s: an outage then a closed window that reopens; a name trading every 100 s. Harm: an illiquid or band-locked name makes the account manage-only all day, a dead subscription goes unnoticed, or an alert per quiet name floods the owner's chat (PR 39, HAT-DATA-116).
- `PBT bars.live_flags_parsed` `HAT_EXCHANGE_DAY_RANGE` turns the exchange-range seed on exactly for a true value (off unset or for anything else), `HAT_SYMBOL_SILENT_SECONDS` is a positive whole number, else 120, and `HAT_ENTRY_TIMEOUT_MINUTES` (PR 49, HAT-EXEC-046) a positive whole number, else 60. Harm: unverified tick fields seed the opening range and the stop by default, a typo reports every symbol (PR 39), or a typo cancels every entry at once or never.
- `PBT bars.delayed_seed_spoiled_by_gap` A seed covers prints only up to the earlier of its exchange time and its arrival, so a feed gap that began between the two (a delayed tick arriving after the gap, found before or after the tick) or later inside the bars it covers leaves the opening range and day low as they would be without it. Harm: prints the gap hid, in neither the seed nor the bars, leave a range or a low the session never had (PR 39, CodeAnt).
- `PBT bars.seed_needs_ordered_prices` A seed is taken only from a tick inside the session with `0 < low <= price <= high`; any other leaves the opening range and day low as they would be without it. Harm: a garbled or stale high/low field publishes a range or a low the session never had (PR 39).
- `PBT bars.usable_price` For every quote, clock and band, the price a Take, a placement or an exit may act on is the quote's price exactly when it is under 30 seconds from the clock either way (by exchange time) and inside the known bounds, else none with `price_unknown`, `stale_price` or `price_out_of_band`; a tick exactly 30 seconds old no longer counts (PR 51, HAT-DATA-116). Harm: one quiet symbol's hours-old print prices a Take while the feed as a whole is live.
- `PBT bars.price_book_dates_each_tick` Through `LiveData` and its tick listener, the price book keeps per instrument the latest tick's price with its exchange time (`exch_feed_time`), else its arrival, and a print arriving after a newer one does not replace it. Harm: a price that has not traded for an hour reads as current, or a late print regresses it.
- `PBT bars.circuit_locks_read_the_band` For every previous close, band and tick, the upper circuit price is the band's top rounded down to the tick grid and the lower one its bottom rounded up; a price is locked at a circuit exactly when it is at or beyond it, neither lock holds without a band (F&O) and both are unknown without a band row or a previous close. Harm: an entry is taken into a frozen upper circuit, or a lower-circuit print is treated as sellable.
- `PBT bars.exits_and_placement_read_the_checked_price` Wired as the runtime wires them, `LivePrices` over the price book and the stored band (widened by one tick) gives the exit views a view exactly when the price is usable, `LiveRoute.check` fills `ltp` and `ltp_refusal` with exactly that price and reason, and `placement_refusal` (given no stop; the stop refusal is `router.routes_by_mode`'s and `router.take_rechecks_max_chase`'s) passes exactly then. Harm: a stale or impossible print fires a target exit or prices an entry.

## Run

```bash
cd backend
CI=1 python -m pytest tests/hat/properties/test_feed.py tests/hat/properties/test_bars.py tests/hat/properties/test_gapscan.py \
  tests/hat/properties/test_announcements.py tests/hat/properties/test_minute_history_cli.py -q --no-cov     # about 40 s under the ci profile
python -m pytest tests/hat/conformance/test_conf_imports.py -q --no-cov
```

No network, broker or database server is touched: ticks come from generated payloads, MCP rows from `PricedListingServer`, feeds from `rss_item`/`rss_document` and two recorded fixtures (`rss_announcements.xml`, `rss_financial_results.xml`, `mcp_cm_equity_stocks_reli.json`), and the database is in-memory SQLite.

## Bug or model error

A failing property prints the falsifying example and a `reproduce_failure` blob. Replay the example by hand with the real functions first.

- Bars: feed the printed ticks to `BarBuilder.on_tick(tick, now)` and `advance(now)`. If the bar the code returns disagrees with the spec rule (bar start by integer arithmetic on seconds of the IST day, open and close by exchange time, a tick arriving at or after end + 2 s is late), it is a bug in `bars.py`. The oracle `expected_bars` is the spec rule, not the code: change it only if the spec changes.
- Staleness: the rule is the property's, "no tick in (t - 30 s, t]". The original statement said "more than 30 seconds"; the two disagree at exactly 30 s and the acceptance wins (recorded under Scope changes).
- Listener isolation and the feed: a failure means an exception reached another listener or the SDK thread, or the socket, replay or hooks disagree with the model. Fix `LiveFeed` (`feed.py`) or `ProviderSession`, never the property. The state-machine properties print the failing rule sequence; replay it against `make_feed` in `tests/hat/fakes/data_socket.py`.
- Gap scan: the F&O rule is `[OWNER-PENDING]` (a known price-band row with a null `band_pct`). If the owner changes it, change `docs/decisions.md`, then `Gapper` and the property together.
- Announcements: the feed field names (`symbol`, `isin`, `company`, `category`, the `Company - Subject` title, `pubDate`) and URLs are `[A]`, built from recorded fixtures only. A live poll that shows different names is a model error in the parser and the fixtures, not a failure of these properties.
- Never weaken a property or shrink a strategy to pass; record a narrowing under "Scope changes".

## Updating

- A new tick rule or bar rule: change `bars.py`, the oracle in `test_bars.py` and this skill.
- A new feed field or gap rule: add a recorded fixture and extend the matching property; never read live feeds in a test.
- Every new check needs a recorded break under "Scratch breaks" (HAT-VER-072) and a Properties bullet naming its harm.
- After re-confirming everything, run `python backend/tests/hat/conformance/hat_conf_skills.py verify-live-data` and update the sync commit and digest below.

## Coverage

Code under check:
- `backend/app/hat/data/bars.py`: `BarBuilder`, `Tick`, `Bar`, `volume_pace`
- `backend/app/hat/data/bars_store.py`: `BarStore`
- `backend/app/hat/data/staleness.py`: `StalenessMonitor`, `SilenceWatch`
- `backend/app/hat/data/subscriptions.py`: `LiveSubscriptions` (with `HEARTBEAT_SYMBOL`), `load_inputs`
- `backend/app/hat/settings.py`: `load_settings` (`exchange_day_range`, `symbol_silent_seconds` only; the rest is `verify-governance`'s and `verify-execution`'s)
- `backend/app/hat/data/preopen.py`: `PreopenGaps`, `gap_pct`, `meets_gap`
- `backend/app/hat/data/gapscan.py`: `scan_rows`, `select_gappers`, `run_gap_scan`
- `backend/app/hat/data/announcements.py`: `parse_feed`, `store_items`, `attached_announcements`, `catalyst_attached`, `next_poll_at`, `poll_feed`
- `backend/app/hat/data/live.py`: `LiveData` (with the exchange-range seed and the silence watch), `EventFeedHealth`, `tick_from_payload`, `Quote`, `usable_price`; `silence_alert` and `off_loop` are wiring, not checked here
- `backend/app/hat/engine/trigger.py`: `TriggerMonitor`, `in_window`, `upper_circuit_price`, `lower_circuit_price`, `locked_upper_circuit`, `locked_lower_circuit`
- `backend/app/hat/daily/live.py`: `PriceBook`, `band_bounds`, `LivePrices`, the price read of `ExitViews` and `LiveRoute.check` (PR 51; the rest of the file is `verify-execution`'s)
- `backend/app/hat/engine/live_entry.py`: `TriggerService`, `DataHealthKill`
- `backend/app/hat/clock.py`: `FixedClock`, `require_aware`
- `backend/app/hat/data/feed.py`: `LiveFeed`, `connect_status_message`
- `backend/app/hat/data/provider/data_socket.py`: `DataSocket`, `SocketCallbacks`
- `backend/app/hat/data/provider/provider_session.py`: `ProviderSession`, `RetryPolicy`
- `backend/app/hat/data/provider/ticks.py`: `normalize_tick`
- `backend/app/hat/data/provider/metrics.py`: `FeedMetrics`, `log_exception_throttled`
- `backend/app/hat/data/provider/fyers_websocket.py`: `fyers_socket_factory`, `fyers_token`

- `backend/app/hat/data/minute_history.py`: `parse_candles`, `candle_prints`, `aggregate`, `load_minute_history`, `HistoryLimits`, `CallPacer`, `Progress`, `HistoryReport`, `main` (driven by `minute_history_cli.root_and_report` with the Fyers history call, the database session and `history_universe` replaced; `history_universe` itself reaches the database and is not checked here)

Checks:
- `backend/tests/hat/properties/test_feed.py`
- `backend/tests/hat/properties/test_bars.py`
- `backend/tests/hat/properties/test_minute_history_cli.py`
- `backend/tests/hat/hat_support.py`
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
Covered digest: `2335194cb3fbd70f0375f826d1c8a43a540989b3eda588e2c1de53693442ee9d`

## Scope changes

- PR 73b (PR 39 follow-up): the runtime now gives `LiveSubscriptions` its alert, so `subscription_limit` reaches the owner as a health alert (HAT-DATA-112); its checks are `ops.subscription_limit_alerts` and CONF `ops.subscription_alert_wired` (`verify-scheduler`). `bars.subscription_set` is unchanged. Nothing was narrowed.
- PR 39: the heartbeat `NIFTY50` (`NSE:NIFTY50-INDEX`) is always in the `"hat"` owner's set, so feed staleness measures the socket. A watched symbol silent for `HAT_SYMBOL_SILENT_SECONDS` (default 120) is subscribed again (and again after each further threshold) and named in one health alert `symbol_silent` per check, once per symbol per day; never a kill (`SilenceWatch`, wired in `daily/runtime.py`; the alert is sent off the loop, and an `&` in a symbol is written `_`, as the alert text keeps only plain characters). With `HAT_EXCHANGE_DAY_RANGE` on, a tick's `high_price` and `low_price` seed the builder (`BarBuilder.seed`), so a symbol first seen late, after a restart or after a gap has the opening range when its seeding tick traded inside the first bar, and otherwise the day low once that tick's bar is final; the field names are `[A]`: no recorded websocket tick exists (the fixtures hold REST replies only), so the flag defaults to off and, until it is on, such a symbol has neither (HAT-DATA-114). The bar builder now keeps every gap start of a day, not only the first. Nothing was narrowed: `bars.subscription_set`, `bars.session_state_never_partial` and `bars.trigger_never_uses_a_partial_session` were strengthened.
- PR 48: `daily/live.py:SaleBands` reads the day's lower band and the lower-circuit lock (PR 51's `lower_circuit_price` and `locked_lower_circuit` on the dated last price) for the trade manager's sales; its use is checked by `verify-execution`'s `mpp.*` through a stub band. No live-data check changed.
- PR 61 (HAT-BT-025): the entry-filter loop moved out of `engine/trigger.py:plan_entry` into `trigger.evaluate_entry_filters`, with `trigger.entry_filter_context` building the filters' `LiveContext`, so the backtest runs the same filters live runs. `plan_entry` returns the same skip or failing filter as before; no live behaviour changes and the trigger properties are unchanged.
- PR 12 added the minute-history loader (HAT-DATA-119). `FyersClient.get_historical_data` named by the spec does not exist; the loader takes the history call as an injected function, so the properties fake it and no check reaches Fyers.
- PR 43 (PR 41 follow-ups 1 and 7): the command line takes its root from `operator_settings()` (loads `.env` first) and appends its coverage report to `minute_history/report-YYYY-MM-DD.txt` under that root. `minute_history_cli.root_and_report` drives `main` in its own file, because `main` reads the wall clock (the period ends yesterday; the report is named by the day) and `test_bars.py` promises no property reads one; the report is matched by pattern. Name lookups and sockets are blocked and recorded. Only a run that finishes saves its report: one interrupted or failed by an exception saves none (follow-up). Nothing was narrowed.
- PR 41 (owner decision D131) made the download runnable on the Fyers Standard plan: per-symbol writes replace PR 12's all-or-nothing (the old CodeAnt break below is retired with it), with a call pacer, resumable progress and the plan-limit clamp; `BarStore.write` now carries the untouched rows of a day as Arrow data (`bars.persisted_equals_live` guards it). The plan's refusal text is from the audit 2026-10-22 live checks ("Fyers history depth"); a recording of the exact response is a follow-up.
- PR 25 (HAT-BRK-082): Fyers -360 stops a minute-history run (`_FYERS_RATE_LIMITED`, `history_rate_limited` in `_RUN_STOPS`). The -360 reply was not recorded live; the code is matched on `code` alone. Nothing was narrowed.
- PR 51 (ME-F5/F6/F7, MX-F11): the live price is dated. There is one quote loop, the feed's tick listener into `daily/live.py:PriceBook`; HAT makes no REST quote call, so no quote spends the broker call budget (PR 50). A Take, a placement and an exit read `LivePrices`, which refuses a price 30 seconds old (the feed's stale threshold, HAT-DATA-116) or outside the day's band widened by one tick (the exchange's own rounding of the band); the circuit locks read the stored band . The decision-time `price_band_ok` of `engine/live_entry.py` still compares the planned entry with itself (MX-F11) and is the engine lane's follow-up; the placement check now refuses an out-of-band price either way. A symbol with no tick in 30 seconds gets no exit view (no rule fires; its broker stop still rests). Nothing was narrowed.

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- PR 75a: `daily/live.py` reads `positions.exit_rules_of` (moved from `trades`, `verify-execution`). No live-data check changed.
- `bars.staleness_trip` and `bars.staleness_boundary`: HAT-DATA-116's statement said "more than 30 seconds" and its acceptance said no tick in (t - 30 s, t], which disagree at exactly 30 s. The acceptance wins; the requirement text is "30 seconds or more".
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
- PR 41 narrowed `CONF single_feed_import_rule`: outside the provider package, `hat/data/minute_history.py` may import `provider.fyers_websocket.fyers_history` and nothing else from it (`PROVIDER_IMPORTS`). The download needs the Fyers session's REST history, and HAT-BRK-080 lets the data package reach the session only through that module; no socket or provider session is built, and importing anything else from the provider still fails (break recorded below).
- PR 15b widened the live trigger path (`engine/live_entry.py`): `watchlist_candidates` and `ep_bot_candidates` take every active bot with a live version and its effective mode (with an optional `calendar`), and `submit_trigger` and `TriggerService` take a `route`. The bar and trigger properties pass unchanged; none was narrowed.
- PR 49: `bars.trigger_only_on_final_bars`'s oracle needs the bar's close at or above the pivot (`confirm: close`, the default) and asserts `bar_close`; `bars.live_flags_parsed` also reads `HAT_ENTRY_TIMEOUT_MINUTES`. The bar listener `execution.entry_timeout.EntryWatch` reads final bars only (checked in `verify-execution`). Nothing was narrowed.
- PR 75b (architecture audit A-F3, HAT-OVR-001): the feed reads the one `SessionCalendar` (`LiveFeed(calendar=..., calendar_source=..., clock=...)`, `feed.calendar_source`) instead of `MarketHoursPolicy`, which is deleted with `MarketPhase`. A weekday the calendar does not hold is now closed to the feed (it was open under the policy's weekday default): the safe rule, checked by `calendar.one_answer` (`verify-reference-data`). `make_feed` builds a weekday calendar around its clock (`fakes.data_socket.weekday_calendar`). `bars.feed_connection_window` and `feed.calendar_closed` keep their properties; `feed.calendar_closed`'s known calendar now holds every weekday row, as `with_weekdays` stores them.

## Scratch breaks

Each check was shown to catch a deliberate break in the real code (HAT-VER-072), then the break was removed and the check returned to passing.

### PBT minute_history_cli.root_and_report

Break: (PR 43) `_run` reads `load_settings()` again (the PR 41 order: `.env` not yet loaded). With the root set only in `.env` the lock, progress and report land in the default root.

```
tests/hat/properties/test_minute_history_cli.py:25: in test_root_and_report
tests/hat/properties/test_minute_history_cli.py:59: in test_root_and_report
E   AssertionError: ['.env', 'default/minute_history/progress.json', 'default/minute_history/progress.lock', 'default/minute_history/report-2026-10-22.txt']
E   assert {'.env', 'default'} == {'.env', 'env_file'}
```

Break: (PR 43) `operator_settings()` no longer loads `.env`.

```
tests/hat/properties/test_minute_history_cli.py:25: in test_root_and_report
tests/hat/properties/test_minute_history_cli.py:59: in test_root_and_report
E   AssertionError: ['.env', 'default/minute_history/progress.json', 'default/minute_history/progress.lock', 'default/minute_history/report-2026-10-22.txt']
E   assert {'.env', 'default'} == {'.env', 'env_file'}
```

Break: (PR 43) the command line no longer saves its report.

```
tests/hat/properties/test_minute_history_cli.py:25: in test_root_and_report
tests/hat/properties/test_minute_history_cli.py:63: in test_root_and_report
E   AssertionError: ['.env', 'env_file/minute_history/progress.json', 'env_file/minute_history/progress.lock']
E   assert 0 == 1
E    +  where 0 = len([])
```

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

Break: (PR 39) `LiveSubscriptions.desired` leaves out the heartbeat (`| {HEARTBEAT_SYMBOL}` removed).

```
E   AssertionError: assert set() == {'NIFTY50'}
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

Break (PR 41, `BarStore.write`): the merge drops the other instruments' stored rows (`stored.filter(pc.invert(mine))` to `SCHEMA.empty_table()`).

```
tests/hat/properties/test_bars.py:1319: in test_persisted_equals_live
tests/hat/properties/test_bars.py:1382: in test_persisted_equals_live
E   AssertionError: assert {('2b83d31f-f...'0.0001'), 0)} == {('106743f9-7...'0.0001'), 0)}
E     
E     Omitting 1 identical items, use -vv to show
E     Right contains 1 more item:
E     {('106743f9-799e-4ed3-bc11-d64ede993d44', datetime.datetime(2026, 10, 21, 3, 45, tzinfo=datetime.timezone.utc)): (Decimal('0.0001'),
E                                                                                                                       Decimal('0.0001'),
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

Break (PR 22): `gap_scan_body` hands over no opens (`on_candidates(found, {})`).

```
E   KeyError: 'A'
E   Failing test case: test_filter(
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

Break (PR 41): `minute_history.py` also imports `fyers_socket_factory` from the provider.

```
tests/hat/conformance/test_conf_imports.py:280: in test_single_feed_import_rule
E   AssertionError: hat/data/minute_history.py
E   assert {'app.hat.dat...cket_factory'} <= {'app.hat.dat...yers_history'}
E
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

Break: (PR 75b) the racer now settles a race inside its own provider call as `LiveFeed` does for every owner (re-subscribe what was taken back, unsubscribe what was let go). Before, two armed races nested (a release during our subscribe, overtaken by a re-subscribe) left the provider without a held symbol that no real caller would leave, and the property failed on main (`add SBIN; arm subscribe release SBIN; arm unsubscribe re-add SBIN; add TCS`). With `regained = []` in `LiveFeed.unsubscribe` it still fails (found in one of three default-profile runs with an empty example database; the original racer in none of three):

```
E   AssertionError: assert set() == {'NSE:SBIN-EQ'}
E     Extra items in the right set:
E     'NSE:SBIN-EQ'
```

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

Break: (PR 75b) in `app/hat/data/feed.py`, `connection_allowed` returns True when the calendar is missing (`if calendar is None: return True`, fail open). The PR 17 break (the feed built with a bare weekday `MarketHoursPolicy`) has no target since PR 75b deleted that policy; `calendar.one_answer` covers an unknown day.

```
E   assert True is False
E   Failing explicit example: test_calendar_closed(
E       offset=1,
E       holidays=set(),
E       today_is_a_holiday=False,
E       second=36000,
E       calendar='missing',
```

### PBT bars.trigger_only_on_final_bars

Break: (PR 49) `price_breaks_level` ignores the close (`held = True`).

```
    | AssertionError: assert datetime.datetime(2026, 10, 21, 9, 20, 2, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) == datetime.datetime(2026, 10, 21, 9, 25, 2, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata'))
    | AssertionError: assert [(datetime.da...rcuit=None)))] == []
```

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

Break: in `backend/app/hat/data/minute_history.py`: chunks one day longer than `chunk_days`.

```
tests/hat/properties/test_bars.py:1553: in test_minute_history_aggregates_to_5m
tests/hat/properties/test_bars.py:1592: in test_minute_history_aggregates_to_5m
E   assert False
E    +  where False = all(<generator object test_minute_history_aggregates_to_5m.<locals>.<genexpr> at 0x7f6804e6b5a0>)
```

Break: the per-response candle cap is skipped (`if len(raw) > limits.max_candles:` to `if False:`).

```
tests/hat/properties/test_bars.py:1553: in test_minute_history_aggregates_to_5m
tests/hat/properties/test_bars.py:1608: in test_minute_history_aggregates_to_5m
    assert report.failed == expected_failed
E   AssertionError: assert {'NSE:AAA-EQ'...story_failed'} == {'NSE:AAA-EQ'...many_candles'}
E     
E     Omitting 1 identical items, use -vv to show
E     Right contains 2 more items:
E     {'NSE:MMM-EQ': 'history_too_many_candles',
```

Break: a failed symbol stops the run instead of being reported (`report.failed[symbol] = exc.code` to `report.stopped = exc.code; break`).

```
tests/hat/properties/test_bars.py:1553: in test_minute_history_aggregates_to_5m
tests/hat/properties/test_bars.py:1580: in test_minute_history_aggregates_to_5m
E   AssertionError: assert 'history_failed' is None
E    +  where 'history_failed' = HistoryReport(start=datetime.date(2026, 10, 21), end=datetime.date(2026, 10, 21), symbols=3, days={}, skipped=0, failed={}, rows=0, calls=1, available_from=None, clamps=0, stopped='history_failed').stopped
E   Failing test case: test_minute_history_aggregates_to_5m(
E       raw=[[1_792_553_400, 1.0, 1.0, 1.0, 1.0, 0],
E        [1_792_553_460, 1.0, 1.0, 1.0, 1.0, 0]],
E       first=0,
```

Break: `parse_candles` drops its row-shape guard (`not isinstance(row, list | tuple) or` removed), so a `None` row raises `TypeError` out of the run.

```
tests/hat/properties/test_bars.py:1553: in test_minute_history_aggregates_to_5m
tests/hat/properties/test_bars.py:1594: in test_minute_history_aggregates_to_5m
E   TypeError: object of type 'NoneType' has no len()
E   Failing test case: test_minute_history_aggregates_to_5m(
E       # The test always failed when commented parts were varied together.
```

Break (CodeAnt review): an answer that is not a mapping raises `history_failed`, so three in a row stop the run.

```
tests/hat/properties/test_bars.py:1553: in test_minute_history_aggregates_to_5m
tests/hat/properties/test_bars.py:1647: in test_minute_history_aggregates_to_5m
E   AssertionError: assert 'history_failed' is None
E    +  where 'history_failed' = HistoryReport(start=datetime.date(2026, 10, 21), end=datetime.date(2026, 10, 21), symbols=3, days={}, skipped=0, faile...ory_failed', 'NSE:ZZZ-EQ': 'history_failed'}, rows=0, calls=3, available_from=None, clamps=0, stopped='history_failed').stopped
```

Break (CodeAnt review): `_price` no longer turns a decimal overflow into `history_bad_candle` (1e300 raises `InvalidOperation` out of the run).

```
tests/hat/properties/test_bars.py:1553: in test_minute_history_aggregates_to_5m
tests/hat/properties/test_bars.py:1607: in test_minute_history_aggregates_to_5m
E   decimal.InvalidOperation: [<class 'decimal.InvalidOperation'>]
E   Failing test case: test_minute_history_aggregates_to_5m(
```

Break (CodeAnt review): an "ok" answer with missing or null `candles` is an empty range again (`response.get("candles") or []`), so the symbol is marked done with nothing.

```
tests/hat/properties/test_bars.py:1553: in test_minute_history_aggregates_to_5m
tests/hat/properties/test_bars.py:1658: in test_minute_history_aggregates_to_5m
E   AssertionError: assert {} == {'NSE:AAA-EQ'...y_bad_answer'}
E
```

### PBT bars.minute_history_budget

Break: the pacer lets one call too many into the minute (`<` to `<=` in `CallPacer.call`).

```
tests/hat/properties/test_bars.py:1623: in test_minute_history_budget
tests/hat/properties/test_bars.py:1670: in test_minute_history_budget
E   AssertionError: assert 2 <= 1
E    +  where 2 = len([datetime.datetime(2026, 10, 31, 0, 0, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')), datetime.datetime(2026, 10, 31, 0, 0, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata'))])
E   Failing test case: test_minute_history_budget(
E       # The test always failed when commented parts were varied together.
E       raw=[],  # or any other generated value
E       per_minute=1,
```

Break: the call count is not saved before the call (`self._progress.save()` removed from `CallPacer.call`).

```
tests/hat/properties/test_bars.py:1623: in test_minute_history_budget
tests/hat/properties/test_bars.py:1685: in test_minute_history_budget
E   AssertionError: assert {'2026-10-31': 0} == {'2026-10-31': 2}
E     
E     Differing items:
E     {'2026-10-31': 0} != {'2026-10-31': 2}
E     Use -v to get more diff
E   Failing test case: test_minute_history_budget(
```

Break: a new pacer starts with an empty minute (`deque(sorted(progress.recent))` to `deque()`).

```
tests/hat/properties/test_bars.py:1640: in test_minute_history_budget
tests/hat/properties/test_bars.py:1691: in test_minute_history_budget
E   AssertionError: assert 2 <= 1
E    +  where 2 = len([datetime.datetime(2026, 10, 31, 0, 5, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')), datetime.datetime(2026, 10, 31, 0, 5, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata'))])
E   Failing test case: test_minute_history_budget(
```

### PBT bars.minute_history_resume

Break: the progress is not saved after a symbol is written.

```
tests/hat/properties/test_bars.py:1690: in test_minute_history_resume
tests/hat/properties/test_bars.py:1728: in test_minute_history_resume
E   AssertionError: assert set() == {'NSE:S0-EQ'}
E     
E     Extra items in the right set:
E     'NSE:S0-EQ'
E     Use -v to get more diff
E   Failing test case: test_minute_history_resume(
```

Break: a resume asks for the period it is given instead of the stopped run's (`start, end = progress.window` to `pass`).

```
tests/hat/properties/test_bars.py: in test_minute_history_resume (a Hypothesis ExceptionGroup of two failures)
    assert resumed.skipped == len(done)
AssertionError: assert 0 == 1
    assert _stored(store, start, end) == _stored(once, start, end)
AssertionError: assert [] == [BarRow(instr...'), volume=0)]
```

### PBT bars.minute_history_plan_clamp

Break: no refusal is taken as the plan's depth (`_plan_refused` returns False).

```
tests/hat/properties/test_bars.py:1756: in test_minute_history_plan_clamp
tests/hat/properties/test_bars.py:1801: in test_minute_history_plan_clamp
E   AssertionError: assert 'history_failed' is None
E    +  where 'history_failed' = HistoryReport(start=datetime.date(2026, 10, 20), end=datetime.date(2026, 10, 23), symbols=3, days={}, skipped=0, faile...tory_failed', 'NSE:S2-EQ': 'history_failed'}, rows=0, calls=3, available_from=None, clamps=0, stopped='history_failed').stopped
E   Failing test case: test_minute_history_plan_clamp(
E       # The test always failed when commented parts were varied together.
E       raw=[],  # or any other generated value
E       late=0,  # or any other generated value
```

Break: every -50 is taken as the plan's depth (`_plan_refused` returns True after the code check).

```
tests/hat/properties/test_bars.py:1756: in test_minute_history_plan_clamp
tests/hat/properties/test_bars.py:1796: in test_minute_history_plan_clamp
E   AssertionError: assert datetime.date(2026, 10, 21) is None
E    +  where datetime.date(2026, 10, 21) = HistoryReport(start=datetime.date(2026, 10, 20), end=datetime.date(2026, 10, 23), symbols=3, days={'NSE:S0-EQ': 0, 'NS...:S2-EQ': 0}, skipped=0, failed={}, rows=0, calls=7, available_from=datetime.date(2026, 10, 21), clamps=1, stopped=None).available_from
E   Failing test case: test_minute_history_plan_clamp(
E       # The test always failed when commented parts were varied together.
E       raw=[],  # or any other generated value
E       late=0,  # or any other generated value
```

### PBT bars.minute_history_rate_limit_stops_run

Break: (PR 25) in `backend/app/hat/data/minute_history.py`, a -360 reply raises `history_failed` instead of `history_rate_limited` (a Hypothesis exception group of two failures).

```
    | AssertionError: assert None == 'history_rate_limited'
    | Failing test case: test_minute_history_rate_limit_stops_run(
    | AssertionError: assert 3 == 1
    | Failing test case: test_minute_history_rate_limit_stops_run(
```

Second break (PR 25): `history_rate_limited` removed from `_RUN_STOPS`, so the run goes on to the next symbol.

```
    | AssertionError: assert None == 'history_rate_limited'
    | Failing test case: test_minute_history_rate_limit_stops_run(
    | AssertionError: assert 4 == 1
    | Failing test case: test_minute_history_rate_limit_stops_run(
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

PR 22 break: `BarBuilder.restore` does not mark the restart as a gap.

```
E   AssertionError: assert Decimal('0.0001') is None
E    +  where Decimal('0.0001') = day_low('AAA', datetime.date(2026, 10, 21))
```

### PBT bars.session_state_never_partial

Break: `BarBuilder.from_open` returns `True` (every symbol counts as watched from the open, as before PR 22).

```
E   AssertionError: assert (Decimal('104...imal('100.0')) == (Decimal('110...mal('100.00'))
E     At index 0 diff: Decimal('104.0') != Decimal('110.00')
```

Break: `LiveData._note_gap` returns at once (no feed gap is ever marked).

```
E   AssertionError: assert Decimal('100.0000') == Decimal('90.00')
```

Break: `BarBuilder.restore` does not mark the restart as a gap (`self.mark_gap(max(bar.end ...))` to `pass`).

```
E   AssertionError: assert Decimal('100.0000') == Decimal('90.00')
E   Failing explicit example: test_session_state_never_partial(
```

Break: `LiveData._note_gap` treats a process with no tick yet as silent since the open (the first version of the fix, which lost every restored opening range).

```
E   AssertionError: assert ((Decimal('110.00'), Decimal('100.00')) is None or datetime.datetime(2026, 10, 21, 9, 25, ...) < (... + datetime.timedelta(seconds=302)))
```

Break: `LiveData.persist` does not write `coverage.json`: same failure as the previous break.

Break (CodeAnt): `LiveData.step` snapshots coverage only on a step that finalises bars (`| {trade_date_of(now)}` removed), so a gap found between bars is lost by a restart.

```
E   AssertionError: assert (Decimal('100...l('100.0000')) == (Decimal('100...imal('90.00'))
```

Break (CodeAnt nitpick): `BarBuilder.restore` marks the restart gap at the last stored bar's end, ignoring `seen_until` (`self.mark_gap(restart)`).

```
E   AssertionError: assert (Decimal('100...l('100.0000')) == (Decimal('100...imal('90.00'))
E     At index 1 diff: Decimal('100.0000') != Decimal('90.00')
```

Break: `BarBuilder._complete` does not require a feed tick at or after the end of the bars it covers (`seen = True`); the code-review case (feed silent from 09:19:55, the first bar's low printed unseen at 09:19:57) publishes a partial opening range at 09:20:02.

```
E   AssertionError: assert (Decimal('100...imal('100.0')) == (Decimal('100...imal('90.00'))
E     At index 1 diff: Decimal('100.0') != Decimal('90.00')
```

Break: (PR 39) `LiveData.on_tick` never seeds (the `_seed` call to `pass`): a late subscriber has no opening range at 09:20:02.

```
AssertionError: assert datetime.datetime(2026, 10, 21, 9, 20, 2, ...) < (datetime.datetime(2026, 10, 21, 9, 15, ...) + datetime.timedelta(seconds=302))
```

Break: (PR 39) `BarBuilder.opening_range` does not widen the seed by the first bar's ticks (`return seed.high, seed.low`).

```
E   AssertionError: assert (Decimal('110...imal('101.0')) == (Decimal('110...mal('100.00'))
```

Break: (PR 39) `BarBuilder._seed_for` ignores a gap after the seed (`return seed`). It first passed; the `@example` of a seed at 09:15:10 whose bar's low traded unseen in a gap now fails it.

```
E   AssertionError: assert (Decimal('100...imal('100.0')) == (Decimal('100...imal('90.00'))
```

Break: (PR 39) `BarBuilder.opening_range` takes a seed from after the first bar (`by` the session end). It first passed; the `@example` of a restart whose next tick knows a high from after 09:20 now fails it.

```
E   AssertionError: assert (Decimal('115...imal('100.0')) == (Decimal('110...mal('100.00'))
```

Break: (PR 39) `BarBuilder.seed` never seeds again after a gap (`if seeds:`).

```
E   AssertionError: assert datetime.datetime(2026, 10, 21, 9, 20, 2, ...) < (datetime.datetime(2026, 10, 21, 9, 15, ...) + datetime.timedelta(seconds=302))
```

### PBT bars.trigger_never_uses_a_partial_session

Break: `BarBuilder.from_open` returns `True`.

```
E   AssertionError: assert Decimal('104.00') == Decimal('110.00')
E   Failing explicit example: test_trigger_never_uses_a_partial_session(
```

Break: (PR 39) `BarBuilder.day_low` ignores the seed's low (`lows.append((seed.at, seed.low))` to `pass`).

```
E   AssertionError: assert Decimal('99.95') == Decimal('89.95')
```

### PBT bars.symbol_silence_resubscribes_and_alerts

Break: (PR 39) `SilenceWatch.check` needs more than the threshold (`>=` to `>`).

```
E   AssertionError: ('AAA', datetime.datetime(2026, 10, 21, 10, 1, 20, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')))
```

Break: (PR 39) `SilenceWatch.on_tick` does not end the silence (its update to `pass`).

```
E   AssertionError: ('AAA', datetime.datetime(2026, 10, 21, 10, 1, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')))
```

Break: (PR 39) `SilenceWatch.check` counts silence while the feed is stale (`feed_stale or` removed).

```
E   AssertionError: ('AAA', datetime.datetime(2026, 10, 21, 10, 2, 20, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')))
```

Break: (PR 39) `SilenceWatch.check` never resubscribes (the `_resubscribe` call removed).

```
E   AssertionError: ('AAA', datetime.datetime(2026, 10, 21, 10, 1, 20, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')))
```

Break: (PR 39) `SilenceWatch.check` calls `resubscribe` unguarded (`_isolated` removed).

```
E   RuntimeError: provider down
```

Break: (PR 39) `SilenceWatch.check` keeps the day's alerted names across a closed market (`self._alerted.clear()` removed). It first passed in the first version of the property, whose market closed at 15:30 and never reopened; the property now draws a closed window that reopens.

```
E   AssertionError: ('AAA', datetime.datetime(2026, 10, 21, 10, 10, 55, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')))
```

Break: (PR 39, code review) `SilenceWatch.check` does not restart the count after a resubscribe (it would ask again at every check).

```
E   AssertionError: ('AAA', datetime.datetime(2026, 10, 21, 10, 1, 25, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')))
```

Break: (PR 39, code review) `SilenceWatch.check` alerts every symbol found, not only those not yet alerted (`fresh = list(found)`).

```
E   AssertionError: ('AAA', datetime.datetime(2026, 10, 21, 10, 3, 15, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')))
```

Break: (PR 39, CodeAnt) `SilenceWatch.check` marks names alerted before the alert is sent, so a failed alert is never sent again.

```
E   AssertionError: ('AAA', datetime.datetime(2026, 10, 21, 10, 2, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')))
```

### PBT bars.delayed_seed_spoiled_by_gap

Break: (PR 39, CodeAnt) `_Seed.covered_until` is the arrival, not the earlier of the exchange time and the arrival (`return self.arrival`).

```
E   AssertionError: assert (Decimal('101'), Decimal('99')) == None
```

### PBT bars.live_flags_parsed

Break: (PR 39) `load_settings` turns the exchange range on unless it is `"0"`.

```
E   AssertionError: assert True == ((None is not None))
```

Break: (PR 39) `load_settings` defaults the silence to 30 seconds.

```
E   AssertionError: assert 30 == 120
```

Break: (PR 49) `load_settings` defaults the entry timeout to 5 minutes.

```
E   AssertionError: assert 5 == 60
```

### PBT bars.seed_needs_ordered_prices

Break: (PR 39) `BarBuilder.seed` takes any prices (`or not Decimal(0) < low <= tick.price <= high` removed).

```
E   assert True == False
E   Failing explicit example: test_seed_needs_ordered_prices(
```

Break: (PR 39) `BarBuilder.seed` takes a tick outside the session (`start is None or` removed).

```
E   AttributeError: 'NoneType' object has no attribute 'date'
```

### PBT bars.gap_record_from_its_own_day

Break: `PreopenGaps.observe` takes the first-tick open at any time (`local.time() >= SESSION_OPEN` to `True`).

```
E   AssertionError: assert Decimal('1000.0') in {Decimal('1010'), Decimal('2000')}
E   Failing explicit example: test_gap_record_from_its_own_day(
E       seen=[(0, 540, False), (1, 540, False)],
```

Break: `PreopenGaps.record` keeps earlier days' records (the pruning loop to `pass`).

```
E   AssertionError: assert {'AAA': GapRe...a/Kolkata')))} == {}
```

Break: `PreopenGaps.records` ignores the trade date (records keyed by symbol only, as before PR 22).

```
E   AssertionError: assert datetime.date(2026, 10, 21) == datetime.date(2026, 10, 22)
```

### PBT bars.usable_price

Break: (PR 51) in `backend/app/hat/data/live.py`: `usable_price` drops the age check (`now - quote.at >= stale_after or …` became `quote.at - now >= stale_after`).

```
E   AssertionError: assert (Decimal('1'), None) == (None, 'stale_price')
E   Failing explicit example: test_usable_price(
E       price=100,
E       age_ms=30000,
```

Break: (PR 51) in `backend/app/hat/data/live.py`: a quote exactly 30 s old still counts (`>=` became `>`).

```
E   AssertionError: assert (Decimal('1'), None) == (None, 'stale_price')
E   Failing explicit example: test_usable_price(
E       price=100,
E       age_ms=30000,
```

Break: (PR 51) in `backend/app/hat/data/live.py`: a quote stamped 30 s or more ahead of the clock is accepted (the future-skew half removed).

```
E   AssertionError: assert (Decimal('1'), None) == (None, 'stale_price')
E   Failing explicit example: test_usable_price(
E       price=100,
E       age_ms=-30000,
```

Break: (PR 51) in `backend/app/hat/data/live.py`: the band check is removed (`if False:`).

```
E   AssertionError: assert (Decimal('0.02'), None) == (None, 'price_out_of_band')
E   Failing test case: test_usable_price(
E       price=2,
E       age_ms=0,
```

### PBT bars.price_book_dates_each_tick

Break: (PR 51, CodeAnt) in `backend/app/hat/daily/live.py`: `PriceBook.on_tick` keeps every print, so a late one (older exchange time) replaces the newer quote.

```
E   AssertionError: assert Quote(price=D...timezone.utc)) == Quote(price=D...timezone.utc))
E       price: Decimal('2') != Decimal('1')
E   Failing explicit example: test_price_book_dates_each_tick(
E       ticks=[(0, 100, 0, 1), (0, 200, -5, 1)],
E   )
```

Break: (PR 51) in `backend/app/hat/data/live.py`: `tick_from_payload` dates every tick by its arrival (`when = arrival`), ignoring `exch_feed_time`.

```
E   AssertionError: assert Quote(price=D...ia/Kolkata'))) == Quote(price=D...timezone.utc))
E     Differing attributes:
E     ['at']
E       at: datetime.datetime(2026, 10, 21, 11, 0, tzinfo=zoneinfo.ZoneInfo(key='Asia/Kolkata')) != datetime.datetime(2026, 10, 21, 5, 29, 59, tzinfo=datetime.timezone.utc)
```

### PBT bars.circuit_locks_read_the_band

Break: (PR 51) in `backend/app/hat/engine/trigger.py`: `lower_circuit_price` rounds down to the tick grid (`floor_to_tick`) instead of up.

```
E   AssertionError: assert Decimal('0.98') <= Decimal('0.95')
E   Failing test case: test_circuit_locks_read_the_band(
E       prev=100,
E       band=Decimal('2'),
E       tick=Decimal('0.05'),
```

Break: (PR 51) in `backend/app/hat/engine/trigger.py`: `locked_lower_circuit` is strict (`price <= lower` became `price < lower`).

```
E   AssertionError: assert False is (Decimal('95.00') <= Decimal('95'))
E   Failing explicit example: test_circuit_locks_read_the_band(
E       prev=10000,
E       band=Decimal('5'),
```

Break: (PR 51) in `backend/app/hat/engine/trigger.py`: `locked_lower_circuit` ignores `band_known` (an unknown band reads as F&O).

```
E   AssertionError: assert False is None
E    +  where False = locked_lower_circuit(Decimal('1'), DailyInputs(prev_close=Decimal('1'), avg50_volume=None, adv20=None, adtr20=None, tick=None, band_known=False, band_pct=None, atr=None, swing_lows=()))
```

### PBT bars.exits_and_placement_read_the_checked_price

Break: (PR 51) in `backend/app/hat/daily/live.py`: `band_bounds` has no tick of slack (`slack = Decimal(0)`).

```
E   AssertionError: assert (Decimal('90'...ecimal('110')) == (Decimal('89....mal('110.05'))
E     At index 0 diff: Decimal('90') != Decimal('89.95')
```

Break: (PR 51) in `backend/app/hat/daily/live.py`: `LivePrices.checked` ignores the band (`usable_price(quote, at)`).

```
E   AssertionError: assert (ExitView(price=Decimal('110.06'), days_held=0, sma={10: Decimal('110.06')}, swing_low={}, regime_off=False) is None) == (None is None)
E   Failing explicit example: test_exits_and_placement_read_the_checked_price(
E       price=11006,
E       age_s=0,
```

Break: (PR 51) in `backend/app/hat/daily/live.py`: `LivePrices.price` (the exits' price) returns the book's raw last price, unchecked.

```
E   AssertionError: assert (ExitView(price=Decimal('110.06'), days_held=0, sma={10: Decimal('110.06')}, swing_low={}, regime_off=False) is None) == (None is None)
E   Failing explicit example: test_exits_and_placement_read_the_checked_price(
E       price=11006,
E       age_s=0,
```

Break: (PR 51) in `backend/app/hat/daily/live.py`: `LiveRoute.check` drops the refusal (`ltp_refusal=None`).

```
E   AssertionError: assert (None, None) == (None, 'price_out_of_band')
E     At index 1 diff: None != 'price_out_of_band'
```

Break: (PR 51) in `backend/app/hat/execution/router.py`: `placement_refusal` lets an entry through with no price (`if check.ltp is None:` became `if False:`).

```
E   assert (None is None) == (None is not None)
E   Failing explicit example: test_exits_and_placement_read_the_checked_price(
E       price=11006,
E       age_s=0,
```

---
name: verify-live-data
description: How to run, read and update the checks behind Hire a Trader's live data - the tick listener hook on LiveMarketService, 5-minute bars, opening range, volume pace, feed staleness, pre-open gap capture, the 09:16 gap scan, the "hat" subscription owner, bars_5m Parquet and the NSE announcement feeds (Hypothesis properties bars, gapscan, announcements; CONF single-feed import rule). Use before and after any change to backend/app/hat/data/bars.py, live.py, staleness.py, subscriptions.py, preopen.py, gapscan.py, announcements.py, bars_store.py or the add_tick_listener hook in live_market_service.py.
---

# verify-live-data

Checks that HAT reads one shared tick feed without ever slowing or breaking it, that 5-minute bars are aligned to 09:15 IST and never change once final, that a silent feed is flagged exactly when the threshold passes, that the 09:16 gap scan selects only candidates that can really gap 10%, and that announcements are stored once and matched by instrument and time only. Specs: `docs/specs/hat/02-data.md` §10-§12 (HAT-DATA-100..104, 110..118, 120..122), `00-overview.md` (HAT-OVR-006), `01-decisions.md` (HAT-DEC-I3, I6, B8..B10), `04-criteria-and-bots.md` (HAT-BOT-052), `06-risk-and-governance.md` (HAT-RISK-063, the health side).

No TLA+ model: `11-verification.md` §3 assigns none to this area. The ordering promises that involve it are `ExecutionRouter!NoOrderWhenStale` and `JobScheduler!IntradayOrder`, which arrive with PR 15 and PR 17.

## Properties

- `PBT bars.alignment` For every tick sequence, in any arrival order, every in-session tick is in exactly the bar starting at 09:15 + 5 min x floor((t - 09:15) / 5 min) in IST, and each bar's OHLCV is the first, max, min and last price and summed volume of its ticks (by exchange time); a tick outside [09:15, 15:30) is in no bar. Harm: an opening-range high or a pivot break is judged on the wrong bar.
- `PBT bars.alignment_finality` No bar is reported before its end plus 2 s, each bar once, and a final bar never changes: a tick arriving at or after the final instant is dropped and counted, so reported bars equal the bars of the on-time ticks. Harm: a trigger fires on a bar that later changes, so live and replay disagree.
- `PBT bars.alignment_boundary_instants` A bar is not final one microsecond before its end plus 2 s and is final at that instant. Harm: the 2-second lag is off by one tick of the clock.
- `PBT bars.opening_range_fixed` Before 09:20:02 there is no opening range; from then it equals the first bar's high and low and never changes, even when first-bar ticks arrive later. Harm: the ORB trigger level drifts and entries are late or never fire.
- `PBT bars.volume_pace` For t in (09:15, 15:30] pace(t) x (elapsed / 375 min) = V exactly, and there is no pace outside that range. Harm: an early trickle projects to a huge pace and fires false breakouts.
- `PBT bars.volume_pace_builder` The builder gives no pace before a symbol's first tick of the session, and afterwards the pace of the day volume of every in-session tick. Harm: pace is read before any data exists.
- `PBT bars.tick_volume_delta` A tick's volume is the change in cumulative day volume, never negative; the first tick contributes only its own print. Harm: bar volume and pace count volume traded before HAT subscribed, or go negative.
- `PBT bars.staleness_trip` The feed is stale at t exactly when t is in market hours and no tick arrived in (t - 30 s, t]; each transition into stale reaches the health sink once and each recovery once. Harm: orders are placed on a frozen tape, or a transient gap leaves a bot disabled.
- `PBT bars.staleness_boundary` Silence of exactly 30 s is stale and 30 s minus one microsecond is not. Harm: the threshold is off by one.
- `PBT bars.staleness_events_recorded` Each transition into stale writes exactly one `feed.stale` event and calls the health kill once; recovery clears the kill and writes nothing. Harm: the owner is never alerted, or is alerted repeatedly.
- `PBT bars.listener_isolation` With any set of listeners, any subset of which raises, every tick still reaches `latest_values` and the broadcast batch, and every listener that did not raise for a tick got it, in order. Harm: a HAT bug freezes SmartTrader's live quotes.
- `PBT bars.subscription_set` The `"hat"` owner's set equals watchlist, open positions and candidates; positions are always in it; the provider holds exactly the symbols some owner holds, and releasing HAT never unsubscribes a symbol another owner holds; a set at or above the limit alerts. Harm: an open position stops receiving prices so its exits never fire.
- `PBT bars.subscription_inputs` The inputs are today's watchlist symbols, every open position's symbol (not closed ones, not other days' watchlists) and the candidates. Harm: stale symbols hold feed budget, or an open position is missed.
- `PBT bars.persisted_equals_live` The `bars_5m` rows equal the final bars the bar listener saw, one row per (instrument, bar_start), exactly the spec's columns, however writes failed and re-ran. Harm: replays and audits cannot reproduce a live decision.
- `PBT bars.gap_pct` The recorded gap is 100 x (o / c - 1), from the indicative price inside 09:00-09:08 else the first tick's open, and the record names its source; a later first tick never replaces a record. Harm: EP gap filters use a wrong gap.
- `PBT gapscan.covers_universe` The scan evaluates every EQ symbol exactly once with the exact gap, and no other series. Harm: a gapper is missed because a page was truncated.
- `PBT gapscan.filter` The selected set equals {gap >= 10 and universe passes and (band 10 or 20, or F&O)}; a stock with no price-band row is never selected; every selected symbol is handed on to be subscribed. Harm: impossible gaps are chased or valid EP candidates never watched.
- `PBT gapscan.timing_recorded` Every gap-scan run, successful or failed, has a `hat_job_run` row with start and finish, and a run finishing at or after 09:20:00 IST raises an alert. Harm: the EP playbook silently never sees its candidates in time.
- `PBT announcements.poll_schedule` The next poll is 2 minutes after t from 07:30 (inclusive) to 15:30 (exclusive) IST and 15 minutes otherwise. Harm: a 09:10 catalyst is missed by the 09:20 trigger.
- `PBT announcements.poll_schedule_edges` The window edges 07:29:59, 07:30:00, 15:29:59 and 15:30:00. Harm: the cadence changes at the wrong second.
- `PBT announcements.dedupe` For every sequence of polls with overlapping items, stored rows equal distinct GUIDs. Harm: one announcement is attached and shown several times.
- `PBT announcements.dedupe_recorded_fixtures` The recorded feeds: a GUID listed twice stores once, repeated polls add nothing, and an item with no GUID gets a stable derived one. Harm: the real feed shape breaks the dedupe.
- `PBT announcements.unmatched_kept` Every polled item has a row, `raw` equals the item as received, an unmatched item has a null instrument until a later poll finds it. Harm: a catalyst for a renamed stock is lost.
- `PBT announcements.match_window` An item is attached iff its instrument is the candidate's and t - within_hours <= published_at <= t. Harm: a stale or unrelated headline is shown as the reason to take a trade.
- `PBT announcements.text_never_gates` Items identical except title, category text and URL give the same `catalyst_attached` outcome. Harm: a text heuristic becomes an unreviewed trading rule.
- `CONF imports.no_second_websocket` No module under `app.hat` imports a broker data-socket or WebSocket library, and the single tick input is `add_tick_listener`. Harm: a second socket breaks the broker session and disconnects SmartTrader's feed.
- `CONF single_feed_import_rule` HAT imports neither the live service module nor the provider session; live data arrives only through the listener hook and the `"hat"` registry owner. Harm: HAT could read or open a feed some other way.

## Run

```bash
cd backend
CI=1 python -m pytest tests/hat/properties/test_bars.py tests/hat/properties/test_gapscan.py \
  tests/hat/properties/test_announcements.py -q --no-cov     # about 40 s under the ci profile
python -m pytest tests/hat/conformance/test_conf_imports.py -q --no-cov
# the existing live and stream tests must stay green, unmodified:
python -m pytest tests/test_live_market_characterization.py tests/test_live_market_regressions.py \
  tests/test_live_ticks.py tests/test_websocket_stream_characterization.py \
  tests/test_websocket_stream_origin.py tests/test_subscription_registry.py -q --no-cov
```

No network, broker or database server is touched: ticks come from generated payloads, MCP rows from `PricedListingServer`, feeds from `rss_item`/`rss_document` and two recorded fixtures (`rss_announcements.xml`, `rss_financial_results.xml`, `mcp_cm_equity_stocks_reli.json`), and the database is in-memory SQLite.

## Bug or model error

A failing property prints the falsifying example and a `reproduce_failure` blob. Replay the example by hand with the real functions first.

- Bars: feed the printed ticks to `BarBuilder.on_tick(tick, now)` and `advance(now)`. If the bar the code returns disagrees with the spec rule (bar start by integer arithmetic on seconds of the IST day, open and close by exchange time, a tick arriving at or after end + 2 s is late), it is a bug in `bars.py`. The oracle `expected_bars` is the spec rule, not the code: change it only if the spec changes.
- Staleness: the rule is the property's, "no tick in (t - 30 s, t]". The statement in 02 §11 says "more than 30 seconds"; the two disagree at exactly 30 s and the acceptance wins (recorded under Scope changes).
- Listener isolation: a failure means an exception reached the broadcast path or another listener; fix `LiveMarketService._notify_tick_listeners`, never the property.
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
- `backend/app/services/live_market_service.py`: `add_tick_listener`, `_notify_tick_listeners`

Checks:
- `backend/tests/hat/properties/test_bars.py`
- `backend/tests/hat/properties/test_gapscan.py`
- `backend/tests/hat/properties/test_announcements.py`
- `backend/tests/hat/conformance/test_conf_imports.py`
- `backend/tests/hat/hat_live_support.py`
- `backend/tests/hat/fixtures/rss_announcements.xml`
- `backend/tests/hat/fixtures/rss_financial_results.xml`

## Sync commit

The verification above was last confirmed against the covered files at this commit (a squash merge discards a branch commit, so the digest below, a sha256 over the covered files, is what CI compares):

Sync commit: `SYNCSHA`
Covered digest: `SYNCDIGEST`

## Scope changes

Every narrowing of a property or strategy, with its reason (HAT-VER-034):

- `bars.staleness_trip` and `bars.staleness_boundary`: HAT-DATA-116's statement said "more than 30 seconds" and its acceptance said no tick in (t - 30 s, t], which disagree at exactly 30 s. The acceptance wins; the statement in `02-data.md` was edited to "30 seconds or more".
- The session is the half-open interval [09:15, 15:30) IST; a tick at 15:30:00 or later is in no bar.
- A late tick (at or after its bar's end + 2 s) is left out of bars but still counts in the day volume, so volume pace stays right.
- `bars.listener_isolation` and `bars.persisted_equals_live` use a reduced `max_examples` and bounded sequences, because each example builds a service or a Parquet directory.
- `announcements.text_never_gates` draws text of at most 10 characters, at most 8 stored items, 40 examples and has no shrink phase: a broken variant fails on almost every example and shrinking over arbitrary text grew without bound. The failing example is still printed, unminimised. Database engines are disposed as examples go.
- Pre-open field names (`indicative_open_price`, `pre_open_price`) and the RSS field names and URLs are `[A]`, built from recorded fixtures only.
- `bars.history_matches_builder` (HAT-DATA-119) belongs to PR 12 and is not checked here.

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

Break: in `app/services/live_market_service.py`, replaced `try: listener(tick) except Exception as exc: # isolate: never break the stream for a liste` with `listener(tick)`.

```
tests/hat/properties/test_bars.py F
tests/hat/properties/test_bars.py:454: in test_listener_isolation
tests/hat/properties/test_bars.py:510: in test_listener_isolation
tests/hat/properties/test_bars.py:489: in scenario
tests/hat/properties/test_bars.py:450: in __call__
E   RuntimeError: listener bug
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

Break: in `app/hat/data/live.py`, replaced `import asyncio import logging` with `import asyncio import websockets # noqa import logging`.

```
tests/hat/conformance/test_conf_imports.py F
tests/hat/conformance/test_conf_imports.py:154: in test_imports__no_second_websocket
E   AssertionError: assert [('live.py', 'websockets')] == []
E     
E     Left contains one more item: ('live.py', 'websockets')
E     
```

### CONF single_feed_import_rule

Break: in `app/hat/data/subscriptions.py`, replaced `import logging import threading` with `import logging import threading from app.services import live_market_service # noqa`.

```
tests/hat/conformance/test_conf_imports.py F
tests/hat/conformance/test_conf_imports.py:176: in test_single_feed_import_rule
E   AssertionError: subscriptions.py
E   assert not True
E    +  where True = any(<generator object test_single_feed_import_rule.<locals>.<genexpr> at 0x7f759beea840>)
FAILED tests/hat/conformance/test_conf_imports.py::test_single_feed_import_rule - AssertionError: subscriptions.py
```

# 02: Data

Scope: every data feed HAT reads, how it is fetched, checked, stored and derived, from reference data to the nightly ingest, the historical build, features, announcements and live 5-minute bars. Requirement format and verification methods: see README.md and 11-verification.md.

Times are IST (exchange-local) unless stated. "Trade date" is the NSE session date. "Active universe" is the set of active EQ/BE instruments in `hat_instrument` on the trade date.

## §1 Source map

The MVP runs on four data feeds: NSE MCP for nightly prices and corporate actions, NSE RSS for announcements, Fyers for live data, and NSE bhavcopy archives for history and fallback. Copied from DS "Source map" (tested 20 Oct 2026; *verify* items not yet tested):

| Need | Source | When | Fallback | Status |
| --- | --- | --- | --- | --- |
| Daily OHLCV, all EQ symbols | NSE MCP `get_bulk_quote` (50 per call) | Poll from ~18:00 IST until date = today | NSE bhavcopy CSV download | Tested 20 Oct: works; data for 20 Oct not yet loaded at ~18:00 |
| Repair a missed day for one symbol | NSE MCP `get_stock_history` (≤ 3 months per call) | On demand | Bhavcopy download | Tested schema |
| Equity universe list | NSE MCP `cm_get_equity_stocks` (≤ 500 per call, prefix filter) | Weekly | NSE equity list CSV | Tested: 3,121 equity symbols listed |
| Corporate actions (split, bonus) | NSE MCP `get_corporate_actions` (adjustment factor) | Nightly for symbols with events | NSE Corporate Actions RSS | Tested schema |
| Index levels and India VIX (regime engine) | NSE MCP `get_index_snapshot` (~170 indices) | Nightly | Index CSV download | Tested schema |
| Market breadth (regime engine) | NSE MCP `get_market_breadth` | Nightly | none | Tested schema |
| Live ticks, 5-min bars, pre-open | Fyers WebSocket (SmartTrader feed) | 09:00–15:30 | None in MVP | *verify* pre-open fields |
| Intraday history for EP backtest | Fyers historical API | One-off | Paid vendor | *verify* depth |
| Announcements, results | NSE RSS: Announcements, Financial Results | Every 2 min, 07:30–15:30 | Tapetide news (chat only) | NSE RSS page |
| Price bands | NSE price-band file | Nightly | Broker quote circuit limits | *verify* file location |
| Industry classification | NSE equity list / index constituents | Weekly | Tapetide (chat only) | *verify* field |
| Trading holidays | NSE holiday list | Yearly + updates | Broker holiday API | none |
| Fundamentals (P2) | NSE/BSE XBRL filings, stamped with filing time | Later | none | P2 (PL5) |
| 2010+ history | NSE bhavcopy archives (two formats) | One-off build | Bhavcopy archive download | §9 |

The NSE MCP returns volume but no traded value; turnover is computed as close × volume (§7).

#### HAT-DATA-010: Engine data comes only from the source map
- **Statement:** Every input that can influence a scan, a signal or an order comes from a source in the §1 map; research MCPs (Tapetide and others) are chat-side only and never feed a scan or an order.
- **Source:** DS "MCP role and open verifications"; DS "Source map"; I1, I16.
- **PR:** 4
- **Verification:** CONF `imports.engine_data_sources`
- **Acceptance:**
  - Property: for every module under `backend/app/hat/`, the only MCP servers it can address are `nse-bhavcopy` and `cm-market`, and no module under `app/hat/engine`, `app/hat/risk` or `app/hat/execution` imports a research or chat MCP client. Harm: an unvetted research source silently changes which stocks are scanned or traded.

#### HAT-DATA-011: Bhavcopy is a drop-in replacement for the MCP price pull
- **Statement:** The bhavcopy path produces raw daily bars in the same normalised schema as the MCP path, so either can supply a trade date.
- **Source:** DS "Nightly ingest from NSE MCP" (open item on NSE terms); DS "Historical data build"; MA "Risks and open items"; I1.
- **PR:** 5, 7
- **Verification:** PBT `ingest.sources_same_schema`
- **Acceptance:**
  - Property: for every trade date and every set of source rows, the MCP normaliser and the bhavcopy normaliser emit rows with the identical column set and types of `bars_daily_raw` (§8), differing only in `source`, `ingest_run_id` and `traded_value` (null from MCP). Harm: if NSE's MCP terms rule out automated use, or the MCP is down, the system cannot fall back without a schema break.
- **[OWNER-PENDING]** NSE MCP terms for automated personal use: default is to keep using the MCP with bhavcopy as the drop-in fallback (DS open item); owner to confirm with NSE.

## §2 Reference data

One canonical instrument ID, ISIN + exchange + series, keys every table. Broker symbols and NSE symbols are attributes that can change without breaking history (DS "Reference data").

| Dataset | Key fields | Refresh | Notes |
| --- | --- | --- | --- |
| Instrument master | ISIN, exchange, series, NSE symbol, name, listing/delisting date, face value | Weekly + on corporate action | Delisted rows kept (survivorship-free backtests) |
| Broker symbol map | instrument ID, broker, broker symbol/token | On adapter start | Fyers style `NSE:SBIN-EQ` via `symbol_master.to_fyers` (OWN-20OCT: no OpenAlgo mapping in MVP) |
| Price band | instrument ID, band % (2/5/10/20/none), effective date | Nightly | Feeds `price_band_allowed` and the EP band check |
| Industry | instrument ID, sector, industry | Weekly | Feeds the per-industry cap |
| Corporate actions | instrument ID, ex-date, type, adjustment factor, raw text | Nightly | §5 |
| Holidays | date, exchange, segment | Yearly + changes | Scheduler skips non-trading days |
| Index levels | index name, date, OHLC | Nightly | Regime gate and RS benchmark (§8 `index_daily`) |

#### HAT-DATA-020: Instrument identity is (market, ISIN, series)
- **Statement:** An instrument is identified by (market_id, isin, series); its NSE symbol and broker symbols are mutable attributes.
- **Source:** DS "Reference data"; SCH "Reference and market data" (`hat_instrument` unique (market_id, isin, series)).
- **PR:** 4
- **Verification:** PBT `reference.instrument_identity_stable`, DB `reference.instrument_unique`
- **Acceptance:**
  - Property: for every sequence of weekly reference refreshes, including symbol renames, name changes and face-value changes, an instrument's `id` never changes while its (market_id, isin, series) is unchanged, and no two rows share (market_id, isin, series). Harm: a rename splits one stock's history into two instruments, breaking features, positions and backtests.

#### HAT-DATA-021: Delisted instruments are kept
- **Statement:** A delisted instrument is marked (`delisted_on` set, `active = false`), never deleted.
- **Source:** DS "Reference data" ("Delisted rows kept (survivorship-free backtests)"); DS "Historical data build".
- **PR:** 4
- **Verification:** PBT `reference.delisted_never_deleted`
- **Acceptance:**
  - Property: for every sequence of refreshes in which an instrument disappears from the source listing, the instrument row still exists afterwards with `active = false` and `delisted_on` set, and the count of `hat_instrument` rows never decreases. Harm: backtests become survivorship-biased and overstate returns.

#### HAT-DATA-022: Symbol renames are recorded with non-overlapping validity
- **Statement:** A symbol change writes a `hat_instrument_symbol_history` row so that old symbols in history files resolve to the right instrument.
- **Source:** SCH "Reference and market data" (`hat_instrument_symbol_history`); DS "Historical data build" step 3 ("symbol changes resolved through ISIN").
- **PR:** 4
- **Verification:** PBT `reference.symbol_history_resolves`
- **Acceptance:**
  - Property: for every sequence of renames, the validity intervals [valid_from, valid_to) of one instrument's symbol rows never overlap, and for every (symbol, date) the resolver returns at most one instrument, namely the one whose row covers that date. Harm: an old symbol in a 2015 bhavcopy is attached to the wrong company.
  - Property: seeding from an injected rename source (HAT reads no legacy table) is idempotent: running it twice yields the same rows. Harm: duplicate history rows make symbol resolution ambiguous.

#### HAT-DATA-023: Weekly equity list refresh is complete
- **Statement:** The instrument master is refreshed weekly from `cm_get_equity_stocks`, prefix-paged A–Z (≤ 500 per call), with the NSE equity list CSV as fallback.
- **Source:** DS "Source map"; DS "Nightly ingest" step 2; I1.
- **PR:** 4
- **Verification:** PBT `reference.prefix_paging_complete`
- **Acceptance:**
  - Property: for every listing of symbols (any size, any distribution of first letters), the paged refresh yields every listed symbol exactly once, and a page returned at the 500-row cap is never accepted as complete for its prefix. Harm: silently truncated pages drop stocks from the universe, so their setups are never scanned and their positions lose reference data.
  - Property: when the MCP refresh fails, the CSV fallback produces the same instrument keys for the same listing. Harm: the master goes stale for a week.

#### HAT-DATA-024: Fyers broker symbols round-trip through symbol_master
- **Statement:** `hat_broker_symbol` rows for Fyers are produced with `symbol_master.to_fyers` (HAT's own `app/hat/data/symbols.py` since K2) and refreshed on adapter start; unique (broker, broker_symbol). The adapter's instrument list is read from these rows (`fyers_instruments`): active instruments only, never an index. [Reworded in K2; the legacy `symbol_master.known_symbols` returned only indices.]
- **Source:** DS "Reference data" (broker symbol map); SCH (`hat_broker_symbol`); OWN-20OCT (direct Fyers adapter, no OpenAlgo mapping).
- **PR:** 4
- **Verification:** PBT `reference.fyers_symbol_roundtrip`, DB `reference.broker_symbol_unique`
- **Acceptance:**
  - Property: for every active instrument with a Fyers row, `symbol_master.to_db(broker_symbol)` equals the instrument's NSE symbol and no two instruments share a Fyers `broker_symbol`. Harm: an order for one stock is sent under another stock's broker symbol.

#### HAT-DATA-025: Price band is the latest row on or before the date
- **Statement:** Price bands (2/5/10/20/none) load nightly from the NSE price-band file, with broker quote circuit limits as fallback; the current band is the latest row per instrument.
- **Source:** DS "Source map" and "Reference data"; SCH (`hat_price_band`, "Latest row per instrument is current").
- **PR:** 4
- **Verification:** PBT `reference.current_band`
- **Acceptance:**
  - Property: for every instrument and date d, the band used by criteria on d is the `band_pct` of the row with the greatest `effective_from ≤ d` (null meaning no band), and `band_pct ∈ {2, 5, 10, 20, null}`. Harm: an EP candidate in a 5% band is treated as gap-eligible, or a valid one is excluded.
- **[A]** Price-band file location and fields: default parse a recorded fixture of the NSE file and fall back to broker quote circuit limits; validated on the first live download (DS "Verify before or during build").

#### HAT-DATA-026: Industry classification feeds the industry cap
- **Statement:** Sector and industry per instrument load weekly from the NSE equity list / index constituents into `hat_industry`; the latest row per instrument is current.
- **Source:** DS "Reference data"; SCH (`hat_industry` "Feeds the per-industry cap").
- **PR:** 4
- **Verification:** PBT `reference.current_industry`
- **Acceptance:**
  - Property: for every instrument and date d, the industry used by the per-industry cap is the row with the greatest `effective_from ≤ d`, and an instrument with no row is reported (never silently assigned to an industry shared with others). Harm: the 2-per-industry cap (06) is bypassed or misapplied.
- **[A]** Industry field location: default from the NSE equity list; validated on the first download (DS "Verify").

#### HAT-DATA-027: Trading calendar drives market hours
- **Statement:** NSE holidays load into `hat_trading_calendar` (yearly + on change, broker holiday API as fallback) and HAT's `MarketHoursPolicy` (`app/hat/data/sessions.py`) is built from the loaded calendar (`MarketCalendar.policy`). Nothing is installed process-wide and there is no `DEV_MODE` bypass. [Changed in K1; it fed the legacy `market_hours.DEFAULT_POLICY`.]
- **Source:** DS "Source map" and "Reference data"; SCH (`hat_trading_calendar`); PLAN C7.
- **PR:** 4, 17
- **Verification:** PBT `calendar.holiday_closed`, PBT `calendar.weekday_rows`
- **Acceptance:**
  - Property (PR 17): `calendar.refresh` stores a row for every weekday of the years the holiday list covers: a listed day as listed, every other weekday as a trading day with the market's default session, and no weekend. Harm: the scheduler and the risk checks, which read a session only from a stored row, treat every ordinary weekday as closed.
  - Property: for every date with `is_trading = false` in `hat_trading_calendar`, `MarketHoursPolicy` reports the market closed at every instant of that date, so every HAT order path refuses it; for every date with `is_trading = true`, the session is `open_at`–`close_at`. Harm: orders are attempted on an exchange holiday, or the scheduler runs scans for a day with no data.
  - Property: for every calendar, the scheduler's set of trading days equals the set of dates with `is_trading = true`. Harm: jobs run on holidays or skip trading days.

## §3 NSE MCP client

#### HAT-DATA-030: Thin MCP client behind a transport protocol
- **Statement:** `app/hat/data/mcp_client.py` is a thin JSON-RPC-over-HTTP MCP client (`tools/call`) behind a `McpTransport` protocol, with one configurable endpoint per server (`HAT_MCP_BHAVCOPY_URL` for `nse-bhavcopy`, `HAT_MCP_CMMARKET_URL` for `cm-market`). `HttpTransport` speaks MCP streamable HTTP: it opens one session per server (`initialize`, then `notifications/initialized`) and sends every `tools/call` with that session's `Mcp-Session-Id`; a 404 means the server lost the session (the NSE servers run several replicas), so the call is sent once more on a new session, and a session lost again is dropped; a 404 on a handshake step starts the handshake once more, and an `initialize` answered with a JSON-RPC error opens no session. Each server has its own session lock. [Changed in PR 23: the live servers answered a bare `tools/call` with HTTP 400 and about a third of in-session requests with 404, live checks 2026-10-22.]
- **Source:** DS "Nightly ingest from NSE MCP" ("calls the MCP server over HTTP with the Python MCP client"); PLAN PR 4, §7; live checks 2026-10-22.
- **PR:** 4, 23
- **Verification:** CONF `imports.mcp_transport_only`, PBT `mcp.session_handshake`
- **Acceptance:**
  - Property: every MCP call made by HAT code passes through `McpTransport`, and no other HAT module opens HTTP connections to an MCP endpoint, so every call can be replaced by a recorded fixture. Harm: a hidden network path calls real NSE from tests or bypasses retry, limits and logging.
  - Property: for every sequence of calls and session losses, `HttpTransport` sends no `tools/call` outside a session the server opened and initialized, resends a call whose session was lost exactly once on a new session, and otherwise keeps its session. Harm: every live MCP call fails, or a third of them do.
- **[OWNER-PENDING]** Client implementation: default a thin JSON-RPC client on the existing HTTP stack; adopt the official `mcp` SDK only if it locks without moving the fastapi/starlette pins (PLAN Q1); owner to confirm.

#### HAT-DATA-031: Bulk quotes are fetched in batches of at most 50
- **Statement:** `get_bulk_quote` is called with at most 50 symbols per call.
- **Source:** DS "Nightly ingest" step 3; MA "Your overrides" I1 (50 symbols per bulk call, ~63 calls for 3,121 symbols).
- **PR:** 4, 7
- **Verification:** PBT `ingest.batches_le_50`
- **Acceptance:**
  - Property: for every list of n symbols, the batches partition the list (each symbol in exactly one batch), each batch has at most 50 symbols, and the number of batches is ⌈n / 50⌉ (63 for n = 3,121). Harm: oversize calls are rejected by the server and whole batches of stocks go missing.

#### HAT-DATA-032: At most 4 MCP calls in flight
- **Statement:** The client runs at most 4 MCP calls concurrently.
- **Source:** DS "Nightly ingest" step 3 ("4 in parallel (*assumption*; lower if the server throttles)").
- **PR:** 4, 7
- **Verification:** PBT `ingest.concurrency_bounded`
- **Acceptance:**
  - Property: for every workload and every interleaving of call completions, the number of in-flight calls observed by the transport never exceeds the configured limit (default 4). Harm: the server throttles or bans the client and the night's data is late or missing.
- **[A]** Parallelism 4: default 4; validated by observing NSE MCP rate limits for 2 weeks (DS "Verify before or during build").

#### HAT-DATA-033: Retries are bounded with backoff
- **Statement:** A failed MCP call is retried at most 3 times with increasing backoff.
- **Source:** DS "Nightly ingest" step 3 ("retry ×3 with backoff").
- **PR:** 4
- **Verification:** PBT `mcp.retry_bounded`
- **Acceptance:**
  - Property: for every sequence of transport failures, a logical call makes at most 4 attempts (1 + 3 retries), the delay before each retry is strictly greater than the previous one, and after the last failure the call raises a typed error rather than returning an empty result. Harm: unbounded retries stall the nightly window, and an empty result is mistaken for "no data" and passes as a holiday.

#### HAT-DATA-034: Every MCP call is logged
- **Statement:** Every MCP call is logged with tool, symbol count and returned date.
- **Source:** DS "Nightly ingest" step 3 ("every call logged with symbol count and returned date").
- **PR:** 4
- **Verification:** PBT `mcp.call_logged`
- **Acceptance:**
  - Property: for every call (success, retry or failure), exactly one log record per attempt is written containing the tool name, the number of symbols requested and the returned data date (or the failure code), and the job's `hat_job_run.calls` equals the number of attempts. Harm: a short or stale pull cannot be diagnosed after the fact.

#### HAT-DATA-035: MCP errors never leak raw text
- **Statement:** MCP transport and tool errors surface as typed error codes; raw server text never reaches alerts, API responses or Telegram.
- **Source:** `.claude/skills/trading-safety/SKILL.md` (no broker or DB error text in responses, carried by CLAUDE.md); PLAN PR 4 tests.
- **PR:** 4
- **Verification:** PBT `mcp.no_raw_error_text`
- **Acceptance:**
  - Property: for every error payload string returned by the transport, no user-facing message (alert text, API error, Telegram text) contains that string or any substring of it longer than 8 characters. Harm: internal URLs, tokens or stack traces leak to an outside channel.

#### HAT-DATA-036: A date mismatch is reported, never stored as the trade date
- **Statement:** Rows whose returned date differs from the requested trade date are reported and not stored under that trade date.
- **Source:** DS "Nightly ingest" steps 1 and 4 ("date equals trade date"); MA I1 ("polls until the date matches").
- **PR:** 4, 7
- **Verification:** PBT `ingest.date_mismatch_rejected`
- **Acceptance:**
  - Property: for every batch response, a row is written to `bars_daily_raw/date=d` only if its returned date equals d; every other row is counted in the mismatch report. Harm: yesterday's prices are stored as today's and every scan and stop decision runs on stale data.

## §4 Nightly ingest

About 100 MCP calls a night: ~63 bulk-quote batches for 3,121 symbols, a few index calls, and corporate-action lookups only for flagged symbols (DS "Nightly ingest from NSE MCP"). Steps:

| # | Step | Rule |
| --- | --- | --- |
| 1 | Probe (from 18:00) | `get_bulk_quote(["RELIANCE"])`; if `date` < today, wait 10 minutes and retry; stop at 22:00 and switch to the bhavcopy fallback |
| 2 | Universe | Active EQ/BE symbols from the instrument master (weekly refresh, §2) |
| 3 | Fetch | Batches of 50, 4 in parallel *[A]*, retry ×3 with backoff; every call logged (§3) |
| 4 | Row checks | Date equals trade date; low ≤ open, close ≤ high; prices > 0; volume ≥ 0 |
| 5 | Corporate actions | Flag and look up (§5) |
| 6 | Store | Raw rows to date-partitioned Parquet; adjustment factors to Postgres; adjusted prices computed, never stored over raw |
| 7 | Quality gate | §6 |
| 8 | Features | §7 |
| 9 | Scans | Each bot's universe and setup stages → watchlist rows → Telegram summary (04, 08) |

#### HAT-DATA-040: Probe until the data date matches, then fall back at 22:00
- **Statement:** From 18:00 the ingest probes `get_bulk_quote(["RELIANCE"])` every 10 minutes until the returned date equals the trade date; at 22:00 it stops probing and runs the bhavcopy fallback for the day.
- **Source:** DS "Nightly ingest" step 1; MA "Daily cycle" steps 7–8; I1.
- **PR:** 7
- **Verification:** TLA `NightlyPipeline!FallbackAt2200`, PBT `ingest.probe_schedule`
- **Acceptance:**
  - Property: in every run, the full fetch starts only after a probe returned date = trade date; consecutive probes are 10 minutes apart; no probe is issued at or after 22:00. Harm: the fetch stores the previous day's prices, or hammers the server.
  - Property: in every run, the bhavcopy fallback starts if and only if 22:00 is reached with no matching probe, and it runs at most once per trade date. Harm: a late MCP day is never ingested, or two sources write the same day.

#### HAT-DATA-041: Fetch covers the active EQ/BE universe
- **Statement:** The nightly fetch requests every active EQ/BE instrument in the instrument master.
- **Source:** DS "Nightly ingest" step 2.
- **PR:** 7
- **Verification:** PBT `ingest.fetch_covers_universe`
- **Acceptance:**
  - Property: for every instrument master, the set of symbols requested equals the set of active instruments with series in {EQ, BE}. Harm: stocks are dropped before the coverage gate can count them, or delisted stocks are requested and inflate failures.

#### HAT-DATA-042: Row checks
- **Statement:** A fetched row is accepted only if its date equals the trade date, low ≤ min(open, close), max(open, close) ≤ high, all prices > 0 and volume ≥ 0.
- **Source:** DS "Nightly ingest" step 4.
- **PR:** 7
- **Verification:** PBT `ingest.row_checks`
- **Acceptance:**
  - Property: for every row (any OHLCV values, any date), the row is accepted if and only if date = trade date ∧ low ≤ open ≤ high ∧ low ≤ close ≤ high ∧ open, high, low, close > 0 ∧ (prev_close is null or prev_close > 0) ∧ volume ≥ 0; every rejected row is counted as an OHLC violation for the gate (§6). Harm: a corrupt bar produces a false breakout or a false stop trigger, and a zero `prev_close` would divide by zero in the corporate-action and big-move checks.

#### HAT-DATA-043: Raw bars are written once per date
- **Statement:** Accepted rows are written to `bars_daily_raw/date=<trade_date>` with `traded_value` null (MCP), `source` and `ingest_run_id`; once committed, a date's raw partition is never rewritten.
- **Source:** DS "Nightly ingest" step 6; SCH "Parquet datasets" ("Raw files are never rewritten").
- **PR:** 7, 17
- **Verification:** PBT `ingest.raw_write_once`, TLA `JobScheduler!AtMostOnceSuccess`, PBT `ingest.checks_kept_for_retry`
- **Acceptance:**
  - Property: for every sequence of ingest runs, retries and fallbacks for one trade date, at most one committed raw partition exists for that date and its content never changes after commit. Harm: history mutates under backtests and adjusted prices can no longer be reproduced.
  - Property: the run that writes the partition first writes its row-check counts beside it (`ingest-checks.json`, atomically), so a retry that reuses the partition, after a later failure or a crash right after the write, reports the counts of the run that wrote it. Harm: a retry's quality gate reads 0 dropped rows and passes a day the first run's checks would fail.

#### HAT-DATA-044: Index snapshot and breadth are stored nightly
- **Statement:** The nightly ingest stores the `get_index_snapshot` levels (including NIFTY 50, NIFTY 500, Nifty Smallcap 250 and India VIX) to `index_daily` and the `get_market_breadth` advance/decline record to `market_breadth/date=d` (counted from our own bars, `source = derived`, when the MCP has none) for the regime engine. The breadth record is snake_case: `date`, `total_stocks`, `advances`, `declines`, `unchanged`, `ad_ratio`, `total_volume` (recorded live 2026-10-22; PR 23 reads `total_volume`, which was stored as 0).
- **Source:** DS "Source map" (index levels and India VIX; market breadth); BL "Regime engine v1" (inputs); PLAN PR 7.
- **PR:** 7
- **Verification:** PBT `ingest.index_snapshot_stored`, PBT `ingest.nightly_index_breadth`
- **Acceptance:**
  - Property: for every successful ingest of trade date d, `index_daily/date=d` contains one row per returned index with OHLC, including NIFTY 500 and India VIX, and `market_breadth/date=d` has its one row; if either is missing the run does not report success. Harm: the regime engine classifies the market on stale inputs and scales capital wrongly.

#### HAT-DATA-045: Each ingest run ends with exactly one outcome event
- **Statement:** Every ingest run records exactly one of `ingest.completed` or `ingest.failed`, in the same transaction as its `hat_job_run` status.
- **Source:** SCH "Event types (MVP)" (Data group); PLAN PR 7.
- **PR:** 7
- **Verification:** PBT `ingest.outcome_event`
- **Acceptance:**
  - Property: for every run outcome (success, MCP failure, fallback success, fallback failure, crash before commit), the event log holds exactly one `ingest.completed` or `ingest.failed` event for that run if and only if its `hat_job_run` row reached a final status. Harm: audits and alerts disagree with what was actually stored.

#### HAT-DATA-046: Nightly steps run in order, each after the previous succeeds
- **Statement:** After ingest, the steps run in the order quality gate → corporate-action adjustment → features → regime → allocate → scan → watchlist digest, and a step runs only when its predecessor succeeded for that trade date.
- **Source:** MA "Daily cycle" step 7; DS "Nightly ingest" steps 7–9; PLAN §4.3 (job order).
- **PR:** 7, 17
- **Verification:** TLA `NightlyPipeline!NightlyPipelineOrder`
- **Acceptance:**
  - Property: in every reachable schedule, including restarts and retries, no step of the chain is marked successful for trade date d before every earlier step is successful for d. Harm: scans run on unadjusted or ungated data and publish wrong watchlists.

#### HAT-DATA-047: No data by the cutoff means no scans and manage-only
- **Statement:** If neither the MCP nor the bhavcopy fallback yields data for the trade date, scans are skipped, an alert is sent and bots stay manage-only.
- **Source:** MA "Daily cycle" step 8; MA "Failure handling" ("Data missing or fails the quality gate → no new scans; exits and stops continue").
- **PR:** 7
- **Verification:** TLA `NightlyPipeline!NoDataNoScan`, TLA `KillSwitch!ExitsContinue`
- **Acceptance:**
  - Property: in every reachable state where trade date d has no committed raw partition, no scan for d succeeds, every bot is manage-only for the next session, and stop and exit processing for open positions continues. Harm: the engine trades on yesterday's watchlist, or stops protecting open positions because a data job failed.

#### HAT-DATA-048: MCP call budget per night
- **Statement:** A nightly ingest uses ⌈N / 50⌉ bulk-quote calls (N = active universe), a few index calls, and `get_corporate_actions` calls only for flagged symbols (about 100 calls in total for 3,121 symbols).
- **Source:** DS "Nightly ingest from NSE MCP" (intro); MA I1.
- **PR:** 7
- **Verification:** PBT `ingest.call_budget`
- **Acceptance:**
  - Property: for every universe of N symbols with F flagged, excluding retries, the number of MCP calls in a run is at most ⌈N / 50⌉ + (probe calls) + (index and breadth calls) + F. Harm: per-symbol calls (~3,100 to repair a day) exhaust rate limits and push the scan past the evening window.

#### HAT-DATA-049: Symbol renames are rebuilt from the stored bars
- **Statement:** HAT reads no legacy symbol table. A symbol that changes for one (ISIN, series) between two days of `bars_daily_raw` is a rename, effective on the first day of the new symbol; the renames feed `hat_instrument_symbol_history` through the `renames` source of `reference.weekly`.
- **Source:** Owner decision D68 (Q30): renames are rebuilt from NSE data in PR 7; DS "Historical data build" step 3 (symbol changes resolved through ISIN).
- **PR:** 7
- **Verification:** PBT `ingest.renames_from_nse_data`
- **Acceptance:**
  - Property: for every history of bars in which each (ISIN, series) trades under a sequence of symbols, the derived renames, once seeded, make every (symbol, date) seen in the bars resolve to the instrument that traded it, with non-overlapping intervals, and seeding twice changes nothing. Harm: an old symbol in a 2015 bhavcopy is attached to the wrong company, or a rename splits one stock's history.

## §5 Corporate actions and the adjusted view

#### HAT-DATA-050: Corporate-action flagging rule
- **Statement:** A symbol is flagged when its `prev_close` differs from our stored close, or it moved more than 20% in a day, or it appears in the NSE Corporate Actions RSS.
- **Source:** DS "Nightly ingest" step 5; B27.
- **PR:** 7
- **Verification:** PBT `corporate_actions.flag_rule`
- **Acceptance:**
  - Property: for every (stored previous close c₀, fetched prev_close p, fetched close c, RSS set R), the symbol is flagged if and only if p ≠ c₀ ∨ |c / p − 1| > 0.20 ∨ symbol ∈ R. Harm: an unflagged split looks like a 50% crash, triggers stops in backtests and corrupts every moving average.

#### HAT-DATA-051: Corporate actions are fetched only for flagged symbols
- **Statement:** The nightly ingest calls `get_corporate_actions` for flagged symbols only. (The `ca.adjust` step adds one lookup per held or watchlisted instrument for the next session, HAT-EXEC-140; a few calls a night.)
- **Source:** DS "Nightly ingest" step 5.
- **PR:** 7
- **Verification:** PBT `corporate_actions.lookup_only_flagged`
- **Acceptance:**
  - Property: for every night, the set of symbols passed to `get_corporate_actions` equals the flagged set. Harm: the call budget explodes, or a flagged symbol is never adjusted.

#### HAT-DATA-052: Corporate actions are stored idempotently
- **Statement:** Actions are stored in `hat_corporate_action`, unique (instrument_id, ex_date, action_type), with `adjustment_factor`, `raw_text` and `source`.
- **Source:** SCH "Reference and market data" (`hat_corporate_action`); DS "Nightly ingest" step 6.
- **PR:** 5, 7
- **Verification:** PBT `corporate_actions.upsert_idempotent`, DB `reference.corporate_action_unique`
- **Acceptance:**
  - Property: for every sequence of loads (MCP, RSS-triggered, archive fallback) containing repeated actions, the stored set equals the set of distinct (instrument_id, ex_date, action_type) keys and reloading never changes a stored factor without a new source row. Harm: a split applied twice halves prices a second time.

#### HAT-DATA-053: Adjusted prices are raw prices times cumulative factors
- **Statement:** `bars_daily_adj` is a DuckDB view: raw bars × the product of adjustment factors of every action of that instrument with ex_date after the bar date; raw files are never rewritten.
- **Source:** SCH "Parquet datasets" ("Adjusted prices are a DuckDB view: raw bars × cumulative adjustment factors"); DS "Nightly ingest" step 6; B27.
- **PR:** 5
- **Verification:** PBT `corporate_actions.adjusted_view`
- **Acceptance:**
  - Property: for every instrument, set of actions A and raw bar on date d, adj_price(d) = raw_price(d) × Π_{a ∈ A, a.ex_date > d} a.adjustment_factor for each of open, high, low and close; adj_prev_close(d) = raw_prev_close(d) × Π_{a ∈ A, a.ex_date > d} a.adjustment_factor (the factor of d itself), because NSE publishes the ex-date bar's `prev_close` already adjusted for the action (the official previous close is the action-adjusted base price; the HAT-DATA-050 flag rule relies on the same fact) *[verify on the first stored ex-date row]*; for d after the latest ex_date, adj = raw. Harm: features and stops see a fake gap at every split and bonus, and an ex-date `prev_close` adjusted twice inflates `adtr20` for 20 sessions (audit F14).
  - The ex-date true range and `adtr20` across a split are checked by `features.split_invariant` (HAT-DATA-073): on the ex-date bar the unsplit series and the adjusted split series give the same features.
  - Property: for every computation of the view, the raw Parquet files are byte-identical before and after. Harm: raw evidence is lost and adjustments become irreproducible.
  - Property: for a 1:2 split (factor 0.5), every pre-ex-date adjusted price is exactly half of raw and every post-ex-date price is unchanged. Harm: the canonical split case is wrong.

#### HAT-DATA-054: Adjusted volume
- **Statement:** Adjusted volume is raw volume divided by the same cumulative factor, so price × volume is preserved.
- **Source:** Docs silent (SCH only names prices); needed for ADV20 in sizing (BL "Position size").
- **PR:** 5
- **Verification:** PBT `corporate_actions.adjusted_volume`
- **Acceptance:**
  - Property: for every bar and set of actions, adj_volume(d) = raw_volume(d) ÷ Π_{a.ex_date > d} factor and adj_close(d) × adj_volume(d) = raw_close(d) × raw_volume(d) (exact in Decimal). Harm: the 1%-of-ADV20 cap is computed on pre-split share counts and sizes positions wrongly.
- **[OWNER-PENDING]** Volume adjustment: default divide by the cumulative factor (no plan §8 default; standard practice); owner to confirm.

#### HAT-DATA-055: Split and bonus factors from text sources
- **Statement:** When the factor comes from archive text rather than the MCP field, it is parsed with the shared helper derived from `phase1_build._parse_corp_action_factor`/`find_bonus_ratio`: a split from face value a to b gives factor b / a, a bonus of x new for y held gives y / (x + y).
- **Source:** DS "Historical data build" step 4 (archive fallback); PLAN PR 5 (reuse of the phase-1 factor parsing).
- **PR:** 5
- **Verification:** PBT `corporate_actions.factor_parse`
- **Acceptance:**
  - Property: for every split (a > b > 0) and bonus (x, y > 0) text in the supported formats, the parsed factor equals b / a or y / (x + y) exactly and lies in (0, 1]; unparseable text is reported and never yields a factor. Harm: a misparsed ratio silently distorts years of adjusted history.

## §6 Quality gate

The nightly data-quality gate (I14) blocks scans when the day's data is not trustworthy. Fail → scans blocked, bots manage-only, Telegram alert (DS "Nightly ingest" step 7).

| Check | Pass condition |
| --- | --- |
| `coverage` | rows stored / active universe ≥ 99% *[A]* |
| `ohlc` | zero OHLC violations (§4 row checks) |
| `big_moves` | every > 20% move is explained by a corporate action or excluded |
| `index_snapshot` | index snapshot present for the trade date (including NIFTY 500 and India VIX) |

#### HAT-DATA-060: Gate fails when coverage < 99% of active universe
- **Statement:** The quality gate fails when fewer than 99% of active-universe instruments have a stored bar for the trade date.
- **Source:** DS "Nightly ingest" step 7; I14.
- **PR:** 7
- **Verification:** PBT `quality_gate.coverage_threshold`, TLA `KillSwitch!DataHealthManageOnly`
- **Acceptance:**
  - Property: for every active-universe size N ≥ 1 and stored count k (0 ≤ k ≤ N), the `coverage` check is FAIL if and only if k / N < 0.99 (so 98.9% fails and 99.0% passes). Harm: scans run on a partial market and miss or misrank setups.
  - Property: whenever any check is FAIL for trade date d, a `hat_quality_check` FAIL row exists for that check, a `quality.failed` event is recorded, and every bot is manage-only until a later gate passes. Harm: a known-bad day still produces entries.
- **[A]** Coverage 99%: default 0.99; validated by observing NSE MCP coverage and load times for 2 weeks (DS "Verify before or during build").

#### HAT-DATA-061: Gate fails on any OHLC violation
- **Statement:** The gate fails if any fetched row violated the §4 row checks.
- **Source:** DS "Nightly ingest" step 7 ("zero OHLC violations").
- **PR:** 7
- **Verification:** PBT `quality_gate.ohlc_zero_tolerance`
- **Acceptance:**
  - Property: for every fetched set, the `ohlc` check is FAIL if and only if at least one row failed HAT-DATA-042. Harm: corrupt source data reaches features unnoticed.

#### HAT-DATA-062: Every move over 20% is explained or excluded
- **Statement:** The gate fails if any instrument moved more than 20% in a day without a stored corporate action for that date and without being excluded from that day's scans.
- **Source:** DS "Nightly ingest" step 7.
- **PR:** 7
- **Verification:** PBT `quality_gate.big_moves_explained`
- **Acceptance:**
  - Property: for every day's bars, the `big_moves` check is FAIL if and only if there is an instrument with |adj_close / adj_prev_close − 1| > 0.20 that has no `hat_corporate_action` row with ex_date = trade date and is not in the day's exclusion list; every excluded instrument is absent from every scan's universe for that date. Harm: an unadjusted split creates a fake breakout or a fake crash in the watchlist.

#### HAT-DATA-063: Gate fails without the index snapshot
- **Statement:** The gate fails if the index snapshot for the trade date is missing.
- **Source:** DS "Nightly ingest" step 7 ("index snapshot present").
- **PR:** 7
- **Verification:** PBT `quality_gate.index_snapshot_present`
- **Acceptance:**
  - Property: for every ingest result, the `index_snapshot` check is FAIL if and only if `index_daily/date=d` lacks NIFTY 500 or India VIX. Harm: the regime gate and allocator run on yesterday's market state.

#### HAT-DATA-064: Gate results are recorded per check
- **Statement:** Every gate run writes one `hat_quality_check` row per check (`check_name`, `status` PASS/FAIL, `detail`) linked to its `hat_job_run`.
- **Source:** SCH "Backtest and ops tables" (`hat_quality_check` "Gate results that block scans").
- **PR:** 7
- **Verification:** PBT `quality_gate.rows_written`
- **Acceptance:**
  - Property: for every gate run, the rows written are exactly one per check in the §6 table, and the gate outcome is PASS if and only if every row is PASS. Harm: a scan is unblocked by a missing row rather than a passing one.

#### HAT-DATA-065: A failed gate blocks scans but never exits
- **Statement:** A FAIL blocks that date's scans, puts bots manage-only through the data-health kill state (automatic re-arm when healthy) and sends a Telegram alert; exits and stops continue.
- **Source:** DS "Nightly ingest" step 7; BL "Kill switches" ("Data or broker health check fails → affected bots manage-only; re-arm automatic when healthy"); MA "Failure handling".
- **PR:** 7, 10, 17
- **Verification:** TLA `KillSwitch!DataHealthManageOnly`, TLA `KillSwitch!ExitsContinue`, TLA `NightlyPipeline!NoScanAfterGateFail`, TLA `KillSwitch!HealthClearsOwnSource`, PBT `quality_gate.rows_written`
- **Acceptance:**
  - Property: in every reachable state where the gate for d is FAIL, no `watchlist.published` event exists for d and no entry order is created until a later gate passes, while exit and stop actions remain enabled. Harm: bad data creates entries, or a data failure leaves open positions unmanaged.
  - Property: in every reachable state, the data-health kill state clears automatically when the next gate passes, and only then. Harm: bots stay idle forever after one bad night, or resume on a still-failing feed.
  - Property: every FAIL produces exactly one health alert through the `Notifier` protocol, and a retry that fails the same checks for the same date records no second `quality.failed` and sends no second alert. Harm: the owner does not learn that the bots stopped entering, or is alerted every 10 minutes until morning.

## §7 Features

Features are computed with polars (T11, PLAN C1) on adjusted bars (§5) and written to `features_daily/date=…`. "Days" are trading days. Columns (SCH "Parquet datasets"; DS "Nightly ingest" step 8):

| Column | Definition |
| --- | --- |
| `instrument_id` | instrument |
| `sma10`, `sma20`, `sma50`, `sma150`, `sma200` | mean of adjusted close over the last N days ending d; null if fewer than N bars |
| `high_52w`, `low_52w` | max adjusted high / min adjusted low over the last 252 days ending d *[OWNER-PENDING: 252 days, high/low]* |
| `adr20` | average daily range %, 100 × (mean over 20 days of high / low − 1) *[OWNER-PENDING]* |
| `adtr20` | average daily true range in price, mean over 20 days of max(high, prev_close) − min(low, prev_close) *[OWNER-PENDING]* |
| `adv20` | mean adjusted volume (shares) over 20 days |
| `turnover20` | mean over 20 days of traded value, using close × volume where `traded_value` is null |
| `ret21`, `ret63`, `ret126`, `ret252` | close_d / close_{d−N} − 1 |
| `rs_pct` | universe percentile (0–100) of 0.4·r₃ + 0.2·r₆ + 0.2·r₉ + 0.2·r₁₂, r_m = return over m months (63/126/189/252 days) |
| `mom_rank` | max over windows {21, 63, 126} of the universe percentile of ret_N *[OWNER-PENDING]* |

"Universe percentile" ranks among all instruments with a value on date d in the active universe; ties take the average rank: percentile = 100 × average rank / n, where n counts the instruments with a value on d, so the best instrument has 100 and the percentile of the worst is 100 / n. An instrument outside the universe, or without the history a value needs, gets null.

Implementation rules the table leaves open (PR 6; they follow the definitions above and are what the properties check):
- **Types.** `instrument_id` is text; every other column is a 64-bit float, null where undefined. Float formulas hold within 1e-9 relative; a rolling mean is a running sum whose last bits depend on where the computation's frame starts (about 1e-14 relative).
- **Windows are over the instrument's own bars.** "Last N days" is the last N rows of that instrument's adjusted bars ending d; a day on which an instrument has no bar is not a row. A window is null unless it is full. A bar whose low is not positive makes `adr20` null for the 20 windows containing it; a return whose base close is not positive is null.
- **`adtr20` and a missing previous close.** `prev_close` is the bar's own (adjusted) `prev_close`. A bar with a previous close of zero or less (a first listing day) uses its own range, high − low.
- **`mom_rank`** is the maximum over the windows that have a value; null when none has.
- **Breadth** is stored in `breadth_daily/date=…` (one row: `universe_size`, `with_sma50`, `above_sma50`, `breadth_pct`). A close is above its SMA50 only when it exceeds it by more than a relative 1e-12, so float noise in the mean never flips a flat price.
- **Incremental runs.** A run computes the dates that have no partition; a change in the stored corporate actions (adjusted history moves) or in the universe (the percentiles and breadth move) makes the run a full rebuild, and so does the `full` flag; the change is recorded only once every date is rebuilt, so a run limited to earlier dates leaves the next run to finish it. Runs are serialised by a lock. A correction of bars of an already featured date needs the `full` flag.

#### HAT-DATA-070: Feature dataset has exactly the specified columns
- **Statement:** `features_daily/date=…` has exactly the columns in the §7 table, one row per instrument per date.
- **Source:** SCH "Parquet datasets" (`features_daily/`); DS "Nightly ingest" step 8.
- **PR:** 6
- **Verification:** CONF `features.schema`
- **Acceptance:**
  - Property: every partition written by the features job has exactly the §7 column set with the declared types and unique `instrument_id`. Harm: criteria read a missing or renamed column and silently score every stock zero.

#### HAT-DATA-071: Features use bars up to and including d only
- **Statement:** Features for date d depend only on bars dated ≤ d.
- **Source:** BL regime v2 note ("known at close t, used from t+1"); T12 (one engine for backtest and live); PLAN PR 6.
- **PR:** 6
- **Verification:** PBT `features.no_lookahead`
- **Acceptance:**
  - Property: for every bar history and every date d, changing any bar dated after d leaves every feature value for d unchanged. Harm: backtests use future data and report edge that live trading cannot have.

#### HAT-DATA-072: Feature formulas
- **Statement:** SMA, 52-week high/low, ADR20, ADTR20, ADV20, turnover20 and returns follow the §7 definitions exactly.
- **Source:** DS "Nightly ingest" step 8; DS "Source map" (turnover = close × volume); SCH "Parquet datasets".
- **PR:** 6
- **Verification:** PBT `features.formulas`
- **Acceptance:**
  - Property: for every adjusted bar series (any length, including fewer bars than the window), each feature equals a direct pure-Python evaluation of its §7 definition (exact for Decimal inputs, within 1e-9 relative for floats) and is null when the window is not full. Harm: a wrong ADV or ADTR changes position size and entry filters on every trade.
  - Property: for every bar with null `traded_value`, the turnover input is close × volume, and with non-null `traded_value` it is `traded_value`. Harm: the ₹10 cr liquidity filter admits illiquid stocks.

#### HAT-DATA-073: Features are invariant under corporate actions
- **Statement:** Because features use adjusted bars, a split or bonus causes no jump in any feature.
- **Source:** SCH "Parquet datasets" (adjusted view); B27.
- **PR:** 6
- **Verification:** PBT `features.split_invariant`
- **Acceptance:**
  - Property: for every price series and every corporate action with factor f applied at an ex-date (raw prices before it multiplied by 1/f, with the action stored), the computed price features for every date equal those of the unsplit series (within 1e-9 relative), and `ret*`/`rs_pct` are unchanged. Harm: every split looks like a crash, triggering MA exits and corrupting RS ranks.

#### HAT-DATA-074: RS percentile weights and monotonicity
- **Statement:** `rs_pct` is the universe percentile of 0.4·r₃ + 0.2·r₆ + 0.2·r₉ + 0.2·r₁₂ over 3/6/9/12 months.
- **Source:** BL "Criteria library" (`rs_rank`: weights 3/6/9/12 m (0.4/0.2/0.2/0.2)); PLAN PR 6.
- **PR:** 6
- **Verification:** PBT `features.rs_pct`
- **Acceptance:**
  - Property: for every universe on date d, `rs_pct ∈ [0, 100]`, and for any two instruments a weighted return strictly greater implies `rs_pct` greater or equal. Harm: the RS ≥ 70 hard filter admits laggards or rejects leaders.
- **[OWNER-PENDING]** Month length: 21 trading days per month (63/126/189/252); no plan §8 default; owner to confirm.

#### HAT-DATA-075: Momentum rank
- **Statement:** `mom_rank` is the best universe percentile across the 21, 63 and 126-day returns.
- **Source:** BL "Criteria library" (`momentum_rank`: windows (21, 63, 126 days), top % (2), any/all (any)); SCH "Parquet datasets" (`mom_rank`).
- **PR:** 6
- **Verification:** PBT `features.mom_rank`
- **Acceptance:**
  - Property: for every universe on date d, `mom_rank = max(pct(ret21), pct(ret63), pct(ret126)) ∈ [0, 100]`, so a stock in the top 2% of any window has `mom_rank ≥ 98`. Harm: the Qullamaggie momentum filter selects the wrong stocks.
- **[OWNER-PENDING]** Single `mom_rank` column definition: default max of per-window percentiles (supports `match: any`; `match: all` reads the `ret*` columns); owner to confirm.

#### HAT-DATA-076: Breadth inputs for the regime
- **Statement:** The features job computes, per date, the % of universe members above their 50-day SMA and stores it in `breadth_daily/date=…` (universe size, members with an SMA50, members above it, the percentage). The advance/decline ratio from `get_market_breadth` is not part of that dataset; the nightly ingest stores it (PR 7).
- **Source:** BL "Regime engine v1" (Breadth input); PLAN PR 6.
- **PR:** 6
- **Verification:** PBT `features.breadth`
- **Acceptance:**
  - Property: for every universe on date d, breadth% = 100 × |{i : adj_close_i > sma50_i, by more than a relative 1e-12}| / |{i : sma50_i not null}|, in [0, 100], when the denominator is > 0; when no member has a 50-day SMA yet (the first 49 sessions of history), breadth is null and the regime engine stores no state for that date. Harm: the regime engine misreads breadth and moves sleeves between states wrongly.

#### HAT-DATA-077: Incremental features equal a full rebuild
- **Statement:** The `features` job runs incrementally by date with a full-rebuild flag; both give identical results.
- **Source:** PLAN PR 6 ("incremental by date; full rebuild flag"); T12.
- **PR:** 6
- **Verification:** PBT `features.incremental_equals_full`
- **Acceptance:**
  - Property: for every bar history and every sequence of incremental runs, the features for every date equal those of a full rebuild over the same bars. Harm: live features drift from backtest features and the shared-engine guarantee breaks.

#### HAT-DATA-078: HAT data code uses polars, pandas only at boundaries
- **Statement:** `app/hat/**` computes with polars and DuckDB; pandas appears only at the boundary with existing modules via Arrow.
- **Source:** T6, T11; PLAN C1.
- **PR:** 6
- **Verification:** CONF `imports.hat_polars_only`
- **Acceptance:**
  - Property: no module under `backend/app/hat/` imports pandas except those listed as boundary adapters in the check, and existing pandas modules outside `app/hat/` are unchanged. Harm: two indicator implementations diverge and backtest and live disagree.

## §8 Parquet layout and DuckDB views

Root: `HAT_DATA_ROOT = DATA_ROOT / "hat"` (env-overridable, gitignored; PLAN C9). Datasets (SCH "Parquet datasets"):

| Dataset | Partition | Columns | Written by |
| --- | --- | --- | --- |
| `bars_daily_raw/` | `date=` | instrument_id, isin, symbol, series, open, high, low, close, prev_close, volume, traded_value (null from MCP), source, ingest_run_id | Nightly ingest, historical build |
| `features_daily/` | `date=` | §7 columns | Nightly features job |
| `bars_5m/` | `date=` | instrument_id, bar_start, open, high, low, close, volume; beside it `coverage.json` (instruments watched from the open, first feed gap, how far the feed was seen; HAT-DATA-114) | Live bar builder, Fyers history (its download progress in `minute_history/progress.json`, HAT-DATA-119) |
| `index_daily/` | `date=` | index_name, open, high, low, close | Nightly ingest |
| `breadth_daily/` | `date=` | universe_size, with_sma50, above_sma50, breadth_pct | Nightly features job |
| `market_breadth/` | `date=` | total_stocks, advances, declines, unchanged, ad_ratio, volume, source | Nightly ingest |
| `raw/` | by file | raw bhavcopy zips, untouched | Historical build, fallback |
| `backtests/run_id=…/trades.parquet` | run | backtest trades (09) | Backtest |

DuckDB views: `bars_daily_adj` (§5) and `bars_daily` (one row per instrument and date); Postgres is attached for joins.

#### HAT-DATA-080: HAT data root is configurable and outside git
- **Statement:** All HAT Parquet data lives under `HAT_DATA_ROOT`, defined in `app/paths.py` as `DATA_ROOT / "hat"`, overridable by environment, gitignored.
- **Source:** PLAN C9, Q9; CLAUDE.md (paths come from `app/paths.py`).
- **PR:** 5
- **Verification:** CONF `store.data_root`
- **Acceptance:**
  - Property: every path the store writes resolves under `HAT_DATA_ROOT`, the path is defined only in `app/paths.py`, and `git check-ignore` matches it. Harm: gigabytes of market data land in the repo or in a directory that is not backed up.

#### HAT-DATA-081: Dataset layout
- **Statement:** The datasets, partitions and columns are exactly as in the §8 table.
- **Source:** SCH "Parquet datasets".
- **PR:** 5, 6, 12, 14
- **Verification:** CONF `store.layout`
- **Acceptance:**
  - Property: every Parquet file under `HAT_DATA_ROOT` belongs to one §8 dataset, sits in a `date=YYYY-MM-DD` (or `run_id=`) partition and has exactly that dataset's columns. Harm: readers silently skip misplaced files and backtests run on partial history.

#### HAT-DATA-082: Writes are atomic
- **Statement:** Parquet partitions are written to a temporary file and atomically replaced, so readers never see a partial file.
- **Source:** PLAN PR 5 ("atomic file replace"); MA "Runtime" (restart-safe jobs).
- **PR:** 5
- **Verification:** PBT `store.atomic_write`
- **Acceptance:**
  - Property: for every write interrupted at any point, a subsequent read of that partition returns either the complete previous content or the complete new content, never a truncated or mixed file. Harm: a crash mid-write leaves a corrupt day that breaks every scan until repaired.

#### HAT-DATA-083: Raw layers are immutable
- **Statement:** Raw bhavcopy zips in `raw/` are kept untouched forever, and adjusted values are never written into `bars_daily_raw`.
- **Source:** SCH "Parquet datasets"; SCH "Retention" (raw zips forever, "they're the evidence"); DS "Historical data build" step 2.
- **PR:** 5
- **Verification:** PBT `store.raw_immutable`
- **Acceptance:**
  - Property: for every sequence of store operations, no existing file under `raw/` is modified or deleted, and every value in `bars_daily_raw` equals the value parsed from its source. Harm: the evidence for a disputed price is lost.

#### HAT-DATA-084: bars_daily has one row per instrument and date
- **Statement:** The `bars_daily` view yields exactly one row per (instrument_id, date), choosing the latest ingest run when sources overlap, adjusted per §5.
- **Source:** SCH "Parquet datasets"; PLAN PR 5 ("`bars_daily` (latest per instrument)").
- **PR:** 5
- **Verification:** PBT `store.bars_daily_unique`
- **Acceptance:**
  - Property: for every set of raw partitions, `bars_daily` has no duplicate (instrument_id, date) and every row comes from the latest `ingest_run_id` for that key. Harm: duplicate bars double-count volume and distort averages.

#### HAT-DATA-085: Backtest and live read the same dataset
- **Statement:** The nightly ingest and the historical build write the same layout, so backtest and live read one dataset.
- **Source:** DS "Historical data build" step 7; T12.
- **PR:** 5, 7
- **Verification:** PBT `ingest.sources_same_schema`
- **Acceptance:**
  - Property: for every trade date, the partition written by the historical build and the one written by the nightly ingest from the same source rows are equal except for `source` and `ingest_run_id`. Harm: backtests run on data shaped differently from live, so their results do not transfer.

## §9 Historical build

Rebuild 2010 to today from NSE daily bhavcopy files: about 4,150 trading days in two formats, because NSE switched to UDiFF on 8 Jul 2024 (DS "Historical data build").

| Period | File | Location |
| --- | --- | --- |
| 2010 → 5 Jul 2024 | CM bhavcopy `cmDDMMMYYYYbhav.csv.zip` | `nsearchives.nseindia.com/content/historical/EQUITIES/YYYY/MMM/` |
| 8 Jul 2024 → today | CM-UDiFF Common Bhavcopy `BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip` | `nsearchives.nseindia.com/content/cm/` |

CLI: `python -m app.hat.data.history_build --from 2010-01-01 [--to] [--resume]`. Real-network runs are manual only; checks use recorded fixtures.

Running it (an ops step, from `backend/`, with the HAT database up, `HAT_DATA_ROOT` set and, for actions, index history and the validation sample, `HAT_MCP_BHAVCOPY_URL` of 10-ops.md configured): `python -m app.hat.data.history_build --from 2010-01-01` downloads at about 1 request per second (about 70 minutes for 4,150 days), keeps every zip under `HAT_DATA_ROOT/raw/`, writes `bars_daily_raw` partitions, loads corporate actions and index history, then prints the validation summary. After an interruption, rerun with `--resume`. The exit code is non-zero when a sampled close differs from the MCP, a security has no instrument, a file is unusable, or a day has no file and is not on the holiday list, or a download failed (the day stays unaccounted), or a corporate-action lookup failed or came back partial. `--actions-archive FILE` adds an NSE corporate-action CSV as a fallback (the first value per action key wins). Securities that appear only in history (delisted, merged) are registered as inactive instruments with their symbol as name and face value 0 until a weekly refresh lists them. Indexes load from `get_index_history` (12-month chunks); the parser for NSE's `ind_close_all` archive CSV is available for operator-supplied files. Smallcap 250's first day is `[A]`: until checked, every index is expected from the start of the build and the report lists the gap.

#### HAT-DATA-090: File format is chosen by date
- **Statement:** Dates up to 5 Jul 2024 use the old CM bhavcopy name and location; dates from 8 Jul 2024 use the UDiFF name and location.
- **Source:** DS "Historical data build" (format table).
- **PR:** 5
- **Verification:** PBT `bhavcopy.format_by_date`
- **Acceptance:**
  - Property: for every weekday d from 2010-01-01 to today, the URL built for d follows the old pattern if d ≤ 2024-07-05 and the UDiFF pattern if d ≥ 2024-07-08, with the date fields formatted as in the table. Harm: a whole era of history fails to download or is parsed with the wrong parser.
- **[A]** URL patterns: from a TradingQnA thread; validated by the first download (DS "Historical data build").

#### HAT-DATA-091: Day list and holiday candidates
- **Statement:** The day list is every weekday since 1 Jan 2010; a missing file is logged as a holiday candidate and checked against NSE's holiday lists.
- **Source:** DS "Historical data build" step 1.
- **PR:** 5
- **Verification:** PBT `bhavcopy.missing_file_is_holiday_candidate`
- **Acceptance:**
  - Property: for every download outcome set, each weekday is either downloaded, or recorded as a holiday candidate; a candidate that is not in the NSE holiday list is reported as a gap. Harm: a missing trading day is silently treated as a holiday and indicators skip a day.

#### HAT-DATA-092: Download is polite, resumable and keeps raw zips
- **Statement:** Downloads use browser-like headers at about 1 request per second, are resumable, and keep raw zips untouched in `raw/`.
- **Source:** DS "Historical data build" step 2.
- **PR:** 5
- **Verification:** PBT `bhavcopy.resume_skips_done`, PBT `bhavcopy.rate_limited`
- **Acceptance:**
  - Property: for every interrupted run followed by `--resume`, no already-downloaded day is requested again and every missing day is requested exactly once. Harm: re-downloading 4,150 files wastes ~70 minutes and risks an NSE block.
  - Property: for every run, the time between the starts of consecutive requests is at least the configured interval (default 1 s). Harm: NSE blocks the client's IP.
- **[A]** Request rate ~1 per second: default 1.0 s; validated on the first full run (DS step 2).

#### HAT-DATA-093: Both formats normalise to one schema keyed by ISIN + series
- **Statement:** The old CM and UDiFF parsers emit the same normalised rows keyed by ISIN + series, with symbol changes resolved through ISIN.
- **Source:** DS "Historical data build" step 3; PLAN PR 5 (reuse of `phase1_build._bhavcopy_equity_rows` column knowledge in a shared helper).
- **PR:** 5
- **Verification:** PBT `bhavcopy.parsers_agree`
- **Acceptance:**
  - Property: for every set of security rows rendered into both file formats, the two parsers return identical normalised rows (same ISIN, series, OHLC, prev_close, volume, traded_value). Harm: a discontinuity on 8 Jul 2024 appears in every indicator that spans it.
  - Property: the phase-1 pipeline keeps producing its existing golden outputs after the helper is shared. Harm: SmartTrader's existing data system breaks.
- **[A]** Old-format column list: validated on day one of the build (DS "The UDiFF file carries ISIN; confirm the old file's column list on day one").

#### HAT-DATA-094: Corporate actions back to 2010
- **Statement:** Historical corporate actions come from `get_corporate_actions` with `fromDate` 2010, or from NSE's corporate-action archive if the MCP does not reach back that far.
- **Source:** DS "Historical data build" step 4.
- **PR:** 5
- **Verification:** PBT `corporate_actions.upsert_idempotent`
- **Acceptance:**
  - Property: for every combination of MCP and archive records covering the same actions, the stored set is the union keyed by (instrument_id, ex_date, action_type) with no duplicates. Harm: pre-MCP splits stay unadjusted and early history is wrong.

#### HAT-DATA-095: Index history
- **Statement:** NIFTY 50, NIFTY 500, Nifty Smallcap 250 and India VIX history load into `index_daily` from NSE index archives.
- **Source:** DS "Historical data build" step 5 (check Smallcap 250's start date); PLAN PR 5 (adds India VIX for the regime).
- **PR:** 5
- **Verification:** PBT `ingest.index_snapshot_stored`
- **Acceptance:**
  - Property: for every trading day in the built range on or after each index's start date, `index_daily` has exactly one row for that index. Harm: the regime engine and RS benchmark have gaps that bias backtests.

#### HAT-DATA-096: Validation report
- **Statement:** The build reports row counts per day and compares 50 random symbol-days with NSE MCP `get_stock_history`; closes must match exactly.
- **Source:** DS "Historical data build" step 6.
- **PR:** 5
- **Verification:** PBT `bhavcopy.validation_flags_mismatch`
- **Acceptance:**
  - Property: for every built dataset and sample of 50 symbol-days, the report marks the build failed if and only if some sampled close differs from the MCP close (exact comparison), and it lists the row count for every day. Harm: a parser bug corrupts years of history without anyone noticing.

#### HAT-DATA-097: Bhavcopy traded value is kept
- **Statement:** Rows from bhavcopy carry `traded_value` (which the MCP lacks).
- **Source:** DS "Historical data build" ("By-product worth using: the bhavcopy carries traded value").
- **PR:** 5
- **Verification:** PBT `bhavcopy.traded_value_kept`
- **Acceptance:**
  - Property: for every parsed bhavcopy row with a traded value field, the stored `traded_value` equals it; only MCP rows have null `traded_value`. Harm: the turnover filter uses the approximation where the exact figure is available.

## §10 Announcements RSS

NSE publishes official RSS feeds for Announcements, Financial Results and Corporate Actions; the engine polls these three and attaches matching items to EP candidates; nothing is parsed for meaning in the MVP (DS "Announcements for EP catalysts").

| Feed | Poll | Used for |
| --- | --- | --- |
| Financial Results | Every 2 min, 07:30–15:30; every 15 min otherwise | `catalyst_attached` on EP candidates |
| Announcements | Same | `catalyst_attached`; shown in Telegram |
| Corporate Actions | Nightly | Flags symbols for `get_corporate_actions` (§5) |

#### HAT-DATA-100: Poll cadence
- **Statement:** Announcements and Financial Results are polled every 2 minutes from 07:30 to 15:30 IST and every 15 minutes otherwise; Corporate Actions nightly.
- **Source:** DS "Announcements for EP catalysts"; DS "Source map"; I6.
- **PR:** 14
- **Verification:** PBT `announcements.poll_schedule`
- **Acceptance:**
  - Property: for every instant t, the next poll of Announcements and Financial Results is scheduled 2 minutes after t if 07:30 ≤ t < 15:30 IST and 15 minutes after t otherwise. Harm: an EP catalyst published at 09:10 is missed by the 09:20 trigger.
- **[A]** Feed URLs and item fields: default from recorded fixtures; validated on first live poll (DS "Verify").

#### HAT-DATA-101: Items are deduplicated by GUID
- **Statement:** Each feed item is stored once in `hat_announcement`, keyed by its GUID.
- **Source:** DS "Announcements" ("feed GUID (dedupe key)"); SCH (`hat_announcement` unique (guid)).
- **PR:** 14
- **Verification:** PBT `announcements.dedupe`, DB `reference.announcement_guid_unique`
- **Acceptance:**
  - Property: for every sequence of polls returning overlapping items, the number of stored rows equals the number of distinct GUIDs. Harm: one announcement is attached several times and shown repeatedly.

#### HAT-DATA-102: Stored fields and unmatched items
- **Statement:** Each item stores title, category, published time, attachment URL, raw item, feed and GUID; items that match no instrument are kept with a null `instrument_id`.
- **Source:** DS "Announcements" ("Stored"); SCH (`hat_announcement` "unmatched items kept with null instrument").
- **PR:** 14
- **Verification:** PBT `announcements.unmatched_kept`
- **Acceptance:**
  - Property: for every polled item, a row exists after the poll whether or not its company matched an instrument, and the stored `raw` equals the item as received. Harm: a catalyst for a renamed stock is lost instead of being matchable later.

#### HAT-DATA-103: Match rule
- **Statement:** An item attaches to a candidate when its company maps to the candidate's instrument (by symbol or ISIN) and it was published within 24 hours (parameter `within_hours`).
- **Source:** DS "Announcements" ("Match rule"); BL "Criteria library" (`catalyst_attached`: within hours (24)).
- **PR:** 14
- **Verification:** PBT `announcements.match_window`
- **Acceptance:**
  - Property: for every candidate at time t and every stored item, the item is attached if and only if its `instrument_id` equals the candidate's and t − within_hours ≤ published_at ≤ t. Harm: a stale or unrelated headline is shown as the reason to take a trade.
- **[A]** Company-to-symbol mapping (ISIN present or not): validated on the first live feed (DS "Verify").

#### HAT-DATA-104: Announcements are never parsed for meaning
- **Statement:** In the MVP, whether a catalyst is attached depends only on the match rule, never on the item's text; any later summary is advisory and never a gate.
- **Source:** DS "Announcements" ("nothing is parsed for meaning in the MVP"; "advisory only, never a gate").
- **PR:** 14
- **Verification:** PBT `announcements.text_never_gates`
- **Acceptance:**
  - Property: for every pair of items identical except for title, category text and URL, the `catalyst_attached` outcome is the same. Harm: a text heuristic silently becomes an unreviewed trading rule.

## §11 Live bars and staleness

The live service subscribes to under 200 symbols (watchlists, open positions and the morning's EP candidates) on the Fyers WebSocket through SmartTrader's feed layer (DS "Live data").

| Window (IST) | Action | Source |
| --- | --- | --- |
| 08:45 | Subscribe watchlist + open positions | Fyers WebSocket |
| 09:00–09:08 | Pre-open order collection; record indicative gaps for subscribed symbols | Fyers (*verify* pre-open fields) |
| 09:16–09:17 | Full-market gap scan (§12) | NSE MCP `cm_get_equity_stocks` |
| 09:17 | Subscribe gappers ≥ 10% that pass universe criteria; attach announcements | Fyers + NSE RSS |
| 09:20 | First 5-min bar closes → opening-range high and low fixed | Bar builder |
| 09:20–15:20 | Triggers: level breaks, volume pace | Bar builder |
| 15:20–15:25 | Live price used as close proxy for MA exits | Fyers |

#### HAT-DATA-110: One live feed, owned by HAT
- **Statement:** HAT's `LiveFeed` owns the single Fyers data socket; SmartTrader's legacy read side attaches as a tick listener and no second WebSocket to the broker exists.
- **Source:** MA "Boundary rules" ("never a second WebSocket to the same broker"); I3; PLAN C4, PR 14.
- **PR:** 14
- **Verification:** CONF `imports.no_second_websocket`
- **Acceptance:**
  - Property: no module outside `backend/app/hat/data/provider/fyers_websocket.py` imports or names the Fyers data-socket SDK; all tick consumers attach through `LiveFeed.add_tick_listener`. Harm: a second connection breaks the broker's session limit and disconnects SmartTrader's feed.

#### HAT-DATA-111: A tick listener can never block or break the broadcast
- **Statement:** Tick listeners run on the loop thread; a listener exception is logged and isolated and never blocks or drops delivery to the other listeners.
- **Source:** PLAN PR 14 (listener hook); MA "Reuse vs build" (reuse the real-time feed).
- **PR:** 14
- **Verification:** PBT `bars.listener_isolation`
- **Acceptance:**
  - Property: for every tick sequence and every set of listeners where any subset raises, every non-raising listener receives every tick in order, and a removed listener receives none. Harm: one bad listener freezes live quotes for every consumer.

#### HAT-DATA-112: HAT subscriptions are one registry owner
- **Statement:** HAT registers as `SubscriptionRegistry` owner `"hat"` for watchlist, open positions and EP candidates; open positions are always subscribed.
- **Source:** DS "Live data"; PLAN PR 14.
- **PR:** 14
- **Verification:** PBT `bars.subscription_set`
- **Acceptance:**
  - Property: for every state of watchlists, positions and candidates, the `"hat"` owner's subscription set equals (watchlist ∪ open positions ∪ EP candidates) in DB symbol format, and unsubscribing HAT never removes a symbol held by another owner. Harm: an open position stops receiving prices so its live exits never fire.
- **[A]** Under 200 symbols / Fyers symbol limit: validated during build (DS "Verify: Fyers WebSocket symbol limit"); a set at or above the limit raises a health alert.

#### HAT-DATA-113: 5-minute bars are aligned to 09:15 and final at close + 2 s
- **Statement:** 5-minute bars are built from ticks, aligned to 09:15:00 IST; a bar is final at its close plus 2 seconds.
- **Source:** DS "Bar builder rules"; B8, B9.
- **PR:** 14
- **Verification:** PBT `bars.alignment`
- **Acceptance:**
  - Property: for every tick sequence, each tick at time t in the session belongs to exactly the bar starting at 09:15 + 5·⌊(t − 09:15) / 5 min⌋, and each bar's OHLCV equals the open/max/min/last price and summed volume of its ticks. Harm: an opening-range high or pivot break is evaluated on the wrong bar.
  - Property: no bar is reported final before bar_end + 2 s, and a final bar never changes. Harm: a trigger fires on a bar that later changes, so live and replay disagree.

#### HAT-DATA-114: Opening range is fixed when the first bar closes
- **Statement:** The opening-range high and low are the high and low of the 09:15–09:20 bar, fixed when it becomes final. They exist only for a symbol watched from the open: its first tick that trade date arrived at or before 09:15:00 and no feed gap began before 09:20:00. The day low (the lowest low of the day's final bars up to the trigger bar, HAT-EXEC-051) exists only for a symbol watched from the open with no feed gap that day. Both exist only once a tick of the feed (any symbol) has arrived at or after the end of the last bar they cover: a feed that went silent before a bar ended may have lost its last prints, and a silence is marked as a gap only at the stale threshold. A feed gap is a silence of the stale threshold (HAT-DATA-116) inside the session, from its last tick (or the open), and a restart is a gap from the last instant the stopped process saw the feed (`coverage.json` keeps it up to the first bar's end), or the end of its last stored bar if earlier. Otherwise both are undefined (fail closed: no opening-range trigger, `stop_input_missing`).
- **Source:** DS "Live data" (09:20 row); MA "Daily cycle" step 4; B9; audit 2026-10-22 F4.
- **PR:** 14, 22
- **Verification:** PBT `bars.opening_range_fixed`, PBT `bars.restore_matches_live`, PBT `bars.session_state_never_partial`, PBT `bars.trigger_never_uses_a_partial_session`
- **Acceptance:**
  - Property: for every tick sequence watched from the open with a tick in the first bar, from the first feed tick at or after 09:20:00 (and no earlier than 09:20:02) the opening-range high and low equal the first bar's high and low and never change for the rest of the day. Harm: the ORB trigger level drifts upward and entries are late or never fire.
  - Property: after a restart mid-session, a bar builder given the day's stored final bars and coverage (`BarBuilder.restore`) has the same opening ranges as one that saw every tick when the stopped process had seen the feed past the first bar (none otherwise), the same day volumes for every symbol with no bar still building at the restart (its unfinished bar is lost), a restored bar never replaces a live one, and it has no day low. Harm: after a restart the opening range is lost and no ORB entry fires for the rest of the day.
  - Property: whatever HAT missed of the session (a first tick after the open, a feed gap, a restart and its downtime), a published opening range is the session's real one and a published day low is the real low of the day's final bars; a trigger on the opening range fires only at the real opening-range high, and a `low_of_day` stop is the real low less its buffer or `stop_input_missing`. Harm: EP buys under its own breakout level with a stop above the day's real low, too tight and oversized.

#### HAT-DATA-115: Volume pace
- **Statement:** Volume pace = cumulative day volume ÷ fraction of session elapsed, compared with the criterion's average-volume baseline.
- **Source:** DS "Bar builder rules"; BL "Criteria library" (`volume_pace`: × 50-day avg, projected to full day (1.5)).
- **PR:** 14
- **Verification:** PBT `bars.volume_pace`
- **Acceptance:**
  - Property: for every time t in (09:15, 15:30] and cumulative volume V, pace(t) = V / ((t − 09:15) / 375 min), and pace is never computed before the first tick of the session. Harm: an early-morning trickle projects to a huge pace and fires false breakouts.
- **[OWNER-PENDING]** Baseline: DS says "compared with ADV(20)", BL `volume_pace` says "× 50-day avg"; default the criterion's parameter (`vs_avg50`, so a 50-day average volume) and keep ADV(20) for `early_volume`; owner to confirm.

#### HAT-DATA-116: Feed silence of 30 seconds or more marks the feed stale
- **Statement:** A silence of 30 seconds or more (no tick in the last 30 seconds) during market hours marks the feed stale, records `feed.stale` and blocks new orders until ticks resume. The acceptance below fixes the boundary: a tick exactly 30 seconds ago no longer counts.
- **Source:** DS "Bar builder rules"; BL "Invariants" (4: no order when the live feed is stale); BL "Kill switches" (health → manage-only, automatic re-arm).
- **PR:** 14, 15
- **Verification:** PBT `bars.staleness_trip`, PBT `bars.stale_feed_trips_health_kill`, TLA `ExecutionRouter!NoOrderWhenStale`
- **Acceptance:**
  - Property: for every tick timeline during market hours, the feed is stale at t if and only if no tick arrived in (t − 30 s, t]; each transition to stale records exactly one `feed.stale` event. Harm: orders are sized and placed on prices that are minutes old.
  - Property: in every reachable state where the feed is stale, no entry order is created and affected bots are manage-only; exits and broker-side stops are unaffected. Harm: a frozen feed lets a breakout entry fire at a price that no longer exists.

#### HAT-DATA-117: 5-minute bars persist to Parquet
- **Statement:** Final 5-minute bars are written to `bars_5m/date=…` (instrument_id, bar_start, OHLC, volume), and after them, at each step that changed it, the day's `coverage.json` (the instruments watched from the open, the first feed gap and how far the feed was seen up to the first bar's end; a union, a minimum and a maximum), which a restart reads back (HAT-DATA-114).
- **Source:** SCH "Parquet datasets" (`bars_5m/`); PLAN PR 14.
- **PR:** 14
- **Verification:** PBT `bars.persisted_equals_live`
- **Acceptance:**
  - Property: for every session, the persisted bars equal the final bars the trigger monitor saw, one row per (instrument, bar_start). Harm: replays and audits cannot reproduce a live decision.

#### HAT-DATA-118: Pre-open gap capture
- **Statement:** From 09:00 to 09:08 the indicative gap % of each subscribed symbol is recorded; when Fyers lacks pre-open fields, the open of the first tick at or after 09:15 is used (before it the feed's open is the previous session's). Records are kept per (symbol, trade date).
- **Source:** DS "Live data" (09:00–09:08 row); MA "Daily cycle" step 3; audit 2026-10-22 F13.
- **PR:** 14, 22
- **Verification:** PBT `bars.gap_pct`, PBT `bars.gap_record_from_its_own_day`
- **Acceptance:**
  - Property: for every symbol with previous close c and indicative (or first-tick) open o, the recorded gap % equals 100 × (o / c − 1), and the record says which source supplied o. Harm: EP gap filters use a wrong gap.
  - Property: in a process running across days, every gap record of trade date d holds d's open (its indicative open, or the feed's open from a tick at or after 09:15 on d). Harm: `gap_up` runs on yesterday's open.
- **[A]** Fyers pre-open fields: validated during build (DS "Verify: Fyers WebSocket ... pre-open data").

#### HAT-DATA-119: Optional Fyers minute history for EP backtests
- **Statement:** An optional loader fills `bars_5m` from the Fyers 1-minute history for EP backtests. `python -m app.hat.data.minute_history --days N [--resume]` loads the trading universe (the active EQ/BE instruments with a Fyers symbol, §2) through HAT's own Fyers session from N days back to yesterday (today's bars belong to the live builder) and prints a coverage report. On the Fyers Standard plan (D131) it stays within 100 days a call, 50 calls in any minute and 5,000 calls an IST day (all configurable), writes each symbol as soon as it loads, and keeps its progress (calls per IST day and the last minute's calls, the period, the symbols done and the ones that failed, the earliest date the plan serves) in `minute_history/progress.json`, so `--resume` continues a run stopped by the day's budget or a failure: over that run's period (not the shifted one a later day would ask for), skipping the symbols done. A -50 refusal whose reason is the plan's history depth clamps the start date (to the plan's 365 days, then a day at a time, at most 3 times a run); it is not a failure. A symbol whose calls are refused, or whose answer is malformed (not a mapping, bad candle rows) or holds more than 400 candles per day asked for, is reported with a fixed code and the run goes on; 3 symbols whose calls were refused in a row stop it (a malformed answer does not count), and a resume tries the symbols that failed last. Only one response's candles are held at a time; each is aggregated as it arrives.
- **Source:** I4; DS "Source map" (intraday history for EP backtest); PLAN PR 12, Q4; owner decision D131 (the 1-year Standard-plan download; older intraday history later from NSE).
- **PR:** 12, 41
- **Verification:** PBT `bars.history_matches_builder`, PBT `bars.minute_history_aggregates_to_5m`, PBT `bars.minute_history_budget`, PBT `bars.minute_history_resume`, PBT `bars.minute_history_plan_clamp`
- **Acceptance:**
  - Property: for every minute series, aggregating it to 5-minute bars with the loader gives the same bars as feeding the same prints through the live bar builder. Harm: EP backtests use bars built differently from live and mislead promotion decisions.
  - Property: for every period, the loader asks for each symbol's 1-minute candles in consecutive chunks of at most the configured days covering exactly the period and writes each symbol's builder bars as soon as it loads; a symbol whose call fails, whose answer holds more candles than the cap or whose candle rows are garbage is reported with a fixed code without the broker's text and has nothing written, while every other symbol is stored. Harm: one bad symbol throws away hours of loaded history, or a broker message reaches the store or the logs.
  - Property: for every call timing and every count already spent that day, also across a run started right after another, no 60 seconds hold more than the per-minute limit of calls and no IST day more than the daily limit; a spent day stops the run with `history_daily_limit` and the count is kept for the next run. Harm: Fyers throttles or blocks the app, and live trading loses its session.
  - Property: a run stopped anywhere and then resumed on a later day leaves `bars_5m` as one uninterrupted run would, and never asks again for a symbol already loaded over the period. Harm: a multi-day download wastes its call budget or leaves gaps.
- **Deviation (PR 12):** `FyersClient.get_historical_data` does not exist. `app/hat/data/minute_history.py` takes the history call as an injected function (the Fyers SDK's `history` request: `resolution "1"`, `date_format "1"` with yyyy-mm-dd ranges, `cont_flag "1"`), so no test reaches Fyers; a candle counts as its four prints (open with the volume, high, low, close) in the order the live builder would see them. Only the command line builds the call, from `FyersSession`'s SDK model; a `no_data` answer is an empty range.
- **Changed (PR 41, D131):** PR 12 wrote nothing unless every symbol loaded; a year of the whole universe takes about three days of the Standard plan's budget, so each symbol is now written as it loads and the run is resumable.
- **[A]** Fyers minute-history depth: checked live 2026-10-22 (audit "Fyers history depth"): the Standard plan serves intraday candles from exactly 365 days back, rolling.

## §12 09:16 gap scan

EP candidates come from a full-market gap scan just after the open, because the nightly watchlist cannot know tomorrow's gappers. A 10% gap is impossible in 2% or 5% band stocks, so EP candidates come only from 10%/20%-band stocks and F&O names (DS "Live data"; BL "Qullamaggie-style bot").

#### HAT-DATA-120: Full-market gap scan at 09:16
- **Statement:** At 09:16–09:17 the scan computes open vs previous close for every EQ symbol via `cm_get_equity_stocks`, prefix-paged (~26 calls).
- **Source:** DS "Live data" (09:16–09:17 row); PLAN PR 14.
- **PR:** 14
- **Verification:** PBT `gapscan.covers_universe`
- **Acceptance:**
  - Property: for every listing, the scan evaluates every active EQ symbol exactly once with gap % = 100 × (open / prev_close − 1). Harm: a gapper is missed because its prefix page was truncated.

#### HAT-DATA-121: Gap-scan selection rule
- **Statement:** Gappers of at least 10% that pass the bot's universe criteria and are in a 10% or 20% band or are F&O names are subscribed at 09:17, with matching announcements attached. Each gapper's open for `gap_up` is the feed's record of that trade date (HAT-DATA-118), else the open the scan's listing gave (a gapper is usually not subscribed before the scan).
- **Source:** DS "Live data" (09:17 row); BL "Qullamaggie-style bot" (India-specific consequence); BL `gap_up` (min gap % (10)).
- **PR:** 14, 22
- **Verification:** PBT `gapscan.filter`
- **Acceptance:**
  - Property: for every scan result, the selected set equals {s : gap%(s) ≥ 10 ∧ universe(s) passes ∧ (band(s) ∈ {10, 20} ∨ s is F&O)}, each is handed over with the listing's open, and every selected symbol is added to the `"hat"` subscriptions with announcements matched per HAT-DATA-103. Harm: impossible gaps are chased or valid EP candidates are never watched.
- **[OWNER-PENDING]** F&O identification: default "no price band" (`band_pct` null) marks an F&O name, since NSE F&O stocks carry no band; owner to confirm.

#### HAT-DATA-122: Gap-scan timing is recorded
- **Statement:** Every gap scan records its start and finish times so that its latency against the 09:20 bar can be measured.
- **Source:** DS "Live data" ("*Verify:* MCP gap-scan latency at 09:16 (the scan is useless if it lands after the 09:20 bar)").
- **PR:** 14
- **Verification:** PBT `gapscan.timing_recorded`, OPS `gapscan.latency_observed`
- **Acceptance:**
  - Property: every gap-scan run writes a `hat_job_run` row with `started_at` and `finished_at`, and a run that finishes at or after 09:20:00 raises a health alert. Harm: the EP playbook silently never sees its candidates in time.
- **[A]** Scan latency under 4 minutes: validated by observing `cm_get_equity_stocks` latency for 2 weeks (DS "Verify").

#### HAT-DATA-123: One provider socket, bounded reconnects
- **Statement:** `ProviderSession` runs at most one data socket at a time, builds it through the socket factory with the current token, and stops reconnecting after a bounded number of failures.
- **Source:** MA "Boundary rules"; I3; PLAN PR 14.
- **PR:** 14
- **Verification:** PBT `feed.session_one_socket`, PBT `feed.session_gives_up`, PBT `feed.session_close_stops_a_backing_off_runner`, PBT `feed.socket_factory`, PBT `feed.token_source`, PBT `feed.traceback_throttle`
- **Acceptance:**
  - Property: for every interleaving of connect, disconnect, close, SDK reconnect and failure, at most one socket is live, a backing-off runner stops on close, the session gives up after its failure budget, the socket factory receives the current token, and a repeating exception site logs a traceback at most once per interval. Harm: two sockets break the broker session limit, or a dead feed retries forever and floods the log.

#### HAT-DATA-124: Ticks are normalised once
- **Statement:** Every raw provider message becomes a tick with a canonical symbol, price, volume and exchange time, and malformed messages are dropped.
- **Source:** PLAN PR 14.
- **PR:** 14
- **Verification:** PBT `feed.tick_normalised`, PBT `feed.tick_flow`
- **Acceptance:**
  - Property: for every raw message, the normaliser returns a tick whose fields equal the message values in canonical form or drops the message, and every accepted message reaches every listener once through `LiveFeed`. Harm: a mis-parsed price triggers a wrong entry or exit.

#### HAT-DATA-125: The subscription registry is reference counted per owner
- **Statement:** `SubscriptionRegistry` tracks symbols per owner and reports a symbol as wanted while any owner holds it.
- **Source:** DS "Live data"; PLAN PR 14.
- **PR:** 14
- **Verification:** PBT `feed.registry_model`, PBT `feed.registry_concurrent`
- **Acceptance:**
  - Property: for every sequence of subscribe and unsubscribe calls by any owners, including concurrent ones, the wanted set equals the union of owner sets and a symbol leaves it only when its last owner releases it. Harm: one owner's unsubscribe silences another owner's open position.

#### HAT-DATA-126: The provider converges on the wanted set
- **Statement:** `LiveFeed` makes the provider's subscribed set equal the registry's wanted set, even when changes race with calls to the provider and SDK reconnects.
- **Source:** PLAN PR 14.
- **PR:** 14
- **Verification:** PBT `feed.provider_converges`
- **Acceptance:**
  - Property: for every sequence of subscribe, unsubscribe, connect, disconnect and SDK reconnect with changes landing during in-flight provider calls, the provider's net subscribed set equals the wanted set once the feed is idle and connected. Harm: a held symbol silently stops ticking, or an unwanted symbol eats the symbol limit.

#### HAT-DATA-127: The feed follows login state and market hours
- **Statement:** `LiveFeed` connects on login and token change, honours an explicit disconnect, stops outside market hours, and its monitor survives errors without blocking the loop.
- **Source:** PLAN PR 14.
- **PR:** 14
- **Verification:** PBT `feed.auth_hooks`, PBT `feed.off_hours_stops_a_backing_off_runner`, PBT `feed.monitor_survives_errors`, PBT `feed.token_check_off_loop`, PBT `feed.calendar_closed`
- **Acceptance:**
  - Property: for every sequence of login, token change, disconnect and expiry, the feed runs only with a valid token and no explicit disconnect, a backing-off runner is stopped outside market hours, the monitor continues after any exception, the token check never runs on the event loop thread, and the feed never connects on a holiday of HAT's trading calendar or while that calendar is unknown. Harm: the feed trades on a dead token, stays connected overnight or on an exchange holiday, or stops forever after one error.

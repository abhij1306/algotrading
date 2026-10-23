# Decisions in force

Owner and build decisions that still bind the code. No history; a reversed decision is deleted
here.

## Product

- **Scope (I8, I12, I13, BD1-3).** NSE only, one user, Fyers only (direct adapter behind HAT's own
  `HatBroker`), one allowlisted Telegram chat, two sleeves: S1 `stage2-contraction-breakout`
  (Minervini-style) and S2 `momentum-breakout-ep` (Qullamaggie-style, breakout and episodic pivot).
  No OpenAlgo, no Zerodha/Kotak, no US markets, no WhatsApp, no shorts, no multi-user.
- **Modes (B1, B2).** Each bot is `shadow`, `confirm` or `auto`. A new bot asks for confirm and runs
  shadow until its sleeve is Live, its account is active with GTT delivery and DDPI confirmed, and
  the owner confirmed the policy weights. Auto places only high-confidence signals without a card;
  it is a personal switch, never a product feature.
- **Confidence labels (D132, D149).** A signal's label is fixed when it triggers, from its bucket
  (playbook, setup-score band 0.60-0.75 / 0.75-0.90 / 0.90-1.00) in the bot's evidence run that
  had completed by then: high is at least 30 trades with a positive 90% lower bound of mean R
  (raw, undiscounted); low is no evidence, fewer than 30 trades or a mean at or below 0 R; medium
  is the rest. Auto places only high; medium and low go to the owner. The 30 trades, the band
  edges and the raw mean are owner-pending defaults. No manual trading, no order route, no manual
  exit button.
- **Delivery only (B24).** Every real order is CNC. Long only. One entry per position, never add
  to winners (B20) or losers.
- **Process.** Backtest at least 5 years of both bots, then daily paper, then the owner approves
  every trade; auto stays behind a switch with confidence labels.

## Strategy and sizing

- **Criteria (B3-B12, B22, B23, B25).** Parameterised library (25 types); hard criteria must pass,
  scored ones feed a weighted setup score (threshold 0.6). S1 triggers on a 5-minute bar breaking
  the pivot with volume pace at least 1.5x the 50-day average; S2 above the 5-minute opening-range
  high fixed at 09:20. Default universe NSE EQ/BE with 20-day turnover at least 10 crore; include
  and exclude lists per universe version. Skip at upper circuit (`max_chase`). Regime gate blocks
  new entries only and cannot be overridden. EP candidates get NSE announcements attached
  (advisory, never a gate). Earnings filter is registered but off.
- **Risk (B13-B16, B21, B26).** Fixed-percentage risk per trade 0.25% of bot equity from the stop
  distance; per-sleeve position caps (S1 20%, S2 25% of equity); ADV cap 1% of 20-day volume per position; global open risk 1.5%;
  at most 6 open positions per bot, 2 per industry, 3 entries and 3 stop-outs per day; exposure
  ladder L1 2 positions at 0.5x, L2 4 at 1.0x, L3 6 at 1.0x, up after 2 wins, down after 2 losses;
  kill switches at 10% drawdown (account and per bot) make bots manage-only. All risk numbers live
  in the versioned risk policy, never in a bot or strategy.
- **Portfolio (PL1-PL5).** Unit is a sleeve (strategy version x universe version). Regime (rules
  over trend, India VIX, breadth; 4 states, changes after 3 days) scales sleeve weights within
  [0.25, 1.0], moving at most 0.25 a day; it never promotes or retires. Base weights S1 50% and S2
  50%, cluster cap 100%. Lifecycle: research, backtested, shadow (20 trading days), live,
  probation, retired, on evidence only; every backtest run counts as a trial; promotion needs
  deflated-Sharpe probability at least 0.95 and 200 trades. Risk increases wait 7 days.
- **Stops and exits (B17-B19).** Intraday stop order on fill, swapped to a GTT stop around 15:25
  for overnight; MA exits evaluated 15:20-15:25 on the live price; exits are an ordered stack,
  full beats partial, stops only tighten.

## Data

- **Sources (I1-I6, I14, I16).** Nightly prices from the NSE MCP (50 symbols a call, polled from
  18:00) with bhavcopy fallback at 22:00; history from bhavcopy archives; one Fyers WebSocket for
  live data; NSE announcement RSS; research MCPs are chat-side only and never feed a scan or
  order. A quality gate after every ingest blocks scans (never exits). Adjusted prices are raw
  bars times cumulative corporate-action factors; raw bars are never rewritten (B27).
- **Storage (T5, T6, T10, T11).** Postgres is the system of record; price history is date-partitioned
  Parquet read through DuckDB; strategy configs are versioned JSONB with YAML import/export;
  indicators are own polars code on adjusted bars.
- **Engine (T7, T8, T12).** In-process scheduler with jobs in Postgres; one async session owner per
  broker account; one engine for backtest and live.
- **Backtest.** Behaviours not modelled are listed in `KNOWN_GAPS` (`engine/backtest.py`) and
  reported by every run; the golden replay fixture pins the result.

## Operations and security

- **Host (T9, T13, T14, T15).** One always-on self-hosted machine with an ISP static IP registered
  with Fyers, a UPS and a 4G link used for alerts and manual login only. Secrets live in `.env`
  outside the repo, mode 0600. Fyers login is manual each morning via a Telegram link (08:30,
  repeated 09:05). Logs are structured; health failures raise Telegram alerts.
- **API (T3, BD5).** No API authentication, loopback bind, read-only routes; the two Fyers login
  writes need the `X-HAT-Request` header and a frontend `Origin` (`api/guard.py`).
- **Schema (T1, SCH).** Every owned table carries `user_id` (one fixed UUID, `HAT_USER_ID`); uuid v7
  keys generated in Python; money is `Numeric(18,4)`/Decimal; no migration tool, fresh Postgres.
  Append-only event log by trigger only (no separate app role).
- **Verification (T16).** HAT code is proved with TLA+ models, Hypothesis properties, Postgres
  checks and conformance checks, not example unit tests; each new check is seen failing on a
  deliberate break.

## Planned, not built

- Regime model v2 (HMM/GMM), `hat_fundamental` filled from XBRL, screener-driven universes, US
  markets and broker, Zerodha and Kotak Neo adapters, multi-user (all P2/P3).
- Intraday history older than 365 days from an NSE source (Fyers Standard serves 365 days).
- PDF summaries for approval cards and a chat MCP server: advisory only, never a gate.
- A CLI to re-run one job for a past trade date.

## Defaults awaiting owner confirmation (OWNER-PENDING)

Chosen where the original docs were silent; the code implements them.

- Universe: NIFTY 500 point-in-time plus liquidity criteria, empty include/exclude; total equity
  read from broker funds at session start.
- Regime: first day bootstraps as confirmed; percentile rank over the trailing 252 values;
  precedence Bear > Correction > Bull volatile > Bull quiet with equality counting as not above;
  `confirmed` means raw equals confirmed state.
- Lifecycle: Probation to Live is by owner promotion only; health 0 means Probation (weight 0);
  failure of a probation sleeve compares shadow mean R with the discounted benchmark; discount
  0.58; CI is a two-sided Student-t interval on mean R; N = 1 gives SR0 = 0 and an undefined DSR
  is 0 (fail closed).
- Criteria curves: empty setup score is 1.0; "rising" is point to point over `lookback_days`;
  VCP pivot is the final contraction high with a mean-of-four linear score; other score curves
  linear; `mom_rank` is the max of per-window percentiles; a month is 21 sessions; F&O names are
  those with no price band; volume pace baseline is the 50-day average.
- Data: volume adjusts by the cumulative factor; a stock listed this month uses its latest close
  for the tick; daily-bar trigger proxy when minute history is absent.
- Schema value sets: kill-switch state {tripped, armed}; order status {pending, placed,
  unconfirmed, partially_filled, filled, cancelled, rejected, expired}; child tables reach
  `user_id` through their parents.
- Ops: MCP backoff 1 s doubling to 30 s; NTP offset at most 1 s; a 2% fallback stop percentage;
  statutory cost rates are versioned config from current schedules; backup target and credentials
  from the owner; TLC v1.7.4, `max_examples=500`, 5-minute budget per verification job.
- Shadow results do not move the exposure ladder.

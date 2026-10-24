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
  kill switches at 10% drawdown (account and per bot) make bots manage-only, measured on equity
  marked to market intraday, not only the end-of-day realised P&L (X9, 2026-10-23); before a
  holding's first trade of the session it is marked at its previous official close (D210); a
  re-arm restarts the peak at the equity then, so the kill trips again only on a new 10% fall
  (D232, Q41, 2026-10-24). All risk
  numbers live in the versioned risk policy, never in a bot or strategy.
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
  bars times cumulative corporate-action factors; raw bars are never rewritten (B27). A bonus row
  of preference shares or debentures (NCRPS, "Bonus Debentures") is price-neutral, never a
  factor (a split row stays a split whatever its text); the owner corrects a stored factor with
  `admin correct-action`, journalled, and no later load re-creates a key it vacated (D1, D232).
- **Corporate-action file import (CA1, D232, owner 2026-10-24).** Splits and bonuses the MCP
  missed, rights issues and demergers come from a collected NSE file (`data/ca_import.py`, source
  `nse_ca_file`), stored only when the ex-date open over the previous close is within 20% of the
  factor and at least twice as close to it as to 1; anything else is a review case. Rights factor
  TERP / cum price (neutral at or above the cum price); a demerger's parent factor is 1 - r x
  resulting company's first close / parent's previous close, cross-checked by that gate, or the
  ex-date gap itself when no resulting listing is found and the drop exceeds 10%. Both are
  stored as type `other`; the resulting company is never linked into the parent's lineage. The
  file's instrument class is a hint only: the bars decide. An import never rewrites a stored key.
- **Corporate-action research import and corrections (CA2, D241/D245, owner 2026-10-24).** A
  demerger's stored factor is the market factor, ex-date open / previous close, when a research
  source confirms the event and date (`data/ca_research.py`, source `ca_research`). The CoA %
  (a book-value tax allocation) and the ratio priced at the resulting company's first close are
  cross-checks only, written beside it (`market_gap; coa X disagrees; ratio Y`), never the
  factor. The gap is stored when a cross-check agrees (the CA1 gate, the check as expected);
  with none agreeing or none at all, only when the date is confirmed, the drop is at least 10%
  and the close five sessions on holds it. Anything else is a review case. Research rights terms are stored through the CA1 gate,
  an ambiguous gate as `price_unconfirmed`. Wrong stored factors are fixed by a reviewed list
  (`admin correct-actions`, through `correct_action`, plus `add` for a missing leg), each line
  with its bar evidence; a bonus to public shareholders only (promoters waived) is old shares /
  new shares, not the stated ratio.
- **Dividends and total return (CA3, owner 2026-10-24).** Equity dividends from 2011 come from
  NSE's corporate-actions feed (`data/ca_dividends.py`, source `nse_ca_feed`), stored as
  `dividend` rows with factor 1 and the rupees per share in `cash_amount`; a text with no single
  amount, a percentage of face value and cash above half the previous close are review cases,
  never guessed. Every dividend counts, however small: no threshold, since each one is cash a
  holder receives and leaving small ones out biases returns down. Signals read the total-return
  series (`bars_daily`: the price factors times 1 - D / previous raw close for each dividend);
  the price basis (`bars_daily_cap`) is what fills, marks, ticks, bands, the rescale and the GTT
  check read. Stored level features (SMAs, 52-week high/low, ADTR) are point in time: on the
  scale of their own date, so a later dividend never changes a stored row (the features' inputs
  digest covers the dividends). A backtest fills on the price basis and credits `qty x D` to cash
  on the ex-date to a position open at its start (the data has no pay date); the credit is part
  of the trade's P&L and R (R-DIV). A bonus of debentures or preference shares worth at least 1% of the
  previous close is such a dividend (value per share as the cash, ex-date confirmed by the bars
  or NSE's own date, source `ca_research`); below 1% it stays price-neutral (D1). Live orders
  are unchanged: Fyers cancels resting orders on such an ex-date as before.
- **Dividends going forward (CA4, owner 2026-10-24).** The nightly `ca.adjust` stores the
  dividends going ex on the trade date from NSE's daily corporate-action file (the `bc` CSV of
  the PR archive on nsearchives, every security, published about 17:30 IST; not the
  `www.nseindia.com/api` feed), the day's file being the last word on a revised amount, through
  the CA3 checks (source `nse_pr_bc`): a review or conflict case and a stored dividend without
  an amount are alerted, never guessed, and `lineage report` lists every dividend without one.
  The MCP lookup covers only held, watchlisted and flagged names, so it is not the amount
  source; its amount-less rows get the file's. The owner sets an amount with `correct-action
  --cash` (or a `cash` column in `correct-actions`), journalled, and no import rewrites it. A
  shadow sleeve's equity counts the dividends its open virtual positions earned, by the
  backtest's rule (`engine/trade_dividends.py`); a closed one's are in its trade result (R-DIV).
- **Trade results include dividends (R-DIV, owner 2026-10-24).** A closed trade's P&L and R
  include the cash dividends its shares earned while held, by the backtest's rule (ex-date in
  the holding period, shares held at the start of the ex-date session: none for a buy on the
  ex-date, kept by a sale on it), in the backtest, paper and live alike (`positions.write_result`,
  `engine/trade_dividends.py`); `hat_trade_result.dividends` holds that part, and the backtest's
  `trades.parquet` and the Telegram close message show it. Fills, marks and orders stay on the
  price basis and the signals on the total-return series, so a dividend is counted once in each.
  A dividend is stored on its ex-date's night and an amount can be corrected later, so the
  nightly `ca.adjust` restates every closed trade's dividends, P&L and R from the stored ones
  (`positions.restate_dividends`); the exposure-ladder step taken at the close stands.
- **Point-in-time adjustments (CA5, owner 2026-10-24).** The adjusted series stays the
  hindsight view; each stored action records when its factor became knowable (`known_on`,
  `known_basis`, `data/ca_known.py`): the ex-date for a split, bonus, TERP rights factor or
  dividend (`ex_open`), the close five sessions on for a market-gap demerger with no dated
  agreeing cross-check (`settled_close`), the resulting company's first close for a ratio-priced
  demerger (`resulting_first_close`), the source's date (never before the ex-date) for a value
  only research or a correction supplies (`research_source_date`, `correction`), and NULL when
  nobody knows: never guessed. A linked ISIN succession records the last bar its rules read
  (reported, not a pending window: a nightly build links with the bars there are).
  A backtest neither scans nor enters a name from an ex-date until its `known_on` (on and on when
  NULL); an open position keeps its price-basis handling. Live is unchanged. Not the full as-of
  rebuild of the series per day.
- **Identity (audit D5, owner 2026-10-23).** An instrument is (market, ISIN): one id and one
  history whatever the series. The series is an attribute: `hat_instrument.series` is the one it
  trades under now (its Fyers symbol, `-EQ` or `-BE`; any other series has none) with
  `series_since` and `previous_series`, and each `bars_daily_raw` row carries the series of its
  day. A day keeps one row per ISIN, the normal-market series' (EQ, BE, BZ, then the rest: BL and
  IL windows trade beside EQ/BE). Databases from before are migrated by `data/isin_merge.py`.
  A position held across a series change keeps its exits (D210). BZ stays unmapped: only an active
  EQ or BE instrument is scanned or entered, and a held one moved to BZ is alerted `unmapped`.
- **ISIN lineage (PR 74, owner 2026-10-24).** A new ISIN for the same shares (face-value split or
  consolidation, reissue, symbol or name change) keeps its own instrument; `hat_isin_lineage`
  links old -> new and the adjusted view reads the chain as one series under the current ISIN's
  id, each corporate action applied once per chain; a position held under an older ISIN reads its
  chain's bars, features and tick. Link only on evidence: succession within 5 exchange sessions
  (weekdays less recorded and stored holidays, never only the stored partitions), the same symbol or an NSE symbol-change record, NSE's first PREVCLOSE carrying the
  old close (1%), the first open within 10% of the factor-adjusted old close, no session move over
  25% unexplained by a stored factor in the 10 days each side, no restructuring text. Anything
  else is a review case, never a link: demergers, mergers, schemes and relistings keep separate
  histories, and a demerger parent stays unadjusted until its factor is stored. The owner's
  `lineage link/unlink` of a found succession overrules the rules and holds across rebuilds (D1).
- **Ranked universe (D257, DB5).** `rs_pct`, `mom_rank` and breadth rank only ordinary equity:
  a row whose own ISIN is `IN` + `E` or `9` + issuer + security type `01`. ETF and fund units
  (`INF`), rights entitlements (type `20`, `-RE`), InvIT and REIT units get features but no
  percentile and are not counted. Read from the bar's ISIN, so it is point in time; part of the
  features' inputs digest.
- **Storage (T5, T6, T10, T11).** Postgres is the system of record; price history is date-partitioned
  Parquet read through DuckDB; strategy configs are versioned JSONB with YAML import/export;
  indicators are own polars code on adjusted bars.
- **Engine (T7, T8, T12).** In-process scheduler with jobs in Postgres; one async session owner per
  broker account; one engine for backtest and live.
- **Backtest.** Behaviours not modelled are listed in `KNOWN_GAPS` (`backtest/spec.py`) and
  reported by every run. No fixture pins the result: a change to the loop is checked by a base vs
  head run on real data.

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
- **Verification (T16).** A change is verified by running it once on real data (the affected job,
  CLI or route; `HAT_DATA_ROOT` and the local database read-only), never by a backtest. Tests are
  small and only for pure functions at their seams; no test runs a backtest, the pipeline or a
  full database on generated data, and no deliberate-break records or test digests (CLAUDE.md).

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

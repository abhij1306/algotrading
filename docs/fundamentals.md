# Fundamentals store

Every figure the cached results sources give, long format, and the ratios derived from them point
in time (`backend/app/hat/data/fundamentals/`; why: `decisions.md`, Fundamentals store; commands:
`runbook.md`, Fundamentals). It reads the raw results cache only (`raw/nse-results/`,
`raw/secondary-results/`) and never the network. The tables are projections of that cache: each
`extract` and `derive` run replaces them. Nothing in the strategy or backtest path reads them.

## Tables (hat_live)

**`hat_fundamental_filing`**: one version of one source document's figures for one period end
and basis.

- NSE (`source=nse`, `point_in_time=true`): one results XBRL (Reg 33 or Integrated Filing). The
  filings are those the results store reads (same listings, scope and consolidated-first rule).
  Per (period end, basis), documents take versions in filing order; one whose facts repeat the
  latest version's adds nothing. `public_at`, `public_at_source` and `public_at_estimated` follow
  the results timing rules (`data/results/timing.py`). `format` is the XBRL format; `family` is
  `indas`, `nbfc`, `bank`, `insurer` or `other`.
- The secondary source, screener.in (`source=screener`, `family=screener`,
  `point_in_time=false`): the figures one company page shows for one period end. A page shows
  figures as restated when it was fetched, **not the originals as first published**. `public_at`
  is the earliest NSE filing of the same period end (same basis first). Otherwise it is the
  regulatory deadline at 23:59 IST, `estimated`: 45 days after a quarter end, 60 for a March
  quarter or a year, 21 for a shareholding pattern alone. It is never later than the page's fetch
  time (`observed`). The fetch-time summary is public when fetched. Where both sources give a
  figure, NSE's is used.

**`hat_fundamental_fact`**: one numeric figure of a filing, keyed by `(filing_id, n)`.

| column | meaning |
| --- | --- |
| `statement` | `pl`, `bs`, `cf`, `segment`, `other` (NSE); `ratio`, `shareholding`, `snapshot` (screener) |
| `period_kind`, `period_start`, `period_end` | `quarter`; `ytd` (year to date, under a year, from `period_start`); `annual`; `instant` (as at `period_end`) |
| `element`, `context` | NSE: the XBRL element and context id. Screener: the row label and the page section |
| `item` | the canonical item where one maps (below), else null |
| `value`, `unit` | rupees (`inr`); `inr_per_share` (EPS, price); `shares`; `pure`; `fraction` (a percentage / 100); `days`; `count` |

An NSE document gives its quarter (`OneD`), its year to date (`FourD`, which carries the
half-year and year-end cash flow statement), its balance sheet as at the period end (`OneI`,
`FourI`; half-year documents only), and the dimensional contexts named after them (segments,
lines of other expenses). Comparatives (`PY_I`) are left out. XBRL values are already in rupees.
Screener crore figures are multiplied by 10^7. To get the long format with the filing's columns,
join on `filing_id`. `derive.facts_as_of(session, instrument_id, ts)` does this at a moment: the
latest version public by `ts`.

**`hat_fundamental_metric`**: `(instrument_id, metric, as_of)`. A row is written when a metric's
value or period changes, so the value at a moment is the latest row at or before it
(`derive.metrics_as_of`). The moments are each filing's `public_at`, plus the latest session's
close. `period_end` is the quarter, year or balance sheet the value reflects.

## Canonical items

Mapping: `items.py` (`NSE_ITEMS`, the elements of each item in priority order; `SCREENER_ITEMS`).

- P&L: `revenue` (banks: interest earned), `other_income`, `total_income`, `total_expenses`
  (banks: before provisions), `material_cost`, `purchases_stock`, `inventory_change`,
  `employee_cost`, `finance_cost` (banks: interest expended), `depreciation`, `other_expenses`,
  `provisions`, `exceptional_items`, `pbt`, `tax`, `net_profit` (the period's),
  `net_profit_owners`, `eps_basic`, `eps_diluted`. Screener only: `operating_expenses`,
  `operating_profit`, `opm`, `tax_rate`, `dividend_payout`, `financing_profit`.
- Balance sheet: `total_assets`, `total_liabilities`, `equity`, `equity_owners`, `share_capital`,
  `reserves`, `minority_interest`, `borrowings` (stated total), `borrowings_current`,
  `borrowings_noncurrent`, `debt_securities`, `subordinated_liabilities`, `deposits`, `cash`,
  `inventories`, `receivables`, `payables`, `current_assets`, `current_liabilities`,
  `fixed_assets`, `cwip`, `investments`, `investments_current`, `investments_noncurrent`,
  `advances`. Screener only: `other_liabilities`, `other_assets`, `total_equity_and_liabilities`.
- Cash flow: `cfo`, `cfi`, `cff`, `net_cash_flow`, `capex_ppe`, `capex_intangibles`,
  `dividends_paid`; screener `free_cash_flow`.
- Other: `paid_up_capital`, `face_value`, bank `gross_npa`, `net_npa`, `gross_npa_pct`,
  `net_npa_pct`, `cet1_ratio`; screener ratios (`debtor_days`, `inventory_days`, `payable_days`,
  `cash_conversion_cycle`, `working_capital_days`, `roce`, `roe`), shareholding (`promoters_pct`,
  `fii_pct`, `dii_pct`, `government_pct`, `public_pct`, `others_pct`, `shareholders`) and the
  fetch-time summary (`market_cap`, `price`, `pe`, `book_value_per_share`, `dividend_yield`,
  `face_value`).

Banks have no current assets or liabilities, inventories or receivables. So debt/equity, current
ratio, ROCE, interest coverage, OPM and the working-capital days are not derived for them. NBFC
borrowings are borrowings + debt securities + subordinated liabilities. Insurer taxonomies share
almost nothing with Ind AS: their facts keep raw elements.

## Derived metrics

Each formula is in `formulas.py`; `book.py` picks the inputs. One basis is used per moment:
consolidated when the latest quarter has consolidated figures, else standalone. Profit is the
owners' share where tagged, else the period's. A missing input, a zero denominator, or (where
marked +) a non-positive denominator leaves the metric undefined.

| metric | formula |
| --- | --- |
| quarter flow | the quarter's figure, else for a year end: the year - the nine months |
| `ttm_revenue`, `ttm_net_profit`, `ttm_eps`, `ttm_operating_profit` | the sum of the latest four quarters, else the year when the latest quarter ends one |
| operating profit | revenue - (total expenses - finance cost - depreciation), else screener's |
| `opm`, `npm` | TTM operating profit / TTM revenue; TTM profit / TTM revenue |
| `revenue_yoy`, `profit_yoy`, `eps_yoy`, `revenue_qoq`, `profit_qoq` | quarter / same quarter a year (or a quarter) earlier - 1, prior + |
| `sales_cagr_Ny`, `profit_cagr_Ny` (N = 3, 5, 10) | (year / year N earlier) ^ (1/N) - 1, both + |
| equity | owners' equity, else equity - minority interest, else share capital + reserves |
| borrowings | stated total, else current + non-current; + debt securities + subordinated |
| `roe` | TTM profit / mean(equity now, a year earlier) + |
| `roce` | (TTM PBT + TTM finance cost) / mean(equity + borrowings, now and a year earlier) + |
| `roa`, `asset_turnover` | TTM profit, TTM revenue / mean(total assets now, a year earlier) + |
| `debt_to_equity`, `current_ratio` | borrowings / equity +; current assets / current liabilities |
| `interest_coverage` | (TTM PBT + TTM finance cost) / TTM finance cost + |
| `debtor_days`, `inventory_days`, `payable_days` | receivables / TTM revenue x 365; inventories, payables / TTM cost of goods (materials + purchases + inventory change) x 365, + |
| `cfo`, `free_cash_flow`, `cfo_to_net_profit` | the latest year's CFO; CFO - capex (PPE + intangibles), else screener's; CFO / the year's profit |
| `shares` | paid-up capital / face value (latest NSE quarter), else screener's share capital / face value; x 1/factor for each split or bonus after that period end up to the price's session |
| `price`, `market_cap` | the last close known at the moment (from 15:30 IST that session); shares x close |
| `pe`, `earnings_yield`, `pb`, `book_value_per_share` | market cap / TTM profit +; TTM profit / market cap; market cap / equity +; equity / shares |
| `dividend_yield` | the latest year's dividends paid (cash flow), else payout x the year's profit, / market cap |

Valuations are stored only at the moments above. For any other day, apply the same formulas to
that day's close and the metrics as of that day. EPS is as reported: a quarter before a split or
bonus is not restated, so `ttm_eps` and `eps_yoy` across one are not comparable. `pe` uses profit
and market cap, so it is unaffected.

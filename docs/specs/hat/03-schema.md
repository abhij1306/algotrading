# 03: Schema

Scope: the 41 Postgres tables (five groups), the event log, the rules the database itself enforces, indexes, retention, and how the schema is created and evolved in this repo. Parquet datasets are specified in 02 §8. Requirement format and verification methods: see README.md and 11-verification.md.

Types in the column tables are the logical Postgres types; §10 says how each maps to a portable SQLAlchemy type. "PK" = primary key, "FK" = foreign key, "null" = nullable (columns are NOT NULL unless marked null).

## §1 Conventions

Copied from SCH "Summary and conventions":

| Convention | Rule |
| --- | --- |
| Prefix | `hat_` on every table, so the module can leave SmartTrader cleanly |
| Keys | `uuid` primary keys (v7, time-ordered); event log uses `bigint` identity |
| Tenancy | `user_id` on every owned table (T1), single user in MVP |
| Market | `market_id` on instruments, strategies and calendars (ready for P2) |
| Money | `numeric(18,4)` plus a `currency` column; never float |
| Time | `timestamptz` in UTC; `trade_date date` in exchange-local terms |
| Config | Versioned JSONB validated against criterion schemas in the app; `config_hash` (sha256) stored |
| Immutability | Published strategy versions, risk-policy versions and events are never updated; changes create new rows |
| Virtual vs real | `is_virtual boolean` on orders, fills and positions; shadow mode uses the same tables |

Tables whose SCH column list starts with `id` have a uuid v7 `id` PK. Tables without an `id` column use the natural key stated in their table below as PK. Ratios and weights (factors, multipliers, scores, percentages) use unscaled `numeric`, never float.

#### HAT-SCH-010: Every HAT table is prefixed and self-contained
- **Statement:** Every HAT table is named `hat_*`, and no HAT table has a foreign key to a non-HAT table.
- **Source:** SCH "Summary and conventions" (Prefix); MA "Boundary rules" ("`hat` owns its own tables"); PLAN §4.2 ("Existing tables ... are inputs for seeding, not FK targets").
- **PR:** 2, 3
- **Verification:** CONF `schema.prefix_and_boundary`
- **Acceptance:**
  - Property: for every table registered on `Base.metadata` by `app/hat/models/`, the name starts with `hat_` and every FK target is a `hat_*` table. Harm: HAT cannot be extracted later, and a SmartTrader migration can break HAT rows.

#### HAT-SCH-011: Keys are uuid v7 generated in Python
- **Statement:** `id` PKs are uuid v7 produced by HAT's own `uuid7()` helper (not the 3.14-only `uuid.uuid7`); `hat_event.id` is a bigint identity.
- **Source:** SCH conventions (Keys); PLAN §1 ("add a small `uuid7()` helper"), §3 Migrations item 1.
- **PR:** 2
- **Verification:** PBT `ids.uuid7_ordered`, CONF `schema.key_types`
- **Acceptance:**
  - Property: for every sequence of `uuid7()` calls in one process, each value has version 7 and the RFC 4122 variant, and values are strictly increasing in byte order. Harm: ids collide or lose time ordering, breaking idempotency keys and index locality.
  - Property: every HAT table with an `id` column has a uuid PK, except `hat_event` whose PK is (id bigint identity, recorded_at). Harm: mixed key types break FK joins and the event partitioning.

#### HAT-SCH-012: Tenancy
- **Statement:** Owned root tables carry `user_id` (uuid, not null); in the MVP every row uses the single `HAT_USER_ID` from settings.
- **Source:** SCH conventions (Tenancy); T1; PLAN §4.1 (T1: "one fixed user uuid in settings").
- **PR:** 2, 3
- **Verification:** CONF `schema.user_id_columns`
- **Acceptance:**
  - Property: `hat_strategy`, `hat_risk_policy_version`, `hat_broker_account`, `hat_bot`, `hat_universe` and `hat_event` each have a non-null `user_id`, and every other owned table reaches a `user_id` through a non-null FK chain to one of them. Harm: a later multi-user build cannot tell whose bots, trades or policies a row belongs to.
- **[OWNER-PENDING]** `user_id` on child runtime tables: default follow the SCH column lists (user_id on the roots above; child tables reach it through `bot_id`/`broker_account_id`) rather than PLAN §4.1's "every hat table"; owner to confirm.

#### HAT-SCH-013: Market scoping
- **Statement:** `market_id` is on instruments, strategies, calendars, universes, regime-model versions and regime states.
- **Source:** SCH conventions (Market); MA "Phase progression" ("carry `market_id` everywhere").
- **PR:** 2
- **Verification:** CONF `schema.market_id_columns`
- **Acceptance:**
  - Property: `hat_instrument`, `hat_strategy`, `hat_trading_calendar`, `hat_universe`, `hat_regime_model_version` and `hat_regime_state` have a non-null `market_id` FK to `hat_market`. Harm: P2 (US) needs a schema change instead of a new `hat_market` row.

#### HAT-SCH-014: Money is exact
- **Statement:** Every money column is `numeric(18,4)` with a `currency` column on its row (or on `hat_market` for reference rows); no money or price column is float.
- **Source:** SCH conventions (Money).
- **PR:** 2, 3
- **Verification:** CONF `schema.money_types`
- **Acceptance:**
  - Property: every HAT column holding a price, amount, fee, equity, capital or P&L is `Numeric(18, 4)`, no HAT column is `Float`, and each such row has a `currency` column (`hat_signal`, `hat_order`, `hat_fill`, `hat_position`, `hat_trade_result`, `hat_allocation_daily`, `hat_daily_bot_snapshot`) or belongs to a market whose `currency` applies (`hat_instrument`, `hat_watchlist_entry`, `hat_corporate_action`). Harm: float rounding mis-sizes orders and P&L drifts from the broker's numbers.

#### HAT-SCH-015: Time is UTC-aware; trade dates are exchange-local
- **Statement:** Timestamps are `timestamptz` stored in UTC with timezone-aware defaults; `trade_date` is a `date` in exchange-local (IST) terms.
- **Source:** SCH conventions (Time); PLAN §1 ("HAT PRs ... use timezone-aware UTC defaults").
- **PR:** 2
- **Verification:** CONF `schema.time_types`, PBT `time.trade_date_ist`
- **Acceptance:**
  - Property: every HAT timestamp column is `DateTime(timezone=True)`; record-creation columns that are `not null` (`created_at`, `recorded_at`, `requested_at` and the like) have an aware-UTC default (no `datetime.utcnow`), while nullable event timestamps (`placed_at`, `closed_at`, `decided_at`, `superseded_at` and the like) have no default and stay null until the event happens; and every `trade_date` column is `Date`. Harm: naive datetimes shift sessions by 5:30 and add deprecation warnings.
  - Property: for every UTC instant t, `trade_date_of(t)` equals the calendar date of t in Asia/Kolkata. Harm: an evening job writes rows under tomorrow's or yesterday's trade date.

#### HAT-SCH-016: Configs are validated JSON with a canonical hash
- **Statement:** Configs (strategy, universe definition, risk policy, regime model) are JSONB validated in the app against criterion schemas, with `config_hash` = sha256 of the canonical JSON.
- **Source:** SCH conventions (Config); T10.
- **PR:** 2, 8
- **Verification:** PBT `config.hash_canonical`
- **Acceptance:**
  - Property: for every config, the hash is unchanged by key reordering and whitespace, and changes whenever any value changes. Harm: two different strategies share a hash, or the same strategy appears as two versions.
  - Property: for every config that fails its criterion schemas, no row is written. Harm: an invalid config reaches the engine at the next scan.

#### HAT-SCH-017: Virtual and real rows share tables
- **Statement:** `hat_order`, `hat_fill` and `hat_position` carry `is_virtual`; shadow mode writes the same tables with `is_virtual = true`.
- **Source:** SCH conventions (Virtual vs real); B1.
- **PR:** 3
- **Verification:** PBT `runtime.virtual_consistency`
- **Acceptance:**
  - Property: for every reachable set of rows, a fill has the same `is_virtual` as its order, an order linked to a position has the same `is_virtual` as that position, and a position's `is_virtual` is true exactly when its signal's mode was shadow or the signal was skipped in confirm mode. Harm: virtual trades count as real P&L, or real positions are managed as virtual and lose their broker stops.

## §2 Reference tables (9)

The instrument row (ISIN + exchange + series) is the anchor every other table points to (SCH "Reference and market data").

**`hat_market`**

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| code | text | NSE (US later); unique |
| timezone | text | `Asia/Kolkata` for NSE |
| currency | char(3) | `INR` for NSE |
| session_open | time | 09:15 for NSE |
| session_close | time | 15:30 for NSE |

**`hat_instrument`**: unique (market_id, isin, series); delisted rows kept.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| market_id | uuid FK hat_market | |
| isin | text | |
| series | text | EQ, BE, ... |
| symbol | text | current NSE symbol |
| name | text | |
| face_value | numeric(18,4) | |
| listed_on | date null | |
| delisted_on | date null | |
| active | boolean | |

**`hat_instrument_symbol_history`**: PK (instrument_id, valid_from); resolves old symbols in history files.

| Column | Type | Notes |
| --- | --- | --- |
| instrument_id | uuid FK hat_instrument | |
| symbol | text | |
| valid_from | date | |
| valid_to | date null | null = current |

**`hat_broker_symbol`**: PK (instrument_id, broker); unique (broker, broker_symbol).

| Column | Type | Notes |
| --- | --- | --- |
| instrument_id | uuid FK hat_instrument | |
| broker | text | `fyers` in MVP |
| broker_symbol | text | e.g. `NSE:SBIN-EQ` |
| broker_token | text null | |
| refreshed_at | timestamptz | |

**`hat_price_band`**: PK (instrument_id, effective_from); latest row per instrument is current.

| Column | Type | Notes |
| --- | --- | --- |
| instrument_id | uuid FK hat_instrument | |
| band_pct | numeric null | 2, 5, 10, 20; null = no band |
| effective_from | date | |

**`hat_industry`**: PK (instrument_id, effective_from); feeds the per-industry cap.

| Column | Type | Notes |
| --- | --- | --- |
| instrument_id | uuid FK hat_instrument | |
| sector | text | |
| industry | text | |
| effective_from | date | |

**`hat_corporate_action`**: unique (instrument_id, ex_date, action_type); adjusted prices computed from these.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| instrument_id | uuid FK hat_instrument | |
| ex_date | date | |
| action_type | text | split, bonus, ... |
| adjustment_factor | numeric | unscaled; never rounded to 4 dp |
| raw_text | text | |
| source | text | mcp, rss, archive |

**`hat_trading_calendar`**: PK (market_id, date); the scheduler and bar alignment read this.

| Column | Type | Notes |
| --- | --- | --- |
| market_id | uuid FK hat_market | |
| date | date | |
| is_trading | boolean | |
| open_at | timestamptz null | null when not trading |
| close_at | timestamptz null | |
| note | text null | holiday name, special session |

**`hat_announcement`**: unique (guid); unmatched items kept with null instrument.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| guid | text | feed GUID, dedupe key |
| feed | text | announcements, financial_results, corporate_actions |
| instrument_id | uuid FK hat_instrument null | |
| company_raw | text | |
| category | text | |
| title | text | |
| url | text | attachment URL |
| published_at | timestamptz | |
| raw | jsonb | item as received |

#### HAT-SCH-020: Reference tables have exactly the specified columns
- **Statement:** The nine reference tables exist with the columns, types and keys in §2.
- **Source:** SCH "Reference and market data".
- **PR:** 2
- **Verification:** CONF `schema.reference_columns`
- **Acceptance:**
  - Property: for each of the nine tables, the column set, types, nullability and PK reflected from the created schema (SQLite and Postgres) equal the §2 table. Harm: loaders and criteria write to or read from columns that do not exist.

#### HAT-SCH-021: NSE market row is seeded
- **Statement:** `hat_market` holds an NSE row (Asia/Kolkata, INR, 09:15–15:30) after `init_db()`.
- **Source:** SCH (`hat_market`); MA "Phase progression" (NSE 09:15–15:30 IST, ₹); PLAN PR 2.
- **PR:** 2
- **Verification:** CONF `seed.market_nse`
- **Acceptance:**
  - Property: after any number of `init_db()` runs there is exactly one `hat_market` row with code `NSE`, and its timezone, currency and session times equal the values above. Harm: sessions and trade dates are computed in the wrong zone, or duplicate markets split instruments.

#### HAT-SCH-022: Reference uniqueness is enforced by the database
- **Statement:** The database rejects duplicates of `hat_instrument` (market_id, isin, series), `hat_broker_symbol` (broker, broker_symbol), `hat_corporate_action` (instrument_id, ex_date, action_type) and `hat_announcement` (guid).
- **Source:** SCH "Reference and market data" (Notes column).
- **PR:** 2
- **Verification:** DB `reference.instrument_unique`, DB `reference.broker_symbol_unique`, DB `reference.corporate_action_unique`, DB `reference.announcement_guid_unique`
- **Acceptance:**
  - Property: for each of the four keys, inserting a second row with the same key raises an integrity error on Postgres, whatever the other column values. Harm: duplicate instruments, broker symbols, double-applied splits or repeated catalysts.

#### HAT-SCH-023: Price band values are restricted
- **Statement:** `hat_price_band.band_pct` is one of 2, 5, 10, 20 or null.
- **Source:** DS "Reference data" (band % (2/5/10/20/none)).
- **PR:** 2
- **Verification:** DB `reference.band_pct_check`
- **Acceptance:**
  - Property: an insert with any other `band_pct` value raises on Postgres and SQLite. Harm: a mistyped band admits stocks into the EP gap scan that cannot gap 10%.

## §3 Strategy, risk policy and bot tables (9)

A strategy holds versions; a bot deploys one live version to one broker account; the risk policy is a separate versioned record holding every risk number. Nothing in a strategy version can set size (SCH "Strategy, risk policy and bots").

**`hat_criterion_type`**: PK (code, version); the library catalogue the app validates configs against.

| Column | Type | Notes |
| --- | --- | --- |
| code | text | e.g. `sma_above`, `vcp` |
| version | int | |
| stage | text | universe, setup, trigger, entry_filter |
| param_schema | jsonb | JSON Schema |
| data_needs | jsonb | |

**`hat_strategy`**

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| user_id | uuid | |
| market_id | uuid FK hat_market | |
| name | text | e.g. `stage2-contraction-breakout` |
| persona | text | |
| created_at | timestamptz | |

**`hat_strategy_version`** (SCH SQL, verbatim):

```sql
create table hat_strategy_version (
  id                uuid primary key,
  strategy_id       uuid not null references hat_strategy(id),
  version           int  not null,
  status            text not null check (status in ('draft','backtested','shadow','live','retired')),
  config            jsonb not null,
  config_hash       text not null,
  parent_version_id uuid references hat_strategy_version(id),
  backtest_run_id   uuid references hat_backtest_run(id),
  shadow_started_at timestamptz,
  promoted_at       timestamptz,
  created_at        timestamptz not null default now(),
  unique (strategy_id, version),
  check (status <> 'live' or (backtest_run_id is not null and shadow_started_at is not null))
);
create unique index hat_one_live_version on hat_strategy_version (strategy_id) where status = 'live';
```

**`hat_risk_policy_version`** (SCH SQL, verbatim). `policy` holds risk %, caps, ladder, kill thresholds, per-bot allocations (base weights) and the multiplier bounds (06 §1):

```sql
create table hat_risk_policy_version (
  id             uuid primary key,
  user_id        uuid not null,
  version        int  not null,
  policy         jsonb not null,
  direction      text not null check (direction in ('initial','increase','decrease')),
  requested_at   timestamptz not null default now(),
  effective_from timestamptz not null,
  gate_evidence  jsonb,
  superseded_at  timestamptz,
  unique (user_id, version),
  check (direction <> 'increase' or effective_from >= requested_at + interval '7 days')
);
```

**`hat_broker_account`**: capabilities come from the adapter (GTT, tag field).

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| user_id | uuid | |
| broker | text | `fyers` in MVP |
| account_ref | text | |
| openalgo_ref | text null | unused in MVP; kept for a later adapter (PLAN C5) |
| capabilities | jsonb | GTT for delivery, order-tag field and length, order types, static-IP requirement |
| status | text | |

**`hat_bot`** (the sleeve; no risk columns):

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| user_id | uuid | |
| strategy_id | uuid FK hat_strategy | |
| live_version_id | uuid FK hat_strategy_version null | |
| broker_account_id | uuid FK hat_broker_account | |
| execution_mode | text | shadow, confirm, auto; default shadow |
| status | text | active, paused, manage_only |
| universe_version_id | uuid FK hat_universe_version null | portfolio layer |
| lifecycle_state | text | research, backtested, shadow, live, probation, retired |
| risk_cluster | text | S1 and S2 share one |

**`hat_mode_change`**: mode changes queue for the next session.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| bot_id | uuid FK hat_bot | |
| from_mode | text | |
| to_mode | text | shadow, confirm, auto |
| requested_at | timestamptz | |
| effective_from | timestamptz | next session open |
| via | text | telegram, dashboard |

**`hat_kill_switch`**: latest row per scope is current.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| scope | text | account, bot |
| bot_id | uuid FK hat_bot null | required when scope = bot |
| state | text | tripped, armed |
| tripped_at | timestamptz | |
| reason | text | drawdown_account, drawdown_bot, manual, data_health, broker_health |
| rearmed_at | timestamptz null | |

**`hat_exposure_state`**: PK bot_id; progressive-exposure ladder position.

| Column | Type | Notes |
| --- | --- | --- |
| bot_id | uuid FK hat_bot | |
| level | text | L1, L2, L3 |
| consecutive_wins | int | ≥ 0 |
| consecutive_losses | int | ≥ 0 |
| updated_at | timestamptz | |

#### HAT-SCH-030: Strategy, risk and bot tables have exactly the specified columns
- **Statement:** The nine tables in §3 exist with the columns, types and keys shown, including the plan additions on `hat_bot` and `hat_broker_account`.
- **Source:** SCH "Strategy, risk policy and bots" and "Portfolio layer" (`hat_bot` gains `universe_version_id`, `lifecycle_state`, `risk_cluster`); PLAN C5.
- **PR:** 2
- **Verification:** CONF `schema.strategy_risk_bot_columns`
- **Acceptance:**
  - Property: for each of the nine tables, the reflected column set, types, nullability, PK and uniques equal §3. Harm: governance code and triggers rely on columns that are missing or typed differently.

#### HAT-SCH-031: Status and mode domains are enforced
- **Statement:** CHECK constraints restrict `hat_strategy_version.status`, `hat_risk_policy_version.direction`, `hat_bot.execution_mode` (shadow/confirm/auto), `hat_bot.status` (active/paused/manage_only), `hat_bot.lifecycle_state`, `hat_kill_switch.scope`/`state`, `hat_exposure_state.level` and `hat_criterion_type.stage` to the values in §3.
- **Source:** SCH (Notes columns and SQL); B1 (revised), B2 (default shadow); PLAN C10 (`shadow|confirm|auto`).
- **PR:** 2
- **Verification:** DB `governance.domain_checks`
- **Acceptance:**
  - Property: for every one of these columns, inserting or updating to a value outside its §3 domain raises on Postgres and SQLite, and `hat_bot.execution_mode` defaults to `shadow`. Harm: an unknown mode such as the superseded `signal` reaches the router and is treated as auto.
- **[OWNER-PENDING]** Value sets the docs do not list: `hat_kill_switch.state` {tripped, armed}, `hat_kill_switch.reason` {drawdown_account, drawdown_bot, manual, data_health, broker_health}, `hat_mode_change.via` {telegram, dashboard} and `hat_quality_check.status` {PASS, FAIL}: defaults derived from BL "Kill switches" and the event catalogue; owner to confirm.

#### HAT-SCH-032: Bots and strategy versions hold no risk numbers
- **Statement:** `hat_bot` has no risk columns and a strategy version config cannot set size; every risk number lives in `hat_risk_policy_version.policy`.
- **Source:** SCH "Strategy, risk policy and bots" ("Nothing in a strategy version can set size"; `hat_bot` "no risk columns"); BL "Risk policy is account-level".
- **PR:** 2, 8
- **Verification:** CONF `schema.bot_no_risk_columns`, PBT `config.no_risk_keys`
- **Acceptance:**
  - Property: `hat_bot` has no column for risk %, position size, capital, caps or kill thresholds, and for every strategy config accepted by validation, no key at any depth names a risk, size, capital or cap parameter. Harm: a strategy edit quietly raises risk without the cooling-off gate.

#### HAT-SCH-033: Kill-switch rows are well formed
- **Statement:** A `hat_kill_switch` row with scope `bot` has a `bot_id`; a row with scope `account` has none; the latest row per scope (and bot) is the current state.
- **Source:** SCH (`hat_kill_switch` "Latest row per scope is current"); BL "Kill switches".
- **PR:** 2, 10
- **Verification:** DB `governance.kill_switch_scope_check`, PBT `kill.current_state`
- **Acceptance:**
  - Property: inserting scope = bot with null `bot_id`, or scope = account with a `bot_id`, raises. Harm: a bot-level trip applies to no bot, or an account trip is read as one bot's.
  - Property: for every sequence of kill rows, the state read for (scope, bot) equals the `state` of the row with the greatest `tripped_at`/`rearmed_at` event for that key. Harm: an old armed row hides a newer trip.

#### HAT-SCH-034: Exposure counters are non-negative
- **Statement:** `hat_exposure_state` has one row per bot, `level ∈ {L1, L2, L3}` and non-negative counters.
- **Source:** SCH (`hat_exposure_state`); BL "Progressive exposure (B16)".
- **PR:** 2, 10
- **Verification:** DB `governance.exposure_checks`
- **Acceptance:**
  - Property: a second row for a bot, a level outside L1–L3, or a negative counter raises on Postgres. Harm: the ladder reads a corrupt level and allows 6 positions at start.

## §4 Portfolio tables (7)

Sleeves, universes, regime and daily weights. `hat_bot` is the sleeve (§3). `hat_backtest_run` gains `family_key` (§7) and the multiplier bounds live inside `hat_risk_policy_version.policy`, so raising them inherits the 7-day rule (SCH "Portfolio layer").

**`hat_universe`**

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| user_id | uuid | |
| market_id | uuid FK hat_market | |
| name | text | |
| kind | text | manual, screener; MVP manual only |

**`hat_universe_version`**: unique (universe_id, version); same immutability and one-live-version rules as strategy versions (§8).

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| universe_id | uuid FK hat_universe | |
| version | int | |
| status | text | draft, backtested, shadow, live, retired |
| definition | jsonb | |
| config_hash | text | sha256 of canonical `definition` |
| backtest_run_id | uuid FK hat_backtest_run null | |
| promoted_at | timestamptz null | |

**`hat_universe_member`**: PK (universe_id, instrument_id, effective_from); point-in-time membership.

| Column | Type | Notes |
| --- | --- | --- |
| universe_id | uuid FK hat_universe | |
| instrument_id | uuid FK hat_instrument | |
| effective_from | date | |
| effective_to | date null | null = still a member |
| reason | text | |
| criteria_snapshot | jsonb null | screener changes record the criterion that caused them |

**`hat_regime_model_version`**: v1 rules in MVP; v2 model in P2.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| market_id | uuid FK hat_market | |
| kind | text | rules, hmm, gmm |
| version | int | |
| status | text | |
| config | jsonb | |
| fitted_at | timestamptz null | |
| artifact_path | text null | |

**`hat_regime_state`**: PK (market_id, model_version_id, trade_date); `confirmed` after the 3-day rule.

| Column | Type | Notes |
| --- | --- | --- |
| trade_date | date | |
| market_id | uuid FK hat_market | |
| model_version_id | uuid FK hat_regime_model_version | |
| state | text | bull_quiet, bull_volatile, correction, bear |
| probabilities | jsonb null | P2 models |
| inputs | jsonb | the three inputs |
| confirmed | boolean | |

**`hat_allocation_daily`**: PK (trade_date, bot_id); the audit trail for every weight.

| Column | Type | Notes |
| --- | --- | --- |
| trade_date | date | |
| bot_id | uuid FK hat_bot | |
| base_weight | numeric | |
| regime_mult | numeric | |
| health_mult | numeric | |
| final_weight | numeric | |
| capital | numeric(18,4) | |
| risk_pct | numeric | |
| currency | char(3) | |

**`hat_fundamental`**: created empty in the MVP (P2); screeners read only rows with `filed_at` ≤ the evaluation date.

| Column | Type | Notes |
| --- | --- | --- |
| instrument_id | uuid FK hat_instrument | |
| period_end | date | |
| filed_at | timestamptz | |
| metric | text | |
| value | numeric | |
| source | text | |

PK (instrument_id, metric, period_end, filed_at).

#### HAT-SCH-040: Portfolio tables have exactly the specified columns
- **Statement:** The seven portfolio tables exist with the columns, types and keys in §4; `hat_fundamental` is created empty.
- **Source:** SCH "Portfolio layer (7 tables)".
- **PR:** 2
- **Verification:** CONF `schema.portfolio_columns`
- **Acceptance:**
  - Property: for each of the seven tables, the reflected column set, types, nullability, PK and uniques equal §4, and no MVP code path writes `hat_fundamental`. Harm: the allocator's audit trail or the regime history cannot be stored or replayed.

#### HAT-SCH-041: Portfolio uniqueness and domains
- **Statement:** The database enforces unique (universe_id, version), unique (market_id, model_version_id, trade_date) on regime states, unique (trade_date, bot_id) on allocations, `hat_universe.kind ∈ {manual, screener}`, `hat_regime_model_version.kind ∈ {rules, hmm, gmm}` and `hat_regime_state.state ∈ {bull_quiet, bull_volatile, correction, bear}`.
- **Source:** SCH "Portfolio layer" (Notes); BL "Regime engine v1" (four states).
- **PR:** 2
- **Verification:** DB `portfolio.unique_keys`, DB `portfolio.domain_checks`
- **Acceptance:**
  - Property: a second regime state for the same (market, model version, date), a second allocation for the same (date, bot), a duplicate universe version, or an out-of-domain kind or state raises on Postgres. Harm: two weights for one bot and day make sizing ambiguous.

#### HAT-SCH-042: Universe membership is point-in-time without overlaps
- **Statement:** Membership intervals [effective_from, effective_to) of one (universe, instrument) never overlap, and "member on date d" is answered from them.
- **Source:** SCH (`hat_universe_member` "Point-in-time membership"); BL "Stock universes".
- **PR:** 9
- **Verification:** PBT `universe.membership_intervals`
- **Acceptance:**
  - Property: for every sequence of membership edits, the intervals of each (universe, instrument) are disjoint with effective_from < effective_to (when set), and member(u, i, d) is true exactly when one interval contains d. Harm: backtests include stocks before they joined the universe, a survivorship bias.

#### HAT-SCH-043: Fundamentals are read by filing date
- **Statement:** Any read of `hat_fundamental` for evaluation date d returns only rows with `filed_at ≤ d`.
- **Source:** SCH (`hat_fundamental`); BL "Stock universes" ("a result counts from its filing date, never its period-end date").
- **PR:** P2 (table in 2)
- **Verification:** PBT `universe.fundamentals_point_in_time`
- **Acceptance:**
  - Property: for every set of fundamental rows and evaluation date d, the reader's result contains no row with `filed_at > d`. Harm: backtests use numbers the market had not seen yet.

## §5 Runtime tables (8)

Every signal records the exact strategy version and risk-policy version that produced it, so any trade can be replayed and explained (SCH "Runtime").

**`hat_watchlist_entry`**

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| bot_id | uuid FK hat_bot | |
| strategy_version_id | uuid FK hat_strategy_version | |
| playbook | text | e.g. `breakout`, `episodic_pivot`; `default` for single-playbook bots |
| instrument_id | uuid FK hat_instrument | |
| trade_date | date | the session the entry is for |
| score | numeric | setup score in [0, 1] |
| criteria_results | jsonb | pass/fail/score + value per criterion |
| pivot | numeric(18,4) null | |
| planned_stop | numeric(18,4) null | |
| adtr | numeric(18,4) null | |
| status | text | |

**`hat_signal`**: unique (bot_id, playbook, instrument_id, trade_date), one signal per day; `id` is the idempotency key.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| bot_id | uuid FK hat_bot | |
| strategy_version_id | uuid FK hat_strategy_version | |
| risk_policy_version_id | uuid FK hat_risk_policy_version | |
| playbook | text | |
| instrument_id | uuid FK hat_instrument | |
| trade_date | date | |
| triggered_at | timestamptz | |
| trigger_price | numeric(18,4) | |
| stop_price | numeric(18,4) | |
| qty | int | ≥ 0 |
| risk_amount | numeric(18,4) | |
| currency | char(3) | |
| mode | text | shadow, confirm, auto (mode in force when triggered) |
| state | text | trade lifecycle state (07 §4) |
| risk_decision | jsonb | decision, reason code, warnings |

**`hat_signal_announcement`**: PK (signal_id, announcement_id); EP catalysts attached to a signal.

| Column | Type | Notes |
| --- | --- | --- |
| signal_id | uuid FK hat_signal | |
| announcement_id | uuid FK hat_announcement | |

**`hat_approval`**: PK signal_id; skipped signals continue as shadow trades for the veto score.

| Column | Type | Notes |
| --- | --- | --- |
| signal_id | uuid FK hat_signal | |
| sent_at | timestamptz | |
| expires_at | timestamptz | |
| decided_at | timestamptz null | |
| decision | text null | take, skip, expired |
| price_at_decision | numeric(18,4) null | |
| telegram_message_id | bigint null | |
| token_hash | text | hash of the signed token; the token itself is never stored |

**`hat_order`**: unique (tag).

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| signal_id | uuid FK hat_signal | |
| position_id | uuid FK hat_position null | |
| broker_account_id | uuid FK hat_broker_account | |
| purpose | text | entry, stop_day, stop_gtt, exit_partial, exit_full, flatten |
| side | text | buy, sell |
| qty | int | > 0 |
| order_type | text | |
| price | numeric(18,4) null | |
| trigger_price | numeric(18,4) null | |
| tag | text | 20 chars, `HT` + purpose code + signal encoding (PLAN C12; 07 §10) |
| broker_order_id | text null | null until the broker confirms; always null when virtual |
| status | text | |
| is_virtual | boolean | |
| placed_at | timestamptz null | |
| currency | char(3) | |

**`hat_fill`**: unique (order_id, broker_trade_id).

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| order_id | uuid FK hat_order | |
| qty | int | > 0 |
| price | numeric(18,4) | |
| fees | numeric(18,4) | |
| currency | char(3) | |
| filled_at | timestamptz | |
| broker_trade_id | text null | null for virtual fills |
| is_virtual | boolean | |

**`hat_position`**: the trigger blocks any `current_stop` move away from price (§8).

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| bot_id | uuid FK hat_bot | |
| signal_id | uuid FK hat_signal | |
| instrument_id | uuid FK hat_instrument | |
| is_virtual | boolean | |
| qty_open | int | ≥ 0 |
| avg_entry | numeric(18,4) | |
| initial_stop | numeric(18,4) | |
| current_stop | numeric(18,4) | |
| currency | char(3) | |
| opened_at | timestamptz | |
| closed_at | timestamptz null | |
| state | text | |
| rules_fired | jsonb | exit rules already fired (fire-once) |

**`hat_trade_result`**: PK position_id; written once on close.

| Column | Type | Notes |
| --- | --- | --- |
| position_id | uuid FK hat_position | |
| realized_pnl | numeric(18,4) | |
| fees | numeric(18,4) | |
| currency | char(3) | |
| r_multiple | numeric | |
| holding_days | int | |
| exit_reason | text | |
| mfe_r | numeric | |
| mae_r | numeric | |

#### HAT-SCH-050: Runtime tables have exactly the specified columns
- **Statement:** The eight runtime tables exist with the columns, types and keys in §5.
- **Source:** SCH "Runtime".
- **PR:** 3
- **Verification:** CONF `schema.runtime_columns`
- **Acceptance:**
  - Property: for each of the eight tables, the reflected column set, types, nullability, PK and uniques equal §5. Harm: a trade cannot be replayed or explained from its rows.

#### HAT-SCH-051: One signal per bot, playbook, instrument and day
- **Statement:** The database rejects a second `hat_signal` with the same (bot_id, playbook, instrument_id, trade_date).
- **Source:** SCH (`hat_signal` "one signal per day; `id` is the idempotency key"); MA "Failure handling" ("Duplicate trigger → signal ID is the idempotency key; second order refused").
- **PR:** 3
- **Verification:** DB `runtime.signal_unique_per_day`, TLA `OrderReconciliation!AtMostOneEntryPerSignal`
- **Acceptance:**
  - Property: for every pair of concurrent inserts with the same key, exactly one commits and the other raises an integrity error. Harm: a duplicated trigger opens two positions and doubles the risk.

#### HAT-SCH-052: Every signal names its versions
- **Statement:** `hat_signal.strategy_version_id` and `risk_policy_version_id` are not null and reference existing rows.
- **Source:** SCH "Runtime" ("Every signal records the exact strategy version and risk-policy version that produced it").
- **PR:** 3
- **Verification:** DB `runtime.signal_versions_not_null`
- **Acceptance:**
  - Property: an insert with a null or dangling version id raises on Postgres. Harm: a trade cannot be traced to the rules and risk numbers that produced it.

#### HAT-SCH-053: Order tags are unique and well formed
- **Statement:** `hat_order.tag` is unique and matches the Fyers tag rule `^[A-Za-z0-9]{1,20}$` with the HAT prefix `HT` (20 characters: `HT` + 2-character purpose code + 16-character encoding of the signal uuid).
- **Source:** SCH (`hat_order` unique (tag); "tag is a compact encoding of the signal ID plus a purpose code"); I10; PLAN C12.
- **PR:** 3, 13
- **Verification:** DB `runtime.order_tag_unique`, PBT `tags.format`
- **Acceptance:**
  - Property: a second order with an existing tag raises on Postgres. Harm: a retried placement creates a second broker order instead of finding the first.
  - Property: every tag produced by the encoder for any signal id and purpose matches `^HT[A-Za-z0-9]{18}$`, and decoding it returns the purpose and the 16-character signal prefix it was built from. Harm: reconciliation cannot tell HAT orders from terminal (`ST`) orders, or matches a broker order to the wrong signal.

#### HAT-SCH-054: Order purpose and approval decision domains
- **Statement:** `hat_order.purpose ∈ {entry, stop_day, stop_gtt, exit_partial, exit_full, flatten}`, `side ∈ {buy, sell}`, and `hat_approval.decision ∈ {take, skip, expired}` (or null before a decision).
- **Source:** SCH (`hat_order`, `hat_approval` Notes).
- **PR:** 3
- **Verification:** DB `runtime.domain_checks`
- **Acceptance:**
  - Property: any other value raises on Postgres and SQLite. Harm: an order of unknown purpose escapes the exit-only paths that kill switches allow.

#### HAT-SCH-055: Fills are unique per broker trade
- **Statement:** The database rejects a second `hat_fill` with the same (order_id, broker_trade_id).
- **Source:** SCH (`hat_fill` "Unique (order_id, broker_trade_id)").
- **PR:** 3
- **Verification:** DB `runtime.fill_unique`
- **Acceptance:**
  - Property: inserting the same broker trade twice for an order raises, so replaying a broker trade feed any number of times leaves one fill per broker trade. Harm: a re-polled fill doubles the position quantity.

#### HAT-SCH-056: Approval tokens are stored only as hashes
- **Statement:** `hat_approval` stores `token_hash`, never the signed token.
- **Source:** SCH (`hat_approval.token_hash`); DS "Telegram / Security" (signed, single-use tokens).
- **PR:** 3, 16
- **Verification:** PBT `telegram.token_never_stored`
- **Acceptance:**
  - Property: for every issued token, no column of any HAT table and no log record contains the token string. Harm: anyone with DB or log access can approve trades.

#### HAT-SCH-057: A trade result is written once per position
- **Statement:** `hat_trade_result` has at most one row per position, written when the position closes.
- **Source:** SCH (`hat_trade_result` "Written once on close").
- **PR:** 3, 11
- **Verification:** DB `runtime.trade_result_once`, TLA `TradeLifecycle!ResultOnClose`
- **Acceptance:**
  - Property: a second insert for the same position raises, and in every reachable lifecycle a result row exists if and only if the position's `closed_at` is set. Harm: expectancy and veto scores double-count or miss trades.

#### HAT-SCH-058: Order status values
- **Statement:** `hat_order.status` takes one of the values below; an order row is inserted with status `pending` before any broker call.
- **Source:** SCH (`hat_order.status`, values not listed); PLAN PR 13 ("Persist `hat_order` as PENDING before the broker call"); `.claude/skills/trading-safety/SKILL.md` (persist before call).
- **PR:** 3, 13
- **Verification:** DB `runtime.order_status_check`, TLA `OrderReconciliation!PersistBeforeCall`
- **Acceptance:**
  - Property: every status value outside the set raises, and in every reachable execution a broker `place_order` for a tag happens only after a committed `hat_order` row with that tag and status `pending` exists. Harm: a crash after the broker call leaves a live order HAT does not know about.
- **[OWNER-PENDING]** Status set: default `pending, placed, unconfirmed, partially_filled, filled, cancelled, rejected, expired` (docs silent; derived from the event catalogue and the `SUBMIT_UNCONFIRMED` handling); owner to confirm.

#### HAT-SCH-059: Trade states are the eleven lifecycle states
- **Statement:** `hat_signal.state` is one of the 11 lifecycle states of 07 §4 and `hat_position.state` one of Open, PartiallyExited, ExitPending, Closed; the database rejects any other text.
- **Source:** 07 §4 (D18, Q16); SCH "Runtime" (`state`).
- **PR:** 11
- **Verification:** DB `runtime.state_domain`
- **Acceptance:**
  - Property: an insert or update giving a signal or position a state outside its set raises. Harm: a misspelt state strands a trade that no rule reads.
- **Note:** a bot's entries are evaluated and inserted in one transaction under a Postgres advisory lock keyed by the bot (HAT-EXEC-045); the database check `runtime.per_bot_serialised` runs two such transactions at once and requires that they cannot both take the last position slot.

## §6 Event log

One append-only table records every decision, by the engine or by the user. State tables are updated in the same transaction as the event (SCH "Event log"). DDL (SCH SQL, verbatim):

```sql
create table hat_event (
  id             bigint generated always as identity,
  recorded_at    timestamptz not null default now(),
  occurred_at    timestamptz not null,
  user_id        uuid not null,
  bot_id         uuid,
  aggregate_type text not null,   -- signal, order, position, strategy_version, risk_policy, bot, regime, session, ingest
  aggregate_id   uuid not null,
  event_type     text not null,
  actor          text not null check (actor in ('engine','user','broker','scheduler')),
  correlation_id uuid,            -- the signal ID for anything trade-related
  causation_id   bigint,          -- the event that caused this one
  payload        jsonb not null,
  primary key (id, recorded_at)
) partition by range (recorded_at);  -- monthly partitions

create function hat_event_is_append_only() returns trigger as $$
begin
  raise exception 'hat_event is append-only';
end $$ language plpgsql;

create trigger hat_event_no_update before update or delete on hat_event
  for each row execute function hat_event_is_append_only();
-- and: revoke update, delete on hat_event from the app role
```

### Event types (MVP), verbatim

| Group | Events |
| --- | --- |
| Data | `ingest.completed`, `ingest.failed`, `quality.failed` |
| Signals | `watchlist.published`, `signal.triggered`, `signal.rejected`, `signal.approved`, `signal.skipped`, `signal.expired` |
| Orders | `order.placed`, `order.rejected`, `order.filled`, `order.cancelled`, `gtt.placed`, `gtt.refreshed` |
| Positions | `position.opened`, `stop.moved`, `exit.partial`, `position.closed` |
| Control | `mode.change_requested`, `bot.paused`, `bot.resumed`, `kill.tripped`, `kill.rearmed`, `flatten.executed` |
| Governance | `strategy.version_created`, `strategy.promoted`, `risk.change_requested`, `risk.change_effective` |
| Portfolio | `sleeve.lifecycle_changed`, `regime.state_recorded`, `allocation.computed` |
| Sessions | `broker.logged_in`, `broker.session_lost`, `feed.stale` |

Every user-initiated event carries `actor = user` and the Telegram chat ID in the payload, so discipline metrics (skips, flattens, pauses) come straight from this table.

#### HAT-SCH-060: Event table shape
- **Statement:** `hat_event` has exactly the columns of the DDL above, PK (id, recorded_at), `actor ∈ {engine, user, broker, scheduler}`, and on Postgres is range-partitioned monthly on `recorded_at`.
- **Source:** SCH "Event log" (DDL); I15.
- **PR:** 3
- **Verification:** CONF `schema.event_columns`, DB `events.partitioned`
- **Acceptance:**
  - Property: the reflected columns, types, nullability and PK equal the DDL on both dialects, and on Postgres `hat_event` is a partitioned parent with RANGE (recorded_at). Harm: audits and replay read a log that cannot hold the fields they need, or a single unbounded table.

#### HAT-SCH-061: The event log is append-only
- **Statement:** Any UPDATE or DELETE on `hat_event` raises, enforced by the `hat_event_no_update` trigger.
- **Source:** SCH "Event log" (trigger); I15.
- **PR:** 3
- **Verification:** DB `events.append_only`
- **Acceptance:**
  - Property: for every existing event row and every UPDATE (any column) or DELETE statement on it, Postgres raises `hat_event is append-only` and the row is unchanged; with the trigger dropped (broken variant) the check fails. Harm: the system of record for audits, veto scoring and replay can be rewritten after the fact.

#### HAT-SCH-062: State changes and their events commit together
- **Statement:** `app/hat/events.record(...)` writes in the caller's transaction, so a state change and its event commit or roll back together.
- **Source:** SCH "Event log" ("State tables are updated in the same transaction as the event, so the two can't drift"); PLAN PR 3.
- **PR:** 3
- **Verification:** PBT `events.atomic_with_state`
- **Acceptance:**
  - Property: for every state-changing operation and every failure injected at any point before commit, after rollback neither the state change nor its event exists, and after commit both exist. Harm: the log says a stop moved when it did not, or a position changed with no record.

#### HAT-SCH-063: Event types are exactly the MVP catalogue
- **Statement:** `record()` accepts only the event types in the catalogue above; adding one requires a spec change here.
- **Source:** SCH "Event types (MVP)"; PLAN PR 3 ("the event catalogue matches spec 03 §6 exactly").
- **PR:** 3, 9
- **Verification:** CONF `events.catalogue`, PBT `events.unknown_type_rejected`
- **Acceptance:**
  - Property: the set of event types known to `app/hat/events.py` equals the 35 types in the catalogue table. Harm: code and audits disagree on what can happen.
  - Property: for every string not in the catalogue, `record()` raises and writes nothing. Harm: a typo creates an event that no discipline metric or replay will ever read.

#### HAT-SCH-064: Aggregate types and correlation
- **Statement:** `aggregate_type ∈ {signal, order, position, strategy_version, risk_policy, bot, regime, session, ingest}`; trade-related events carry the signal ID as `correlation_id`; `causation_id` names the causing event when there is one.
- **Source:** SCH "Event log" (DDL comments).
- **PR:** 3, 9
- **Verification:** PBT `events.correlation`
- **Acceptance:**
  - Property: for every recorded event, `aggregate_type` is in the set, every event of the Signals, Orders and Positions groups has `correlation_id` equal to its signal's id, and every non-null `causation_id` refers to an earlier event. Harm: a trade's history cannot be reassembled from the log.

#### HAT-SCH-065: User actions are attributable
- **Statement:** Every user-initiated event has `actor = user` and the Telegram chat ID in its payload.
- **Source:** SCH "Event types (MVP)" (closing paragraph).
- **PR:** 3, 16
- **Verification:** PBT `events.user_actor_chat_id`
- **Acceptance:**
  - Property: for every event caused by a Telegram command or button, `actor = 'user'` and `payload.chat_id` is the sender's chat id. Harm: skips, flattens and pauses cannot be counted in the discipline metrics.

#### HAT-SCH-066: Monthly partitions exist ahead of time
- **Statement:** `ensure_hat_schema` creates the current month's and the next 2 months' `hat_event` partitions, and the nightly `events.partitions` job keeps creating them ahead.
- **Source:** SCH "Event log" ("monthly partitions"); PLAN §3 Migrations item 2, PR 3.
- **PR:** 3
- **Verification:** DB `events.partitions_ahead`
- **Acceptance:**
  - Property: after `ensure_hat_schema` or the partition job runs at any date d, partitions covering the month of d and the 2 following months exist, and an event recorded at any instant in that range lands in the partition of its month. Harm: on the first day of an uncovered month every event insert fails, and with it every state change.

#### HAT-SCH-067: Event ids increase on SQLite too
- **Statement:** On SQLite, where the composite PK with identity is not supported, event ids are assigned by an emulation (sequence table or Python), chosen and documented in PR 3.
- **Source:** PLAN §3 Migrations item 3.
- **PR:** 3
- **Verification:** PBT `events.ids_increasing`
- **Acceptance:**
  - Property: for every sequence of `record()` calls on either dialect, event ids are unique and strictly increase in commit order within a process. Harm: replay order differs from the order things happened.

#### HAT-SCH-068: App-role revoke is deferred
- **Statement:** In the MVP the append-only rule is enforced by the trigger only; `revoke update, delete on hat_event` from a separate app role is deferred.
- **Source:** SCH "Event log" (revoke comment); PLAN C11, Q7.
- **PR:** 3
- **Verification:** DB `events.append_only`
- **Acceptance:**
  - Property: the append-only property of HAT-SCH-061 holds for the single application DB user. Harm: without the trigger the shared app user could rewrite history.
- **[OWNER-PENDING]** Role split: default triggers only in MVP; a separate `hat_app` role is added with the Compose deployment (PR 17) if the owner wants it (PLAN Q7); owner to confirm.

## §7 Backtest and ops tables (7)

**`hat_backtest_run`**: trades go to `backtests/run_id=…/trades.parquet` (02 §8).

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| strategy_version_id | uuid FK hat_strategy_version | |
| risk_policy_version_id | uuid FK hat_risk_policy_version | |
| family_key | text | strategy × universe; every run counts as a trial of its family |
| period_start | date | |
| period_end | date | |
| data_snapshot | jsonb | |
| status | text | |
| metrics | jsonb | |

**`hat_broker_session`**: PK (broker_account_id, trade_date); morning login state.

| Column | Type | Notes |
| --- | --- | --- |
| broker_account_id | uuid FK hat_broker_account | |
| trade_date | date | |
| logged_in_at | timestamptz null | |
| expires_at | timestamptz null | |
| status | text | |

**`hat_job_run`**: unique (job, trade_date) makes jobs idempotent.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| job | text | |
| trade_date | date | |
| started_at | timestamptz | |
| finished_at | timestamptz null | |
| status | text | |
| calls | int | |
| rows | int | |
| error | text null | error code, never raw provider text |

**`hat_quality_check`**: PK (job_run_id, check_name); gate results that block scans.

| Column | Type | Notes |
| --- | --- | --- |
| job_run_id | uuid FK hat_job_run | |
| check_name | text | coverage, ohlc, big_moves, index_snapshot (02 §6) |
| status | text | PASS, FAIL |
| detail | jsonb | |

**`hat_telegram_message`**: links cards to signals.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| chat_id | bigint | |
| kind | text | |
| ref_id | uuid null | e.g. signal id |
| sent_at | timestamptz | |
| message_id | bigint | |

**`hat_command_log`**: includes rejected commands from unknown chats.

| Column | Type | Notes |
| --- | --- | --- |
| id | uuid PK | |
| chat_id | bigint | |
| command | text | |
| args | text null | |
| accepted | boolean | |
| reason | text null | |
| received_at | timestamptz | |

**`hat_daily_bot_snapshot`**: PK (bot_id, trade_date); kill-switch and drawdown source.

| Column | Type | Notes |
| --- | --- | --- |
| bot_id | uuid FK hat_bot | |
| trade_date | date | |
| equity | numeric(18,4) | |
| equity_peak | numeric(18,4) | |
| open_risk | numeric(18,4) | |
| realized_r | numeric | |
| exposure_level | text | L1, L2, L3 |
| currency | char(3) | |

#### HAT-SCH-070: Backtest and ops tables have exactly the specified columns
- **Statement:** The seven tables in §7 exist with the columns, types and keys shown; `hat_job_run` and `hat_quality_check` arrive in PR 2, the others in PR 3.
- **Source:** SCH "Backtest and ops tables (7)"; PLAN §4.2.
- **PR:** 2, 3
- **Verification:** CONF `schema.backtest_ops_columns`
- **Acceptance:**
  - Property: for each of the seven tables, the reflected column set, types, nullability, PK and uniques equal §7. Harm: job idempotency, gate results or kill-switch inputs cannot be stored.

#### HAT-SCH-071: Every backtest run belongs to a family
- **Statement:** `hat_backtest_run.family_key` (strategy × universe) is not null, so every run counts as a trial of its family.
- **Source:** SCH "Portfolio layer" (`hat_backtest_run` gains `family_key`); PL4.
- **PR:** 3, 12
- **Verification:** DB `ops.family_key_not_null`
- **Acceptance:**
  - Property: an insert with a null `family_key` raises on Postgres and SQLite. Harm: a run escapes the trial count and the deflated Sharpe overstates the evidence.

#### HAT-SCH-072: Jobs are unique per trade date
- **Statement:** The database rejects a second `hat_job_run` with the same (job, trade_date).
- **Source:** SCH (`hat_job_run` "Unique (job, trade_date) makes jobs idempotent"); T7.
- **PR:** 2
- **Verification:** DB `ops.job_run_unique`, TLA `JobScheduler!AtMostOnceSuccess`
- **Acceptance:**
  - Property: for every pair of concurrent inserts for the same (job, trade_date), exactly one commits. Harm: two scheduler instances run the same nightly job and double-write data or double-send alerts.

#### HAT-SCH-073: Daily bot snapshots are unique per day
- **Statement:** `hat_daily_bot_snapshot` has at most one row per (bot_id, trade_date).
- **Source:** SCH (`hat_daily_bot_snapshot` "Kill-switch and drawdown source").
- **PR:** 3, 10
- **Verification:** DB `ops.snapshot_unique`
- **Acceptance:**
  - Property: a second snapshot for the same bot and day raises. Harm: the drawdown kill switch reads an ambiguous equity and trips late or not at all.

#### HAT-SCH-074: Unknown-chat commands are logged
- **Statement:** `hat_command_log` records every inbound command, including rejected ones from unknown chats (`accepted = false`, with reason).
- **Source:** SCH (`hat_command_log` "Includes rejected commands from unknown chats"); DS "Telegram / Security".
- **PR:** 3, 16
- **Verification:** PBT `telegram.every_command_logged`
- **Acceptance:**
  - Property: for every inbound update, exactly one `hat_command_log` row is written, with `accepted = false` and a reason whenever the chat is not allowlisted or the command is refused. Harm: probing or abuse of the bot leaves no trace.

## §8 DB-enforced governance

The database itself enforces the governance rules (SCH "Governance enforced in the database"):

| Rule | Enforcement |
| --- | --- |
| Published strategy versions can't change | Trigger rejects UPDATE of `config` when status ≠ draft |
| One live version per strategy | Partial unique index on (strategy_id) where status = live |
| No promotion without evidence | CHECK: status = live requires backtest_run_id and shadow_started_at not null; app checks 20 trading days of shadow |
| Risk increases wait 7 days | CHECK: direction = increase → effective_from ≥ requested_at + 7 days |
| Risk versions are immutable | Trigger rejects UPDATE/DELETE except setting superseded_at |
| Mode changes take effect next session | App sets effective_from to next session open from `hat_trading_calendar` |
| Universe versions | Same immutability and one-live-version rules as strategy versions |
| Invariant 1 | Trigger `hat_position_stop_guard`: a long position's stop can only rise |
| Event log | Trigger `hat_event_no_update` (§6) |

Invariant-1 trigger (SCH SQL, verbatim):

```sql
-- invariant 1 in the database: a long position's stop can only rise
create function hat_stop_only_tightens() returns trigger as $$
begin
  if new.current_stop < old.current_stop then
    raise exception 'stop for position % cannot move from % to %', old.id, old.current_stop, new.current_stop;
  end if;
  return new;
end $$ language plpgsql;

create trigger hat_position_stop_guard
  before update of current_stop on hat_position
  for each row execute function hat_stop_only_tightens();
```

#### HAT-SCH-080: Published strategy versions are immutable
- **Statement:** A trigger rejects any UPDATE of `hat_strategy_version.config` when the row's status is not `draft`.
- **Source:** SCH "Governance enforced in the database"; B5, T10.
- **PR:** 2
- **Verification:** DB `governance.version_config_immutable`
- **Acceptance:**
  - Property: for every version with status in {backtested, shadow, live, retired} and every new config value, an UPDATE of `config` raises and the stored config and `config_hash` are unchanged; for a `draft` row it succeeds. With the trigger dropped (broken variant) the check fails. Harm: a live strategy changes under open trades and signals no longer match the version they cite.

#### HAT-SCH-081: `current_stop` can only rise
- **Statement:** The `hat_position_stop_guard` trigger rejects any UPDATE that lowers `hat_position.current_stop`, and the `hat_position_no_delete_guard` trigger rejects any DELETE of a position, so a delete and re-insert cannot restart the stop lower.
- **Source:** SCH "Runtime" (trigger SQL); BL "Invariants" (1: a stop is never moved further from price).
- **PR:** 3, 11
- **Verification:** DB `governance.stop_only_rises`, TLA `StopPlacement!StopOnlyTightens`
- **Acceptance:**
  - Property: for every position and every pair (old, new) of stop values, an UPDATE setting `current_stop = new` raises if new < old and succeeds if new ≥ old; and every DELETE of a position raises; with either trigger dropped (broken variant) the check fails. Harm: a bug or a manual edit widens a stop and turns a planned 0.25% loss into a larger one.

#### HAT-SCH-082: One live version per strategy and per universe
- **Statement:** Partial unique indexes allow at most one `live` row per `strategy_id` in `hat_strategy_version` and per `universe_id` in `hat_universe_version`.
- **Source:** SCH "Governance enforced in the database"; SCH "Portfolio layer" (universe versions: same rules).
- **PR:** 2
- **Verification:** DB `governance.one_live_version`
- **Acceptance:**
  - Property: for every sequence of status updates, any statement that would leave two `live` rows for one strategy (or one universe) raises, on Postgres and SQLite (`postgresql_where`/`sqlite_where`). Harm: a bot trades two strategy versions at once and its signals cannot be attributed.

#### HAT-SCH-083: No live version without evidence
- **Statement:** A CHECK requires `backtest_run_id` and `shadow_started_at` to be non-null when status = `live`; the app additionally requires 20 trading days of shadow.
- **Source:** SCH "Governance enforced in the database"; BL "Governance" (shadow for 20 trading days, *assumption*); PL4.
- **PR:** 2, 9
- **Verification:** DB `governance.live_requires_evidence`, TLA `SleeveLifecycle!NoLiveWithoutEvidence`
- **Acceptance:**
  - Property: any insert or update producing status = `live` with a null `backtest_run_id` or `shadow_started_at` raises on both dialects. Harm: an untested strategy reaches real money.
  - Property: in every reachable lifecycle, promotion to `live` happens only when the number of trading days in `hat_trading_calendar` from `shadow_started_at` to the promotion date is at least 20. Harm: a strategy is promoted on a few lucky shadow days.
- **[A]** Shadow period 20 trading days: default 20; validated in live review of the first promotions (BL "Governance").

#### HAT-SCH-084: Risk increases wait 7 days
- **Statement:** A CHECK rejects a risk-policy version with direction `increase` and `effective_from < requested_at + 7 days`; on SQLite the app enforces the same rule.
- **Source:** SCH "Governance enforced in the database" (SQL check); BL "Governance" (7-day cooling-off, *assumption*); PLAN §3 Migrations item 2.
- **PR:** 2
- **Verification:** DB `governance.risk_increase_cooling_off`, PBT `risk_policy.cooling_off`
- **Acceptance:**
  - Property: for every (requested_at, effective_from) pair, an `increase` row is accepted if and only if effective_from − requested_at ≥ 7 days; `decrease` and `initial` rows are unaffected. Harm: risk is raised in the heat of a winning streak, bypassing the cooling-off.
- **[A]** Cooling-off 7 days: default 7 days; validated in live review (BL "Governance").

#### HAT-SCH-085: Risk-policy versions are immutable except `superseded_at`
- **Statement:** A trigger rejects DELETE of any `hat_risk_policy_version` row and any UPDATE other than setting `superseded_at` from null.
- **Source:** SCH "Governance enforced in the database"; PLAN §3 Migrations item 2 ("only `superseded_at` may be set").
- **PR:** 2
- **Verification:** DB `governance.risk_version_immutable`
- **Acceptance:**
  - Property: for every existing row, an UPDATE changing any column other than `superseded_at`, an UPDATE of a non-null `superseded_at`, or a DELETE raises; setting a null `superseded_at` succeeds. With the trigger dropped (broken variant) the check fails. Harm: past trades lose the risk numbers they were sized under, and an increase can be rewritten as a decrease.

#### HAT-SCH-086: Mode changes take effect at the next session open
- **Statement:** The app sets `hat_mode_change.effective_from` to the next session open from `hat_trading_calendar`.
- **Source:** SCH "Governance enforced in the database"; MA "Execution modes" (changed only outside market hours); BL "Governance".
- **PR:** 10
- **Verification:** PBT `calendar.next_session_open`, TLA `ExecutionRouter!ModeChangeNextSession`
- **Acceptance:**
  - Property: for every request time t outside market hours and every calendar, `effective_from` equals the `open_at` of the first trading day whose open is after t. Harm: a mode switch to auto lands in the middle of a session or on a holiday.

#### HAT-SCH-087: Universe versions are immutable once published
- **Statement:** A trigger rejects UPDATE of `hat_universe_version.definition` when status ≠ `draft`.
- **Source:** SCH "Portfolio layer" (`hat_universe_version` "Same immutability and one-live-version rules as strategy versions").
- **PR:** 2
- **Verification:** DB `governance.universe_version_immutable`
- **Acceptance:**
  - Property: for every non-draft universe version, an UPDATE of `definition` raises and the stored definition and `config_hash` are unchanged; with the trigger dropped (broken variant) the check fails. Harm: a sleeve's universe changes silently and its backtest no longer describes what it trades.

## §9 Indexes and retention

Indexes (SCH "Indexes", plus the unique keys above):

- `hat_signal (bot_id, trade_date)` · `hat_watchlist_entry (bot_id, trade_date)`
- `hat_order (broker_order_id)` · unique `hat_order (tag)`
- `hat_position (bot_id) where closed_at is null`
- `hat_event (correlation_id)` · `hat_event (aggregate_type, aggregate_id)`
- `hat_announcement (instrument_id, published_at desc)`
- unique `hat_signal (bot_id, playbook, instrument_id, trade_date)` (§5)

Retention (SCH "Retention"):

| Data | Keep |
| --- | --- |
| Events, signals, orders, positions, results | Forever |
| Parquet bars and features | Forever (roughly 10M daily rows since 2010, *estimate*) |
| Raw bhavcopy zips | Forever (they're the evidence) |
| Telegram messages, command log | 1 year |

#### HAT-SCH-090: Indexes exist
- **Statement:** Every index in the §9 list exists, including the partial `hat_position (bot_id) where closed_at is null` on Postgres and SQLite.
- **Source:** SCH "Indexes"; PLAN PR 3.
- **PR:** 3
- **Verification:** CONF `schema.indexes`
- **Acceptance:**
  - Property: the set of HAT indexes reflected from the created schema includes every §9 entry with the stated columns, uniqueness and partial predicate. Harm: reconciliation by tag and open-position lookups degrade to full scans during market hours.

#### HAT-SCH-091: Retention purge touches only Telegram messages and the command log
- **Statement:** A purge job deletes `hat_telegram_message` and `hat_command_log` rows older than 1 year; nothing else is ever purged.
- **Source:** SCH "Retention"; PLAN PR 3 ("Retention purge job for Telegram/command logs (1 year)").
- **PR:** 3
- **Verification:** PBT `retention.purge_scope`
- **Acceptance:**
  - Property: for every database state and run date d, after the purge a row is gone if and only if it is in `hat_telegram_message` or `hat_command_log` and older than 1 year before d; rows in all other tables, Parquet files and raw zips are untouched. Harm: trade history or evidence is deleted, or chat logs are kept beyond the stated period.

## §10 Migration mechanism

The repo has no Alembic: `app/database.py:init_db()` runs `Base.metadata.create_all`, then idempotent `ensure_*_schema()` functions; tests build the schema with `create_all` on SQLite (PLAN §2 "Migrations", §3 "Migrations"; CLAUDE.md).

| Logical type | SQLAlchemy type |
| --- | --- |
| uuid | `sa.Uuid` (uuid7 generated in Python) |
| jsonb | `JSON().with_variant(JSONB, "postgresql")` |
| numeric(18,4) | `Numeric(18, 4)` |
| numeric (ratio) | `Numeric()` |
| timestamptz | `DateTime(timezone=True)` with aware-UTC defaults |
| check | plain `CheckConstraint` accepted by both dialects |
| partial unique | `Index(..., unique=True, postgresql_where=..., sqlite_where=...)` |

#### HAT-SCH-100: HAT models live on the shared Base and create on SQLite
- **Statement:** HAT models live in `app/hat/models/` on the shared `app.base.Base`, use only the portable types in §10, and `create_all` creates them on SQLite and Postgres.
- **Source:** PLAN §3 Migrations item 1, §2 ("any HAT model on the shared `Base` must create cleanly on SQLite"); PLAN C8.
- **PR:** 2
- **Verification:** CONF `schema.portable_types`, CONF `schema.sqlite_create_all`
- **Acceptance:**
  - Property: every column of every HAT model uses a type from the §10 table, and `Base.metadata.create_all` on an empty SQLite database succeeds and creates all 41 tables. Harm: the existing test suite (1510 tests) breaks or Postgres-only types leak into SQLite.

#### HAT-SCH-101: `ensure_hat_schema` adds the Postgres-only DDL idempotently
- **Statement:** `app/hat/schema.py: ensure_hat_schema(bind)` is called from `init_db()` after `create_all`; it is idempotent, does nothing on SQLite, and creates or replaces the trigger functions and triggers (`CREATE OR REPLACE TRIGGER`, Postgres ≥ 14), the interval CHECKs and the event partitions.
- **Source:** PLAN §3 Migrations item 2; C8.
- **PR:** 2, 3
- **Verification:** DB `migration.ensure_idempotent`
- **Acceptance:**
  - Property: running `init_db()` any number n ≥ 1 of times on Postgres yields the same catalog (tables, indexes, constraints, triggers, partitions) as running it once, and on SQLite `ensure_hat_schema` issues no DDL. Harm: a restart fails or duplicates triggers during the deferred-startup retry loop.

#### HAT-SCH-102: Postgres-only rules have postgres-marked checks
- **Statement:** Every rule the database enforces (triggers, partitions, partial indexes, interval checks) has a `@pytest.mark.postgres` check that runs in CI's `backend-postgres` job.
- **Source:** PLAN §3 Migrations item 5; CLAUDE.md (schema changes).
- **PR:** 2, 3
- **Verification:** CONF `verification.db_rules_covered`
- **Acceptance:**
  - Property: every requirement in this file whose Verification lists a `DB` check names a test that exists under `backend/tests/hat/db/`, is marked `postgres`, and fails when its trigger, index or constraint is removed. Harm: a database guarantee silently disappears in a refactor.

#### HAT-SCH-103: Later column additions follow the ensure pattern
- **Statement:** Columns added to existing HAT tables after first release are added by an idempotent `ensure_hat_columns` (`ALTER TABLE … ADD COLUMN IF NOT EXISTS`), like `ensure_backtest_schema`.
- **Source:** PLAN §3 Migrations item 4.
- **PR:** 2
- **Verification:** DB `migration.ensure_idempotent`
- **Acceptance:**
  - Property: on a Postgres database created at any earlier HAT schema version, `init_db()` brings every HAT table to the current column set without data loss, and running it again changes nothing. Harm: an upgrade on the production box fails at startup during market hours.

#### HAT-SCH-104: FK targets exist before their referrers
- **Statement:** A table is created in the same PR as, or after, every table it references (for example `hat_backtest_run`, referenced by `hat_strategy_version.backtest_run_id` and `hat_universe_version.backtest_run_id`).
- **Source:** SCH (DDL of `hat_strategy_version` references `hat_backtest_run`); PLAN §4.2 (strategy group in PR 2, `hat_backtest_run` listed for PR 3).
- **PR:** 2
- **Verification:** CONF `schema.sqlite_create_all`
- **Acceptance:**
  - Property: at every merged PR, `create_all` succeeds with every declared FK resolvable. Harm: PR 2's schema cannot be created, or the FK is silently dropped and promotion evidence is unenforced.
- **[OWNER-PENDING]** Placement of `hat_backtest_run`: default create it in PR 2 together with the strategy tables (deviation from PLAN §4.2, which lists it in PR 3); owner to confirm.

#### HAT-SCH-105: The HAT runtime refuses to start on SQLite
- **Statement:** With `HAT_ENABLED=true`, the HAT runtime refuses to start when the database is SQLite (fail closed); SQLite is only for tests.
- **Source:** PLAN §3 Migrations item 6 ("DB-enforced governance is part of the design").
- **PR:** 2, 17
- **Verification:** CONF `runtime.refuses_sqlite`
- **Acceptance:**
  - Property: for every settings combination with `HAT_ENABLED=true` and a SQLite URL, HAT startup raises before any job or live task starts, and SmartTrader's own startup is unaffected. Harm: HAT trades without the triggers that protect stops, versions and the event log.

#### HAT-SCH-106: HAT migrations never alter SmartTrader tables
- **Statement:** `ensure_hat_schema` and HAT models create and alter only `hat_*` objects.
- **Source:** MA "Boundary rules"; PLAN §4.2.
- **PR:** 2, 3
- **Verification:** CONF `schema.prefix_and_boundary`
- **Acceptance:**
  - Property: every DDL statement issued by `ensure_hat_schema` names only `hat_*` tables, indexes, functions and triggers. Harm: a HAT release breaks the terminal, screener or VCP features.

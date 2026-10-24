# Invariants

The rules the code must keep. If code and this file disagree, one is wrong. The mechanics for
touching orders: `.claude/skills/trading-safety/SKILL.md`.

## Trading (no configuration can change them)

1. A stop is never moved further from price: `current_stop` only rises (`positions.raise_stop`,
   in the backtest too, backed by a Postgres trigger live); a resting broker stop moves down only
   for a split or bonus (`execution/stop_plans.raise_only`).
2. No order adds to a position: never to a loser, and a bot never adds to an open position.
3. No entry without a stop price below the entry price.
4. No entry when the day's data or the live feed is stale, or the instrument's last price is
   unknown, 30 s old or outside the day's price band.
5. No order outside the session in `hat_trading_calendar` (09:15-15:30 IST). A date the calendar
   lacks is no trading day; a special session has only its stored hours, else it is closed.
6. `max_chase` always applies, even if a config deletes the criterion.

## Money safety

- **One path to an order.** `evaluate_signal` judges, `reserve_order` records, `send_order` sends.
  No manual ticket, no second risk or sizing function, no override. `size_position` is called
  from `decide` only.
- **Shadow never reaches the broker.** Confirm and auto need an active account with GTT delivery,
  DDPI confirmed by the owner and policy weights confirmed; otherwise the bot runs shadow. A mode
  change takes effect at the next session open.
- **Orders are idempotent.** Every order has an `HT` tag (signal id + purpose + sequence)
  committed `pending` before the broker is called; an order of unknown outcome is looked up by tag
  before any retry, and an unknown entry is never re-sent. A filled, cancelled, rejected or
  expired order never moves again: a book showing it otherwise is logged and ignored.
- **Exact math.** `Decimal` for prices, notional, P&L and size; floats and non-finite numbers are
  refused. Product CNC, a finite positive price, the stored broker symbol.
- **Risk never raises.** A missing input is a REJECT with a typed reason. Size is
  `floor(min(E*r/(entry-stop), E*p_max/entry, adv_cap*ADV20, cash/entry))`; zero skips.
- **Exits are never blocked** by a cap, kill switch, data failure, reconciliation state or mode.
  Kill switches, pause and a failed gate stop new entries only (manage-only). A held instrument
  that changes series keeps its exits under the new symbol. An account holding an open real
  position gets a desk at start whatever its status, so its fill sync, stops, exits and flatten
  keep running, unless another account's desk holds its broker login (then it is logged); the
  mode gates keep real entries to active accounts. An account of a broker other than the active
  one (`HAT_BROKER`) has no adapter, so if it holds an open real position or a working real order
  the start is refused (`inactive_broker_holds_positions`), never left without a desk.
- **Fail closed on the broker.** A missing client, open circuit or failed read raises
  `BrokerUnavailableError`, never an empty book; a write of unknown outcome is `unconfirmed`. No
  entry on a trade date before its first reconciliation succeeded; a mismatch blocks entries and
  never auto-closes or adopts a position.
- **Every real position has a resting broker stop** (intraday, then GTT overnight), re-verified
  nightly and against the GTT book at 08:40 and each fill sync; a dropped GTT is replaced at once;
  a lost broker answer is settled against the broker's book, never left frozen.
- **Kill switches are HAT's own** (account and bot drawdown, health, manual, pause). Drawdown and
  manual kills re-arm by hand with a second confirmation; a health kill clears itself. A re-arm
  restarts the peak at the equity then. Drawdown equity is capital + realised P&L + open positions
  marked to market (latest fresh trade, official close at night; before the session's first
  trade the previous close; a stale price at the lower of last trade and stop).
- **Approvals.** Tokens are signed, bound to signal and expiry, single-use, stored as hashes;
  expiry 5 minutes; Take re-checks `max_chase`; unknown chats are ignored. Free text never makes
  an order; auto, `/arm` and `/flatten` need a second confirmation.
- **Policy changes.** Live levers only reduce risk; an increase waits 7 days and passes the
  evidence gate; policy and strategy versions are immutable.
- **One owner per resource.** One live socket, one session owner per broker account, one token
  store, one fake broker, one risk authority, one HAT runtime per database.
- **Nothing leaks.** Results carry fixed codes, never broker or database text; secrets are never
  logged, returned or committed.
- **Events.** Every state change and its event commit in one transaction; `hat_event` is
  append-only (trigger); trade state replays from events.
- **Schema.** The database holds exactly the models' tables, columns, types, constraints and
  indexes plus `schema.PG_DDL`; HAT refuses to start on any difference and prints the DDL.
  `create_all` never alters a table: a change ships as idempotent `PG_DDL` or a runbook DDL step.

## Data

- **Gate and point in time.** Scans never run on data that failed the quality gate; features use
  bars up to the scan date only; raw bars (`bars_daily_raw`, `bars_5m`) are immutable. Broker
  minute history is converted to raw with the factors as of its fetch day; a day that does not
  fit the raw daily bar is ledgered (`bars_5m_rejects`), never written.
- **Adjustment.** Adjusted prices are raw times cumulative factors, each action applied once per
  ISIN chain; two ISINs share a history only through a `linked` lineage row. Price rules on
  absolute rupees (`min_price`, the tick slab) read the raw close of their date; ratio and
  indicator rules read adjusted bars. A bonus of preference shares or debentures is
  price-neutral (a dividend when worth at least 1% of the previous close).
- **Stored factors are bar-confirmed.** An imported split, bonus or rights factor is stored only
  when the ex-date open over the previous close is within 20% of it and at least twice as close to
  it as to 1; otherwise a review case (a research rights factor the gate finds ambiguous is
  stored tagged `price_unconfirmed`). Rights adjust by TERP / cum price (1 at or above the cum
  price). No load rewrites a stored key.
- **Demergers** adjust the parent by the market gap (ex-date open / previous close). The CoA % and
  the ratio priced at the resulting company's first close are cross-checks, never the factor (a
  file import's ratio value is stored only when the gap confirms it through the price gate); a
  ratio more than 10% (log) from the applied value is disputed and listed. Without an agreeing
  cross-check the gap is stored only for a confirmed date, a drop of at least 10% and a close five
  sessions on that holds it. The resulting company is never linked into the parent's lineage. The
  NIFTY 500-neutralised gap is applied only with the owner's approval. A position held into a
  demerger whose record date was public (`announced_on`) strictly before the last session before
  the ex-date exits at that session's close (exit reason `demerger`), in backtest and live; a
  real position is only alerted, never sold by HAT. Undated or late, it holds through.
- **Dividends.** Every cash dividend counts, none guessed: one without an amount is alerted and
  listed until one is stored. Signals read the total-return series (`bars_daily`); fills, marks,
  ticks, bands, the rescale and orders read the price basis (`bars_daily_cap`). A closed trade's
  P&L and R include the dividends its shares earned (ex-date in the holding, shares held at the
  ex-date's open), in backtest, shadow and live alike; the nightly `ca.adjust` restates them from
  the stored amounts.
- **Versions and `known_on`.** Every write of a corporate action appends a version in the same
  transaction (`hat_corporate_action_version`, append-only); `hat_corporate_action` is the
  hindsight projection. Each action records when its value became knowable (`known_on`; NULL when
  unknown, never guessed). A backtest neither scans nor enters a name on a session where no
  version was available or the available one differs from the series' (`factor_pending`); a
  version available later never changes what a backtest reads before it.
- **Ranked universe.** `rs_pct`, `mom_rank` and breadth rank only ordinary equity (ISIN `IN` + `E`
  or `9` + issuer + type `01`, read from the bar's own ISIN); ETFs, funds, rights entitlements,
  InvITs and REITs get features but no rank.
- **Owner corrections.** A stored factor, amount or `known_on` changes only through the owner's
  `correct-action` / `correct-actions` (journalled, every version kept) or a lineage `link` /
  `unlink`, which holds across rebuilds. No job applies a correction list.

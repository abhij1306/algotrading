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
   unknown, 30 s old or outside the day's price band. An exit view may act on an older last
   trade of the day only while the feed itself is fresh (an illiquid name), never an entry.
5. No order outside the session in `hat_trading_calendar` (09:15-15:30 IST). A date the calendar
   lacks is no trading day; a special session has only its stored hours, else it is closed.
   Continuous trading ends 15 minutes before the stored close (15:15 IST; NSE's closing auction
   takes the rest). The close phase is set against that end: `exits.close` (real and shadow) 10
   to 5 minutes before it, the GTT swap 5 minutes before it, the trigger window ends 10 minutes
   before it, and no sale goes out from it on; the session end is set against the stored close.
   The open phase (`live.prepare`, `modes.apply`, the gap scan, the regime gate, the pre-open
   capture, the bar grid, the trigger window's start) is set against the stored open, so both
   follow an early close or a Muhurat session. A close-phase decision (MA exits,
   `close_above_sma`, failed-break closes, time stops) uses the live last trade or the last
   finished 5-minute bar at decision time, never the official close: that is the closing
   auction's, settled around 15:40. What needs the official close (the nightly ingest, the
   recorded gap between the exit price and the official close, features) runs after 15:40. The
   backtest's close-phase rules decide and fill on the daily close, which stands in for that live
   price (`close_rules_decided_on_official_close`).
6. `max_chase` always applies, even if a config deletes the criterion.
7. No entry is placed, and no Take passes, once its break failed: a final 5-minute bar after the
   trigger bar closed below the level. An unfilled entry whose break failed is cancelled, and its
   `order.cancelled` carries the reason.

## Money safety

- **One path to an order.** `evaluate_signal` judges, `reserve_order` records, `send_order` sends.
  No manual ticket, no second risk or sizing function, no override. `size_position` is called
  from `decide` only.
- **Shadow never reaches the broker.** Confirm and auto need an active account with GTT delivery,
  DDPI confirmed by the owner and policy weights confirmed; otherwise the bot runs shadow. A mode
  change takes effect at the next session open; confirm or auto is refused for a sleeve that is
  not Live (`sleeve_not_live`), at the request and at the open.
- **Orders are idempotent.** Every order has an `HT` tag (signal id + purpose + sequence)
  committed `pending` before the broker is called; an order of unknown outcome is looked up by tag
  before any retry, and an unknown entry is never re-sent. A filled, cancelled, rejected or
  expired order never moves again: a book showing it otherwise is logged and ignored.
- **A broker fill is a fact.** Every fill is recorded, never refused by the lifecycle: a late
  entry fill on an ended trade opens or reopens its position so the stop covers the shares, and
  raises URGENT `fill.off_lifecycle`.
- **Exact math.** `Decimal` for prices, notional, P&L and size; floats and non-finite numbers are
  refused. Product CNC, a finite positive price, the stored broker symbol.
- **Risk never raises.** A missing input is a REJECT with a typed reason. Size is
  `floor(min(E*r/(entry-stop), E*p_max/entry, adv_cap*ADV20, cash/entry))`; zero skips. The
  caps count a position's unfilled entry remainder while that entry still works.
- **Exits are never blocked** by a cap, kill switch, data failure, reconciliation state or mode.
  Kill switches, pause and a failed gate stop new entries only (manage-only); a drawdown or
  manual kill or a pause also cancels the bot's entries still working at the broker, and a
  placement re-reads the mode gates, so an entry routed before a gate closed is refused. A held
  instrument that changes series keeps its exits under the new symbol. An account holding an open real
  position gets a desk at start whatever its status, so its fill sync, stops, exits and flatten
  keep running, unless another account's desk holds its broker login (then it is logged); the
  mode gates keep real entries to active accounts. An account of a broker other than the active
  one (`HAT_BROKER`) has no adapter, so if it holds an open real position or a working real order
  the start is refused (`inactive_broker_holds_positions`), never left without a desk.
- **Fail closed on the broker.** A missing client, open circuit or failed read raises
  `BrokerUnavailableError`, never an empty book; a write of unknown outcome is `unconfirmed`. No
  entry on a trade date before its first reconciliation succeeded, and the live conditions and
  price are checked again after it, right before the send; a mismatch blocks entries and never
  auto-closes or adopts a position. Once the broker calls of the day are spent (the budget's day
  cap, or Fyers' `-360`), no entry goes out and the reads that serve only entries (the
  reconciliation, the entry funds read) stop until the next trade date; stops and sales still
  go out on the writes' share.
- **Every real position has a resting broker stop** (intraday, then GTT overnight), re-verified
  nightly and against the GTT book at 08:40 and each fill sync; a dropped GTT is replaced at once;
  a lost broker answer is settled against the broker's book, never left frozen. A lost sale of
  an earlier session is settled from the holdings net of the owner's own shares (the holding
  baseline a reconciliation recorded before the sale); with no baseline the position sells
  nothing until the owner books that sale. A lost entry of an earlier session is never rejected
  on today's book: it ends only when the holdings show no shares beyond HAT's other open
  positions of the symbol; otherwise it stays unsure and the owner is told URGENT
  (`entry.unresolved`).
- **Corporate-action locks.** A position whose prices may be on the wrong scale gets no exit
  evaluation and its instrument no entry; its broker stop rests and `/flatten` still sells it:
  a stored split or bonus not yet rescaled, an action the rescale cannot handle, and a held name
  whose live price is at or below 0.6 of the previous raw close with no action stored since
  (URGENT `position.split_gap`, until the owner stores the action or resolves it), and a position
  held across an ISIN change (a `linked` lineage row from its instrument, effective after it
  opened: URGENT `position.isin_changed`, until the owner resolves it). No instrument of the
  lineage chain of a locked position takes an entry. A real position is rescaled only once its
  account's fills were read after the previous session's close; until then (at the latest the
  09:00 deadline, then URGENT `job.failed`) it stays locked.
- **Kill switches are HAT's own** (account and bot drawdown, health, manual, pause). Drawdown and
  manual kills re-arm by hand with a second confirmation; a health kill clears itself. A re-arm
  restarts the peak at the equity then, the drawdown kill's and, for a bot, the health
  multiplier's (QA10). Drawdown equity is capital (weight x the broker funds
  total, which carries the realised P&L) + the realised P&L of trades closed since that funds
  read + open positions marked to market (latest fresh trade, official close at night; before the session's first
  trade the previous close; a stale price at the lower of last trade and stop).
- **Approvals.** Tokens are signed, bound to signal and expiry, single-use, stored as hashes;
  expiry 5 minutes; Take re-checks `max_chase`; unknown chats are ignored. Free text never makes
  an order; auto, `/arm` and `/flatten` need a second confirmation. A confirmed flatten trips the
  account kill before it sells. An entry fill landing after a flatten sold its position is sold
  under that same confirmation.
- **Policy changes.** Live levers only reduce risk; an increase waits 7 days and passes the
  evidence gate; a decrease withdraws every pending version, so a later decrease is never
  overridden; policy and strategy versions are immutable. The gate counts the live trades
  closed since the version in force took effect, so any change, a decrease too, restarts that
  window; every sleeve's evidence run must pass the lifecycle's span, gap and ambiguity
  conditions (QA8), and the gate reads only the runs and trades that existed when it was asked.
- **One owner per resource.** One live socket, one session owner per broker account, one token
  store, one fake broker, one risk authority, one HAT runtime per database. A runtime whose
  lock another runtime took writes no order (no entry, stop or sale) until it is restarted.
- **Nothing leaks.** Results carry fixed codes, never broker or database text; secrets are never
  logged, returned or committed.
- **Events.** Every state change and its event commit in one transaction; `hat_event` is
  append-only (triggers refuse UPDATE, DELETE and TRUNCATE, on every partition too); trade state
  replays from events.
- **Schema.** The database holds exactly the models' tables, columns, types, constraints and
  indexes plus `schema.PG_DDL`; HAT refuses to start on any difference and prints the DDL.
  `create_all` never alters a table: a change ships as idempotent `PG_DDL` or a runbook DDL step.

## Data

- **Gate and point in time.** Scans never run on data that failed the quality gate; features use
  bars up to the scan date only; raw bars (`bars_daily_raw`, `bars_5m`) are immutable. Broker
  minute history is converted to raw with the factors as of its fetch day; a day that does not
  fit the raw daily bar is ledgered (`bars_5m_rejects`), never written.
- **Adjustment.** Adjusted prices are raw times cumulative factors, each action applied once per
  ISIN chain; two ISINs share a history only through a `linked` lineage row, and a pair that
  gains or loses that link is journalled (`lineage.changed`). Price rules on
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
  real position is only alerted, never sold by HAT. Undated or late, it holds through. A name
  with a demerger notice takes no entry from that session until the demerger's factor is stored
  (`demerger_pending`; the backtest skips that session, and `factor_pending` covers the rest). A
  stored notice is never retired by a load: one the night's `bc` file stops listing is alerted.
- **Dividends.** Every cash dividend counts, none guessed: one without an amount is alerted and
  listed until one is stored. Signals read the total-return series (`bars_daily`); fills, marks,
  ticks, bands, the rescale and orders read the price basis (`bars_daily_cap`). A closed trade's
  P&L and R include the dividends its shares earned (ex-date in the holding, shares held at the
  ex-date's open), in backtest, shadow and live alike; the nightly `ca.adjust` restates them from
  the stored amounts, each change with a `position.dividends_restated` event of the figures
  before and after.
- **Versions and `known_on`.** Every write of a corporate action appends a version in the same
  transaction (`hat_corporate_action_version`, append-only); `hat_corporate_action` is the
  hindsight projection, checked every night by `ca.adjust` (alert `corporate_action.projection`). Each action records when its value became knowable (`known_on`; NULL when
  unknown, never guessed). A backtest neither scans nor enters a name on a session where no
  version was available or the available one differs from the series' (`factor_pending`); a
  version available later never changes what a backtest reads before it. A correction that
  re-dates `known_on` later appends a version available from the later date and withdraws
  nothing: the earlier version stays what is read before then (accepted, QA1).
- **Ranked universe.** `rs_pct`, `mom_rank` and breadth rank only ordinary equity (ISIN `IN` + `E`
  or `9` + issuer + type `01`, read from the bar's own ISIN); ETFs, funds, rights entitlements,
  InvITs and REITs get features but no rank.
- **Owner corrections.** A stored factor, amount or `known_on` changes only through the owner's
  `correct-action` / `correct-actions` (journalled, every version kept) or a lineage `link` /
  `unlink`, which holds across rebuilds. No job applies a correction list.

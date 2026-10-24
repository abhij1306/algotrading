# Invariants

The money-safety and trading rules the code must keep. Code is the source of truth; if code and
this file disagree, one of them is wrong and a check should say which. The mechanics for touching
orders are in `.claude/skills/trading-safety/SKILL.md`.

## Six trading invariants (no configuration can change them)

1. A stop is never moved further from price (`current_stop` only rises: `positions.raise_stop`
   refuses any other move, in the backtest too, and the Postgres trigger backs it in live; a
   resting broker stop moves only where `execution/stop_plans.raise_only` allows, down only for a
   split or bonus).
2. No order adds to a losing position, and a bot never adds to an open position.
3. No entry without a stop price below the entry price.
4. No entry when the day's data or the live feed is stale, or the instrument's own last price is
   unknown, 30 s old or outside the day's price band.
5. No order outside the exchange session from `hat_trading_calendar` (09:15-15:30 IST); a date the
   calendar does not hold is no trading day for any reader. A special session (a weekend one, or
   one the recorded file names) has only the hours stored for it, never the default: without
   them it is UNKNOWN, closed for orders.
6. `max_chase` always applies, even if a config deletes the criterion.

## Money safety

- **One path to an order.** A signal is judged by `evaluate_signal`, recorded by `reserve_order`,
  sent by `send_order`. No manual ticket, no second risk or sizing function, no override, bypass
  or force switch. `size_position` is called from `decide` only.
- **Shadow never reaches the broker.** Confirm and auto need: account active, adapter reports GTT
  delivery, DDPI confirmed by the owner, policy weights confirmed. Otherwise the bot runs shadow.
  A mode change takes effect at the next session open, never inside market hours.
- **Orders are idempotent.** Every order has an `HT` tag (signal id + purpose + sequence), committed
  as `pending` before the broker is called. An order of unknown outcome is looked up by tag before
  any retry; an unknown entry is never re-sent. Fyers does not enforce tag uniqueness, so HAT does.
- **Exact payloads and exact math.** `Decimal` for prices, notional, P&L and size; floats and
  non-finite numbers are refused. Product is CNC; a finite positive price; the stored broker symbol.
- **Risk never raises.** A missing or unreadable input is a REJECT with a typed reason; no fallback
  to a hard-coded price. Size is `floor(min(E*r/(entry-stop), E*p_max/entry, adv_cap*ADV20, cash/entry))`;
  zero skips the trade.
- **Exits are never blocked** by a cap, kill switch, data failure, reconciliation state or mode.
  Kill switches, pause and a failed quality gate stop new entries only (manage-only). A held
  instrument that changes series (EQ to BE) keeps its exits under the new symbol.
- **Fail closed on the broker.** A missing client, open circuit or failed read raises
  `BrokerUnavailableError`, never an empty book. A write with unknown outcome is `unconfirmed`.
  No entry on a trade date before that date's first reconciliation succeeded; a mismatch blocks
  entries and never auto-closes or auto-adopts a position.
- **Every real position has a resting broker stop** (intraday stop, then GTT overnight), re-verified
  nightly; the GTT book is read at 08:40 and every few minutes by the fill sync, and a GTT the
  broker dropped is ended and replaced at once (an unreadable book changes nothing); a lost broker answer never freezes a position and is settled against the broker's book.
- **Kill switches are HAT's own** (account and bot drawdown, health, manual, pause). A drawdown or
  manual kill re-arms by hand with a second confirmation; a health kill clears itself. A re-arm
  restarts the peak: the scope is measured from its equity at the first point after the re-arm
  (D232). Drawdown
  equity is capital + realised P&L + open positions marked to market (the latest fresh trade in
  the session, the official close at night); before its first trade of the session a holding is
  marked at its previous official close, and a stale price (or no trade and no close) at the lower
  of its last trade and its stop, never higher.
- **Approvals.** Tokens are signed, bound to signal and expiry, single-use, stored only as hashes;
  approval expires after 5 minutes; Take re-checks `max_chase`; unknown chats are ignored and logged.
  Free text never produces an order; auto, `/arm` and `/flatten` need a second confirmation.
- **Policy changes.** Live levers only reduce risk; an increase waits 7 days and passes the
  evidence gate; decreases apply next session; policy and strategy versions are immutable.
- **One owner per resource.** One live socket, one session owner per broker account, one token
  store, one fake broker, one risk authority. HAT code imports no legacy or manual-trading module.
- **Nothing leaks.** Results carry fixed codes, never broker or database text; tokens and secrets
  are never logged or returned; no secret value in the repo.
- **Events.** Every state change and its event commit in one transaction; `hat_event` is
  append-only (trigger); trade state replays from events.
- **Data.** Scans never run on data that failed the gate; features use bars up to the scan date
  only; raw bars are immutable; adjusted prices are raw times cumulative factors, each action
  applied once per ISIN chain, and two ISINs share a history only through a `linked` lineage
  row. A rule on an absolute rupee price (`min_price`, the NSE tick slab) reads the raw close of
  its date, never an adjusted one, in the scan and the backtest alike; ratio and indicator rules
  read adjusted bars. A dividend is in the signals' total-return series once per chain and never
  in the price basis that fills, marks and orders read; a backtest pays its cash once, on the
  ex-date, to the shares held into it. `bars_5m` is raw too: broker history is converted with the stored factors as of its fetch
  day, and a day that does not fit the raw daily bar is ledgered (`bars_5m_rejects`), never
  written.

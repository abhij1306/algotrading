"""A rotation strategy's live book against the database (`strategy.config.Rotation`), on paper.

The rules are `engine/rotation.py`'s, the ones the backtest's book (`backtest/rotation.py`)
calls. Each step works in the caller's open transaction, never commits and takes the bot's lock
first.

- **The night** (`plan_night`, from the nightly scan): on a rebalance close
  (`rotation.is_rebalance` of the night and the next session) the members of the next session
  passing the universe stage that night are scored on their total-return closes and ranked
  (`rotation.ranking`); the ranking and the regime gate are recorded as `rotation.planned` for
  the next session, once per (bot, trade date).
- **The open** (`rebalance`, the `rotation.rebalance` job): the target list is selected from that
  ranking with the hold buffer (`rotation.targets`), a name the live price says cannot be bought
  now passed over as the backtest passes over one it cannot buy at the open; the order diff to an
  equal share of the sleeve's equity (`rotation.orders`) is carried out sales and trims first,
  then buys and top-ups scaled down together to the free cash (`rotation.buy_share`, then each
  cut to what the cash left pays, as the backtest's `_buy_all`). A sale or trim is a virtual sale
  (`shadow.sell_position`) of the holding's lots, newest first (`rotation.lot_sales`); a buy or
  top-up is a signal through `entries.open_entry` (the universe check, then `evaluate_signal`
  with `ROTATION_BUY`: the kills, a pause, the market, feed and quality checks, the free cash)
  in shadow, its virtual position a lot of the holding. A sale with no usable price waits for
  the next session's job (`pending_sales`). The outcome is recorded as `rotation.rebalanced`.

Its prices are the live last trade at 09:20 or after, the definition the backtest and the
replay share (`engine/fill_0920.py`: the last trade before 09:20, the 09:15 bar's close).

Paper only: real rotation orders are not built (no stop for the broker's protective order, no
sale card), so the caller routes shadow whatever mode the bot asks for and says so. A rotation
holding takes no slot or open risk (`snapshot._NOT_ROTATION`) and has no exit stack (its config
has none); a demerger exit sells it as any virtual position.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..configs.records import Bot, StrategyVersion
from ..configs.unit import configs_of
from ..costs import DEFAULT_COST_MODEL, DEFAULT_SLIPPAGE_BPS, BacktestFills, load_cost_schedule
from ..data.live import PRICE_STALE, PRICE_UNKNOWN
from ..errors import HatError
from ..events import record, recorded_for
from ..governance import Mode
from ..models import HatEvent, HatPosition, HatSignal
from ..money import require_exact
from ..risk.decision import ROTATION_BUY, SignalContext
from . import rotation
from .botlock import lock_bot
from .confidence import Confidence
from .entries import EntryRequest, open_entry
from .positions import TradeError
from .regime_gate import regime_allows_longs
from .scan import ScanInputs, universe_passers
from .shadow import sell_position
from .strategy.config import Rotation, StrategyConfig, parse_config
from .watchlist import bot_universe_members

__all__ = [
    "EVENT_PLANNED",
    "EVENT_REBALANCED",
    "FILLS",
    "Planned",
    "Lot",
    "Rebalanced",
    "RotationUnpricedError",
    "closes_window",
    "plan_night",
    "planned",
    "planned_instruments",
    "pending_sales",
    "night_ranking",
    "priced_targets",
    "rebalance",
    "rebalance_orders",
    "rebalanced_on",
    "record_rebalanced",
    "rotation_config",
    "waits_for_prices",
    "waiting_sales",
]

logger = logging.getLogger(__name__)

EVENT_PLANNED = "rotation.planned"
EVENT_REBALANCED = "rotation.rebalanced"
# A rotation signal's confidence: it has no setup score or evidence bucket.
ROTATION_CONFIDENCE = "rotation_unscored"
NO_PRICE = "no_price"
NO_EQUITY = "no_equity"
VERSION_CHANGED = "version_changed"  # the bot's live version is not the one the night ranked
CASH = "cash"  # a buy the cash cut down or out (the backtest's reason)
# The fills of shadow trading (`shadow.SHADOW_FILLS`'s model), typed for the outlay and the
# affordable shares the buys are scaled with.
FILLS = BacktestFills(load_cost_schedule(DEFAULT_COST_MODEL), DEFAULT_SLIPPAGE_BPS)
_SCORE = Decimal("0.000001")
# An instrument's usable live price, or None and the refusal's code (`LivePrices.checked`).
Price = Callable[[str], tuple[Decimal | None, str | None]]
# The refusals a rebalance waits out (a tick will come) rather than pass the name over.
WAIT_FOR_PRICE = frozenset({PRICE_UNKNOWN, PRICE_STALE})


def rotation_config(session: Session, bot: Bot) -> StrategyConfig | None:
    """The bot's live config when it is a rotation strategy, else None."""
    if bot.live_version_id is None:
        return None
    version = configs_of(session).get(StrategyVersion, bot.live_version_id)
    if version is None:
        return None
    config = parse_config(version.config)
    return config if config.rotation is not None else None


# --- the night ----------------------------------------------------------------------------------


def closes_window(bars: pl.DataFrame, names: Sequence[str], night: date, rows: int) -> np.ndarray:
    """The total-return closes of `names` (columns) over the last `rows` sessions of `bars`
    through `night` (the sessions any bar is stored on), NaN where a name has no bar: the
    window `rotation.scores` reads. `bars` holds `instrument_id`, `date` and `close`, the
    total-return series (`bars_daily`)."""
    out = np.full((rows, len(names)), np.nan)
    if not names or bars.height == 0:
        return out
    upto = bars.filter(pl.col("date") <= night)
    sessions = sorted(set(upto["date"].to_list()))[-rows:]
    first = rows - len(sessions)  # a short history leaves the oldest rows empty
    row_of = {day: first + n for n, day in enumerate(sessions)}
    column_of = {name: n for n, name in enumerate(names)}
    frame = upto.filter(pl.col("date") >= sessions[0]) if sessions else upto.clear()
    for name, day, close in zip(
        frame["instrument_id"].cast(pl.String).to_list(),
        frame["date"].to_list(),
        frame["close"].cast(pl.Float64).to_list(),
        strict=True,
    ):
        column = column_of.get(name)
        if column is not None and close is not None and close > 0:
            out[row_of[day], column] = close
    return out


def _planned_event(session: Session, bot_id: uuid.UUID, trade_date: date) -> HatEvent | None:
    for event in session.scalars(
        select(HatEvent).where(
            HatEvent.bot_id == bot_id,
            HatEvent.event_type == EVENT_PLANNED,
            recorded_for(trade_date),
        )
    ):
        if event.payload.get("trade_date") == trade_date.isoformat():
            return event
    return None


def plan_night(
    session: Session, bot_id: uuid.UUID, inputs: ScanInputs, trade_date: date, now: datetime
) -> bool:
    """Rank a rotation bot's universe on the close of `inputs.as_of` for the rebalance at
    `trade_date`'s open and record it (`rotation.planned`). False when the bot is not a rotation,
    the night closes no rebalance period, it has no universe or the plan is already recorded."""
    lock_bot(session, bot_id)
    bot = configs_of(session).get(Bot, bot_id)
    config = None if bot is None else rotation_config(session, bot)
    if bot is None or config is None or config.rotation is None:
        return False
    rules: Rotation = config.rotation
    night = inputs.as_of
    if not rotation.is_rebalance(night, trade_date, rules.rebalance):
        return False
    if _planned_event(session, bot_id, trade_date) is not None:
        return False
    night_rank = night_ranking(session, bot, config, inputs, trade_date)
    if night_rank is None:
        return False
    passers, ranks, gate_on = night_rank
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type=EVENT_PLANNED,
        actor="engine",
        payload={
            "trade_date": trade_date.isoformat(),
            "night": night.isoformat(),
            "gate_on": gate_on,
            "strategy_version_id": str(bot.live_version_id),
            "passers": len(passers),
            # how deep the open's selection may reach: its subscriptions (`planned_instruments`)
            "depth": rules.top_n + rules.buffer_rank,
            "ranking": [[name, str(_score(value))] for name, value in ranks],
        },
        bot_id=bot_id,
        occurred_at=now,
    )
    session.flush()
    return True


def night_ranking(
    session: Session, bot: Bot, config: StrategyConfig, inputs: ScanInputs, trade_date: date
) -> tuple[list[str], tuple[tuple[str, float], ...], bool] | None:
    """(the passers, their ranking, the regime gate) of a rotation `config` on the close of
    `inputs.as_of` for the rebalance at `trade_date`: the members of `trade_date` (as the
    backtest's rotation reads them) passing the universe stage that night, scored on their
    total-return closes (`inputs.bars`). None without a universe or a member."""
    rules = config.rotation
    if rules is None:
        return None
    members = bot_universe_members(session, bot, trade_date)
    if not members:
        return None
    passers = universe_passers(config, replace(inputs, members={str(m) for m in members}))
    closes = closes_window(inputs.bars, passers, inputs.as_of, rotation.window_rows(rules))
    ranks = rotation.ranking(passers, closes, rules)
    gate_on = regime_allows_longs(config.regime, inputs.regime_closes, inputs.breadth)
    return passers, ranks, gate_on


def _score(value: float) -> Decimal:
    return require_exact(value, "score", floats=True).quantize(_SCORE)


@dataclass(frozen=True)
class Planned:
    """A recorded night: its ranking (best first, scores as stored) and the regime gate."""

    night: date
    trade_date: date
    gate_on: bool
    ranking: tuple[tuple[str, Decimal], ...]
    strategy_version_id: str | None = None


def planned(session: Session, bot_id: uuid.UUID, trade_date: date) -> Planned | None:
    """The night recorded for the rebalance at `trade_date`'s open, if any."""
    event = _planned_event(session, bot_id, trade_date)
    if event is None:
        return None
    payload = event.payload
    return Planned(
        night=date.fromisoformat(payload["night"]),
        trade_date=trade_date,
        gate_on=bool(payload["gate_on"]),
        ranking=tuple((str(name), Decimal(score)) for name, score in payload["ranking"]),
        strategy_version_id=payload.get("strategy_version_id"),
    )


def planned_instruments(session: Session, trade_date: date, depth: int) -> set[uuid.UUID]:
    """The instruments the rebalances at `trade_date`'s open may buy: the first names of each
    recorded ranking, as deep as the plan says its selection may reach (its `top_n` and
    `buffer_rank`) and at least `depth` (subscribed and priced from the day's start)."""
    found: set[uuid.UUID] = set()
    for (payload,) in session.execute(
        select(HatEvent.payload).where(
            HatEvent.event_type == EVENT_PLANNED, recorded_for(trade_date)
        )
    ):
        if payload.get("trade_date") == trade_date.isoformat():
            reach = max(depth, int(payload.get("depth") or 0))
            found.update(uuid.UUID(name) for name, _ in payload["ranking"][:reach])
    return found


# --- the open -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Lot:
    """One position of a rotation holding (a first buy or a top-up)."""

    position_id: uuid.UUID
    qty: int
    entry: Decimal


class RotationUnpricedError(HatError):
    """A rebalance found a name it may buy without a live price yet (no tick, or a stale one):
    it trades nothing rather than pass over that name, and the job tries again."""

    def __init__(self, names: Sequence[str]) -> None:
        super().__init__("rotation_unpriced")
        self.names = tuple(names)


@dataclass
class Rebalanced:
    """What a rebalance did: one row per order planned (traded or not) and per target held
    unchanged; `mode` the mode the bot asked for when it was not shadow (paper only)."""

    trade_date: date
    rows: list[dict[str, Any]] = field(default_factory=list)
    skipped: str | None = None
    # the holdings whose sale found no price, with the reason they are sold: the next job's
    waiting: dict[str, str] = field(default_factory=dict)
    target: tuple[str, ...] = ()  # the plan's target list (a waiting sale of one is off)
    unpriced: list[str] = field(default_factory=list)  # passed over after the wait for prices
    refused: dict[str, str] = field(default_factory=dict)  # buys not made, and why

    def absorb(self, rebalanced: Rebalanced) -> None:
        """Take in the rebalance that ran after this job's waiting sales: its rows, sales left
        waiting, refusals, target and skip. A waiting sale of a name back in the target is off,
        as the backtest's (`RotationBook.rebalance`): the holding stays."""
        self.rows.extend(rebalanced.rows)
        self.waiting.update(rebalanced.waiting)
        self.refused.update(rebalanced.refused)
        self.target = rebalanced.target
        self.skipped = rebalanced.skipped
        self.unpriced = list(rebalanced.unpriced)
        for name in self.target:
            self.waiting.pop(name, None)

    def row(
        self, name: str, action: str, reason: str | None, qty: int, price: Decimal | None
    ) -> None:
        self.rows.append(
            {
                "instrument_id": name,
                "action": action,
                "reason": reason,
                "qty": qty,
                "price": None if price is None else str(price),
            }
        )


def _lots(session: Session, bot_id: uuid.UUID) -> dict[str, list[Lot]]:
    """The bot's open rotation holdings: per instrument (text id), its lots (position id, open
    shares, entry price), oldest first. Only the shadow book's (a veto trade is not the bot's)."""
    rows = session.execute(
        select(
            HatPosition.instrument_id, HatPosition.id, HatPosition.qty_open, HatPosition.avg_entry
        )
        .join(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(
            HatPosition.bot_id == bot_id,
            HatPosition.closed_at.is_(None),
            HatPosition.is_virtual.is_(True),
            HatSignal.mode == Mode.SHADOW,
            HatSignal.playbook == rotation.PLAYBOOK,
        )
        .order_by(HatPosition.opened_at, HatPosition.id)
    ).all()
    out: dict[str, list[Lot]] = {}
    for instrument_id, position_id, qty, entry in rows:
        if qty > 0:
            out.setdefault(str(instrument_id), []).append(
                Lot(position_id, int(qty), Decimal(entry))
            )
    return out


def _sell(
    session: Session,
    lots: Sequence[Lot],
    qty: int,
    price: Decimal,
    reason: str,
    now: datetime,
) -> int:
    """Sell `qty` shares of a holding over its lots, newest first; the shares sold."""
    sold = 0
    for position_id, take in rotation.lot_sales(qty, [(lot.position_id, lot.qty) for lot in lots]):
        if sell_position(session, position_id, take, price, reason, now):
            sold += take
    return sold


def pending_sales(
    session: Session,
    bot_id: uuid.UUID,
    waiting: Mapping[str, str],
    price: Price,
    now: datetime,
    done: Rebalanced,
) -> None:
    """Sell the holdings in `waiting` (an earlier job's sales that found no price, with their
    reasons) where a price is usable now; one still without a price waits again."""
    lock_bot(session, bot_id)
    lots = _lots(session, bot_id)
    for name, reason in waiting.items():
        held = lots.get(name)
        if not held:
            continue
        found, _ = price(name)
        if found is None:
            done.row(name, rotation.SELL, NO_PRICE, 0, None)
            done.waiting[name] = reason
            continue
        sold = _sell(session, held, sum(lot.qty for lot in held), found, reason, now)
        done.row(name, rotation.SELL, reason, sold, found)


def rebalance(
    session: Session,
    bot_id: uuid.UUID,
    night: Planned,
    *,
    price: Price,
    context: Callable[[], SignalContext],
    now: datetime,
    wait_until: datetime,
) -> Rebalanced:
    """Trade the rebalance `night` decided at the open of its trade date (module docstring),
    on paper. `price` is an instrument's (text id) usable live price, or None and the refusal's
    code (`data.live`); `context` the bot's entry context read now (the sleeve's equity at cost
    and its free cash among it), read again after the sales.

    The capital is the book's equity on the live prices (`rotation.book_equity`, the backtest's
    basis: cash plus the holdings marked). Until `wait_until` a name the selection reaches with
    no tick or a stale one, or a stale feed, raises `RotationUnpricedError` before anything is
    written, so the job tries again rather than buy a lower-ranked name in its place
    (`waits_for_prices`); from then on such a name is passed over as the backtest passes over a
    name with no bar, the rest trades and the names are kept in `unpriced`. A name outside the
    day's band cannot be bought now and is passed over, as the backtest passes over one locked
    at its upper band."""
    lock_bot(session, bot_id)
    done = Rebalanced(night.trade_date)
    bot = configs_of(session).get(Bot, bot_id)
    config = None if bot is None else rotation_config(session, bot)
    if bot is None or config is None or config.rotation is None:
        done.skipped = "not_a_rotation"
        return done
    if night.strategy_version_id != str(bot.live_version_id):
        # ranked under another version's rules: not traded on this one's
        done.skipped = VERSION_CHANGED
        return done
    rules = config.rotation
    lots = _lots(session, bot_id)
    held = {name: sum(lot.qty for lot in parts) for name, parts in lots.items()}
    ranking = tuple((name, float(score)) for name, score in night.ranking)
    target, filtered, quote, unpriced = priced_targets(
        ranking, held, rules, gate_on=night.gate_on, price=price
    )
    first = context()
    missing = ["feed", *unpriced] if first.feed_stale is True else list(unpriced)
    if waits_for_prices(missing, now, wait_until):
        raise RotationUnpricedError(missing)
    done.unpriced = missing  # passed over: the backtest's name with no bar
    done.target = target
    chosen = rotation.Plan(night.night, ranking, target, filtered=filtered)
    prices = {n: p for n in {*target, *held} if (p := quote(n)) is not None}
    at_cost = first.equity
    if at_cost is None or at_cost <= 0:
        done.skipped = NO_EQUITY  # no allocation for the night: nothing trades
        return done
    marked = {name: [(lot.qty, lot.entry) for lot in parts] for name, parts in lots.items()}
    _, orders = rebalance_orders(chosen, marked, prices, at_cost, rules)
    buys: list[rotation.Order] = []
    for order in orders:
        name = order.instrument_id
        if order.action not in (rotation.SELL, rotation.TRIM):
            buys.append(order)
            continue
        found = prices.get(name)
        reason = (
            rotation.sale_reason(chosen, name)
            if order.action == rotation.SELL
            else rotation.TRIMMED
        )
        if found is None:
            done.row(name, order.action, NO_PRICE, 0, None)
            if order.action == rotation.SELL:  # a trim is not carried over: the next plan's
                done.waiting[name] = reason
            continue
        sold = _sell(session, lots.get(name, []), order.qty, found, reason, now)
        done.row(name, order.action, reason, sold, found)
    traded = {o.instrument_id for o in orders}
    for name in target:
        if name in held and name not in traded:
            done.row(name, "hold", None, 0, prices.get(name))
    _buy_all(session, bot_id, night.trade_date, buys, prices, context(), now, done)
    return done


def waits_for_prices(unpriced: Sequence[str], now: datetime, wait_until: datetime) -> bool:
    """Whether a rebalance holds back for `unpriced` (the names its selection reached with no
    tick or a stale one): only before `wait_until`, the cutoff a tick of a trading name comes
    by. After it a name still unpriced is taken as not trading (halted, illiquid), passed over
    as the backtest passes over a name with no bar, and the rest trades."""
    return bool(unpriced) and now < wait_until


def priced_targets(
    ranking: Sequence[tuple[str, float]],
    held: Mapping[str, int],
    rules: Rotation,
    *,
    gate_on: bool,
    price: Price,
) -> tuple[tuple[str, ...], bool, Callable[[str], Decimal | None], list[str]]:
    """(the target list, whether the market filter emptied it, the price of a name, the names
    the selection reached that have no tick yet or a stale one): `rotation.targets` with a
    name buyable when its live price is usable, each price read once. A name outside the day's
    band is passed over; one in `unpriced` was too, and the caller must not trade on the list."""
    quotes: dict[str, tuple[Decimal | None, str | None]] = {}
    unpriced: list[str] = []

    def quote(name: str) -> Decimal | None:
        if name not in quotes:
            quotes[name] = price(name)
        return quotes[name][0]

    def can_buy(name: str) -> bool:
        found = quote(name)
        if found is None and quotes[name][1] in WAIT_FOR_PRICE and name not in unpriced:
            unpriced.append(name)
        return found is not None

    target, filtered = rotation.targets(ranking, held, rules, gate_on=gate_on, can_buy=can_buy)
    return target, filtered, quote, unpriced


def rebalance_orders(
    chosen: rotation.Plan,
    lots: Mapping[str, Sequence[tuple[int, Decimal]]],
    prices: Mapping[str, Decimal],
    at_cost: Decimal,
    rules: Rotation,
) -> tuple[Decimal, list[rotation.Order]]:
    """(the capital, the orders) of a live rebalance: the book's equity on `prices`
    (`rotation.book_equity` of `at_cost`, the equity with the holdings at cost, and the
    holdings' `lots` of (shares, entry price)), and the order diff to `chosen`'s target on it.
    The shadow-day replay sizes live's side with it."""
    held = {name: sum(qty for qty, _ in parts) for name, parts in lots.items()}
    equity = rotation.book_equity(at_cost, lots, prices)
    return equity, rotation.orders(chosen, held, prices, equity, rules)


def _buy_all(
    session: Session,
    bot_id: uuid.UUID,
    day: date,
    buys: Sequence[rotation.Order],
    prices: Mapping[str, Decimal],
    ctx: SignalContext,
    now: datetime,
    done: Rebalanced,
) -> None:
    """The buys and top-ups (the backtest's `_buy_all`): each scaled down by the share of their
    outlay the free cash pays, then cut to what the cash left pays, each a shadow signal."""
    cash = ctx.free_cash if ctx.free_cash is not None else Decimal(0)
    total = sum((FILLS.outlay(o.qty, prices[o.instrument_id], day) for o in buys), Decimal(0))
    share = rotation.buy_share(total, cash)
    for order in buys:
        name, at = order.instrument_id, prices[order.instrument_id]
        qty = int((order.qty * share).to_integral_value(ROUND_FLOOR))
        qty = min(qty, FILLS.affordable(cash, at, day))
        if qty <= 0:
            done.row(name, order.action, CASH, 0, at)
            done.refused[name] = CASH
            continue
        request = EntryRequest(
            bot_id=bot_id,
            playbook=rotation.PLAYBOOK,
            instrument_id=uuid.UUID(name),
            trade_date=day,
            triggered_at=now,
            context=replace(
                ctx, action=ROTATION_BUY, entry_price=at, trigger_level=at, quantity=qty
            ),
        )
        try:
            outcome = open_entry(
                session,
                request,
                mode=Mode.SHADOW,
                fills=FILLS,
                confidence=Confidence.low(ROTATION_CONFIDENCE),
            )
        except TradeError as refused:  # not in the universe, no live version: nothing written
            done.row(name, order.action, str(refused), 0, at)
            done.refused[name] = str(refused)
            continue
        decision = outcome.decision
        if outcome.duplicate or decision is None or not decision.accepted:
            reason = "duplicate" if outcome.duplicate else (decision.reason if decision else None)
            done.row(name, order.action, reason, 0, at)
            done.refused[name] = str(reason)
            continue
        bought = int(decision.quantity or 0)
        cash -= FILLS.outlay(bought, at, day)
        done.row(name, order.action, None if bought == order.qty else CASH, bought, at)


def record_rebalanced(
    session: Session, bot_id: uuid.UUID, done: Rebalanced, now: datetime, *, asked: str
) -> None:
    """Record what the session's rotation job did for the bot (`rotation.rebalanced`): its rows,
    the sales still waiting for a price, and the mode the bot asked for (it trades on paper)."""
    record(
        session,
        aggregate_type="bot",
        aggregate_id=bot_id,
        event_type=EVENT_REBALANCED,
        actor="engine",
        payload={
            "trade_date": done.trade_date.isoformat(),
            "skipped": done.skipped,
            "asked_mode": asked,
            "rows": done.rows,
            "waiting": done.waiting,
            "refused": done.refused,
            "unpriced": done.unpriced,
        },
        bot_id=bot_id,
        occurred_at=now,
    )
    session.flush()


def rebalanced_on(session: Session, bot_id: uuid.UUID, trade_date: date) -> bool:
    """Whether the bot's rotation job already ran for `trade_date`."""
    for (payload,) in session.execute(
        select(HatEvent.payload).where(
            HatEvent.bot_id == bot_id,
            HatEvent.event_type == EVENT_REBALANCED,
            recorded_for(trade_date),
        )
    ):
        if payload.get("trade_date") == trade_date.isoformat():
            return True
    return False


def waiting_sales(session: Session, bot_id: uuid.UUID) -> dict[str, str]:
    """The sales the bot's latest rotation job left waiting for a price, with their reasons."""
    payload = session.scalar(
        select(HatEvent.payload)
        .where(HatEvent.bot_id == bot_id, HatEvent.event_type == EVENT_REBALANCED)
        .order_by(HatEvent.occurred_at.desc(), HatEvent.id.desc())
        .limit(1)
    )
    found = {} if payload is None else payload.get("waiting") or {}
    return {str(name): str(reason) for name, reason in found.items()}

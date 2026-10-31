"""The live and paper books' daily report: positions, realised and unrealised P&L, costs,
exposure and the invested share, each closed trade's R, and the Backtest Result Standard's
headline metrics against the NIFTY 500 TRI (`backtest.standard.book_metrics`, the code a run's
`run.json` uses), so a book and a backtest compare one to one.

A book is one bot's real positions (`live`) or its shadow ones (`paper`; a veto trade, the
virtual trade of a skipped confirm signal, is scored elsewhere and is in neither). Its start is
the bot's first allocation with capital (`capital` for live, the sleeve's nominal
`virtual_capital` for paper) and its capital that allocation's. Each session's close of the book
is rebuilt from its fills, its positions' dividends and the official closes: equity = start +
the fills' net cash (sales less purchases, less fees) + the dividends credited on their
ex-dates by then (`trade_dividends`, the backtest's rule) + the shares held at the session's
close (the last close before it when the stock did not trade, the fill price before any
close). The rules are the pure functions here; `read_books` reads the database and the bar
store, read-only.

`$ADMIN books [--date D]` prints the report as JSON (read-only).
"""

from __future__ import annotations

import json
import uuid
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..backtest.standard import (
    TRADING_DAYS,
    BookSession,
    BookTrade,
    book_metrics,
    rounded,
    sessions_held,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from ..data.store import DataStore

__all__ = [
    "LIVE",
    "PAPER",
    "BookFill",
    "BookReport",
    "ClosedTrade",
    "OpenRow",
    "book_curve",
    "book_report",
    "books_text",
    "open_row",
    "read_books",
    "report_doc",
    "sold_gain",
]

LIVE = "live"
PAPER = "paper"
# A book younger than a trading year has no annual figure worth reading (three days up 1% would
# read as 236% a year): the report shows these as n/a and leads with the total return against
# the TRI's over the same sessions. `book_metrics` itself, a run's code, is left as it is.
ANNUALISED = (
    "cagr_pct",
    "mar",
    "sharpe",
    "sortino",
    "benchmark_cagr_pct",
    "cagr_ex_top5_trades_pct",
    "cagr_per_invested_pct",
)
MIN_ANNUAL_SESSIONS = TRADING_DAYS
_ZERO = Decimal(0)
_HUNDRED = Decimal(100)

Closes = Mapping[str, Sequence[tuple[date, Decimal]]]  # instrument id -> dated closes, in order


@dataclass(frozen=True)
class BookFill:
    """One fill of the book: its session, instrument, shares (bought > 0, sold < 0), price and
    fees; or, with `rescale`, a split or bonus setting the shares held to `qty` (no cash)."""

    day: date
    instrument_id: str
    qty: int
    price: Decimal
    fees: Decimal
    rescale: bool = False


@dataclass(frozen=True)
class OpenRow:
    """An open position as the report shows it, marked at `mark` (the session's close)."""

    symbol: str
    qty: int
    avg_entry: Decimal
    initial_stop: Decimal
    current_stop: Decimal
    mark: Decimal | None
    value: Decimal
    # its fills' net cash and dividends earned + its shares at the mark, less its partial
    # sales' realised P&L
    unrealised: Decimal
    open_r: Decimal | None  # (mark - entry) / (entry - initial stop)
    opened: date
    realised: Decimal = Decimal(0)  # its partial sales' (a trim, an exit partial)
    realised_today: Decimal = Decimal(0)


@dataclass(frozen=True)
class ClosedTrade:
    symbol: str
    entry: date
    exit: date
    hold: int  # sessions
    exit_reason: str
    net_pnl: Decimal
    fees: Decimal
    r: Decimal
    mfe_r: Decimal | None = None  # a paper trade's; None on a real one
    mae_r: Decimal | None = None
    entry_value: Decimal | None = None  # the shares bought times their price


@dataclass(frozen=True)
class BookReport:
    bot: str
    kind: str  # LIVE or PAPER
    as_of: date
    period_start: date
    sessions: int  # the book's sessions through `as_of`
    start_capital: Decimal
    equity: Decimal
    day_pnl: Decimal
    # the closed trades', net of costs, with their dividends, and the open positions' partial
    # sales (`daily.entries.sold_gain`, as the shadow book's free cash counts them)
    realised: Decimal
    realised_today: Decimal
    unrealised: Decimal
    fees: Decimal  # every fill's, closed and open
    fees_today: Decimal
    position_value: Decimal
    invested_pct: float | None  # today's position value over equity
    benchmark_return_pct: float | None  # NIFTY 500 TRI over the same sessions
    metrics: dict[str, Any]
    positions: list[OpenRow] = field(default_factory=list)
    trades: list[ClosedTrade] = field(default_factory=list)
    # positions closed by `as_of` with no trade result yet: in the curve, not in the trades
    missing_results: list[str] = field(default_factory=list)


# --- the rules -------------------------------------------------------------------------------


def _close_on(closes: Sequence[tuple[date, Decimal]], day: date) -> Decimal | None:
    """The last close dated on or before `day`."""
    n = bisect_right([d for d, _ in closes], day)
    return closes[n - 1][1] if n else None


def book_curve(
    start: Decimal,
    days: Sequence[date],
    fills: Sequence[BookFill],
    dividends: Sequence[tuple[date, Decimal]],
    closes: Closes,
) -> list[BookSession]:
    """The book's close on each of `days` (sorted sessions): its equity, its positions' value
    and how many it held. A fill or a dividend dated before the first session counts on it.
    Fills of one session apply in the order given (the reader gives them in time order); a
    rescale sets the shares held and scales the fallback price by the same ratio."""
    ordered = sorted(fills, key=lambda f: f.day)  # stable: a session's own order is kept
    paid = sorted(dividends)
    cash, held = _ZERO, defaultdict(int)
    last_price: dict[str, Decimal] = {}
    i = j = 0
    out = []
    for day in days:
        while i < len(ordered) and ordered[i].day <= day:
            f = ordered[i]
            i += 1
            if f.rescale:
                before = held[f.instrument_id]
                if before > 0 and f.qty > 0 and f.instrument_id in last_price:
                    last_price[f.instrument_id] = last_price[f.instrument_id] * before / f.qty
                held[f.instrument_id] = f.qty
                continue
            cash += -f.qty * f.price - f.fees
            held[f.instrument_id] += f.qty
            last_price[f.instrument_id] = f.price
        while j < len(paid) and paid[j][0] <= day:
            cash += paid[j][1]
            j += 1
        value, count = _ZERO, 0
        for instrument, qty in held.items():
            if qty == 0:
                continue
            mark = _close_on(closes.get(instrument, ()), day) or last_price[instrument]
            value += qty * mark
            count += 1
        out.append(BookSession(day, start + cash + value, value, count))
    return out


def _bench_return(
    benchmark: Sequence[tuple[date, Decimal]], first: date, last: date
) -> float | None:
    """The benchmark's return in % from its last close before `first` to its close of `last`."""
    before = [c for d, c in benchmark if d < first]
    end = dict(benchmark).get(last)
    if not before or end is None or before[-1] <= 0:
        return None
    return rounded((end / before[-1] - 1) * _HUNDRED)


def book_report(
    *,
    bot: str,
    kind: str,
    start: Decimal,
    period_start: date,
    days: Sequence[date],
    fills: Sequence[BookFill],
    dividends: Sequence[tuple[date, Decimal]],
    closes: Closes,
    trades: Sequence[ClosedTrade],
    positions: Sequence[OpenRow],
    benchmark: Sequence[tuple[date, Decimal]],
    missing_results: Sequence[str] = (),
) -> BookReport:
    """The report of one book through the last of `days` (its sessions from `period_start`)."""
    curve = book_curve(start, days, fills, dividends, closes)
    as_of = days[-1] if days else period_start
    today = curve[-1] if curve else BookSession(as_of, start, _ZERO, 0)
    before = curve[-2].equity if len(curve) > 1 else start
    ordered = sorted(trades, key=lambda t: (t.entry, t.symbol, t.exit))
    metrics = book_metrics(
        [
            BookTrade(t.symbol, t.r, t.net_pnl, t.fees, t.hold, t.mfe_r, t.mae_r, t.entry_value)
            for t in ordered
        ],
        [(s.day, s.equity) for s in curve],
        curve,
        start,
        period_start,
        benchmark,
    )
    if len(curve) < MIN_ANNUAL_SESSIONS:
        metrics = {**metrics, **dict.fromkeys(ANNUALISED)}
    return BookReport(
        bot=bot,
        kind=kind,
        as_of=as_of,
        period_start=period_start,
        sessions=len(curve),
        start_capital=start,
        equity=today.equity,
        day_pnl=today.equity - before,
        realised=sum((t.net_pnl for t in trades), _ZERO)
        + sum((p.realised for p in positions), _ZERO),
        realised_today=sum((t.net_pnl for t in trades if t.exit == as_of), _ZERO)
        + sum((p.realised_today for p in positions), _ZERO),
        unrealised=sum((p.unrealised for p in positions), _ZERO),
        fees=sum((f.fees for f in fills if not f.rescale), _ZERO),
        fees_today=sum((f.fees for f in fills if f.day == as_of and not f.rescale), _ZERO),
        position_value=today.position_value,
        invested_pct=rounded(today.position_value / today.equity * _HUNDRED)
        if today.equity > 0
        else None,
        benchmark_return_pct=_bench_return(benchmark, days[0], as_of) if days else None,
        metrics=metrics,
        positions=list(positions),
        trades=ordered,
        missing_results=list(missing_results),
    )


def sold_gain(entry: Decimal, qty: int, price: Decimal, fees: Decimal) -> Decimal:
    """The realised P&L of selling `qty` shares bought at `entry` at `price`, less the sale's
    `fees`: what the sale adds to the free cash beyond the cost of the shares it returns."""
    return (price - entry) * qty - fees


def open_row(
    *,
    symbol: str,
    qty: int,
    avg_entry: Decimal,
    initial_stop: Decimal,
    current_stop: Decimal,
    opened: date,
    fill_cash: Decimal,
    mark: Decimal | None,
    sold: Decimal = _ZERO,
    sold_today: Decimal = _ZERO,
) -> OpenRow:
    """An open position marked at `mark` (None: at its entry): its value, its unrealised P&L
    (`fill_cash`, the net cash of its fills and the dividends it earned, plus its shares at the
    mark, so its costs are in, less `sold`, the realised P&L of its partial sales, which is its
    `realised`) and its open R."""
    price = avg_entry if mark is None else mark
    value = qty * price
    risk = avg_entry - initial_stop
    return OpenRow(
        symbol=symbol,
        qty=qty,
        avg_entry=avg_entry,
        initial_stop=initial_stop,
        current_stop=current_stop,
        mark=mark,
        value=value,
        unrealised=fill_cash + value - sold,
        open_r=(price - avg_entry) / risk if risk > 0 else None,
        opened=opened,
        realised=sold,
        realised_today=sold_today,
    )


# --- the reads -------------------------------------------------------------------------------


def _closes(store: DataStore, ids: Sequence[str], first: date, last: date) -> dict[str, list]:
    from ..data.store import RAW_LATEST
    from ..money import require_exact

    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    sql = (
        f"SELECT CAST(instrument_id AS VARCHAR), date, CAST(close AS VARCHAR) FROM {RAW_LATEST} "  # noqa: S608
        f"WHERE date BETWEEN ? AND ? AND close IS NOT NULL "
        f"AND CAST(instrument_id AS VARCHAR) IN ({marks}) ORDER BY date"
    )
    out: dict[str, list] = defaultdict(list)
    with store.connect() as con:
        for instrument, day, close in con.execute(sql, [first, last, *ids]).fetchall():
            out[instrument].append((day, require_exact(close, "close")))
    return out


def _benchmark(store: DataStore, through: date) -> list[tuple[date, Decimal]]:
    from ..data.index_history import NIFTY_500_TRI
    from ..money import require_exact

    sql = (
        "SELECT date, CAST(close AS VARCHAR) FROM index_daily "
        "WHERE index_name = ? AND date <= ? ORDER BY date"
    )
    with store.connect() as con:
        rows = con.execute(sql, [NIFTY_500_TRI, through]).fetchall()
    return [(d, require_exact(c, "close")) for d, c in rows]


def read_books(session: Session, store: DataStore, as_of: date) -> list[BookReport]:
    """Every bot's live and paper book through the session `as_of`, read-only. A book with no
    allocation with capital is left out."""
    from sqlalchemy import select

    from ..configs.records import Bot
    from ..configs.unit import configs_of
    from ..data.calendar import session_calendar
    from ..engine.rescale import EVENT_RESCALED
    from ..engine.trade_dividends import positions_dividend_credits
    from ..governance import Mode
    from ..models import (
        HatAllocationDaily,
        HatEvent,
        HatFill,
        HatOrder,
        HatPosition,
        HatSignal,
        HatTradeResult,
    )
    from ..money import require_exact
    from ..read.bots import labels
    from ..read.positions import symbol_of
    from ..time import as_utc, trade_date_of

    calendar = session_calendar(session)
    benchmark = _benchmark(store, as_of)
    reports = []
    bots = configs_of(session).all(Bot)
    names = labels(session, (b.id for b in bots))
    for bot in sorted(bots, key=lambda b: (names.get(b.id, ""), str(b.id))):
        for kind in (LIVE, PAPER):
            column = (
                HatAllocationDaily.capital if kind == LIVE else HatAllocationDaily.virtual_capital
            )
            first = session.execute(
                select(HatAllocationDaily.trade_date, column)
                .where(HatAllocationDaily.bot_id == bot.id, column > 0)
                .where(HatAllocationDaily.trade_date <= as_of)
                .order_by(HatAllocationDaily.trade_date)
                .limit(1)
            ).first()
            if first is None:
                continue
            period_start, start = first[0], require_exact(first[1], "capital")
            virtual = kind == PAPER
            query = (
                select(HatPosition, HatSignal.mode)
                .join(HatSignal, HatSignal.id == HatPosition.signal_id)
                .where(HatPosition.bot_id == bot.id, HatPosition.is_virtual.is_(virtual))
            )
            if virtual:
                query = query.where(HatSignal.mode == Mode.SHADOW)  # a veto trade is not paper
            positions = [p for p, _ in session.execute(query)]
            by_signal = {p.signal_id: p for p in positions}
            fill_rows = (
                session.execute(
                    select(
                        HatOrder.signal_id,
                        HatOrder.side,
                        HatFill.qty,
                        HatFill.price,
                        HatFill.fees,
                        HatFill.filled_at,
                    )
                    .join(HatOrder, HatOrder.id == HatFill.order_id)
                    .where(
                        HatOrder.signal_id.in_(list(by_signal)),
                        HatFill.is_virtual.is_(virtual),
                    )
                ).all()
                if by_signal
                else []
            )
            # the fills and the rescales (a split or bonus sets the shares), in time order
            steps = [
                (as_utc(at), signal_id, -qty if side == "sell" else qty, price, fee, False)
                for signal_id, side, qty, price, fee, at in fill_rows
            ]
            by_id = {p.id: p for p in positions}
            if by_id:
                for event in session.scalars(
                    select(HatEvent).where(
                        HatEvent.event_type == EVENT_RESCALED,
                        HatEvent.aggregate_type == "position",
                        HatEvent.aggregate_id.in_(list(by_id)),
                    )
                ):
                    qty = int(event.payload["to"]["qty_open"])
                    position = by_id[event.aggregate_id]
                    steps.append((as_utc(event.occurred_at), position.signal_id, qty, 0, 0, True))
            fills, cash, held = [], defaultdict(lambda: _ZERO), defaultdict(int)
            sold, sold_today = defaultdict(lambda: _ZERO), defaultdict(lambda: _ZERO)
            bought = defaultdict(lambda: _ZERO)
            for at, signal_id, qty, price, fee, rescale in sorted(steps, key=lambda s: s[0]):
                day = trade_date_of(at)
                if day > as_of:
                    continue
                position = by_signal[signal_id]
                fills.append(
                    BookFill(
                        day, str(position.instrument_id), qty, Decimal(price), Decimal(fee), rescale
                    )
                )
                if rescale:
                    held[signal_id] = qty
                    continue
                cash[signal_id] += -qty * Decimal(price) - Decimal(fee)
                held[signal_id] += qty
                if qty > 0:
                    bought[signal_id] += qty * Decimal(price)
                if qty < 0:  # a sale: its realised P&L, as the shadow book's free cash counts it
                    gain = sold_gain(
                        Decimal(position.avg_entry), -qty, Decimal(price), Decimal(fee)
                    )
                    sold[signal_id] += gain
                    if day == as_of:
                        sold_today[signal_id] += gain
            days = sorted(
                d
                for d, row in (calendar.days.items() if calendar else ())
                if row.is_trading and period_start <= d <= as_of
            )
            ids = sorted({f.instrument_id for f in fills})
            closes = _closes(store, ids, period_start, as_of)
            trades, dividends, open_rows, missing = [], [], [], []
            # each position's dividends on their ex-dates, through its exit or `as_of`
            earned_by = positions_dividend_credits(
                session,
                [
                    (p.id, p.instrument_id, min(trade_date_of(as_utc(p.closed_at)), as_of))
                    if p.closed_at
                    else (p.id, p.instrument_id, as_of)
                    for p in positions
                ],
            )
            for p in positions:
                opened = trade_date_of(as_utc(p.opened_at))
                closed = trade_date_of(as_utc(p.closed_at)) if p.closed_at else None
                earned = earned_by.get(p.id, [])
                symbol = symbol_of(session, p.instrument_id)
                result = session.get(HatTradeResult, p.id) if closed else None
                if result is not None and closed is not None and closed <= as_of:  # closed by then
                    trades.append(
                        ClosedTrade(
                            symbol,
                            opened,
                            closed,
                            sessions_held(days, opened, closed),
                            result.exit_reason,
                            Decimal(result.realized_pnl),
                            Decimal(result.fees),
                            Decimal(result.r_multiple),
                            None if result.mfe_r is None else Decimal(result.mfe_r),
                            None if result.mae_r is None else Decimal(result.mae_r),
                            bought.get(p.signal_id) or None,
                        )
                    )
                    dividends.extend(earned)
                elif closed is not None and closed <= as_of:  # closed, its result not written
                    dividends.extend(earned)
                    missing.append(symbol)
                elif opened <= as_of and held[p.signal_id] > 0:  # its shares on `as_of`
                    dividends.extend(earned)
                    series = closes.get(str(p.instrument_id), ())
                    open_rows.append(
                        open_row(
                            symbol=symbol,
                            qty=held[p.signal_id],
                            avg_entry=Decimal(p.avg_entry),
                            initial_stop=Decimal(p.initial_stop),
                            current_stop=Decimal(p.current_stop),
                            opened=opened,
                            fill_cash=cash[p.signal_id] + sum((c for _, c in earned), _ZERO),
                            mark=_close_on(series, as_of),
                            sold=sold[p.signal_id],
                            sold_today=sold_today[p.signal_id],
                        )
                    )
            reports.append(
                book_report(
                    bot=names.get(bot.id, str(bot.id)[:8]),
                    kind=kind,
                    start=start,
                    period_start=period_start,
                    days=days,
                    fills=fills,
                    dividends=dividends,
                    closes=closes,
                    trades=trades,
                    positions=open_rows,
                    benchmark=benchmark,
                    missing_results=missing,
                )
            )
    return reports


# --- the text --------------------------------------------------------------------------------


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}%"


def _num(value: object) -> str:
    return "n/a" if value is None else f"{value}"


def books_text(as_of: date, reports: Sequence[BookReport], *, detail: bool = False) -> str:
    """The books report as plain text: per book its equity and day, its P&L, costs and invested
    share, its return against the NIFTY 500 TRI and the standard's headline metrics, the trades
    closed on `as_of` with their R; with `detail`, every open position and every closed trade."""
    lines = [f"Books {as_of.isoformat()}"]
    if not reports:
        lines.append("No book: no bot has an allocation yet.")
    for r in reports:
        m = r.metrics
        lines += [
            "",
            f"{r.bot} {r.kind} (since {r.period_start.isoformat()}, "
            f"start {_money(r.start_capital)})",
            f"Return {_pct(m.get('total_return_pct'))} vs NIFTY 500 TRI "
            f"{_pct(r.benchmark_return_pct)} over {r.sessions} sessions",
            f"Equity {_money(r.equity)}  day {_money(r.day_pnl)}",
            f"Realised {_money(r.realised)} (today {_money(r.realised_today)})  unrealised "
            f"{_money(r.unrealised)}  costs {_money(r.fees)} (today {_money(r.fees_today)})",
            f"Invested {_pct(r.invested_pct)} today, {_pct(m.get('invested_pct'))} mean  "
            f"open {len(r.positions)}  value {_money(r.position_value)}",
            f"Trades {m.get('trades')}  win {_pct(m.get('win_pct'))}  exp "
            f"{_num(m.get('expectancy_r'))}R  max DD {_pct(m.get('max_dd_pct'))}  cost share "
            f"{_pct(m.get('cost_share_pct'))}",
            f"CAGR {_pct(m.get('cagr_pct'))} vs {_pct(m.get('benchmark_cagr_pct'))}  Sharpe "
            f"{_num(m.get('sharpe'))}"
            if r.sessions >= MIN_ANNUAL_SESSIONS
            else f"CAGR and Sharpe from {MIN_ANNUAL_SESSIONS} sessions",
        ]
        if r.missing_results:
            lines.append(f"Closed with no trade result yet: {', '.join(r.missing_results)}")
        closed = r.trades if detail else [t for t in r.trades if t.exit == r.as_of]
        for t in closed:
            lines.append(
                f"  closed {t.symbol} {t.entry.isoformat()}..{t.exit.isoformat()} "
                f"{t.r:+.2f}R {_money(t.net_pnl)} ({t.exit_reason})"
            )
        for p in r.positions if detail else ():
            open_r = "n/a" if p.open_r is None else f"{p.open_r:+.2f}R"
            mark = "n/a" if p.mark is None else _money(p.mark)
            lines.append(
                f"  open {p.symbol} {p.qty} @ {_money(p.avg_entry)} mark {mark} stop "
                f"{_money(p.current_stop)} {open_r} {_money(p.unrealised)}"
            )
    return "\n".join(lines)


# --- the command line ------------------------------------------------------------------------


def _jsonable(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(type(value).__name__)


def report_doc(reports: Sequence[BookReport]) -> list[Any]:
    """The reports as JSON values: money and dates as text."""
    return json.loads(json.dumps([asdict(r) for r in reports], default=_jsonable))

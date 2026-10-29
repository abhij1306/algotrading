"""The run's ledger reconciliation (accuracy plan W2 B1): every run checks, after its last
session, that its money adds up from its fills and the market data alone, and fails when it does
not (`backtest_ledger_mismatch`); the report goes into the run audit (`run_audit.check_ledger`).

It reads the run's fills (`FillRow`: strategy and rotation trades), the overlay's journal (its
ETF fills, tax payments and distributed units, `OverlayJournal`) and the market (`Market`: the
cost schedule, the slippage, each day's bar, last traded close, NSE tick and dividends), never
the engine's accounts, and rebuilds:

- each fill's charges, `charges(schedule.on(day), ...)` on the fill's price, the DP charge on
  an instrument's first sale of the day only (one debit per scrip per sell day): it must equal
  the fill's fees;
- each fill's price against its day's bar. Slippage moves a fill against the trade from a price
  inside the bar, so a buy prints between the low and the high after slippage and a sell between
  the low after slippage and the high (`slipped`; a LIMIT buy caps at its limit, inside that).
  A sale on a day without a bar (a suspension's or a delisting's exit) prints between the
  suspended mark and the last traded close, each after slippage; a buy without a bar is a
  finding. An overlay fill priced on an estimate (`sleeve_price`) is counted, not checked;
- each session's dividends: the cash per share going ex that day times the shares held into it
  (from the fills of earlier sessions), to the paisa (`dividend_credit`);
- the cash at each session's close: the start, plus each sale's value, less each buy's value and
  the recomputed charges, plus the recomputed dividends, less the overlay's tax. It must equal
  the engine's `cash` and never be below zero;
- the shares held at each close from the fills: they must equal the engine's positions and
  rotation holdings, and nothing is held after the last session;
- each held instrument's mark: its close on a day with a bar, else its last traded close,
  marked down one band per session after the run's `suspension_sessions` without a bar
  (`suspended_mark`); the ETF sleeve's units at their last close. The shares at these marks
  must equal the engine's `position_value` and, with the cash, its `equity`;
- each trade's P&L: its sales less its entry less its charges plus its dividends (a trade
  bought in one fill), and for every instrument the trades' P&L less dividends equal to its
  fills' net flow; and the trades' dividends equal to the dividends recomputed.

All of it is exact Decimal; a cash or value difference beyond `TOLERANCE` (one rupee) and any
charge or price outside its rule is a finding.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Protocol

from ..costs import CostModel, charges, slipped
from ..money import dividend_credit
from .fills import SUSPENSION_BAND_PCT, suspended_mark
from .report import FillRow, TradeRow

__all__ = [
    "PRICE_TOLERANCE",
    "TOLERANCE",
    "Held",
    "Market",
    "OverlayJournal",
    "SessionBook",
    "reconcile",
]

TOLERANCE = Decimal(1)  # rupees
PRICE_TOLERANCE = Decimal("0.0001")  # prices are stored to 4 places
MAX_LISTED = 25
PASS, FAIL = "pass", "fail"
_ZERO = Decimal(0)


class Market(Protocol):
    """What the reconciliation reads of the market, by instrument id (an ETF by its name)."""

    suspension_sessions: int

    def model(self, day: date) -> CostModel:
        """The cost model in force on `day` (`CostSchedule.on`)."""
        ...

    def slippage_bps(self, instrument_id: str) -> Decimal: ...

    def bar(self, instrument_id: str, day: date) -> tuple[Decimal, Decimal, Decimal] | None:
        """The (low, high, close) of the instrument's tradeable bar of `day`; None without one."""
        ...

    def last_close(self, instrument_id: str, day: date) -> tuple[date, Decimal] | None:
        """The instrument's last tradeable session on or before `day` and its close."""
        ...

    def stale_sessions(self, instrument_id: str, seen: date, day: date) -> int:
        """The sessions after `seen` up to `day` the instrument had no bar in (a session skipped
        near a price jump is not one)."""
        ...

    def tick(self, instrument_id: str, day: date) -> Decimal: ...

    def dividend(self, instrument_id: str, day: date) -> Decimal:
        """The cash per share of the instrument's dividends going ex on `day`."""
        ...


@dataclass(frozen=True)
class Held:
    """One open position (or rotation holding) at a session's close: its shares and mark."""

    instrument_id: str
    qty: int
    mark: Decimal


@dataclass(frozen=True)
class SessionBook:
    """A session's book as the engine reported it (`equity`, `cash`, `position_value`, as in
    `equity.csv`), what it held at the close, the overlay's units at their closes
    (`sleeve_value`) and the session's cash dividends."""

    day: date
    equity: Decimal
    cash: Decimal
    position_value: Decimal
    held: tuple[Held, ...] = ()
    sleeve_value: Decimal = _ZERO
    dividends: Decimal = _ZERO


@dataclass(frozen=True)
class OverlayJournal:
    """The ETF overlay's records: its fills (`estimated`: the indices of those priced on an
    estimate), its tax payments and the units LIQUIDBEES' distributions added."""

    fills: tuple[FillRow, ...] = ()
    estimated: frozenset[int] = frozenset()
    taxes: tuple[tuple[date, Decimal], ...] = ()
    distributions: tuple[tuple[date, str, int], ...] = ()


NO_OVERLAY = OverlayJournal()


@dataclass
class _Walk:
    """The account rebuilt session by session."""

    cash: Decimal
    shares: Counter[str] = field(default_factory=Counter)
    units: Counter[str] = field(default_factory=Counter)
    sold: set[tuple[date, str]] = field(default_factory=set)
    worst: Decimal = _ZERO
    dividends: Decimal = _ZERO
    found: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))


def _off(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) > TOLERANCE


def _finding(code: str, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"code": code, "count": len(items), "items": list(items[:MAX_LISTED])}


def _fill_item(fill: FillRow, **extra: object) -> dict[str, Any]:
    return {
        "date": fill.day.isoformat(),
        "instrument_id": fill.instrument_id,
        "side": fill.side,
        "qty": fill.qty,
        "price": str(fill.price),
        **{k: str(v) for k, v in extra.items()},
    }


def mark_of(market: Market, instrument_id: str, day: date) -> Decimal | None:
    """The instrument's mark at `day`'s close: its close with a bar, else its last traded
    close, one band lower per session past `suspension_sessions` (`suspended_mark`); None
    before its first close."""
    found = market.bar(instrument_id, day)
    if found is not None:
        return found[2]
    last = market.last_close(instrument_id, day)
    if last is None:
        return None
    seen, close = last
    sessions = market.stale_sessions(instrument_id, seen, day)
    after = market.suspension_sessions
    if sessions <= after:
        return close
    return suspended_mark(
        close, sessions, after, SUSPENSION_BAND_PCT, market.tick(instrument_id, day)
    )


def price_range(market: Market, fill: FillRow) -> tuple[Decimal, Decimal] | None:
    """The prices `fill` may print at (the module docstring); None when none is possible (a buy
    on a day without a bar, or an instrument never traded)."""
    bps = market.slippage_bps(fill.instrument_id)
    found = market.bar(fill.instrument_id, fill.day)
    if found is not None:
        low, high, _ = found
        if fill.side == "buy":
            return low, slipped(high, "buy", bps)
        return slipped(low, "sell", bps), high
    if fill.side == "buy":
        return None
    mark = mark_of(market, fill.instrument_id, fill.day)
    last = market.last_close(fill.instrument_id, fill.day)
    if mark is None or last is None:
        return None
    return slipped(mark, "sell", bps), slipped(last[1], "sell", bps)


def _fill(walk: _Walk, market: Market, fill: FillRow, *, checked: bool = True) -> Decimal:
    """Check `fill`'s charges and (if `checked`) its price; its cash flow on the recomputed
    charges."""
    key = (fill.day, fill.instrument_id)
    dp = fill.side == "sell" and key not in walk.sold
    if fill.side == "sell":
        walk.sold.add(key)
    fees = charges(market.model(fill.day), fill.side, fill.qty, fill.price, dp=dp).total
    if fees != fill.fees:
        walk.found["charges"].append(_fill_item(fill, fees=fill.fees, recomputed=fees))
    if checked:
        bounds = price_range(market, fill)
        if bounds is None:
            walk.found["fill_without_bar"].append(_fill_item(fill))
        elif not bounds[0] - PRICE_TOLERANCE <= fill.price <= bounds[1] + PRICE_TOLERANCE:
            walk.found["fill_outside_bar"].append(_fill_item(fill, low=bounds[0], high=bounds[1]))
    value = fill.qty * fill.price
    return (value if fill.side == "sell" else -value) - fees


def _held_diff(shares: Counter[str], book: SessionBook) -> dict[str, Any] | None:
    """The instruments whose shares from the fills differ from the engine's at `book`'s close."""
    held: Counter[str] = Counter()
    for line in book.held:
        held[line.instrument_id] += line.qty
    wanted = {k: v for k, v in shares.items() if v}
    if wanted == {k: v for k, v in held.items() if v}:
        return None
    keys = [k for k in sorted(set(wanted) | set(held)) if wanted.get(k, 0) != held[k]]
    return {
        "date": book.day.isoformat(),
        "from_fills": {k: wanted.get(k, 0) for k in keys},
        "engine": {k: held[k] for k in keys},
    }


def _value(walk: _Walk, market: Market, book: SessionBook) -> Decimal:
    """The held shares at their marks and the sleeve's units at their closes, at `book`'s
    close; an instrument without a mark is a finding."""
    total = _ZERO
    for instrument_id, qty in sorted(walk.shares.items()):
        if not qty:
            continue
        mark = mark_of(market, instrument_id, book.day)
        if mark is None:
            walk.found["no_mark"].append(
                {"date": book.day.isoformat(), "instrument_id": instrument_id}
            )
            continue
        total += qty * mark
    for asset, units in sorted(walk.units.items()):
        last = market.last_close(asset, book.day) if units else None
        total += _ZERO if last is None else units * last[1]
    return total


def _session(
    walk: _Walk, market: Market, book: SessionBook, fills: Sequence[FillRow], journal: _Day
) -> None:
    """One session: its dividends on the shares held into it, its fills, the overlay's, its
    tax, then the cash, holdings and value checks at its close."""
    day = book.day.isoformat()
    dividends = sum(
        (
            dividend_credit(qty, per_share)
            for instrument_id, qty in sorted(walk.shares.items())
            if qty and (per_share := market.dividend(instrument_id, book.day))
        ),
        _ZERO,
    )
    walk.dividends += dividends
    if _off(dividends, book.dividends):
        walk.found["dividends"].append(
            {"date": day, "recomputed": str(dividends), "engine": str(book.dividends)}
        )
    walk.cash += dividends
    for fill in fills:
        walk.cash += _fill(walk, market, fill)
        walk.shares[fill.instrument_id] += fill.qty if fill.side == "buy" else -fill.qty
    for fill, estimated in journal.fills:
        walk.cash += _fill(walk, market, fill, checked=not estimated)
        walk.units[fill.instrument_id] += fill.qty if fill.side == "buy" else -fill.qty
    for asset, units in journal.distributions:
        walk.units[asset] += units
    walk.cash -= journal.tax
    walk.worst = max(walk.worst, abs(walk.cash - book.cash))
    if _off(walk.cash, book.cash):
        walk.found["cash_walk"].append(
            {"date": day, "walked": str(walk.cash), "engine": str(book.cash)}
        )
    if walk.cash < -TOLERANCE:
        walk.found["negative_cash"].append({"date": day, "walked": str(walk.cash)})
    if (held := _held_diff(walk.shares, book)) is not None:
        walk.found["holdings"].append(held)
    value = _value(walk, market, book)
    if _off(value, book.position_value):
        walk.found["position_value"].append(
            {"date": day, "marked": str(value), "engine": str(book.position_value)}
        )
    if _off(walk.cash + value, book.equity):
        walk.found["equity"].append(
            {"date": day, "rebuilt": str(walk.cash + value), "engine": str(book.equity)}
        )


@dataclass
class _Day:
    """The overlay's records of one session."""

    fills: list[tuple[FillRow, bool]] = field(default_factory=list)
    tax: Decimal = _ZERO
    distributions: list[tuple[str, int]] = field(default_factory=list)


def _by_day(journal: OverlayJournal) -> dict[date, _Day]:
    days: dict[date, _Day] = defaultdict(_Day)
    for n, fill in enumerate(journal.fills):
        days[fill.day].fills.append((fill, n in journal.estimated))
    for day, paid in journal.taxes:
        days[day].tax += paid
    for day, asset, units in journal.distributions:
        days[day].distributions.append((asset, units))
    return days


def _sessions(
    start: Decimal,
    books: Sequence[SessionBook],
    fills: Sequence[FillRow],
    market: Market,
    journal: OverlayJournal,
) -> _Walk:
    """The account walked over every session (`_session`), and what is left after the last."""
    by_day: dict[date, list[FillRow]] = defaultdict(list)
    for fill in fills:
        by_day[fill.day].append(fill)
    overlay = _by_day(journal)
    walk = _Walk(start)
    sessions = {b.day for b in books}
    for day in sorted((set(by_day) | set(overlay)) - sessions):
        walk.found["fill_outside_sessions"].append({"date": day.isoformat()})
    for book in sorted(books, key=lambda b: b.day):
        _session(walk, market, book, by_day.get(book.day, ()), overlay.get(book.day, _Day()))
    left = {k: v for k, v in (walk.shares + walk.units).items() if v}
    if left:
        walk.found["holdings"].append({"date": "after_last_session", "from_fills": left})
    return walk


def _trades(
    trades: Sequence[TradeRow], fills: Sequence[FillRow], dividends: Decimal
) -> dict[str, list[dict[str, Any]]]:
    """Each trade's P&L from its legs and entry, the instruments' P&L from their fills, and the
    trades' dividends against those recomputed."""
    found: dict[str, list[dict[str, Any]]] = defaultdict(list)
    buys: dict[str, list[date]] = defaultdict(list)
    for fill in fills:
        if fill.side == "buy":
            buys[fill.instrument_id].append(fill.day)
    by_instrument: dict[str, Decimal] = defaultdict(Decimal)
    for trade in trades:
        by_instrument[trade.instrument_id] += trade.realized_pnl - trade.dividends
        sold = sum(leg.qty for leg in trade.legs)
        bought = [d for d in buys[trade.instrument_id] if trade.entry_date <= d <= trade.exit_date]
        if not trade.legs or sold != trade.qty or len(bought) > 1:
            continue  # bought in several fills (a rotation's top-ups): checked by its instrument
        recomputed = (
            sum((leg.qty * leg.price for leg in trade.legs), _ZERO)
            - trade.qty * trade.entry_price
            - trade.fees
            + trade.dividends
        )
        if _off(recomputed, trade.realized_pnl):
            found["trade_pnl"].append(
                {
                    "instrument_id": trade.instrument_id,
                    "entry_date": trade.entry_date.isoformat(),
                    "recomputed": str(recomputed),
                    "engine": str(trade.realized_pnl),
                }
            )
    flows: dict[str, Decimal] = defaultdict(Decimal)
    for fill in fills:
        value = fill.qty * fill.price
        flows[fill.instrument_id] += (value if fill.side == "sell" else -value) - fill.fees
    for instrument_id in sorted(set(flows) | set(by_instrument)):
        if _off(flows[instrument_id], by_instrument[instrument_id]):
            found["instrument_pnl"].append(
                {
                    "instrument_id": instrument_id,
                    "fills": str(flows[instrument_id]),
                    "trades": str(by_instrument[instrument_id]),
                }
            )
    paid = sum((t.dividends for t in trades), _ZERO)
    if _off(paid, dividends):
        found["trade_dividends"].append({"trades": str(paid), "recomputed": str(dividends)})
    return found


def reconcile(
    start: Decimal,
    books: Sequence[SessionBook],
    fills: Sequence[FillRow],
    trades: Sequence[TradeRow],
    market: Market,
    journal: OverlayJournal = NO_OVERLAY,
) -> dict[str, Any]:
    """The ledger report of a run (the module docstring): `status` `pass` or `fail`, what was
    checked, and a finding per kind of mismatch."""
    walk = _sessions(start, books, fills, market, journal)
    found = dict(walk.found)
    for code, items in _trades(trades, fills, walk.dividends).items():
        found.setdefault(code, []).extend(items)
    findings = [_finding(code, items) for code, items in sorted(found.items()) if items]
    return {
        "status": FAIL if findings else PASS,
        "tolerance_inr": str(TOLERANCE),
        "sessions": len(books),
        "fills": len(fills),
        "overlay_fills": len(journal.fills),
        "overlay_estimated_fills": len(journal.estimated),
        "trades": len(trades),
        "dividends": str(walk.dividends),
        "overlay_tax": str(sum((t for _, t in journal.taxes), _ZERO)),
        "worst_cash_diff": str(walk.worst),
        "findings": findings,
    }

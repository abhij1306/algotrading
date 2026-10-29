"""The run's ledger reconciliation (accuracy plan W2 B1): every run checks, after its last
session, that its money adds up from its fills alone, and fails when it does not
(`backtest_ledger_mismatch`); the report goes into the run audit (`run_audit.check_ledger`).

From the start capital and the run's fills (`FillRow`: strategy and rotation trades), the cash
dividends credited each session and the ETF overlay's cash flow each session (its trades and tax),
it rebuilds independently of the engine's accounts:

- the cash at each session's close: the start, plus each sale's value, less each buy's value and
  every fill's charges, plus the dividends and the overlay's flow. It must equal the session's
  `cash` as the engine reported it;
- the shares held at each close, by instrument, from the fills: they must equal the positions
  the engine held (its open positions and rotation holdings), and none is held after the last
  session;
- each session's equity: the cash plus the held shares at the engine's marks plus the overlay's
  units at their closes; it must equal the session's `equity` and `position_value`;
- each trade's P&L: its sales less its entry less its charges plus its dividends (a trade
  bought in one fill), and for every instrument the trades' P&L less dividends equal to
  its fills' net flow; and the trades' dividends equal to the dividends credited.

All of it is exact Decimal; a difference beyond `TOLERANCE` (one rupee) is a finding.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .report import FillRow, TradeRow

__all__ = ["TOLERANCE", "Held", "SessionBook", "reconcile"]

TOLERANCE = Decimal(1)  # rupees
MAX_LISTED = 25
PASS, FAIL = "pass", "fail"
_ZERO = Decimal(0)


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
    (`sleeve_value`), and the session's cash dividends and overlay cash flow."""

    day: date
    equity: Decimal
    cash: Decimal
    position_value: Decimal
    held: tuple[Held, ...] = ()
    sleeve_value: Decimal = _ZERO
    dividends: Decimal = _ZERO
    overlay_flow: Decimal = _ZERO


def _flow(fill: FillRow) -> Decimal:
    value = fill.qty * fill.price
    return (value if fill.side == "sell" else -value) - fill.fees


def _off(a: Decimal, b: Decimal) -> bool:
    return abs(a - b) > TOLERANCE


def _finding(code: str, items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {"code": code, "count": len(items), "items": list(items[:MAX_LISTED])}


def _sessions(
    start: Decimal, books: Sequence[SessionBook], fills: Sequence[FillRow]
) -> tuple[list[dict[str, Any]], dict[str, list[dict[str, Any]]]]:
    """The cash walk, the holdings and the equity identity of every session."""
    by_day: dict[date, list[FillRow]] = defaultdict(list)
    for fill in fills:
        by_day[fill.day].append(fill)
    days = {b.day for b in books}
    stray = sorted({f.day for f in fills} - days)
    found: dict[str, list[dict[str, Any]]] = {
        "fill_outside_sessions": [{"date": d.isoformat()} for d in stray],
        "cash_walk": [],
        "holdings": [],
        "position_value": [],
        "equity_identity": [],
    }
    cash = start
    shares: Counter[str] = Counter()
    worst = _ZERO
    for book in sorted(books, key=lambda b: b.day):
        for fill in by_day.get(book.day, ()):
            cash += _flow(fill)
            shares[fill.instrument_id] += fill.qty if fill.side == "buy" else -fill.qty
        cash += book.dividends + book.overlay_flow
        day = book.day.isoformat()
        worst = max(worst, abs(cash - book.cash))
        if _off(cash, book.cash):
            found["cash_walk"].append({"date": day, "walked": str(cash), "engine": str(book.cash)})
        held: Counter[str] = Counter()
        for line in book.held:
            held[line.instrument_id] += line.qty
        wanted = {k: v for k, v in shares.items() if v}
        if wanted != {k: v for k, v in held.items() if v}:
            keys = sorted(set(wanted) | set(held))
            found["holdings"].append(
                {
                    "date": day,
                    "from_fills": {
                        k: wanted.get(k, 0) for k in keys if wanted.get(k, 0) != held[k]
                    },
                    "engine": {k: held[k] for k in keys if wanted.get(k, 0) != held[k]},
                }
            )
        value = sum((h.qty * h.mark for h in book.held), _ZERO) + book.sleeve_value
        if _off(value, book.position_value):
            found["position_value"].append(
                {"date": day, "marked": str(value), "engine": str(book.position_value)}
            )
        if _off(book.cash + book.position_value, book.equity):
            found["equity_identity"].append(
                {"date": day, "cash_plus_held": str(book.cash + value), "equity": str(book.equity)}
            )
    left = {k: v for k, v in shares.items() if v}
    if left:
        found["holdings"].append({"date": "after_last_session", "from_fills": left})
    summary = [{"worst_cash_diff": str(worst)}]
    return summary, found


def _trades(
    trades: Sequence[TradeRow], fills: Sequence[FillRow], dividends: Decimal
) -> dict[str, list[dict[str, Any]]]:
    """Each trade's P&L from its legs and entry, the instruments' P&L from their fills, and the
    trades' dividends against those credited."""
    found: dict[str, list[dict[str, Any]]] = {
        "trade_pnl": [],
        "instrument_pnl": [],
        "dividends": [],
    }
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
        flows[fill.instrument_id] += _flow(fill)
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
        found["dividends"].append({"trades": str(paid), "credited": str(dividends)})
    return found


def reconcile(
    start: Decimal,
    books: Sequence[SessionBook],
    fills: Sequence[FillRow],
    trades: Sequence[TradeRow],
) -> dict[str, Any]:
    """The ledger report of a run (the module docstring): `status` `pass` or `fail`, what was
    checked, and a finding per kind of mismatch."""
    summary, found = _sessions(start, books, fills)
    dividends = sum((b.dividends for b in books), _ZERO)
    found |= _trades(trades, fills, dividends)
    findings = [_finding(code, items) for code, items in found.items() if items]
    return {
        "status": FAIL if findings else PASS,
        "tolerance_inr": str(TOLERANCE),
        "sessions": len(books),
        "fills": len(fills),
        "trades": len(trades),
        "dividends": str(dividends),
        "overlay_flow": str(sum((b.overlay_flow for b in books), _ZERO)),
        **summary[0],
        "findings": findings,
    }

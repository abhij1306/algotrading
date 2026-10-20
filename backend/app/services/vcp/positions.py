"""VCP open positions, manual close/stop edits, the trading halt and the status summary.

Live prices come through a `QuoteSource`. An unknown price stays None all the way to the
response (`price_status` "PRICE_UNAVAILABLE", P&L fields None); it is never read as zero
or as the entry price. Money math runs in Decimal and is rounded half-up to paise.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from ...database import db_session_scope
from ...models import StrategyPosition, VCPScanResult
from ...utils.clock import SYSTEM_CLOCK, Clock
from ...utils.market_hours import IST
from ...utils.runtime_metrics import runtime_metrics
from .config import (
    DEFAULT_UNIVERSE,
    HALT_REASON_KEY,
    HALTED_AT_KEY,
    LAST_SCAN_UNIVERSE_KEY,
    TRADING_HALTED_KEY,
    get_config,
    last_scan_date,
    last_scan_run,
    set_config,
)
from .quotes import QuoteSource
from .universe import is_stored_universe, normalize_universe


def _latest_scan_date(db: Session, code: str) -> date | None:
    """The scan date of the universe's newest completed scan, empty scans included.

    Scans stored before run markers fall back to the newest stored row, then to the
    global last-scan date when that scan was of this universe.
    """
    run = last_scan_run(db, code)
    if run is not None:
        return run.scan_date
    stored = db.query(func.max(VCPScanResult.scan_date)).filter(is_stored_universe(code)).scalar()
    if stored is not None:
        return stored
    if normalize_universe(get_config(db, LAST_SCAN_UNIVERSE_KEY, "")) == code:
        return last_scan_date(db)
    return None


OPEN_STATUSES = ("OPEN", "OPEN_PARTIAL")
MIN_RISK_DISTANCE = Decimal("0.01")
PAISE = Decimal("0.01")

# `price_status` values: where the last price sits against the stop and the 2R target.
PRICE_OPEN = "OPEN"
PRICE_STOP_HIT = "STOP_HIT"
PRICE_TARGET_HIT = "TARGET_HIT"
PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"

SessionScope = Callable[[], AbstractContextManager[Session]]


def _dec(value: float | int) -> Decimal:
    return Decimal(str(value))


def _paise(value: Decimal) -> float:
    return float(value.quantize(PAISE, rounding=ROUND_HALF_UP))


def price_status(ltp: Decimal | None, stop: Decimal, target: Decimal) -> str:
    """Where `ltp` sits: at or below the stop, at or above the 2R target, between, or unknown."""
    if ltp is None:
        return PRICE_UNAVAILABLE
    if ltp <= stop:
        return PRICE_STOP_HIT
    if ltp >= target:
        return PRICE_TARGET_HIT
    return PRICE_OPEN


def position_view(row: StrategyPosition, ltp: Decimal | None) -> dict[str, Any]:
    """One open position priced at `ltp`; P&L fields are None when the price is unknown."""
    entry = _dec(row.entry_price)
    stop = _dec(row.stop_price)
    shares = int(row.shares_remaining)
    view: dict[str, Any] = {
        "id": row.id,
        "symbol": row.symbol,
        "entry_date": row.entry_date.isoformat(),
        "entry_price": _paise(entry),
        "shares": shares,
        "stop_price": _paise(stop),
        "ltp": None,
        "unrealized_pnl": None,
        "unrealized_pnl_pct": None,
        "r_multiple": None,
        "two_r_status": "HIT" if row.two_r_hit else "NOT_HIT",
        "price_status": price_status(ltp, stop, _dec(row.two_r_price)),
        "status": row.status,
    }
    if ltp is None:
        return view
    unrealized = (ltp - entry) * shares
    risk_distance = max(MIN_RISK_DISTANCE, entry - stop)
    view["ltp"] = _paise(ltp)
    view["unrealized_pnl"] = _paise(unrealized)
    view["unrealized_pnl_pct"] = _paise((ltp / entry - 1) * 100)
    view["r_multiple"] = _paise(unrealized / (risk_distance * max(1, shares)))
    return view


def _total_pnl(row: StrategyPosition, entry: Decimal, exit_price: Decimal) -> Decimal:
    """P&L on all shares: the remainder at `exit_price`, any already sold at the 2R hit price."""
    remaining = int(row.shares_remaining)
    sold = int(row.shares) - remaining
    pnl = (exit_price - entry) * remaining
    if sold > 0:
        sold_at = row.two_r_hit_price if row.two_r_hit_price is not None else row.two_r_price
        pnl += (_dec(sold_at) - entry) * sold
    return pnl


class PositionService:
    def __init__(
        self,
        quotes: QuoteSource,
        session_scope: SessionScope = db_session_scope,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._quotes = quotes
        self._session_scope = session_scope
        self._clock = clock

    def list_positions(self) -> dict[str, Any]:
        with self._session_scope() as db:
            rows = (
                db.query(StrategyPosition)
                .filter(StrategyPosition.status.in_(OPEN_STATUSES))
                .order_by(StrategyPosition.entry_date.desc(), StrategyPosition.symbol.asc())
                .all()
            )
            prices = self._quotes.get_last_prices([row.symbol for row in rows]) if rows else {}
            return {"positions": [position_view(row, prices.get(row.symbol)) for row in rows]}

    def close_position(self, position_id: int) -> dict[str, Any]:
        """Close at the last price; with no price, close at entry and record the fallback.

        `pnl_inr` covers every share: shares already sold at 2R plus the remainder at exit.
        A position that is no longer open is refused, so a repeat close cannot overwrite P&L.
        """
        with self._session_scope() as db:
            row = db.query(StrategyPosition).filter(StrategyPosition.id == position_id).first()
            if row is None:
                raise ValueError("Position not found")
            if row.status not in OPEN_STATUSES:
                raise ValueError("Position is not open")
            entry = _dec(row.entry_price)
            ltp = self._quotes.get_last_prices([row.symbol]).get(row.symbol)
            if ltp is None:
                exit_price = entry
                quote_source = "entry_fallback"
                row.notes = (
                    f"{(row.notes + ' | ') if row.notes else ''}"
                    f"manual_close_quote_fallback symbol={row.symbol} ltp=unavailable"
                )
            else:
                exit_price = ltp
                quote_source = "ltp"
            row.exit_date = self._clock.now().astimezone(IST).date()
            row.exit_price = float(exit_price)
            row.exit_reason = "MANUALLY_CLOSED"
            row.status = "MANUALLY_CLOSED"
            pnl = _total_pnl(row, entry, exit_price)
            row.pnl_inr = _paise(pnl)
            row.pnl_pct = _paise(pnl / (entry * max(1, int(row.shares))) * 100)
            row.shares_remaining = 0
            db.commit()
            return {
                "status": "ok",
                "position_id": position_id,
                "exit_price": _paise(exit_price),
                "quote_source": quote_source,
                "ltp_missing": quote_source != "ltp",
            }

    def update_stop(self, position_id: int, stop_price: float) -> dict[str, Any]:
        with self._session_scope() as db:
            row = db.query(StrategyPosition).filter(StrategyPosition.id == position_id).first()
            if row is None:
                raise ValueError("Position not found")
            row.stop_price = float(stop_price)
            db.commit()
            db.refresh(row)
            return {
                "status": "ok",
                "position_id": position_id,
                "stop_price": round(row.stop_price, 2),
            }

    def halt(self, reason: str | None = None) -> None:
        with self._session_scope() as db:
            set_config(db, TRADING_HALTED_KEY, "true")
            set_config(db, HALT_REASON_KEY, reason or "")
            set_config(db, HALTED_AT_KEY, self._clock.now().isoformat())

    def resume(self) -> None:
        with self._session_scope() as db:
            set_config(db, TRADING_HALTED_KEY, "false")
            set_config(db, HALT_REASON_KEY, "")

    def get_status(
        self, universe: str | None, regime: Callable[[], dict[str, Any]]
    ) -> dict[str, Any]:
        """Halt state, latest scan, queue and open-position counts; `regime` serializes today's."""
        with self._session_scope() as db:
            universe_filter = normalize_universe(universe or DEFAULT_UNIVERSE)
            latest_scan = _latest_scan_date(db, universe_filter)
            pending_entries = (
                db.query(VCPScanResult)
                .filter(
                    is_stored_universe(universe_filter),
                    VCPScanResult.signal_status == "PENDING_ENTRY",
                )
                .count()
            )
            open_positions = (
                db.query(StrategyPosition)
                .filter(StrategyPosition.status.in_(OPEN_STATUSES))
                .count()
            )
            halted = get_config(db, TRADING_HALTED_KEY, "false").lower() == "true"
            return {
                "universe": universe_filter,
                "halted": halted,
                "halt_reason": get_config(db, HALT_REASON_KEY, ""),
                "halted_at": get_config(db, HALTED_AT_KEY, ""),
                "latest_scan_date": latest_scan.isoformat() if latest_scan else None,
                "pending_entries": pending_entries,
                "open_positions": open_positions,
                "scheduler": {
                    "status": "manual_phase1",
                    "next_actions": [
                        "scan_run_on_demand",
                        "queued_entries_manual",
                        "reconciliation_placeholder",
                    ],
                },
                "regime": regime(),
                "metrics": runtime_metrics.snapshot(prefix="strategy.vcp.", strip_prefix=True),
            }

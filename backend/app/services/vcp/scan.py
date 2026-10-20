"""VCP universe scan: members on the scan date, pattern detection, sizing, ranking, storage.

The scan is point in time. Members come from `universe.constituents_for_date` on the scan
date, history is loaded up to the scan date only, and a symbol is scanned only when its
last bar is the scan date, so later prices are never read.

`ScanService` reads equity history through the `EquityHistory` port and the market
regime through `RegimeSource`; `scan_symbols` touches neither the database nor the network.
"""

from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol

import pandas as pd
from sqlalchemy import func

from ...database import db_session_scope
from ...models import VCPScanResult
from ...utils.clock import SYSTEM_CLOCK, Clock
from ...utils.runtime_metrics import runtime_metrics
from . import pattern, universe
from .config import (
    DEFAULT_PARAMS,
    DEFAULT_UNIVERSE,
    LAST_SCAN_DATE_KEY,
    LAST_SCAN_RESULT_COUNT_KEY,
    LAST_SCAN_UNIVERSE_KEY,
    VCPParams,
    configured_capital,
    last_scan_date,
    set_config,
)
from .pattern import MIN_HISTORY_BARS, STOP_OFFSET_PCT
from .positions import SessionScope
from .regime import RegimeSnapshot, serialize_regime

logger = logging.getLogger(__name__)

# Log message for a signal dropped because its stop leaves no tradable risk.
INVALID_STOP_DISTANCE = "Invalid stop distance"
UNKNOWN_SECTOR = "Unknown"
SCAN_LOOKBACK_DAYS = 420
DETAIL_LOOKBACK_DAYS = 260
DETAIL_CHART_BARS = 160
GRADE_RANK = {"A": 0, "B": 1, "C": 2}
SIGNAL = "SIGNAL"
PENDING_ENTRY = "PENDING_ENTRY"

# Signal fields copied as-is onto a stored VCPScanResult row.
_RECORD_FIELDS = (
    "symbol",
    "company_name",
    "sector",
    "grade",
    "rs_rating",
    "stage2_conditions_met",
    "contraction_count",
    "contraction_depths",
    "final_contraction_depth",
    "volume_dry_up_pct",
    "pivot_high",
    "stop_level",
    "stop_pct",
    "days_in_base",
    "is_breakout",
    "breakout_price",
    "breakout_volume_mult",
    "close_position_in_range",
    "overhead_clear",
    "regime",
    "metadata_json",
)


class EquityHistory(Protocol):
    def load_equity_history(
        self,
        symbols: list[str],
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        """Daily bars for `symbols` between the dates, inclusive."""
        ...

    def latest_equity_date(self) -> date | None:
        """The last date with equity bars, or None when there are none."""
        ...


class RegimeSource(Protocol):
    def get_regime(self, as_of: date | None = None) -> RegimeSnapshot: ...


@dataclass
class ScanOutcome:
    regime: RegimeSnapshot
    symbols_scanned: int
    signals: list[dict[str, Any]]


def signal_shares(
    symbol: str,
    capital: float,
    risk_pct: float,
    pivot_high: float,
    stop_level: float,
    params: VCPParams = DEFAULT_PARAMS,
) -> int | None:
    """Shares risking `risk_pct` of capital, capped at `max_position_cap`; None if zero."""
    risk_distance = pivot_high - stop_level
    shares = int(math.floor((capital * risk_pct) / risk_distance))
    if shares > 0:
        shares = min(shares, int(math.floor((capital * params.max_position_cap) / pivot_high)))
    if shares > 0:
        return shares
    logger.warning(
        INVALID_STOP_DISTANCE,
        extra={
            "symbol": symbol,
            "capital": round(capital, 2),
            "risk_pct": risk_pct,
            "pivot_high": round(pivot_high, 2),
            "stop_level": round(stop_level, 2),
            "risk_distance": round(risk_distance, 4),
            "shares": shares,
        },
    )
    return None


def build_signal(
    frame: pd.DataFrame,
    rs_rating: int,
    company_meta: dict[str, str],
    regime: RegimeSnapshot,
    capital: float,
    params: VCPParams = DEFAULT_PARAMS,
) -> dict[str, Any] | None:
    """The sized signal for one symbol's bars (last bar = scan date), or None if no setup."""
    setup = pattern.analyze_pattern(frame, rs_rating)
    if setup is None:
        return None

    symbol = str(frame["symbol"].iloc[-1])
    pivot_high = setup.pivot_high
    stop_level = setup.stop_level
    risk_distance = pivot_high - stop_level
    if risk_distance <= 0:
        logger.warning(
            INVALID_STOP_DISTANCE,
            extra={
                "symbol": symbol,
                "pivot_high": round(pivot_high, 2),
                "final_contraction_low": round(setup.final_contraction_low, 2),
                "stop_level": round(stop_level, 2),
                "stop_offset_pct": STOP_OFFSET_PCT,
            },
        )
        return None
    stop_pct = (risk_distance / pivot_high) * 100 if pivot_high > 0 else 0.0

    shares = signal_shares(symbol, capital, regime.risk_pct, pivot_high, stop_level, params)
    if shares is None:
        return None
    depths = setup.contraction_depths
    breakout = setup.breakout
    position_value = shares * pivot_high
    two_r_target = pivot_high + (2.0 * risk_distance)
    return {
        "symbol": symbol,
        "company_name": company_meta.get("name") or symbol,
        "sector": company_meta.get("sector") or UNKNOWN_SECTOR,
        "grade": setup.grade,
        "rs_rating": int(rs_rating),
        "stage2_conditions_met": int(setup.stage2.conditions_met),
        "contraction_count": len(setup.contractions),
        "contraction_depths": depths,
        "final_contraction_depth": float(depths[-1]),
        "volume_dry_up_pct": round(setup.volume.dry_up_pct, 2),
        "pivot_high": round(pivot_high, 2),
        "stop_level": round(stop_level, 2),
        "stop_pct": round(stop_pct, 2),
        "days_in_base": int(setup.days_in_base),
        "is_breakout": breakout.is_breakout,
        "breakout_price": round(setup.latest_close, 2) if breakout.is_breakout else None,
        "breakout_volume_mult": round(breakout.volume_mult, 2),
        "close_position_in_range": round(breakout.close_position, 4),
        "overhead_clear": True,
        "regime": regime.regime,
        "signal_status": SIGNAL,
        "planned_entry": round(pivot_high, 2),
        "planned_shares": shares,
        "planned_position_value": round(position_value, 2),
        "capital_risk": round(shares * max(0.0, pivot_high - stop_level), 2),
        "two_r_target": round(two_r_target, 2),
        "metadata_json": {
            "stage2": setup.stage2.averages(),
            "volume_slope": round(setup.volume.slope, 4),
            "base_depth_pct": round(setup.base_depth_pct, 2),
            "final_contraction_low": round(setup.final_contraction_low, 2),
            "stop_offset_pct": STOP_OFFSET_PCT,
            "risk_pct": regime.risk_pct,
        },
    }


def eligible_frames(history: pd.DataFrame, scan_date: date) -> dict[str, pd.DataFrame]:
    """Per-symbol bars with enough history whose last bar is exactly `scan_date`.

    A symbol with no bar on the scan date (stale) or a bar after it (later data) is skipped.
    """
    return {
        symbol: frame.copy()
        for symbol, frame in history.groupby("symbol", sort=True)
        if len(frame) >= MIN_HISTORY_BARS and frame["date"].max() == scan_date
    }


def _rank_key(signal: dict[str, Any]) -> tuple[int, int, float, int]:
    return (
        GRADE_RANK.get(signal["grade"], len(GRADE_RANK)),
        0 if signal["is_breakout"] else 1,
        signal["final_contraction_depth"],
        -signal["rs_rating"],
    )


def rank_signals(signals: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Grade A first, then breakouts, then the tighter final contraction, then higher RS."""
    return sorted(signals, key=_rank_key)


def serialize_scan_row(row: VCPScanResult, params: VCPParams = DEFAULT_PARAMS) -> dict[str, Any]:
    """A stored signal in the API shape, sized on `params.default_capital`."""
    metadata_json = row.metadata_json or {}
    pivot_high = float(row.pivot_high)
    risk_distance = max(0.01, pivot_high - float(row.stop_level))
    risk_pct = float(metadata_json.get("risk_pct", params.risk_pct_bull))
    capital = params.default_capital
    planned_shares = int(math.floor((capital * risk_pct) / risk_distance))
    planned_shares = max(
        0,
        min(planned_shares, int(math.floor((capital * params.max_position_cap) / pivot_high))),
    )
    return {
        "id": row.id,
        "scan_date": row.scan_date.isoformat(),
        "symbol": row.symbol,
        "company_name": row.company_name or row.symbol,
        "sector": row.sector or UNKNOWN_SECTOR,
        "grade": row.grade,
        "rs_rating": row.rs_rating,
        "stage2_conditions_met": row.stage2_conditions_met,
        "contraction_count": row.contraction_count,
        "contraction_depths": row.contraction_depths,
        "final_contraction_depth": row.final_contraction_depth,
        "volume_dry_up_pct": row.volume_dry_up_pct,
        "pivot_high": row.pivot_high,
        "stop_level": row.stop_level,
        "stop_pct": row.stop_pct,
        "days_in_base": row.days_in_base,
        "is_breakout": row.is_breakout,
        "breakout_price": row.breakout_price,
        "breakout_volume_mult": row.breakout_volume_mult,
        "close_position_in_range": row.close_position_in_range,
        "overhead_clear": row.overhead_clear,
        "regime": row.regime,
        "signal_status": row.signal_status,
        "planned_entry": row.pivot_high,
        "planned_shares": planned_shares,
        "planned_position_value": round(planned_shares * pivot_high, 2),
        "capital_risk": round(planned_shares * risk_distance, 2),
        "two_r_target": round(pivot_high + (2.0 * risk_distance), 2),
        "metadata": metadata_json,
    }


def _rounded(value: object) -> float | None:
    return None if pd.isna(value) else round(float(value), 2)


def chart_rows(history: pd.DataFrame) -> list[dict[str, Any]]:
    """The last bars with the 21-EMA and 50/150/200-day averages; None until warmed up."""
    history = history.sort_values("date").tail(DETAIL_CHART_BARS).copy()
    history["ema_21"] = history["close_ref"].ewm(span=21, adjust=False).mean()
    for window in (50, 150, 200):
        history[f"ma_{window}"] = history["close_ref"].rolling(window).mean()
    return [
        {
            "date": item["date"].isoformat(),
            "open": round(float(item["open"]), 2),
            "high": round(float(item["high"]), 2),
            "low": round(float(item["low"]), 2),
            "close": round(float(item["close"]), 2),
            "volume": int(item["volume"]),
            "ema_21": _rounded(item["ema_21"]),
            "ma_50": _rounded(item["ma_50"]),
            "ma_150": _rounded(item["ma_150"]),
            "ma_200": _rounded(item["ma_200"]),
        }
        for item in history.to_dict("records")
    ]


class ScanService:
    def __init__(
        self,
        history: EquityHistory,
        regime: RegimeSource,
        session_scope: SessionScope = db_session_scope,
        clock: Clock = SYSTEM_CLOCK,
        params: VCPParams = DEFAULT_PARAMS,
    ) -> None:
        self._history = history
        self._regime = regime
        self._session_scope = session_scope
        self._clock = clock
        self._params = params

    def as_of_date(self, requested: date | None = None) -> date:
        """`requested`, or the last date with equity history."""
        if requested is not None:
            return requested
        latest = self._history.latest_equity_date()
        if latest is None:
            raise ValueError("Canonical daily equity history is empty")
        return latest

    def scan_symbols(
        self,
        symbols: list[str],
        scan_date: date,
        capital: float,
        company_meta: Callable[[list[str]], dict[str, dict[str, str]]],
    ) -> ScanOutcome:
        """Signals among `symbols` on `scan_date`, in symbol order, sized on `capital`."""
        history = self._history.load_equity_history(
            symbols=symbols,
            start_date=(pd.Timestamp(scan_date) - pd.Timedelta(days=SCAN_LOOKBACK_DAYS)).date(),
            end_date=scan_date,
        )
        frames = eligible_frames(history, scan_date)
        meta = company_meta(list(frames))
        rs_map = pattern.compute_rs_map(frames)
        regime = self._regime.get_regime(scan_date)
        signals = []
        for symbol, frame in frames.items():
            signal = build_signal(
                frame,
                rs_map.get(symbol, 0),
                meta.get(symbol, {"name": symbol, "sector": UNKNOWN_SECTOR}),
                regime,
                capital,
                self._params,
            )
            if signal is not None:
                signals.append(signal)
        return ScanOutcome(regime=regime, symbols_scanned=len(frames), signals=signals)

    def run_scan(
        self, universe_code: str = DEFAULT_UNIVERSE, scan_date: date | None = None
    ) -> dict[str, Any]:
        """Scan the members of `universe_code` on `scan_date`, store and return the signals."""
        started = time.perf_counter()
        scan_date = self.as_of_date(scan_date)
        scan_id = str(uuid.uuid4())
        scan_timestamp = self._clock.now()
        code = universe_code.upper()
        with self._session_scope() as db:
            symbols, constituent_mode = universe.constituents_for_date(db, universe_code, scan_date)
            if not symbols:
                raise ValueError(
                    f"No constituents found for {universe_code} on {scan_date.isoformat()}"
                )
            outcome = self.scan_symbols(
                symbols,
                scan_date,
                configured_capital(db, self._params),
                lambda found: universe.company_meta(db, found),
            )
            persisted = [
                VCPScanResult(
                    scan_id=scan_id,
                    scan_date=scan_date,
                    scan_timestamp=scan_timestamp,
                    universe=code,
                    signal_status=SIGNAL,
                    **{name: signal[name] for name in _RECORD_FIELDS},
                )
                for signal in outcome.signals
            ]
            db.add_all(persisted)
            db.commit()
            for record in persisted:
                db.refresh(record)

            set_config(db, LAST_SCAN_DATE_KEY, scan_date.isoformat())
            set_config(db, LAST_SCAN_UNIVERSE_KEY, code)
            set_config(db, LAST_SCAN_RESULT_COUNT_KEY, str(len(persisted)))

            results = rank_signals(serialize_scan_row(record, self._params) for record in persisted)
            duration_ms = (time.perf_counter() - started) * 1000.0
            runtime_metrics.observe_ms("strategy.vcp.scan_ms", duration_ms)
            runtime_metrics.increment("strategy.vcp.scan_runs")
            logger.info(
                "VCP scan complete universe=%s scan_id=%s eligible_symbols=%s results=%s "
                "duration_ms=%.2f",
                code,
                scan_id,
                outcome.symbols_scanned,
                len(results),
                duration_ms,
            )
            return {
                "scan_id": scan_id,
                "scan_timestamp": scan_timestamp.isoformat(),
                "scan_date": scan_date.isoformat(),
                "universe": code,
                "regime": serialize_regime(outcome.regime),
                "constituent_mode": constituent_mode,
                "total_symbols_scanned": outcome.symbols_scanned,
                "results": results,
            }

    def _empty_scan(self, code: str) -> dict[str, Any]:
        return {
            "scan_date": None,
            "universe": code,
            "results": [],
            "regime": serialize_regime(self._regime.get_regime()),
        }

    def get_latest_scan(
        self, universe_code: str = DEFAULT_UNIVERSE, show_all: bool = False
    ) -> dict[str, Any]:
        """The newest stored scan for the universe; grades A and B only unless `show_all`."""
        code = universe_code.upper()
        with self._session_scope() as db:
            latest: datetime | date | None = (
                db.query(func.max(VCPScanResult.scan_timestamp))
                .filter(VCPScanResult.universe == code)
                .scalar()
            )
            if latest is None:
                latest = last_scan_date(db)
            if latest is None:
                return self._empty_scan(code)
            latest_row = (
                db.query(VCPScanResult)
                .filter(VCPScanResult.universe == code)
                .order_by(
                    VCPScanResult.scan_timestamp.desc(),
                    VCPScanResult.created_at.desc(),
                    VCPScanResult.id.desc(),
                )
                .first()
            )
            if latest_row is None:
                return self._empty_scan(code)
            rows = (
                db.query(VCPScanResult)
                .filter(
                    VCPScanResult.universe == code,
                    VCPScanResult.scan_id == latest_row.scan_id,
                )
                .order_by(
                    VCPScanResult.grade.asc(),
                    VCPScanResult.is_breakout.desc(),
                    VCPScanResult.rs_rating.desc(),
                )
                .all()
            )
            if not show_all:
                rows = [row for row in rows if row.grade in {"A", "B"}]
            return {
                "scan_id": latest_row.scan_id,
                "scan_timestamp": latest_row.scan_timestamp.isoformat()
                if latest_row.scan_timestamp
                else None,
                "scan_date": latest_row.scan_date.isoformat(),
                "universe": code,
                "regime": serialize_regime(self._regime.get_regime(latest_row.scan_date)),
                "results": [serialize_scan_row(row, self._params) for row in rows],
            }

    def get_signal_detail(self, symbol: str, scan_date: date | None = None) -> dict[str, Any]:
        """The newest stored signal for `symbol` (or the one on `scan_date`) with its chart."""
        with self._session_scope() as db:
            query = db.query(VCPScanResult).filter(VCPScanResult.symbol == symbol.upper())
            row = (
                query.order_by(
                    VCPScanResult.scan_timestamp.desc(),
                    VCPScanResult.created_at.desc(),
                    VCPScanResult.id.desc(),
                ).first()
                if scan_date is None
                else query.filter(VCPScanResult.scan_date == scan_date)
                .order_by(VCPScanResult.scan_timestamp.desc())
                .first()
            )
            if row is None:
                raise ValueError(f"No VCP signal found for {symbol}")

            history = self._history.load_equity_history(
                symbols=[row.symbol],
                start_date=(
                    pd.Timestamp(row.scan_date) - pd.Timedelta(days=DETAIL_LOOKBACK_DAYS)
                ).date(),
                end_date=row.scan_date,
            )
            return {
                "signal": serialize_scan_row(row, self._params),
                "chart": chart_rows(history),
            }

    def queue_signal(self, signal_id: int) -> dict[str, Any]:
        return self._set_status(signal_id, PENDING_ENTRY)

    def cancel_signal(self, signal_id: int) -> dict[str, Any]:
        return self._set_status(signal_id, SIGNAL)

    def _set_status(self, signal_id: int, status: str) -> dict[str, Any]:
        with self._session_scope() as db:
            row = db.query(VCPScanResult).filter(VCPScanResult.id == signal_id).first()
            if row is None:
                raise ValueError("Signal not found")
            row.signal_status = status
            db.commit()
            db.refresh(row)
            return serialize_scan_row(row, self._params)

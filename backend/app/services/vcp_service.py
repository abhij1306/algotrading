from __future__ import annotations

import logging
import math
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from statistics import mean
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..data_fetcher import fetch_fyers_quotes
from ..database import db_session_scope, get_db_session
from ..exceptions import DataUnavailableError
from ..models import (
    BacktestRun,
    Company,
    IndexConstituentHistory,
    IndexUniverseDefinition,
    StrategyPosition,
    SystemConfig,
    VCPScanResult,
)
from ..utils.runtime_metrics import runtime_metrics
from . import backtest_metrics as bt_metrics
from .history_service import daily_history_service

logger = logging.getLogger(__name__)

REGIME_DATA_UNAVAILABLE = "Market regime unavailable: NIFTY50 index history is missing"
DEFAULT_CAPITAL = 1_000_000.0
DEFAULT_UNIVERSE = "NIFTY500"
RISK_PCT_BULL = 0.01
RISK_PCT_BEAR = 0.005
MAX_POSITION_CAP = 0.20
STOP_OFFSET_PCT = 0.001
GAP_UP_MAX_PCT = 0.02
TRAIL_SLIPPAGE_PCT = 0.001
BREAKOUT_VOLUME_MULTIPLIER = 1.5
A_GRADE_BREAKOUT_VOLUME_MULTIPLIER = BREAKOUT_VOLUME_MULTIPLIER * (4.0 / 3.0)
# Log message for a signal or trade dropped because its stop leaves no tradable risk.
INVALID_STOP_DISTANCE = "Invalid stop distance"


@dataclass
class RegimeSnapshot:
    scan_date: date
    regime: str
    close: float
    ma_200: float
    risk_pct: float


@dataclass
class _VCPBacktestState:
    """Inputs and running results of one VCPService.run_backtest call."""

    capital: float
    symbol_frames: dict[str, pd.DataFrame]
    trading_dates: list[date]
    meta: dict[str, dict[str, str]]
    benchmark: pd.DataFrame
    benchmark_start: float
    members_on: Callable[[date], tuple[list[str], str]]
    trades: list[dict[str, Any]] = field(default_factory=list)
    equity_curve: list[dict[str, Any]] = field(default_factory=list)
    benchmark_curve: list[dict[str, Any]] = field(default_factory=list)
    modes: set[str] = field(default_factory=set)


class VCPService:
    def _load_symbol_history(
        self,
        symbols: list[str],
        start_date: date,
        end_date: date,
    ) -> pd.DataFrame:
        return daily_history_service.load_equity_history(
            symbols=symbols,
            start_date=start_date,
            end_date=end_date,
        )

    @staticmethod
    def _get_config(db: Session, key: str, default: str) -> str:
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        return row.value if row else default

    @staticmethod
    def _set_config(db: Session, key: str, value: str) -> None:
        row = db.query(SystemConfig).filter(SystemConfig.key == key).first()
        if row is None:
            row = SystemConfig(key=key, value=value)
            db.add(row)
        else:
            row.value = value
        db.commit()

    def _constituents_for_date(
        self, db: Session, universe: str, as_of: date
    ) -> tuple[list[str], str]:
        definition = (
            db.query(IndexUniverseDefinition)
            .filter(IndexUniverseDefinition.index_code == universe.upper().strip())
            .first()
        )
        if definition is None:
            return [], "unavailable"

        earliest = (
            db.query(func.min(IndexConstituentHistory.effective_from))
            .filter(IndexConstituentHistory.universe_id == definition.id)
            .scalar()
        )
        if earliest and as_of < earliest:
            rows = (
                db.query(IndexConstituentHistory.symbol)
                .filter(
                    IndexConstituentHistory.universe_id == definition.id,
                    IndexConstituentHistory.effective_to.is_(None),
                )
                .order_by(IndexConstituentHistory.symbol.asc())
                .all()
            )
            return [row[0] for row in rows], f"indicative_pre_{earliest.isoformat()}"

        rows = (
            db.query(IndexConstituentHistory.symbol)
            .filter(
                IndexConstituentHistory.universe_id == definition.id,
                IndexConstituentHistory.effective_from <= as_of,
                (
                    IndexConstituentHistory.effective_to.is_(None)
                    | (IndexConstituentHistory.effective_to >= as_of)
                ),
            )
            .order_by(IndexConstituentHistory.symbol.asc())
            .all()
        )
        symbols = [row[0] for row in rows]
        if symbols:
            return symbols, "bias_corrected"

        fallback_rows = (
            db.query(IndexConstituentHistory.symbol)
            .filter(
                IndexConstituentHistory.universe_id == definition.id,
                IndexConstituentHistory.effective_to.is_(None),
            )
            .order_by(IndexConstituentHistory.symbol.asc())
            .all()
        )
        return [row[0] for row in fallback_rows], "live_fallback"

    def _constituents_in_range(
        self, db: Session, universe: str, start_date: date, end_date: date
    ) -> list[str]:
        """Every symbol that was a member at any point in [start_date, end_date]."""
        symbols: set[str] = set()
        definition = (
            db.query(IndexUniverseDefinition)
            .filter(IndexUniverseDefinition.index_code == universe.upper().strip())
            .first()
        )
        if definition is not None:
            rows = (
                db.query(IndexConstituentHistory.symbol)
                .filter(
                    IndexConstituentHistory.universe_id == definition.id,
                    IndexConstituentHistory.effective_from <= end_date,
                    (
                        IndexConstituentHistory.effective_to.is_(None)
                        | (IndexConstituentHistory.effective_to >= start_date)
                    ),
                )
                .all()
            )
            symbols.update(row[0] for row in rows)
        return sorted(symbols)

    def _company_meta(self, db: Session, symbols: list[str]) -> dict[str, dict[str, str]]:
        if not symbols:
            return {}
        rows = db.query(Company).filter(Company.symbol.in_(symbols)).all()
        return {
            row.symbol: {"name": row.name or row.symbol, "sector": row.sector or "Unknown"}
            for row in rows
        }

    @staticmethod
    def _asof_date(requested: date | None = None) -> date:
        if requested is not None:
            return requested
        latest = daily_history_service.latest_equity_date()
        if latest is None:
            raise ValueError("Canonical daily equity history is empty")
        return latest

    def get_regime(self, as_of: date | None = None) -> RegimeSnapshot:
        scoped = daily_history_service.load_index_history("NIFTY50")
        if scoped.empty:
            logger.warning("Regime unavailable: NIFTY50 index history is missing")
            raise DataUnavailableError(REGIME_DATA_UNAVAILABLE)
        scan_date = as_of or scoped["date"].max()
        scoped = scoped[scoped["date"] <= scan_date].sort_values("date").tail(260).copy()
        if scoped.empty:
            logger.warning(
                "Regime unavailable: no NIFTY50 index history on or before %s", scan_date
            )
            raise DataUnavailableError(REGIME_DATA_UNAVAILABLE)
        scoped["ma_200"] = scoped["close"].rolling(200).mean()
        latest = scoped.iloc[-1]
        ma_200 = (
            float(latest["ma_200"]) if not pd.isna(latest["ma_200"]) else float(latest["close"])
        )
        close = float(latest["close"])
        bull = close >= ma_200
        return RegimeSnapshot(
            scan_date=scan_date,
            regime="BULL" if bull else "BEAR",
            close=round(close, 2),
            ma_200=round(ma_200, 2),
            risk_pct=RISK_PCT_BULL if bull else RISK_PCT_BEAR,
        )

    @staticmethod
    def _percentile_rating(series: pd.Series) -> pd.Series:
        ranked = series.rank(method="average", pct=True)
        return ((ranked * 99).round().clip(lower=0, upper=99)).astype(int)

    def _compute_rs_map(self, symbol_frames: dict[str, pd.DataFrame]) -> dict[str, int]:
        records: dict[str, float] = {}
        for symbol, frame in symbol_frames.items():
            closes = frame["close_ref"].reset_index(drop=True)
            if len(closes) < 253:
                continue
            records[symbol] = (
                (0.40 * float(closes.iloc[-1] / closes.iloc[-64] - 1.0))
                + (0.20 * float(closes.iloc[-1] / closes.iloc[-127] - 1.0))
                + (0.20 * float(closes.iloc[-1] / closes.iloc[-190] - 1.0))
                + (0.20 * float(closes.iloc[-1] / closes.iloc[-253] - 1.0))
            )
        if not records:
            return {}
        rating = self._percentile_rating(pd.Series(records))
        return {symbol: int(value) for symbol, value in rating.items()}

    @staticmethod
    def _stage2_metrics(frame: pd.DataFrame) -> tuple[int, dict[str, float]]:
        closes = frame["close_ref"].reset_index(drop=True)
        ma_50 = closes.rolling(50).mean()
        ma_150 = closes.rolling(150).mean()
        ma_200 = closes.rolling(200).mean()
        high_52 = closes.rolling(252).max()
        latest_index = len(closes) - 1
        conditions = [
            closes.iloc[-1] > ma_150.iloc[-1] and closes.iloc[-1] > ma_200.iloc[-1],
            ma_150.iloc[-1] > ma_200.iloc[-1],
            bool(latest_index >= 20 and ma_200.iloc[-1] > ma_200.iloc[-21]),
            closes.iloc[-1] >= (0.75 * high_52.iloc[-1]),
            ma_50.iloc[-1] > ma_150.iloc[-1] > ma_200.iloc[-1],
        ]
        return sum(bool(item) for item in conditions), {
            "ma_50": float(ma_50.iloc[-1]) if not pd.isna(ma_50.iloc[-1]) else 0.0,
            "ma_150": float(ma_150.iloc[-1]) if not pd.isna(ma_150.iloc[-1]) else 0.0,
            "ma_200": float(ma_200.iloc[-1]) if not pd.isna(ma_200.iloc[-1]) else 0.0,
            "high_52": float(high_52.iloc[-1]) if not pd.isna(high_52.iloc[-1]) else 0.0,
        }

    @staticmethod
    def _pivot_points(base_df: pd.DataFrame, window: int = 3) -> tuple[list[int], list[int]]:
        highs = base_df["high"].tolist()
        lows = base_df["low"].tolist()
        pivot_highs: list[int] = []
        pivot_lows: list[int] = []
        for idx in range(window, len(base_df) - window):
            hi = highs[idx]
            lo = lows[idx]
            if all(hi > value for value in highs[idx - window : idx]) and all(
                hi > value for value in highs[idx + 1 : idx + window + 1]
            ):
                pivot_highs.append(idx)
            if all(lo < value for value in lows[idx - window : idx]) and all(
                lo < value for value in lows[idx + 1 : idx + window + 1]
            ):
                pivot_lows.append(idx)
        return pivot_highs, pivot_lows

    @staticmethod
    def _has_no_overhead_resistance(frame: pd.DataFrame, pivot_high: float) -> bool:
        if pivot_high <= 0:
            return False
        prior = frame.tail(252).copy()
        upper = pivot_high * 1.03
        candidates = prior[(prior["close_ref"] > pivot_high) & (prior["close_ref"] <= upper)]
        if candidates.empty:
            return True
        # Use a sliding observed-level check with a 2% band around each candidate level.
        closes = prior["close_ref"].tolist()
        levels = sorted({round(value, 2) for value in candidates["close_ref"].tolist()})
        for level in levels:
            streak = 0
            for close in closes:
                if abs(close - level) / level <= 0.02:
                    streak += 1
                    if streak >= 5:
                        return False
                else:
                    streak = 0
        return True

    @staticmethod
    def _base_window(frame: pd.DataFrame) -> tuple[pd.DataFrame, float] | None:
        """(base from the 60-day high onward, base depth %), or None if not a valid base."""
        base_window = frame.tail(60).reset_index(drop=True)
        base_high_idx = int(base_window["close_ref"].idxmax())
        base_df = base_window.iloc[base_high_idx:].reset_index(drop=True)
        if len(base_df) < 15:
            return None

        base_high = float(base_df["close_ref"].iloc[0])
        base_low = float(base_df["close_ref"].min())
        base_depth = ((base_high - base_low) / base_high) * 100 if base_high > 0 else 100.0
        if base_depth > 30.0:
            return None
        return base_df, base_depth

    def _find_contractions(self, base_df: pd.DataFrame) -> list[dict[str, Any]]:
        """Successive pivot-high to pivot-low pullbacks in the base (at most four)."""
        pivot_highs, pivot_lows = self._pivot_points(base_df, window=3)
        current_high_idx = 0
        current_high_price = float(base_df["close_ref"].iloc[0])
        contractions: list[dict[str, Any]] = []
        while True:
            low_candidates = [idx for idx in pivot_lows if idx > current_high_idx]
            if not low_candidates:
                break
            low_idx = low_candidates[0]
            low_price = float(base_df["low"].iloc[low_idx])
            depth = (
                ((current_high_price - low_price) / current_high_price) * 100
                if current_high_price > 0
                else 100.0
            )
            contractions.append(
                {
                    "high_idx": current_high_idx,
                    "high_price": current_high_price,
                    "low_idx": low_idx,
                    "low_price": low_price,
                    "depth_pct": depth,
                }
            )
            next_highs = [idx for idx in pivot_highs if idx > low_idx]
            if not next_highs or len(contractions) >= 4:
                break
            current_high_idx = next_highs[0]
            current_high_price = float(base_df["high"].iloc[current_high_idx])
        return contractions

    @staticmethod
    def _contractions_tighten(depths: list[float]) -> bool:
        """2-4 contractions, first <= 25%, second <= 15%, last <= 10%, each shallower."""
        if not (2 <= len(depths) <= 4):
            return False
        if depths[0] > 25.0 or depths[-1] > 10.0:
            return False
        if len(depths) > 1 and depths[1] > 15.0:
            return False
        return not any(depths[idx] >= depths[idx - 1] for idx in range(1, len(depths)))

    @staticmethod
    def _volume_dry_up(
        frame: pd.DataFrame, base_df: pd.DataFrame, final_contraction: dict[str, Any]
    ) -> tuple[float, float, float] | None:
        """(50-day average volume, final-contraction volume as % of it, base volume slope).

        None unless volume dried up to at most 40% and trends down across the base.
        """
        final_df = base_df.iloc[
            final_contraction["high_idx"] : final_contraction["low_idx"] + 1
        ].copy()
        if final_df.empty:
            return None

        avg_volume_50 = float(frame["volume"].tail(50).mean())
        final_avg_volume = float(final_df["volume"].mean())
        volume_dry_up_pct = (
            ((final_avg_volume / avg_volume_50) * 100.0) if avg_volume_50 > 0 else 1000.0
        )
        if volume_dry_up_pct > 40.0:
            return None

        slope = np.polyfit(np.arange(len(base_df)), base_df["volume"].to_numpy(dtype=float), 1)[0]
        if slope >= 0:
            return None
        return avg_volume_50, volume_dry_up_pct, slope

    @staticmethod
    def _signal_shares(
        symbol: str, capital: float, risk_pct: float, pivot_high: float, stop_level: float
    ) -> int | None:
        """Shares risking `risk_pct` of capital, capped at MAX_POSITION_CAP; None if zero."""
        risk_distance = pivot_high - stop_level
        shares = int(math.floor((capital * risk_pct) / risk_distance))
        if shares > 0:
            shares = min(shares, int(math.floor((capital * MAX_POSITION_CAP) / pivot_high)))
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

    @staticmethod
    def _breakout_state(
        latest: pd.Series, pivot_high: float, avg_volume_50: float
    ) -> tuple[bool, float, float]:
        """(is breakout, breakout volume multiple, close position in the day's range)."""
        breakout_volume_mult = float(latest["volume"] / avg_volume_50) if avg_volume_50 > 0 else 0.0
        day_range = float(latest["high"] - latest["low"])
        close_position = (
            ((float(latest["close"]) - float(latest["low"])) / day_range) if day_range > 0 else 0.0
        )
        is_breakout = bool(
            float(latest["close"]) > pivot_high
            and breakout_volume_mult >= BREAKOUT_VOLUME_MULTIPLIER
            and close_position >= 0.75
        )
        return is_breakout, breakout_volume_mult, close_position

    @staticmethod
    def _grade(
        rs_rating: int,
        final_depth: float,
        volume_dry_up_pct: float,
        is_breakout: bool,
        breakout_volume_mult: float,
    ) -> str:
        if (
            rs_rating >= 90
            and final_depth <= 5.0
            and volume_dry_up_pct <= 40.0
            and ((not is_breakout) or breakout_volume_mult >= A_GRADE_BREAKOUT_VOLUME_MULTIPLIER)
        ):
            return "A"
        if rs_rating >= 80 and final_depth <= 10.0 and volume_dry_up_pct <= 60.0:
            return "B"
        return "C"

    def _analyze_pattern(
        self,
        frame: pd.DataFrame,
        rs_rating: int,
        company_meta: dict[str, str],
        regime: RegimeSnapshot,
        capital: float,
    ) -> dict[str, Any] | None:
        if len(frame) < 260:
            return None

        stage2_count, stage2_values = self._stage2_metrics(frame)
        if stage2_count < 5:
            return None

        base = self._base_window(frame)
        if base is None:
            return None
        base_df, base_depth = base

        contractions = self._find_contractions(base_df)
        depths = [round(item["depth_pct"], 2) for item in contractions]
        if not self._contractions_tighten(depths):
            return None

        final_contraction = contractions[-1]
        volume_profile = self._volume_dry_up(frame, base_df, final_contraction)
        if volume_profile is None:
            return None
        avg_volume_50, volume_dry_up_pct, slope = volume_profile

        latest = frame.iloc[-1]
        pivot_high = float(final_contraction["high_price"])
        final_contraction_low = float(final_contraction["low_price"])
        stop_level = round(final_contraction_low * (1.0 - STOP_OFFSET_PCT), 2)
        is_breakout, breakout_volume_mult, close_position = self._breakout_state(
            latest, pivot_high, avg_volume_50
        )

        if rs_rating < 80 or not self._has_no_overhead_resistance(frame, pivot_high):
            return None

        risk_distance = pivot_high - stop_level
        if risk_distance <= 0:
            logger.warning(
                INVALID_STOP_DISTANCE,
                extra={
                    "symbol": str(frame["symbol"].iloc[-1]),
                    "pivot_high": round(pivot_high, 2),
                    "final_contraction_low": round(final_contraction_low, 2),
                    "stop_level": round(stop_level, 2),
                    "stop_offset_pct": STOP_OFFSET_PCT,
                },
            )
            return None
        stop_pct = (risk_distance / pivot_high) * 100 if pivot_high > 0 else 0.0
        grade = self._grade(
            rs_rating, depths[-1], volume_dry_up_pct, is_breakout, breakout_volume_mult
        )

        shares = self._signal_shares(
            str(frame["symbol"].iloc[-1]), capital, regime.risk_pct, pivot_high, stop_level
        )
        if shares is None:
            return None
        position_value = shares * pivot_high
        two_r_target = pivot_high + (2.0 * risk_distance)
        return {
            "symbol": str(frame["symbol"].iloc[-1]),
            "company_name": company_meta.get("name") or str(frame["symbol"].iloc[-1]),
            "sector": company_meta.get("sector") or "Unknown",
            "grade": grade,
            "rs_rating": int(rs_rating),
            "stage2_conditions_met": int(stage2_count),
            "contraction_count": len(contractions),
            "contraction_depths": depths,
            "final_contraction_depth": float(depths[-1]),
            "volume_dry_up_pct": round(volume_dry_up_pct, 2),
            "pivot_high": round(pivot_high, 2),
            "stop_level": round(stop_level, 2),
            "stop_pct": round(stop_pct, 2),
            "days_in_base": int(len(base_df)),
            "is_breakout": is_breakout,
            "breakout_price": round(float(latest["close"]), 2) if is_breakout else None,
            "breakout_volume_mult": round(breakout_volume_mult, 2),
            "close_position_in_range": round(close_position, 4),
            "overhead_clear": True,
            "regime": regime.regime,
            "signal_status": "SIGNAL",
            "planned_entry": round(pivot_high, 2),
            "planned_shares": shares,
            "planned_position_value": round(position_value, 2),
            "capital_risk": round(shares * max(0.0, pivot_high - stop_level), 2),
            "two_r_target": round(two_r_target, 2),
            "metadata_json": {
                "stage2": stage2_values,
                "volume_slope": round(float(slope), 4),
                "base_depth_pct": round(base_depth, 2),
                "final_contraction_low": round(final_contraction_low, 2),
                "stop_offset_pct": STOP_OFFSET_PCT,
                "risk_pct": regime.risk_pct,
            },
        }

    @staticmethod
    def _serialize_regime(regime: RegimeSnapshot) -> dict[str, Any]:
        return {
            "scan_date": regime.scan_date.isoformat(),
            "regime": regime.regime,
            "close": regime.close,
            "ma_200": regime.ma_200,
            "risk_pct": round(regime.risk_pct * 100.0, 2),
        }

    @staticmethod
    def _serialize_scan_row(row: VCPScanResult) -> dict[str, Any]:
        metadata_json = row.metadata_json or {}
        risk_distance = max(0.01, float(row.pivot_high) - float(row.stop_level))
        risk_pct = float(metadata_json.get("risk_pct", RISK_PCT_BULL))
        planned_shares = int(math.floor((DEFAULT_CAPITAL * risk_pct) / risk_distance))
        planned_shares = max(
            0,
            min(
                planned_shares,
                int(math.floor((DEFAULT_CAPITAL * MAX_POSITION_CAP) / float(row.pivot_high))),
            ),
        )
        planned_value = round(planned_shares * float(row.pivot_high), 2)
        capital_risk = round(planned_shares * risk_distance, 2)
        return {
            "id": row.id,
            "scan_date": row.scan_date.isoformat(),
            "symbol": row.symbol,
            "company_name": row.company_name or row.symbol,
            "sector": row.sector or "Unknown",
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
            "planned_position_value": planned_value,
            "capital_risk": capital_risk,
            "two_r_target": round(float(row.pivot_high) + (2.0 * risk_distance), 2),
            "metadata": metadata_json,
        }

    def run_scan(
        self, universe: str = DEFAULT_UNIVERSE, scan_date: date | None = None
    ) -> dict[str, Any]:
        started = time.perf_counter()
        scan_date = self._asof_date(scan_date)
        scan_id = str(uuid.uuid4())
        scan_timestamp = datetime.now(UTC)
        with db_session_scope() as db:
            symbols, constituent_mode = self._constituents_for_date(db, universe, scan_date)
            if not symbols:
                raise ValueError(f"No constituents found for {universe} on {scan_date.isoformat()}")
            scoped = self._load_symbol_history(
                symbols=symbols,
                start_date=(pd.Timestamp(scan_date) - pd.Timedelta(days=420)).date(),
                end_date=scan_date,
            )
            symbol_frames = {
                symbol: frame.copy()
                for symbol, frame in scoped.groupby("symbol", sort=True)
                if len(frame) >= 260 and frame["date"].max() == scan_date
            }
            company_meta = self._company_meta(db, list(symbol_frames.keys()))
            rs_map = self._compute_rs_map(symbol_frames)
            regime = self.get_regime(scan_date)
            capital = float(self._get_config(db, "DEFAULT_CAPITAL", str(DEFAULT_CAPITAL)))

            persisted: list[VCPScanResult] = []
            for symbol, frame in symbol_frames.items():
                result = self._analyze_pattern(
                    frame=frame,
                    rs_rating=rs_map.get(symbol, 0),
                    company_meta=company_meta.get(symbol, {"name": symbol, "sector": "Unknown"}),
                    regime=regime,
                    capital=capital,
                )
                if result is None:
                    continue
                record = VCPScanResult(
                    scan_id=scan_id,
                    scan_date=scan_date,
                    scan_timestamp=scan_timestamp,
                    universe=universe.upper(),
                    symbol=result["symbol"],
                    company_name=result["company_name"],
                    sector=result["sector"],
                    grade=result["grade"],
                    rs_rating=result["rs_rating"],
                    stage2_conditions_met=result["stage2_conditions_met"],
                    contraction_count=result["contraction_count"],
                    contraction_depths=result["contraction_depths"],
                    final_contraction_depth=result["final_contraction_depth"],
                    volume_dry_up_pct=result["volume_dry_up_pct"],
                    pivot_high=result["pivot_high"],
                    stop_level=result["stop_level"],
                    stop_pct=result["stop_pct"],
                    days_in_base=result["days_in_base"],
                    is_breakout=result["is_breakout"],
                    breakout_price=result["breakout_price"],
                    breakout_volume_mult=result["breakout_volume_mult"],
                    close_position_in_range=result["close_position_in_range"],
                    overhead_clear=result["overhead_clear"],
                    regime=result["regime"],
                    signal_status="SIGNAL",
                    metadata_json=result["metadata_json"],
                )
                db.add(record)
                persisted.append(record)

            db.commit()
            for record in persisted:
                db.refresh(record)

            self._set_config(db, "LAST_VCP_SCAN_DATE", scan_date.isoformat())
            self._set_config(db, "LAST_VCP_SCAN_UNIVERSE", universe.upper())
            self._set_config(db, "LAST_VCP_SCAN_RESULT_COUNT", str(len(persisted)))

            results = [self._serialize_scan_row(record) for record in persisted]
            results.sort(
                key=lambda item: (
                    {"A": 0, "B": 1, "C": 2}.get(item["grade"], 3),
                    0 if item["is_breakout"] else 1,
                    item["final_contraction_depth"],
                    -item["rs_rating"],
                )
            )
            duration_ms = (time.perf_counter() - started) * 1000.0
            runtime_metrics.observe_ms("strategy.vcp.scan_ms", duration_ms)
            runtime_metrics.increment("strategy.vcp.scan_runs")
            logger.info(
                "VCP scan complete universe=%s scan_id=%s eligible_symbols=%s results=%s "
                "duration_ms=%.2f",
                universe.upper(),
                scan_id,
                len(symbol_frames),
                len(results),
                duration_ms,
            )
            return {
                "scan_id": scan_id,
                "scan_timestamp": scan_timestamp.isoformat(),
                "scan_date": scan_date.isoformat(),
                "universe": universe.upper(),
                "regime": self._serialize_regime(regime),
                "constituent_mode": constituent_mode,
                "total_symbols_scanned": len(symbol_frames),
                "results": results,
            }

    def get_latest_scan(
        self, universe: str = DEFAULT_UNIVERSE, show_all: bool = False
    ) -> dict[str, Any]:
        db = get_db_session()
        try:
            latest_date = (
                db.query(func.max(VCPScanResult.scan_timestamp))
                .filter(VCPScanResult.universe == universe.upper())
                .scalar()
            )
            config_latest_date = self._get_config(db, "LAST_VCP_SCAN_DATE", "")
            if latest_date is None and config_latest_date:
                latest_date = datetime.strptime(config_latest_date, "%Y-%m-%d").date()
            if latest_date is None:
                return {
                    "scan_date": None,
                    "universe": universe.upper(),
                    "results": [],
                    "regime": self._serialize_regime(self.get_regime()),
                }
            latest_row = (
                db.query(VCPScanResult)
                .filter(VCPScanResult.universe == universe.upper())
                .order_by(
                    VCPScanResult.scan_timestamp.desc(),
                    VCPScanResult.created_at.desc(),
                    VCPScanResult.id.desc(),
                )
                .first()
            )
            if latest_row is None:
                return {
                    "scan_date": None,
                    "universe": universe.upper(),
                    "results": [],
                    "regime": self._serialize_regime(self.get_regime()),
                }
            rows = (
                db.query(VCPScanResult)
                .filter(
                    VCPScanResult.universe == universe.upper(),
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
                "universe": universe.upper(),
                "regime": self._serialize_regime(self.get_regime(latest_row.scan_date)),
                "results": [self._serialize_scan_row(row) for row in rows],
            }
        finally:
            db.close()

    def get_signal_detail(self, symbol: str, scan_date: date | None = None) -> dict[str, Any]:
        db = get_db_session()
        try:
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

            history = (
                self._load_symbol_history(
                    symbols=[row.symbol],
                    start_date=(pd.Timestamp(row.scan_date) - pd.Timedelta(days=260)).date(),
                    end_date=row.scan_date,
                )
                .sort_values("date")
                .tail(160)
                .copy()
            )
            history["ema_21"] = history["close_ref"].ewm(span=21, adjust=False).mean()
            history["ma_50"] = history["close_ref"].rolling(50).mean()
            history["ma_150"] = history["close_ref"].rolling(150).mean()
            history["ma_200"] = history["close_ref"].rolling(200).mean()
            return {
                "signal": self._serialize_scan_row(row),
                "chart": [
                    {
                        "date": item["date"].isoformat(),
                        "open": round(float(item["open"]), 2),
                        "high": round(float(item["high"]), 2),
                        "low": round(float(item["low"]), 2),
                        "close": round(float(item["close"]), 2),
                        "volume": int(item["volume"]),
                        "ema_21": round(float(item["ema_21"]), 2)
                        if not pd.isna(item["ema_21"])
                        else None,
                        "ma_50": round(float(item["ma_50"]), 2)
                        if not pd.isna(item["ma_50"])
                        else None,
                        "ma_150": round(float(item["ma_150"]), 2)
                        if not pd.isna(item["ma_150"])
                        else None,
                        "ma_200": round(float(item["ma_200"]), 2)
                        if not pd.isna(item["ma_200"])
                        else None,
                    }
                    for item in history.to_dict("records")
                ],
            }
        finally:
            db.close()

    def queue_signal(self, signal_id: int) -> dict[str, Any]:
        db = get_db_session()
        try:
            row = db.query(VCPScanResult).filter(VCPScanResult.id == signal_id).first()
            if row is None:
                raise ValueError("Signal not found")
            row.signal_status = "PENDING_ENTRY"
            db.commit()
            db.refresh(row)
            return self._serialize_scan_row(row)
        finally:
            db.close()

    def cancel_signal(self, signal_id: int) -> dict[str, Any]:
        db = get_db_session()
        try:
            row = db.query(VCPScanResult).filter(VCPScanResult.id == signal_id).first()
            if row is None:
                raise ValueError("Signal not found")
            row.signal_status = "SIGNAL"
            db.commit()
            db.refresh(row)
            return self._serialize_scan_row(row)
        finally:
            db.close()

    def list_positions(self) -> dict[str, Any]:
        db = get_db_session()
        try:
            rows = (
                db.query(StrategyPosition)
                .filter(StrategyPosition.status.in_(["OPEN", "OPEN_PARTIAL"]))
                .order_by(StrategyPosition.entry_date.desc(), StrategyPosition.symbol.asc())
                .all()
            )
            quotes = fetch_fyers_quotes([row.symbol for row in rows]) if rows else {}
            payload = []
            for row in rows:
                quote = quotes.get(row.symbol, {})
                ltp = float(quote.get("ltp") or row.entry_price)
                unrealized = (ltp - row.entry_price) * row.shares_remaining
                risk_distance = max(0.01, row.entry_price - row.stop_price)
                payload.append(
                    {
                        "id": row.id,
                        "symbol": row.symbol,
                        "entry_date": row.entry_date.isoformat(),
                        "entry_price": round(float(row.entry_price), 2),
                        "shares": row.shares_remaining,
                        "stop_price": round(float(row.stop_price), 2),
                        "ltp": round(ltp, 2),
                        "unrealized_pnl": round(unrealized, 2),
                        "unrealized_pnl_pct": round(((ltp / row.entry_price) - 1.0) * 100.0, 2),
                        "r_multiple": round(
                            unrealized / (risk_distance * max(1, row.shares_remaining)), 2
                        ),
                        "two_r_status": "HIT" if row.two_r_hit else "NOT_HIT",
                        "status": row.status,
                    }
                )
            return {"positions": payload}
        finally:
            db.close()

    def close_position(self, position_id: int) -> dict[str, Any]:
        db = get_db_session()
        try:
            row = db.query(StrategyPosition).filter(StrategyPosition.id == position_id).first()
            if row is None:
                raise ValueError("Position not found")
            quotes = fetch_fyers_quotes([row.symbol])
            quote = quotes.get(row.symbol, {}) if isinstance(quotes, dict) else {}
            quote_source = "ltp"
            if quote.get("ltp") is None:
                logger.warning(
                    "Manual close missing LTP for %s; falling back to entry price. quote=%s",
                    row.symbol,
                    quote,
                )
                exit_price = float(row.entry_price)
                quote_source = "entry_fallback"
                row.notes = (
                    f"{(row.notes + ' | ') if row.notes else ''}"
                    f"manual_close_quote_fallback symbol={row.symbol} quote={quote}"
                )
            else:
                exit_price = float(quote["ltp"])
            row.exit_date = date.today()
            row.exit_price = exit_price
            row.exit_reason = "MANUALLY_CLOSED"
            row.status = "MANUALLY_CLOSED"
            row.pnl_inr = round((exit_price - row.entry_price) * row.shares_remaining, 2)
            row.pnl_pct = round(((exit_price / row.entry_price) - 1.0) * 100.0, 2)
            row.shares_remaining = 0
            db.commit()
            return {
                "status": "ok",
                "position_id": position_id,
                "exit_price": round(exit_price, 2),
                "quote_source": quote_source,
                "ltp_missing": quote_source != "ltp",
            }
        finally:
            db.close()

    def update_stop(self, position_id: int, stop_price: float) -> dict[str, Any]:
        db = get_db_session()
        try:
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
        finally:
            db.close()

    def halt(self, reason: str | None = None) -> dict[str, Any]:
        db = get_db_session()
        try:
            self._set_config(db, "TRADING_HALTED", "true")
            self._set_config(db, "HALT_REASON", reason or "")
            self._set_config(db, "HALTED_AT", datetime.now(UTC).isoformat())
            return self.get_status()
        finally:
            db.close()

    def resume(self) -> dict[str, Any]:
        db = get_db_session()
        try:
            self._set_config(db, "TRADING_HALTED", "false")
            self._set_config(db, "HALT_REASON", "")
            return self.get_status()
        finally:
            db.close()

    def get_status(self, universe: str | None = None) -> dict[str, Any]:
        db = get_db_session()
        try:
            universe_filter = (universe or DEFAULT_UNIVERSE).upper()
            latest_scan = (
                db.query(func.max(VCPScanResult.scan_date))
                .filter(VCPScanResult.universe == universe_filter)
                .scalar()
            )
            if latest_scan is None:
                config_latest_scan = self._get_config(db, "LAST_VCP_SCAN_DATE", "")
                latest_scan = (
                    datetime.strptime(config_latest_scan, "%Y-%m-%d").date()
                    if config_latest_scan
                    else None
                )
            pending_entries = (
                db.query(VCPScanResult)
                .filter(
                    VCPScanResult.universe == universe_filter,
                    VCPScanResult.signal_status == "PENDING_ENTRY",
                )
                .count()
            )
            open_positions = (
                db.query(StrategyPosition)
                .filter(StrategyPosition.status.in_(["OPEN", "OPEN_PARTIAL"]))
                .count()
            )
            halted = self._get_config(db, "TRADING_HALTED", "false").lower() == "true"
            return {
                "universe": universe_filter,
                "halted": halted,
                "halt_reason": self._get_config(db, "HALT_REASON", ""),
                "halted_at": self._get_config(db, "HALTED_AT", ""),
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
                "regime": self._serialize_regime(self.get_regime()),
                "metrics": runtime_metrics.snapshot(prefix="strategy.vcp.", strip_prefix=True),
            }
        finally:
            db.close()

    def _daily_symbol_map(
        self, stock_df: pd.DataFrame, symbols: list[str], start_date: date, end_date: date
    ) -> dict[str, pd.DataFrame]:
        lookback_start = (pd.Timestamp(start_date) - pd.Timedelta(days=420)).date()
        scoped = stock_df[
            (stock_df["symbol"].isin(symbols))
            & (stock_df["date"] >= lookback_start)
            & (stock_df["date"] <= end_date)
        ].copy()
        return {
            symbol: frame.sort_values("date").reset_index(drop=True)
            for symbol, frame in scoped.groupby("symbol")
        }

    @staticmethod
    def _next_trading_date(trading_dates: list[date], current_date: date) -> date | None:
        for item in trading_dates:
            if item > current_date:
                return item
        return None

    @staticmethod
    def _simulate_trade_exit(
        future: pd.DataFrame,
        entry_open: float,
        stop_price: float,
        risk_distance: float,
        shares: int,
    ) -> tuple[float, float, date, str]:
        """Walk the bars from entry: stop, half off at 2R (stop to breakeven), 21-EMA trail.

        Returns (realized P&L, exit price, exit date, exit reason); a position still open
        on the last bar is closed at that bar's close.
        """
        two_r_price = entry_open + (2.0 * risk_distance)
        future["ema_21"] = future["close_ref"].ewm(span=21, adjust=False).mean()

        sold_half = False
        shares_remaining = shares
        realized = 0.0
        exit_reason = "TRAIL_STOP_EXIT"
        exit_price = float(future.iloc[-1]["close"])
        exit_date = future.iloc[-1]["date"]
        for idx, row in future.iterrows():
            low = float(row["low"])

            if low <= stop_price:
                exit_price = stop_price * (1.0 - TRAIL_SLIPPAGE_PCT)
                exit_date = row["date"]
                exit_reason = "BREAKEVEN_EXIT" if sold_half else "STOPPED_OUT"
                break

            if not sold_half and float(row["high"]) >= two_r_price:
                half = math.ceil(shares / 2)
                realized += half * ((two_r_price * (1.0 - TRAIL_SLIPPAGE_PCT)) - entry_open)
                shares_remaining -= half
                sold_half = True
                stop_price = entry_open
                continue

            if sold_half and float(row["close"]) < float(row["ema_21"]):
                next_idx = min(idx + 1, len(future) - 1)
                exit_price = float(future.iloc[next_idx]["open"]) * (1.0 - TRAIL_SLIPPAGE_PCT)
                exit_date = future.iloc[next_idx]["date"]
                exit_reason = "TRAIL_STOP_EXIT"
                break

        realized += shares_remaining * (exit_price - entry_open)
        return realized, exit_price, exit_date, exit_reason

    def _build_backtest_trade(
        self,
        signal: dict[str, Any],
        symbol_frame: pd.DataFrame,
        signal_date: date,
        entry_date: date,
        capital: float,
        risk_pct: float,
    ) -> dict[str, Any] | None:
        entry_row = symbol_frame[symbol_frame["date"] == entry_date]
        if entry_row.empty:
            return None
        entry_open = float(entry_row.iloc[0]["open"])
        if entry_open > float(signal["pivot_high"]) * (1.0 + GAP_UP_MAX_PCT):
            return {
                "symbol": signal["symbol"],
                "entry_date": entry_date.isoformat(),
                "status": "SKIPPED_GAP_UP",
            }

        stop_price = float(signal["stop_level"])
        risk_distance = entry_open - stop_price
        if risk_distance <= 0:
            logger.warning(
                INVALID_STOP_DISTANCE,
                extra={
                    "symbol": signal["symbol"],
                    "entry_price": round(entry_open, 2),
                    "stop_price": round(stop_price, 2),
                    "capital": round(capital, 2),
                    "risk_pct": risk_pct,
                    "risk_distance": round(risk_distance, 4),
                },
            )
            return None
        shares = int(math.floor((capital * risk_pct) / risk_distance))
        shares = max(0, min(shares, int(math.floor((capital * MAX_POSITION_CAP) / entry_open))))
        if shares <= 0:
            logger.warning(
                INVALID_STOP_DISTANCE,
                extra={
                    "symbol": signal["symbol"],
                    "entry_price": round(entry_open, 2),
                    "stop_price": round(stop_price, 2),
                    "capital": round(capital, 2),
                    "risk_pct": risk_pct,
                    "risk_distance": round(risk_distance, 4),
                    "shares": shares,
                },
            )
            return None

        future = symbol_frame[symbol_frame["date"] >= entry_date].copy().reset_index(drop=True)
        realized, exit_price, exit_date, exit_reason = self._simulate_trade_exit(
            future, entry_open, stop_price, risk_distance, shares
        )

        # shares > 0 is guaranteed by the early return above
        pnl_pct = (realized / (entry_open * shares)) * 100.0
        r_multiple = realized / (risk_distance * shares)
        return {
            "symbol": signal["symbol"],
            "grade": signal["grade"],
            "rs_at_entry": signal["rs_rating"],
            "entry_date": entry_date.isoformat(),
            "entry_price": round(entry_open, 2),
            "exit_date": exit_date.isoformat(),
            "exit_price": round(exit_price, 2),
            "exit_reason": exit_reason,
            "shares": shares,
            "pnl_inr": round(realized, 2),
            "pnl_pct": round(pnl_pct, 2),
            "r_multiple": round(r_multiple, 2),
            "hold_days": max(1, (exit_date - entry_date).days),
            "regime": "BULL" if risk_pct >= RISK_PCT_BULL else "BEAR",
            "status": "COMPLETED",
            "signal_date": signal_date.isoformat(),
        }

    def _load_backtest_frames(
        self, db: Session, universe: str, start_date: date, end_date: date
    ) -> tuple[dict[str, pd.DataFrame], list[date]]:
        """(per-symbol history frames, trading dates in range) for a backtest."""
        # Load everyone who was a member at any point in the range, not just the
        # end-date members, so names dropped from the index stay tradable until they left.
        range_symbols = self._constituents_in_range(db, universe, start_date, end_date)
        combined = self._load_symbol_history(
            symbols=range_symbols,
            start_date=(pd.Timestamp(start_date) - pd.Timedelta(days=420)).date(),
            end_date=end_date,
        )
        symbol_frames = {
            symbol: frame.sort_values("date").reset_index(drop=True)
            for symbol, frame in combined.groupby("symbol")
        }
        trading_dates = sorted(
            {
                row_date
                for frame in symbol_frames.values()
                for row_date in frame[(frame["date"] >= start_date) & (frame["date"] <= end_date)][
                    "date"
                ]
                .unique()
                .tolist()
            }
        )
        if not trading_dates:
            raise ValueError("No trading dates available in the selected range")
        return symbol_frames, trading_dates

    @staticmethod
    def _realized_pnl_through(trades: list[dict[str, Any]], as_of: date) -> float:
        return sum(
            float(trade["pnl_inr"])
            for trade in trades
            if datetime.strptime(trade["exit_date"], "%Y-%m-%d").date() <= as_of
        )

    @staticmethod
    def _record_curve_point(run: _VCPBacktestState, as_of: date, realized_pnl: float) -> None:
        """Append the equity point and, when the index traded that day, the benchmark point."""
        run.equity_curve.append(
            {"date": as_of.isoformat(), "equity": round(run.capital + realized_pnl, 2)}
        )
        if run.benchmark.empty:
            return
        bench_row = run.benchmark[run.benchmark["date"] == as_of]
        if bench_row.empty:
            return
        run.benchmark_curve.append(
            {
                "date": as_of.isoformat(),
                "equity": round(
                    run.capital * (float(bench_row.iloc[0]["close"]) / run.benchmark_start), 2
                ),
            }
        )

    def _breakout_candidates(
        self,
        run: _VCPBacktestState,
        trading_date: date,
        constituents: set[str],
        regime: RegimeSnapshot,
        available_capital: float,
    ) -> list[dict[str, Any]]:
        """Breakout signals on `trading_date` among members not already in a trade."""
        day_frames = {
            symbol: frame[frame["date"] <= trading_date].copy()
            for symbol, frame in run.symbol_frames.items()
            if symbol in constituents and not frame[frame["date"] <= trading_date].empty
        }
        rs_map = self._compute_rs_map(day_frames)
        active_symbols = {
            trade["symbol"]
            for trade in run.trades
            if datetime.strptime(trade["exit_date"], "%Y-%m-%d").date() >= trading_date
        }
        candidates: list[dict[str, Any]] = []
        for symbol, frame in day_frames.items():
            if len(frame) < 260 or frame["date"].max() != trading_date or symbol in active_symbols:
                continue
            analysis = self._analyze_pattern(
                frame=frame,
                rs_rating=rs_map.get(symbol, 0),
                company_meta=run.meta.get(symbol, {"name": symbol, "sector": "Unknown"}),
                regime=regime,
                capital=available_capital,
            )
            if analysis and analysis["is_breakout"]:
                candidates.append(analysis)
        return candidates

    def _backtest_day(self, run: _VCPBacktestState, trading_date: date) -> None:
        """Scan one session, enter up to five next-session trades, record the curves."""
        completed_pnl = self._realized_pnl_through(run.trades, trading_date)
        available_capital = run.capital + completed_pnl
        constituents_list, date_mode = run.members_on(trading_date)
        run.modes.add(
            "indicative_pre_history" if date_mode.startswith("indicative_pre_") else date_mode
        )
        regime = self.get_regime(trading_date)
        candidates = self._breakout_candidates(
            run, trading_date, set(constituents_list), regime, available_capital
        )

        next_date = self._next_trading_date(run.trading_dates, trading_date)
        if next_date is None:
            return
        # Trades enter on next_date, so the symbol must still be a member then.
        entry_members = set(run.members_on(next_date)[0])
        candidates = [signal for signal in candidates if signal["symbol"] in entry_members]
        for signal in sorted(candidates, key=lambda item: (item["grade"], -item["rs_rating"]))[:5]:
            trade = self._build_backtest_trade(
                signal=signal,
                symbol_frame=run.symbol_frames[signal["symbol"]],
                signal_date=trading_date,
                entry_date=next_date,
                capital=available_capital,
                risk_pct=regime.risk_pct,
            )
            if trade is None or trade.get("status") == "SKIPPED_GAP_UP":
                continue
            run.trades.append(trade)
        self._record_curve_point(run, trading_date, completed_pnl)

    @staticmethod
    def _backtest_metrics(
        run: _VCPBacktestState, eq_series: pd.Series, drawdown: pd.Series, mode: str
    ) -> dict[str, Any]:
        capital = run.capital
        trades = run.trades
        returns = eq_series.pct_change().fillna(0.0)
        ann_return = bt_metrics.annualized_return(
            float(eq_series.iloc[-1]), capital, len(eq_series)
        )
        sharpe = bt_metrics.sharpe_excess_cagr(ann_return, returns, risk_free=0.065)
        pnl_stats = bt_metrics.trade_stats([trade["pnl_inr"] for trade in trades])
        benchmark_return = (
            ((run.benchmark_curve[-1]["equity"] / capital) - 1.0) * 100.0
            if run.benchmark_curve
            else 0.0
        )
        return {
            "initial_capital": round(capital, 2),
            "final_equity": round(float(eq_series.iloc[-1]), 2),
            "total_return_pct": round(
                bt_metrics.total_return(float(eq_series.iloc[-1]), capital) * 100.0, 2
            ),
            "cagr_pct": round(ann_return * 100.0, 2),
            "max_drawdown_pct": round(abs(bt_metrics.max_drawdown(drawdown)) * 100.0, 2),
            "sharpe_ratio": round(float(sharpe), 4),
            "win_rate_pct": round(pnl_stats.win_rate * 100.0, 2),
            "avg_win_inr": round(pnl_stats.avg_win, 2),
            "avg_loss_inr": round(pnl_stats.avg_loss, 2),
            "profit_factor": round(pnl_stats.profit_factor, 2),
            "total_trades": len(trades),
            "avg_hold_days": round(mean([trade["hold_days"] for trade in trades]), 2)
            if trades
            else 0.0,
            "avg_r_multiple": round(mean([trade["r_multiple"] for trade in trades]), 2)
            if trades
            else 0.0,
            "benchmark_return_pct": round(benchmark_return, 2),
            "alpha_pct": round((ann_return * 100.0) - benchmark_return, 2),
            "survivorship_bias_label": "BIAS-CORRECTED"
            if mode == "bias_corrected"
            else f"WARNING: {mode}",
        }

    def run_backtest(self, payload: dict[str, Any]) -> dict[str, Any]:
        started = time.perf_counter()
        universe = str(payload.get("universe") or DEFAULT_UNIVERSE).upper()
        start_date = datetime.strptime(payload["start_date"], "%Y-%m-%d").date()
        end_date = datetime.strptime(payload["end_date"], "%Y-%m-%d").date()
        capital = float(payload.get("initial_capital") or DEFAULT_CAPITAL)

        with db_session_scope() as db:
            symbol_frames, trading_dates = self._load_backtest_frames(
                db, universe, start_date, end_date
            )
            benchmark = daily_history_service.load_index_history(
                "NIFTY50",
                start_date=start_date,
                end_date=end_date,
            ).sort_values("date")
            membership_cache: dict[date, tuple[list[str], str]] = {}

            def members_on(as_of: date) -> tuple[list[str], str]:
                if as_of not in membership_cache:
                    membership_cache[as_of] = self._constituents_for_date(db, universe, as_of)
                return membership_cache[as_of]

            run = _VCPBacktestState(
                capital=capital,
                symbol_frames=symbol_frames,
                trading_dates=trading_dates,
                meta=self._company_meta(db, list(symbol_frames.keys())),
                benchmark=benchmark,
                benchmark_start=float(benchmark.iloc[0]["close"]) if not benchmark.empty else 1.0,
                members_on=members_on,
            )
            for trading_date in trading_dates[:-1]:
                self._backtest_day(run, trading_date)

            # The loop stops before the last date (no next session to enter on), so record the
            # final equity point here; otherwise exits realised on the last day are dropped.
            final_date = trading_dates[-1]
            self._record_curve_point(
                run, final_date, self._realized_pnl_through(run.trades, final_date)
            )

            # Membership is resolved per trading date;
            # only fully point-in-time runs are bias-corrected.
            modes = run.modes
            mode = (
                "bias_corrected"
                if modes == {"bias_corrected"}
                else ",".join(sorted(modes)) or "unavailable"
            )
            trades = run.trades
            equity_curve = run.equity_curve
            if not equity_curve:
                equity_curve = [{"date": trading_dates[0].isoformat(), "equity": capital}]

            eq_series = pd.Series(
                [row["equity"] for row in equity_curve],
                index=[row["date"] for row in equity_curve],
                dtype=float,
            )
            drawdown = bt_metrics.drawdown_series(eq_series)
            metrics = self._backtest_metrics(run, eq_series, drawdown, mode)
            result_payload = {
                "metrics": metrics,
                "equity_curve": equity_curve,
                "benchmark_curve": run.benchmark_curve,
                "drawdown_curve": [
                    {"date": idx, "drawdown_pct": round(value * 100.0, 4)}
                    for idx, value in drawdown.items()
                ],
                "trade_log": trades,
                "methodology": {
                    "entry": "Next-day open with 2% gap-up skip",
                    "stop": f"Final contraction low * (1 - {STOP_OFFSET_PCT:.4f}), 0.1% slippage",
                    "two_r": "Daily high trigger with 0.1% slippage",
                    "trail": "21-day EMA, exit next open - 0.1%",
                    "regime": "Nifty 50 vs 200-day MA",
                    "universe": universe,
                    "survivorship_mode": mode,
                },
            }
            run_id = f"vcp-{uuid.uuid4().hex[:12]}"
            run = BacktestRun(
                run_id=run_id,
                name=payload.get("name") or f"VCP Backtest {start_date.isoformat()}",
                status="completed",
                created_at=datetime.now(UTC),
                completed_at=datetime.now(UTC),
                strategy_id="VCP",
                universe=universe,
                initial_capital=capital,
                final_capital=float(eq_series.iloc[-1]),
                total_return=metrics["total_return_pct"],
                sharpe_ratio=metrics["sharpe_ratio"],
                max_drawdown=metrics["max_drawdown_pct"],
                instrument_type="equity",
                selection_mode="universe",
                scope_label=universe,
                universe_id=universe,
                strategy_configs={"strategy_id": "VCP", "params": {}},
                portfolio_config={},
                capital_mode="fixed",
                start_date=start_date,
                end_date=end_date,
                summary_metrics=metrics,
                request_payload=payload,
                result_payload=result_payload,
            )
            db.add(run)
            db.commit()
            duration_ms = (time.perf_counter() - started) * 1000.0
            runtime_metrics.observe_ms("strategy.vcp.backtest_ms", duration_ms)
            runtime_metrics.increment("strategy.vcp.backtest_runs")
            logger.info(
                "VCP backtest complete universe=%s run_id=%s trades=%s trading_dates=%s "
                "duration_ms=%.2f",
                universe,
                run_id,
                len(trades),
                len(trading_dates),
                duration_ms,
            )
            return {"run_id": run_id, "status": "completed", "result": result_payload}

    def get_backtest(self, run_id: str) -> dict[str, Any] | None:
        db = get_db_session()
        try:
            row = db.query(BacktestRun).filter(BacktestRun.run_id == run_id).first()
            if row is None or (row.strategy_id or "").upper() != "VCP":
                return None
            return {
                "run_id": row.run_id,
                "status": row.status,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "completed_at": row.completed_at.isoformat() if row.completed_at else None,
                "metrics": row.summary_metrics or {},
                "result": row.result_payload or {},
            }
        finally:
            db.close()

    def list_backtests(self, universe: str | None = None) -> list[dict[str, Any]]:
        db = get_db_session()
        try:
            query = db.query(BacktestRun).filter(BacktestRun.strategy_id == "VCP")
            if universe:
                query = query.filter(BacktestRun.universe == universe.upper())
            rows = query.order_by(BacktestRun.created_at.desc()).limit(50).all()
            return [
                {
                    "run_id": row.run_id,
                    "name": row.name,
                    "status": row.status,
                    "universe": row.universe,
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "start_date": row.start_date.isoformat() if row.start_date else None,
                    "end_date": row.end_date.isoformat() if row.end_date else None,
                    "metrics": row.summary_metrics or {},
                }
                for row in rows
            ]
        finally:
            db.close()


vcp_service = VCPService()

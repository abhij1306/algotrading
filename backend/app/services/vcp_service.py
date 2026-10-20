"""VCP strategy facade for `routers/strategies.py`.

Scan, regime, positions and config live in `app.services.vcp`; this class wires them to
the production adapters and delegates. The backtest still lives here until it moves to
`vcp/backtest.py` (docs/plans/architecture/screener-backtest.md, section 4).
"""

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

import pandas as pd
from sqlalchemy.orm import Session

from ..database import db_session_scope
from ..models import BacktestRun
from ..utils.runtime_metrics import runtime_metrics
from . import backtest_metrics as bt_metrics
from .history_service import daily_history_service
from .vcp import pattern
from .vcp import universe as vcp_universe
from .vcp.config import (
    DEFAULT_CAPITAL,
    DEFAULT_UNIVERSE,
    GAP_UP_MAX_PCT,
    MAX_POSITION_CAP,
    RISK_PCT_BULL,
    TRAIL_SLIPPAGE_PCT,
)
from .vcp.pattern import STOP_OFFSET_PCT
from .vcp.positions import PositionService
from .vcp.quotes import FyersQuoteSource
from .vcp.regime import REGIME_DATA_UNAVAILABLE, RegimeService, RegimeSnapshot
from .vcp.scan import INVALID_STOP_DISTANCE, ScanService, build_signal

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_CAPITAL",
    "DEFAULT_UNIVERSE",
    "REGIME_DATA_UNAVAILABLE",
    "RegimeSnapshot",
    "VCPService",
    "vcp_service",
]


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
    def __init__(
        self,
        positions: PositionService | None = None,
        regime: RegimeService | None = None,
        scan: ScanService | None = None,
    ) -> None:
        self._positions = positions or PositionService(FyersQuoteSource())
        self._regime = regime or RegimeService(daily_history_service)
        self._scan = scan or ScanService(daily_history_service, self._regime)

    # --- scan -------------------------------------------------------------------

    def run_scan(
        self, universe: str = DEFAULT_UNIVERSE, scan_date: date | None = None
    ) -> dict[str, Any]:
        return self._scan.run_scan(universe, scan_date)

    def get_latest_scan(
        self, universe: str = DEFAULT_UNIVERSE, show_all: bool = False
    ) -> dict[str, Any]:
        return self._scan.get_latest_scan(universe, show_all)

    def get_signal_detail(self, symbol: str, scan_date: date | None = None) -> dict[str, Any]:
        return self._scan.get_signal_detail(symbol, scan_date)

    def queue_signal(self, signal_id: int) -> dict[str, Any]:
        return self._scan.queue_signal(signal_id)

    def cancel_signal(self, signal_id: int) -> dict[str, Any]:
        return self._scan.cancel_signal(signal_id)

    # --- regime -----------------------------------------------------------------

    def get_regime(self, as_of: date | None = None) -> RegimeSnapshot:
        return self._regime.get_regime(as_of)

    def get_regime_summary(self) -> dict[str, Any]:
        return self._regime.summary()

    # --- positions and status ---------------------------------------------------

    def list_positions(self) -> dict[str, Any]:
        return self._positions.list_positions()

    def close_position(self, position_id: int) -> dict[str, Any]:
        return self._positions.close_position(position_id)

    def update_stop(self, position_id: int, stop_price: float) -> dict[str, Any]:
        return self._positions.update_stop(position_id, stop_price)

    def halt(self, reason: str | None = None) -> dict[str, Any]:
        self._positions.halt(reason)
        return self.get_status()

    def resume(self) -> dict[str, Any]:
        self._positions.resume()
        return self.get_status()

    def get_status(self, universe: str | None = None) -> dict[str, Any]:
        return self._positions.get_status(universe, regime=self._regime.summary)

    # --- backtest ---------------------------------------------------------------

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
    def _constituents_for_date(
        db: Session, universe_code: str, as_of: date
    ) -> tuple[list[str], str]:
        return vcp_universe.constituents_for_date(db, universe_code, as_of)

    @staticmethod
    def _company_meta(db: Session, symbols: list[str]) -> dict[str, dict[str, str]]:
        return vcp_universe.company_meta(db, symbols)

    def _compute_rs_map(self, symbol_frames: dict[str, pd.DataFrame]) -> dict[str, int]:
        return pattern.compute_rs_map(symbol_frames)

    def _analyze_pattern(
        self,
        frame: pd.DataFrame,
        rs_rating: int,
        company_meta: dict[str, str],
        regime: RegimeSnapshot,
        capital: float,
    ) -> dict[str, Any] | None:
        return build_signal(frame, rs_rating, company_meta, regime, capital)

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
        range_symbols = vcp_universe.constituents_in_range(db, universe, start_date, end_date)
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
        with db_session_scope() as db:
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

    def list_backtests(self, universe: str | None = None) -> list[dict[str, Any]]:
        with db_session_scope() as db:
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


vcp_service = VCPService()

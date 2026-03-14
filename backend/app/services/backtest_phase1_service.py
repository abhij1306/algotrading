"""
Phase-1 backtest service.

Capabilities now exposed for PRD-aligned rebuild:
- Universe/symbol scoped runs (equity)
- Single or portfolio strategy allocation
- Real equity/benchmark/drawdown curves from curated parquet data
- In-memory run store for immediate retrieval

Options contracts are accepted at API boundary but intentionally blocked until
an options historical dataset is onboarded.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ..database import get_db_session
from ..models.backtest import BacktestRun
from ..utils.helpers import safe_float

logger = logging.getLogger(__name__)


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CURATED_ROOT = PROJECT_ROOT / "data_system" / "04_curated" / "phase1"
INDEX_PRICE_DATASET_PATH = (
    PROJECT_ROOT
    / "data_system"
    / "01_sources"
    / "fyers_index_prices"
    / "universe_index_price_daily.parquet"
)

UNIVERSE_SNAPSHOT_MAP: dict[str, Path] = {
    "NIFTY50": CURATED_ROOT / "snapshot_nifty50_daily.parquet",
    "BANKNIFTY": CURATED_ROOT / "snapshot_banknifty_daily.parquet",
}
FYERS_STOCK_DATASET_PATH = (
    PROJECT_ROOT / "data_system" / "01_sources" / "fyers_stock_prices" / "stock_price_daily.parquet"
)

SUPPORTED_INSTRUMENTS = ("equity", "options")
SUPPORTED_STRATEGIES = {
    "EMA20_EMA50_CROSSOVER": "EMA20/EMA50 Crossover",
    "MOMENTUM_2D": "2-Day Momentum",
    "MEAN_REVERSION_3D": "3-Day Mean Reversion",
}
PARAM_SCHEMA_VERSION = 1
STRATEGY_VERSION = "phase1-v1"


@dataclass
class BacktestJob:
    job_id: str
    status: str
    created_at: str
    params: dict[str, Any]
    result: dict[str, Any] | None = None
    error: str | None = None


class Phase1BacktestService:
    def __init__(self) -> None:
        self._jobs: dict[str, BacktestJob] = {}

    @staticmethod
    def _parse_date(value: str) -> date:
        return datetime.strptime(value, "%Y-%m-%d").date()

    @staticmethod
    def _normalize_weight_allocations(strategies: list[dict[str, Any]]) -> list[dict[str, Any]]:
        active = [s for s in strategies if bool(s.get("enabled", True))]
        if not active:
            raise ValueError("At least one enabled strategy is required")

        weight_sum = sum(float(s.get("weight", 0.0)) for s in active)
        if weight_sum <= 0:
            raise ValueError("Total strategy weight must be greater than 0")

        normalized: list[dict[str, Any]] = []
        for s in active:
            strategy_id = str(s.get("strategy_id", "")).upper()
            if strategy_id not in SUPPORTED_STRATEGIES:
                raise ValueError(f"Unsupported strategy: {strategy_id}")
            normalized.append(
                {
                    "strategy_id": strategy_id,
                    "weight": float(s.get("weight", 0.0)) / weight_sum,
                    "params": s.get("params") or {},
                }
            )
        return normalized

    @staticmethod
    def _price_column(df: pd.DataFrame) -> pd.Series:
        adj = pd.to_numeric(df.get("adj_close"), errors="coerce")
        close = pd.to_numeric(df.get("close"), errors="coerce")
        return adj.fillna(close)

    def _load_universe_snapshot(self, universe: str) -> pd.DataFrame:
        universe_id = universe.upper().strip()
        path = UNIVERSE_SNAPSHOT_MAP.get(universe_id)
        if path is None:
            supported = ", ".join(sorted(UNIVERSE_SNAPSHOT_MAP.keys()))
            raise ValueError(f"Unsupported universe '{universe_id}'. Supported: {supported}")
        if not path.exists():
            raise FileNotFoundError(f"Missing artifact: {path}")

        df = pd.read_parquet(path)
        if "date" not in df.columns or "symbol" not in df.columns:
            raise ValueError(f"Invalid universe snapshot schema: {path.name}")
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        if "in_universe" in df.columns:
            df = df[df["in_universe"] == True].copy()  # noqa: E712
        df["price"] = self._price_column(df)
        df = df.dropna(subset=["date", "symbol", "price"])
        return df

    def _load_universe_index_dataset(self, universe: str, start: date, end: date) -> pd.DataFrame:
        """Load index-level daily closes from the consolidated local index dataset."""
        universe_id = universe.upper().strip()
        if not INDEX_PRICE_DATASET_PATH.exists():
            raise ValueError(
                f"Index dataset missing: {INDEX_PRICE_DATASET_PATH}. "
                "Download and consolidate index prices first."
            )
        df = pd.read_parquet(INDEX_PRICE_DATASET_PATH)
        required = {"date", "universe_id", "close"}
        if not required.issubset(set(df.columns)):
            raise ValueError("Index dataset schema mismatch: expected date, universe_id, close")
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df["universe_id"] = df["universe_id"].astype(str).str.upper()
        scoped = df[
            (df["universe_id"] == universe_id) & (df["date"] >= start) & (df["date"] <= end)
        ].copy()
        scoped["price"] = pd.to_numeric(scoped["close"], errors="coerce")
        scoped = scoped.dropna(subset=["date", "price"])
        if scoped.empty:
            raise ValueError(
                f"No index dataset rows for universe={universe_id} in range {start}..{end}"
            )
        return (
            scoped[["date", "price"]]
            .assign(symbol=universe_id)[["date", "symbol", "price"]]
            .sort_values("date")
        )

    def _load_symbol_snapshot(self, symbols: list[str]) -> pd.DataFrame:
        source_path = FYERS_STOCK_DATASET_PATH
        if not source_path.exists():
            raise FileNotFoundError(f"Missing artifact: {FYERS_STOCK_DATASET_PATH}")
        if not symbols:
            raise ValueError("At least one symbol is required when selection mode='symbols'")

        normalized = sorted({s.strip().upper() for s in symbols if s and s.strip()})
        if not normalized:
            raise ValueError("No valid symbols provided")

        df = pd.read_parquet(source_path)
        if "date" not in df.columns or "symbol" not in df.columns:
            raise ValueError(f"Invalid stock snapshot schema: {source_path.name}")
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df = df[df["symbol"].str.upper().isin(normalized)].copy()
        df["price"] = pd.to_numeric(df.get("close"), errors="coerce")
        df = df.dropna(subset=["date", "symbol", "price"])

        existing = set(df["symbol"].str.upper().unique().tolist())
        missing = [s for s in normalized if s not in existing]
        if missing:
            raise ValueError(f"Symbols missing in snapshot_stock_daily: {', '.join(missing[:10])}")
        return df

    def _build_signal(
        self, prices: pd.DataFrame, strategy_id: str, params: dict[str, Any]
    ) -> pd.DataFrame:
        sid = strategy_id.upper()
        if sid == "EMA20_EMA50_CROSSOVER":
            fast = int(params.get("fast_period", 20))
            slow = int(params.get("slow_period", 50))
            if fast <= 0 or slow <= 0 or fast >= slow:
                raise ValueError("EMA strategy requires 0 < fast_period < slow_period")
            ema_fast = prices.ewm(span=fast, adjust=False).mean()
            ema_slow = prices.ewm(span=slow, adjust=False).mean()
            return (ema_fast > ema_slow).astype(int)

        if sid == "MOMENTUM_2D":
            lookback = int(params.get("lookback_days", 2))
            if lookback < 1:
                raise ValueError("Momentum lookback_days must be >= 1")
            mom = prices.pct_change(periods=lookback)
            return (mom > 0).astype(int)

        if sid == "MEAN_REVERSION_3D":
            lookback = int(params.get("lookback_days", 3))
            threshold = float(params.get("threshold_pct", -2.0)) / 100.0
            if lookback < 1:
                raise ValueError("Mean-reversion lookback_days must be >= 1")
            change = prices.pct_change(periods=lookback)
            return (change <= threshold).astype(int)

        raise ValueError(f"Unsupported strategy: {strategy_id}")

    def _signal_to_returns(self, signal: pd.DataFrame, returns: pd.DataFrame) -> pd.Series:
        # T+1 execution: shift signals by 1 day to prevent look-ahead bias
        # Signal on day D is used to calculate returns on day D+1
        shifted = signal.shift(1).fillna(0.0)
        denom = shifted.sum(axis=1).replace(0.0, np.nan)
        weights = shifted.div(denom, axis=0).fillna(0.0)
        series = (weights * returns).sum(axis=1).fillna(0.0)
        series.index = pd.to_datetime(series.index)
        return series

    def _build_trade_log(self, signal: pd.DataFrame, prices: pd.DataFrame) -> list[dict[str, Any]]:
        trades: list[dict[str, Any]] = []
        for symbol in signal.columns:
            s = signal[symbol].fillna(0).astype(int)
            p = prices[symbol]
            prev = s.shift(1).fillna(0).astype(int)
            entries = s[(s == 1) & (prev == 0)].index
            exits = s[(s == 0) & (prev == 1)].index
            exit_iter = iter(exits)
            next_exit = next(exit_iter, None)
            for entry_date in entries:
                while next_exit is not None and next_exit <= entry_date:
                    next_exit = next(exit_iter, None)
                if next_exit is None:
                    break
                entry_price = safe_float(p.loc[entry_date], np.nan)
                exit_price = safe_float(p.loc[next_exit], np.nan)
                if np.isnan(entry_price) or np.isnan(exit_price) or entry_price <= 0:
                    continue
                ret = (exit_price / entry_price) - 1.0
                trades.append(
                    {
                        "symbol": symbol,
                        "entry_date": pd.Timestamp(entry_date).date().isoformat(),
                        "exit_date": pd.Timestamp(next_exit).date().isoformat(),
                        "entry_price": round(entry_price, 4),
                        "exit_price": round(exit_price, 4),
                        "return_pct": round(ret * 100.0, 4),
                    }
                )
        return trades

    def _compute_metrics(
        self,
        equity: pd.Series,
        portfolio_ret: pd.Series,
        drawdown: pd.Series,
        trades: list[dict[str, Any]],
        initial_capital: float,
    ) -> dict[str, Any]:
        final_equity = float(equity.iloc[-1]) if len(equity) else initial_capital
        total_return = (final_equity / initial_capital) - 1.0 if initial_capital > 0 else 0.0

        ret_std = float(portfolio_ret.std(ddof=0))
        sharpe = 0.0
        if ret_std > 0:
            sharpe = float((portfolio_ret.mean() / ret_std) * np.sqrt(252))

        max_dd = float(drawdown.min()) if len(drawdown) else 0.0

        trade_returns = [t["return_pct"] / 100.0 for t in trades]
        wins = len([x for x in trade_returns if x > 0])
        losses = len([x for x in trade_returns if x <= 0])
        win_rate = (wins / len(trade_returns)) if trade_returns else 0.0

        return {
            "initial_capital": round(initial_capital, 2),
            "final_equity": round(final_equity, 2),
            "total_return_pct": round(total_return * 100.0, 4),
            "sharpe_ratio": round(sharpe, 4),
            "max_drawdown_pct": round(max_dd * 100.0, 4),
            "total_trades": len(trades),
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate_pct": round(win_rate * 100.0, 4),
        }

    def get_status(self) -> dict[str, Any]:
        equity_ranges: dict[str, dict[str, Any]] = {}
        index_df: pd.DataFrame | None = None
        universe_ids: list[str] = []
        if INDEX_PRICE_DATASET_PATH.exists():
            try:
                index_df = pd.read_parquet(INDEX_PRICE_DATASET_PATH)
                index_df["date"] = pd.to_datetime(index_df["date"], errors="coerce").dt.date
                index_df["universe_id"] = index_df["universe_id"].astype(str).str.upper()
                universe_ids = sorted(index_df["universe_id"].dropna().unique().tolist())
            except Exception:
                index_df = None

        # Prefer universes from consolidated index dataset.
        # Fallback to legacy fixed map if dataset is unavailable.
        scan_universes = universe_ids or list(UNIVERSE_SNAPSHOT_MAP.keys())

        for universe in scan_universes:
            path = UNIVERSE_SNAPSHOT_MAP.get(universe)
            if index_df is not None:
                udf = index_df[index_df["universe_id"] == universe].copy()
                if not udf.empty:
                    equity_ranges[universe] = {
                        "available": True,
                        "min_date": udf["date"].min().isoformat(),
                        "max_date": udf["date"].max().isoformat(),
                        "rows": int(len(udf)),
                    }
                    continue

            if path is None or not path.exists():
                equity_ranges[universe] = {
                    "available": False,
                    "min_date": None,
                    "max_date": None,
                    "rows": 0,
                }
                continue
            df = self._load_universe_snapshot(universe)
            if df.empty:
                equity_ranges[universe] = {
                    "available": False,
                    "min_date": None,
                    "max_date": None,
                    "rows": 0,
                }
                continue
            equity_ranges[universe] = {
                "available": True,
                "min_date": df["date"].min().isoformat(),
                "max_date": df["date"].max().isoformat(),
                "rows": int(len(df)),
            }

        stock_range = {"available": False, "min_date": None, "max_date": None, "rows": 0}
        stock_source = FYERS_STOCK_DATASET_PATH
        if stock_source.exists():
            sdf = pd.read_parquet(stock_source)
            if "date" in sdf.columns and len(sdf):
                d = pd.to_datetime(sdf["date"], errors="coerce").dt.date.dropna()
                if len(d):
                    stock_range = {
                        "available": True,
                        "min_date": d.min().isoformat(),
                        "max_date": d.max().isoformat(),
                        "rows": int(len(sdf)),
                        "source": stock_source.name,
                    }

        any_equity = any(v["available"] for v in equity_ranges.values()) or stock_range["available"]
        return {
            "data_ready": any_equity,
            "instrument_capabilities": {
                "equity": {"enabled": any_equity, "note": "Backtest ready from curated snapshots"},
                "options": {
                    "enabled": False,
                    "note": "Options historical dataset not onboarded yet",
                },
            },
            "universe_ranges": equity_ranges,
            "stock_range": stock_range,
            "supported_strategies": [{"id": k, "name": v} for k, v in SUPPORTED_STRATEGIES.items()],
        }

    def list_strategies(self) -> dict[str, Any]:
        return {
            "strategies": [
                {
                    "id": "EMA20_EMA50_CROSSOVER",
                    "name": "EMA20/EMA50 Crossover",
                    "default_weight": 1.0,
                    "params_schema": {"fast_period": 20, "slow_period": 50},
                },
                {
                    "id": "MOMENTUM_2D",
                    "name": "2-Day Momentum",
                    "default_weight": 1.0,
                    "params_schema": {"lookback_days": 2},
                },
                {
                    "id": "MEAN_REVERSION_3D",
                    "name": "3-Day Mean Reversion",
                    "default_weight": 1.0,
                    "params_schema": {"lookback_days": 3, "threshold_pct": -2.0},
                },
            ]
        }

    def list_runs(self) -> list[dict[str, Any]]:
        session = get_db_session()
        try:
            runs = (
                session.query(BacktestRun)
                .order_by(BacktestRun.created_at.desc())
                .limit(100)
                .all()
            )
            return [self._serialize_run_summary(run) for run in runs]
        except Exception as exc:
            logger.warning("Falling back to in-memory backtest run listing: %s", exc)
            items = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
            return [
                {
                    "job_id": j.job_id,
                    "status": j.status,
                    "created_at": j.created_at,
                    "params": j.params,
                }
                for j in items
            ]
        finally:
            session.close()

    @staticmethod
    def _serialize_run_summary(run: BacktestRun) -> dict[str, Any]:
        return {
            "job_id": run.run_id,
            "status": run.status,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "params": run.request_payload or {},
            "summary_metrics": run.summary_metrics or {},
        }

    @staticmethod
    def _resolve_run_scope(payload: dict[str, Any]) -> tuple[str, str]:
        selection = payload.get("selection") or {}
        mode = str(selection.get("mode", "universe")).lower()
        if mode == "symbols":
            symbols = [str(symbol).upper() for symbol in selection.get("symbols") or [] if symbol]
            return mode, f"SYMBOLS:{','.join(sorted(set(symbols)))}"
        universe = str(selection.get("universe", "NIFTY50")).upper()
        return mode, universe

    def _build_strategy_snapshot(
        self, strategies: list[dict[str, Any]], normalized: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        raw_by_strategy = {
            str(strategy.get("strategy_id", "")).upper(): strategy for strategy in strategies
        }
        snapshot: list[dict[str, Any]] = []
        for strategy in normalized:
            strategy_id = strategy["strategy_id"]
            raw_cfg = raw_by_strategy.get(strategy_id, {})
            snapshot.append(
                {
                    "strategy_id": strategy_id,
                    "strategy_version": str(raw_cfg.get("strategy_version") or STRATEGY_VERSION),
                    "param_schema_version": int(
                        raw_cfg.get("param_schema_version") or PARAM_SCHEMA_VERSION
                    ),
                    "weight": strategy["weight"],
                    "resolved_params": strategy["params"],
                }
            )
        return snapshot

    def _create_db_run(self, job: BacktestJob, normalized: list[dict[str, Any]]) -> None:
        selection_mode, scope_label = self._resolve_run_scope(job.params)
        strategy_snapshot = self._build_strategy_snapshot(job.params.get("strategies") or [], normalized)
        session = get_db_session()
        try:
            primary_strategy = strategy_snapshot[0]["strategy_id"] if strategy_snapshot else None
            run = BacktestRun(
                run_id=job.job_id,
                name=job.params.get("name"),
                status=job.status,
                strategy_id=primary_strategy,
                universe=scope_label if selection_mode == "universe" else None,
                initial_capital=float(job.params.get("initial_capital", 0.0)),
                instrument_type=str(job.params.get("instrument_type", "equity")).lower(),
                selection_mode=selection_mode,
                scope_label=scope_label,
                universe_id=scope_label if selection_mode == "universe" else None,
                strategy_configs=strategy_snapshot,
                strategy_versions=strategy_snapshot,
                portfolio_config={
                    "selection": job.params.get("selection") or {},
                    "execution": job.params.get("execution") or {},
                },
                capital_mode="NOTIONAL",
                start_date=self._parse_date(str(job.params["start_date"])),
                end_date=self._parse_date(str(job.params["end_date"])),
                request_payload=job.params,
            )
            session.merge(run)
            session.commit()
        finally:
            session.close()

    def _finalize_db_run(self, job: BacktestJob) -> None:
        session = get_db_session()
        try:
            run = session.query(BacktestRun).filter(BacktestRun.run_id == job.job_id).first()
            if run is None:
                return
            run.status = job.status
            run.completed_at = datetime.now(timezone.utc)
            run.error_message = job.error
            if job.result:
                metrics = job.result.get("metrics") or {}
                run.summary_metrics = metrics
                run.result_payload = job.result
                run.final_capital = float(metrics.get("final_equity", 0.0) or 0.0)
                run.total_return = float(metrics.get("total_return_pct", 0.0) or 0.0)
                run.sharpe_ratio = float(metrics.get("sharpe_ratio", 0.0) or 0.0)
                run.max_drawdown = float(metrics.get("max_drawdown_pct", 0.0) or 0.0)
            session.commit()
        finally:
            session.close()

    def run(self, payload: dict[str, Any]) -> dict[str, Any]:
        instrument_type = str(payload.get("instrument_type", "equity")).lower()
        if instrument_type not in SUPPORTED_INSTRUMENTS:
            raise ValueError(f"Unsupported instrument_type='{instrument_type}'")
        if instrument_type == "options":
            raise ValueError(
                "Options backtest is not available until options historical dataset is onboarded"
            )

        start = self._parse_date(str(payload["start_date"]))
        end = self._parse_date(str(payload["end_date"]))
        if start > end:
            raise ValueError("start_date cannot be after end_date")

        initial_capital = float(payload.get("initial_capital", 1_000_000.0))
        if initial_capital <= 0:
            raise ValueError("initial_capital must be > 0")

        selection = payload.get("selection") or {}
        mode = str(selection.get("mode", "universe")).lower()
        if mode == "universe":
            universe = str(selection.get("universe", "NIFTY50")).upper()
            df = self._load_universe_index_dataset(universe, start, end)
            scope_label = f"{universe}:INDEX_DATASET"
        elif mode == "symbols":
            symbols = selection.get("symbols") or []
            if not isinstance(symbols, list):
                raise ValueError("selection.symbols must be a list")
            df = self._load_symbol_snapshot([str(s) for s in symbols])
            scope_label = f"SYMBOLS:{','.join(sorted(set(str(s).upper() for s in symbols if s)))}"
        else:
            raise ValueError("selection.mode must be 'universe' or 'symbols'")

        min_date = df["date"].min()
        max_date = df["date"].max()
        if start < min_date or end > max_date:
            raise ValueError(f"Requested range outside available data: {min_date} to {max_date}")

        df = df[(df["date"] >= start) & (df["date"] <= end)].copy()
        if df.empty:
            raise ValueError("No rows available for requested date range")

        prices = df.pivot(index="date", columns="symbol", values="price").sort_index()
        prices.index = pd.to_datetime(prices.index)
        returns = prices.pct_change().replace([np.inf, -np.inf], np.nan).fillna(0.0)

        strategies = payload.get("strategies") or []
        if not isinstance(strategies, list) or not strategies:
            raise ValueError("At least one strategy must be provided")
        strategy_allocations = self._normalize_weight_allocations(strategies)

        strategy_curves: list[dict[str, Any]] = []
        strategy_returns: list[pd.Series] = []
        combined_trades: list[dict[str, Any]] = []

        for strategy_cfg in strategy_allocations:
            sid = strategy_cfg["strategy_id"]
            weight = float(strategy_cfg["weight"])
            params = strategy_cfg["params"]

            signal = self._build_signal(prices, sid, params)

            # Validate signal generation
            signal_count = int(signal.sum().sum())
            logger.info(f"Strategy {sid}: generated {signal_count} total signals")
            if signal_count == 0:
                logger.warning(f"Strategy {sid}: NO SIGNALS GENERATED - check parameters")

            strat_ret = self._signal_to_returns(signal, returns)
            strat_equity = initial_capital * (1.0 + strat_ret).cumprod()
            strategy_returns.append(strat_ret * weight)

            strategy_curves.append(
                {
                    "strategy_id": sid,
                    "name": SUPPORTED_STRATEGIES[sid],
                    "weight": round(weight, 6),
                    "equity_curve": [
                        {"date": d.date().isoformat(), "equity": round(float(v), 4)}
                        for d, v in strat_equity.items()
                    ],
                }
            )
            combined_trades.extend(self._build_trade_log(signal, prices))

        portfolio_ret = pd.concat(strategy_returns, axis=1).sum(axis=1)

        # Calculate benchmark using buy-and-hold index prices for universe mode
        if mode == "universe":
            # Load index price data for benchmark calculation
            index_df = self._load_universe_index_dataset(universe, start, end)
            index_prices = index_df.set_index("date")["price"]
            index_prices.index = pd.to_datetime(index_prices.index)

            # Align index prices with portfolio returns dates using forward fill
            aligned_index = index_prices.reindex(portfolio_ret.index, method="ffill")

            # Buy-and-hold: (current_price / initial_price) - 1
            initial_index_price = aligned_index.iloc[0]
            benchmark_ret = (aligned_index / initial_index_price) - 1.0
            benchmark_equity = initial_capital * (1.0 + benchmark_ret)
        else:
            # Fallback to equal-weight for symbol mode
            benchmark_ret = returns.mean(axis=1).fillna(0.0)
            benchmark_equity = initial_capital * (1.0 + benchmark_ret).cumprod()

        equity = initial_capital * (1.0 + portfolio_ret).cumprod()
        drawdown = (equity / equity.cummax()) - 1.0

        metrics = self._compute_metrics(
            equity, portfolio_ret, drawdown, combined_trades, initial_capital
        )
        combined_trades.sort(key=lambda x: (x["entry_date"], x["symbol"]))

        return {
            "instrument_type": instrument_type,
            "selection": {"mode": mode, "scope": scope_label},
            "date_range": {"start": start.isoformat(), "end": end.isoformat()},
            "metrics": metrics,
            "equity_curve": [
                {"date": d.date().isoformat(), "equity": round(float(v), 4)}
                for d, v in equity.items()
            ],
            "benchmark_curve": [
                {"date": d.date().isoformat(), "equity": round(float(v), 4)}
                for d, v in benchmark_equity.items()
            ],
            "drawdown_curve": [
                {"date": d.date().isoformat(), "drawdown_pct": round(float(v) * 100.0, 4)}
                for d, v in drawdown.items()
            ],
            "strategy_curves": strategy_curves,
            "trade_log": combined_trades,
        }

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = f"BT-{uuid.uuid4().hex[:10].upper()}"
        normalized = self._normalize_weight_allocations(payload.get("strategies") or [])
        job = BacktestJob(
            job_id=job_id,
            status="running",
            created_at=datetime.now(timezone.utc).isoformat(),
            params=payload,
        )
        db_run_created = False
        try:
            self._create_db_run(job, normalized)
            db_run_created = True
            self._jobs[job_id] = job

            result = self.run(payload)
            job.status = "completed"
            job.result = result
        except Exception as exc:
            job.status = "failed"
            job.error = str(exc)
            if not db_run_created:
                self._jobs.pop(job_id, None)
                logger.exception("Failed to create backtest job %s", job_id, exc_info=exc)
                raise
        finally:
            if db_run_created:
                self._finalize_db_run(job)
        return {"job_id": job_id, "status": job.status}

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        session = get_db_session()
        try:
            run = session.query(BacktestRun).filter(BacktestRun.run_id == job_id).first()
            if run is not None:
                payload: dict[str, Any] = {
                    "job_id": run.run_id,
                    "status": run.status,
                    "created_at": run.created_at.isoformat() if run.created_at else None,
                    "params": run.request_payload or {},
                }
                if run.error_message:
                    payload["error"] = run.error_message
                if run.result_payload:
                    payload["result"] = run.result_payload
                return payload
        finally:
            session.close()

        job = self._jobs.get(job_id)
        if not job:
            return None
        payload = {
            "job_id": job.job_id,
            "status": job.status,
            "created_at": job.created_at,
            "params": job.params,
        }
        if job.status == "failed":
            payload["error"] = job.error
        if job.status == "completed":
            payload["result"] = job.result
        return payload


backtest_phase1_service = Phase1BacktestService()

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
from sqlalchemy import func

from ..database import get_db_session
from ..models import Company, HistoricalPrice

PROJECT_ROOT = Path(__file__).resolve().parents[3]
EQUITY_SOURCE_PATH = (
    PROJECT_ROOT / "data_system" / "01_sources" / "fyers_stock_prices" / "stock_price_daily.parquet"
)
INDEX_SOURCE_PATH = (
    PROJECT_ROOT
    / "data_system"
    / "01_sources"
    / "fyers_index_prices"
    / "universe_index_price_daily.parquet"
)


@dataclass(frozen=True)
class CoverageSnapshot:
    available: bool
    min_date: date | None
    max_date: date | None
    rows: int
    source: str

    def to_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "min_date": self.min_date.isoformat() if self.min_date else None,
            "max_date": self.max_date.isoformat() if self.max_date else None,
            "rows": self.rows,
            "source": self.source,
        }


class DailyHistoryService:
    def __init__(self) -> None:
        self._cached_equity_source: pd.DataFrame | None = None
        self._cached_index_source: pd.DataFrame | None = None

    @staticmethod
    def _empty_equity_frame() -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "symbol",
                "date",
                "open",
                "high",
                "low",
                "close",
                "close_ref",
                "volume",
                "source_layer",
            ]
        )

    @staticmethod
    def _empty_index_frame() -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "universe_id",
                "date",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "source_layer",
            ]
        )

    @staticmethod
    def _normalize_symbols(symbols: list[str] | tuple[str, ...] | set[str] | None) -> list[str]:
        if not symbols:
            return []
        return sorted({str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()})

    def _load_equity_source(self) -> pd.DataFrame:
        if self._cached_equity_source is not None:
            return self._cached_equity_source
        if not EQUITY_SOURCE_PATH.exists():
            self._cached_equity_source = self._empty_equity_frame()
            return self._cached_equity_source

        df = pd.read_parquet(EQUITY_SOURCE_PATH)
        if df.empty:
            self._cached_equity_source = self._empty_equity_frame()
            return self._cached_equity_source

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df["symbol"] = df["symbol"].astype(str).str.upper()
        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["close_ref"] = pd.to_numeric(df["close"], errors="coerce")
        df["source_layer"] = "source.fyers_equity_daily"
        self._cached_equity_source = (
            df[
                [
                    "symbol",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "close_ref",
                    "volume",
                    "source_layer",
                ]
            ]
            .dropna(subset=["symbol", "date", "open", "high", "low", "close", "close_ref", "volume"])
            .sort_values(["symbol", "date"])
            .reset_index(drop=True)
        )
        return self._cached_equity_source

    def _load_index_source(self) -> pd.DataFrame:
        if self._cached_index_source is not None:
            return self._cached_index_source
        if not INDEX_SOURCE_PATH.exists():
            self._cached_index_source = self._empty_index_frame()
            return self._cached_index_source

        df = pd.read_parquet(INDEX_SOURCE_PATH)
        if df.empty:
            self._cached_index_source = self._empty_index_frame()
            return self._cached_index_source

        df = df.copy()
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
        df["universe_id"] = df["universe_id"].astype(str).str.upper()
        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["source_layer"] = "source.fyers_index_daily"
        self._cached_index_source = (
            df[
                [
                    "universe_id",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "source_layer",
                ]
            ]
            .dropna(subset=["universe_id", "date", "close"])
            .sort_values(["universe_id", "date"])
            .reset_index(drop=True)
        )
        return self._cached_index_source

    def _query_equity_db(
        self,
        symbols: list[str],
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        if not symbols:
            return self._empty_equity_frame()

        db = get_db_session()
        try:
            query = (
                db.query(
                    Company.symbol.label("symbol"),
                    HistoricalPrice.date.label("date"),
                    HistoricalPrice.open.label("open"),
                    HistoricalPrice.high.label("high"),
                    HistoricalPrice.low.label("low"),
                    HistoricalPrice.close.label("close"),
                    HistoricalPrice.volume.label("volume"),
                )
                .join(HistoricalPrice, HistoricalPrice.company_id == Company.id)
                .filter(Company.symbol.in_(symbols))
            )
            if start_date is not None:
                query = query.filter(HistoricalPrice.date >= start_date)
            if end_date is not None:
                query = query.filter(HistoricalPrice.date <= end_date)
            rows = query.all()
        finally:
            db.close()

        if not rows:
            return self._empty_equity_frame()

        df = pd.DataFrame(rows, columns=["symbol", "date", "open", "high", "low", "close", "volume"])
        for column in ["open", "high", "low", "close", "volume"]:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        df["symbol"] = df["symbol"].astype(str).str.upper()
        df["close_ref"] = pd.to_numeric(df["close"], errors="coerce")
        df["source_layer"] = "database.historical_prices"
        return (
            df[
                [
                    "symbol",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "close_ref",
                    "volume",
                    "source_layer",
                ]
            ]
            .dropna(subset=["symbol", "date", "open", "high", "low", "close", "close_ref", "volume"])
            .sort_values(["symbol", "date"])
            .reset_index(drop=True)
        )

    def load_equity_history(
        self,
        symbols: list[str] | tuple[str, ...] | set[str],
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        normalized = self._normalize_symbols(symbols)
        if not normalized:
            return self._empty_equity_frame()

        frames = [self._query_equity_db(normalized, start_date, end_date)]

        source_df = self._load_equity_source()
        if not source_df.empty:
            scoped_source = source_df[source_df["symbol"].isin(normalized)]
            if start_date is not None:
                scoped_source = scoped_source[scoped_source["date"] >= start_date]
            if end_date is not None:
                scoped_source = scoped_source[scoped_source["date"] <= end_date]
            frames.append(scoped_source.copy())

        combined = pd.concat(frames, ignore_index=True)
        if combined.empty:
            return self._empty_equity_frame()

        source_rank = {
            "database.historical_prices": 1,
            "source.fyers_equity_daily": 0,
        }
        combined["source_rank"] = combined["source_layer"].map(source_rank).fillna(-1)
        combined = (
            combined.sort_values(["symbol", "date", "source_rank"])
            .drop_duplicates(subset=["symbol", "date"], keep="last")
            .drop(columns=["source_rank"])
            .reset_index(drop=True)
        )
        return combined

    def load_index_history(
        self,
        universe_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> pd.DataFrame:
        normalized = str(universe_id).strip().upper()
        if not normalized:
            return self._empty_index_frame()

        df = self._load_index_source()
        if df.empty:
            return self._empty_index_frame()

        scoped = df[df["universe_id"] == normalized].copy()
        if start_date is not None:
            scoped = scoped[scoped["date"] >= start_date]
        if end_date is not None:
            scoped = scoped[scoped["date"] <= end_date]
        return scoped.reset_index(drop=True)

    def get_equity_coverage(self) -> CoverageSnapshot:
        db = get_db_session()
        try:
            db_min, db_max, db_rows = db.query(
                func.min(HistoricalPrice.date),
                func.max(HistoricalPrice.date),
                func.count(HistoricalPrice.id),
            ).one()
        finally:
            db.close()

        source_df = self._load_equity_source()
        source_min = source_df["date"].min() if not source_df.empty else None
        source_max = source_df["date"].max() if not source_df.empty else None
        source_rows = int(len(source_df))

        min_candidates = [value for value in [db_min, source_min] if value is not None]
        max_candidates = [value for value in [db_max, source_max] if value is not None]
        total_rows = int(db_rows or 0) + source_rows
        return CoverageSnapshot(
            available=bool(min_candidates and max_candidates),
            min_date=min(min_candidates) if min_candidates else None,
            max_date=max(max_candidates) if max_candidates else None,
            rows=total_rows,
            source="database.historical_prices + source.fyers_equity_daily",
        )

    def latest_equity_date(self) -> date | None:
        return self.get_equity_coverage().max_date

    def get_index_coverage_map(self) -> dict[str, dict[str, object]]:
        df = self._load_index_source()
        if df.empty:
            return {}

        coverage: dict[str, dict[str, object]] = {}
        for universe_id, group in df.groupby("universe_id", sort=True):
            coverage[str(universe_id)] = CoverageSnapshot(
                available=True,
                min_date=group["date"].min(),
                max_date=group["date"].max(),
                rows=int(len(group)),
                source="source.fyers_index_daily",
            ).to_dict()
        return coverage


daily_history_service = DailyHistoryService()

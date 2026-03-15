from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import func

from app.database import get_db_session
from app.models import HistoricalPrice, IndexConstituentHistory, IndexUniverseDefinition
from app.services.fyers_client import get_fyers_client
from app.services.universe import get_universe_service
from app.services.symbol_master import symbol_master

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = PROJECT_ROOT / "data_system" / "01_sources" / "fyers_stock_prices"
OUT_YEAR_DIR = OUT_DIR / "yearly"
OUT_PARQUET = OUT_DIR / "stock_price_daily.parquet"
OUT_CSV = OUT_DIR / "stock_price_daily.csv"
MANIFEST_PATH = OUT_DIR / "download_manifest_incremental_daily.json"
FAILED_PATH = OUT_DIR / "failed_symbols_incremental_daily.json"
DEFAULT_FIRST_HISTORY_DATE = date(2016, 1, 1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Canonical FYERS adapter for refreshing consolidated daily equity history."
    )
    parser.add_argument("--universes", type=str, default="", help="Comma-separated universe ids")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated DB symbols")
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--throttle", type=float, default=0.15)
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _parse_csv_list(raw: str) -> list[str]:
    return [item.strip().upper() for item in raw.split(",") if item and item.strip()]


def resolve_symbols(universe_ids: list[str], explicit_symbols: list[str]) -> tuple[list[str], dict[str, Any]]:
    if explicit_symbols:
        symbols = sorted(set(explicit_symbols))
        return symbols, {"mode": "explicit_symbols", "universes": []}

    if universe_ids:
        service = get_universe_service()
        symbols: set[str] = set()
        for universe_id in universe_ids:
            symbols.update(service.get_symbols(universe_id))
        return sorted(symbols), {"mode": "universes", "universes": universe_ids}

    db = get_db_session()
    try:
        rows = (
            db.query(IndexConstituentHistory.symbol)
            .join(
                IndexUniverseDefinition,
                IndexUniverseDefinition.id == IndexConstituentHistory.universe_id,
            )
            .filter(IndexConstituentHistory.effective_to.is_(None))
            .distinct()
            .order_by(IndexConstituentHistory.symbol.asc())
            .all()
        )
        symbols = [row[0] for row in rows]
        return symbols, {"mode": "all_active_index_symbols", "universes": []}
    finally:
        db.close()


def database_max_date() -> date | None:
    db = get_db_session()
    try:
        return db.query(func.max(HistoricalPrice.date)).scalar()
    finally:
        db.close()


def load_existing_dataset() -> pd.DataFrame:
    if not OUT_PARQUET.exists():
        return pd.DataFrame(
            columns=["date", "symbol", "open", "high", "low", "close", "volume", "source", "fyers_symbol", "year"]
        )
    df = pd.read_parquet(OUT_PARQUET)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    return df


def dataset_max_date(df: pd.DataFrame) -> date | None:
    if df.empty:
        return None
    value = df["date"].max()
    return value if isinstance(value, date) else None


def fetch_history(
    fyers_symbol: str,
    start_date: date,
    end_date: date,
    throttle_s: float,
    retries: int = 3,
) -> dict[str, Any]:
    client = get_fyers_client()
    if not client or not client.fyers or not client.validate_token():
        raise RuntimeError("FYERS client unavailable or token invalid")
    last_response: dict[str, Any] = {}
    for attempt in range(retries):
        last_response = client.get_historical_data(
            fyers_symbol,
            "D",
            start_date.isoformat(),
            end_date.isoformat(),
        )
        if isinstance(last_response, dict) and last_response.get("s") == "ok":
            return last_response
        time.sleep(throttle_s * (attempt + 1))
    return last_response


def candles_to_frame(symbol: str, fyers_symbol: str, candles: list[list[Any]]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(
            columns=["date", "symbol", "open", "high", "low", "close", "volume", "source", "fyers_symbol", "year"]
        )
    records: list[dict[str, Any]] = []
    for row in candles:
        if not isinstance(row, list) or len(row) < 6:
            continue
        candle_date = datetime.fromtimestamp(int(row[0]), UTC).date()
        records.append(
            {
                "date": candle_date,
                "symbol": symbol,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": int(row[5] or 0),
                "source": "fyers",
                "fyers_symbol": fyers_symbol,
                "year": candle_date.year,
            }
        )
    return pd.DataFrame.from_records(records)


def write_outputs(df: pd.DataFrame) -> None:
    OUT_YEAR_DIR.mkdir(parents=True, exist_ok=True)
    for year, year_df in df.groupby("year", sort=True):
        year_path = OUT_YEAR_DIR / f"stock_price_daily_{int(year)}.parquet"
        year_df.sort_values(["date", "symbol"]).to_parquet(year_path, index=False)
    df.sort_values(["date", "symbol"]).to_parquet(OUT_PARQUET, index=False)
    df.sort_values(["date", "symbol"]).to_csv(OUT_CSV, index=False)


def main() -> int:
    args = parse_args()
    universes = _parse_csv_list(args.universes)
    explicit_symbols = _parse_csv_list(args.symbols)
    symbols, source_contract = resolve_symbols(universes, explicit_symbols)
    if args.limit_symbols > 0:
        symbols = symbols[: args.limit_symbols]

    existing = load_existing_dataset()
    db_max = database_max_date()
    source_max = dataset_max_date(existing)
    today = datetime.now(UTC).date()
    inferred_start = max(item for item in [db_max, source_max, DEFAULT_FIRST_HISTORY_DATE] if item is not None)
    start_date = (
        datetime.strptime(args.start_date, "%Y-%m-%d").date()
        if args.start_date
        else inferred_start + timedelta(days=1)
    )
    end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else today

    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_contract": source_contract,
        "requested_symbol_count": len(symbols),
        "db_max_date_before": db_max.isoformat() if db_max else None,
        "dataset_max_date_before": source_max.isoformat() if source_max else None,
        "range_from": start_date.isoformat(),
        "requested_range_to": end_date.isoformat(),
        "output_parquet": str(OUT_PARQUET.relative_to(PROJECT_ROOT)),
        "output_csv": str(OUT_CSV.relative_to(PROJECT_ROOT)),
        "field_definitions": {
            "requested_symbol_count": "Unique DB symbols requested for refresh in this run.",
            "rows_total": "Total symbol-date rows present in the consolidated dataset after refresh.",
            "dataset_unique_symbols_total": "Unique symbols present in the consolidated dataset after refresh.",
            "dataset_max_date_before": "Latest date already present in the local provider cache before this run.",
            "requested_range_to": "Requested provider fetch end date for this run.",
            "max_date": "Latest date actually present in the consolidated dataset after refresh.",
        },
    }
    if args.dry_run or start_date > end_date:
        summary["status"] = "dry_run" if args.dry_run else "noop"
        print(json.dumps(summary, indent=2))
        return 0

    failures: dict[str, Any] = {}
    parts: list[pd.DataFrame] = [existing] if not existing.empty else []
    for idx, symbol in enumerate(symbols, start=1):
        try:
            fyers_symbol = symbol_master.to_fyers(symbol)
            response = fetch_history(
                fyers_symbol=fyers_symbol,
                start_date=start_date,
                end_date=end_date,
                throttle_s=args.throttle,
            )
            candles = response.get("candles") if isinstance(response, dict) else None
            if candles:
                frame = candles_to_frame(symbol, fyers_symbol, candles)
                if not frame.empty:
                    parts.append(frame)
            else:
                failures[symbol] = response
        except Exception as exc:
            failures[symbol] = {"error": str(exc)}
        if idx % 100 == 0:
            print(f"[fyers-equity-daily] processed {idx}/{len(symbols)}")
        time.sleep(args.throttle)

    combined = pd.concat(parts, ignore_index=True) if parts else existing
    if not combined.empty:
        combined["date"] = pd.to_datetime(combined["date"], errors="coerce").dt.date
        combined["year"] = pd.to_datetime(combined["date"], errors="coerce").dt.year
        combined = (
            combined.dropna(subset=["date", "symbol"])
            .drop_duplicates(subset=["date", "symbol"], keep="last")
            .sort_values(["date", "symbol"])
            .reset_index(drop=True)
        )
        write_outputs(combined)

    summary.update(
        {
            "rows_total": int(len(combined)),
            "dataset_unique_symbols_total": int(combined["symbol"].nunique()) if not combined.empty else 0,
            "min_date": combined["date"].min().isoformat() if not combined.empty else None,
            "max_date": combined["date"].max().isoformat() if not combined.empty else None,
            "failed_symbols": len(failures),
        }
    )
    MANIFEST_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    FAILED_PATH.write_text(json.dumps(failures, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

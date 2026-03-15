from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.fyers_client import get_fyers_client
from app.services.symbol_master import symbol_master
from app.services.universe import get_universe_service

PROJECT_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = PROJECT_ROOT / "data_system" / "01_sources" / "fyers_index_prices"
OUT_PARQUET = OUT_DIR / "universe_index_price_daily.parquet"
OUT_CSV = OUT_DIR / "universe_index_price_daily.csv"
SUMMARY_PATH = OUT_DIR / "universe_index_price_summary.json"
MANIFEST_PATH = OUT_DIR / "download_manifest_incremental_index.json"
DEFAULT_FIRST_HISTORY_DATE = date(2016, 1, 1)
logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Canonical FYERS adapter for refreshing consolidated daily index history."
    )
    parser.add_argument("--universes", type=str, default="", help="Comma-separated universe ids")
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--throttle", type=float, default=0.15)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _parse_csv_list(raw: str) -> list[str]:
    return [item.strip().upper() for item in raw.split(",") if item and item.strip()]


def resolve_universes(raw: str) -> list[str]:
    explicit = _parse_csv_list(raw)
    if explicit:
        return explicit
    service = get_universe_service()
    rows = service.list_available_indices()
    return sorted({str(row.get("index_code", "")).upper() for row in rows if row.get("index_code")})


def load_existing_dataset() -> pd.DataFrame:
    if not OUT_PARQUET.exists():
        return pd.DataFrame(columns=["date", "universe_id", "fyers_symbol", "open", "high", "low", "close", "volume", "source"])
    df = pd.read_parquet(OUT_PARQUET)
    df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.date
    return df


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


def candles_to_frame(universe_id: str, fyers_symbol: str, candles: list[list[Any]]) -> pd.DataFrame:
    records: list[dict[str, Any]] = []
    for row in candles or []:
        if not isinstance(row, list) or len(row) < 6:
            continue
        try:
            records.append(
                {
                    "date": datetime.fromtimestamp(int(row[0]), UTC).date(),
                    "universe_id": universe_id,
                    "fyers_symbol": fyers_symbol,
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": int(row[5] or 0),
                    "source": "fyers",
                }
            )
        except (ValueError, TypeError) as exc:
            logger.warning(
                "Skipping malformed index candle for %s (%s): %s row=%s",
                universe_id,
                fyers_symbol,
                exc,
                row,
            )
    return pd.DataFrame.from_records(records)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    args = parse_args()
    universes = resolve_universes(args.universes)
    existing = load_existing_dataset()
    today = datetime.now(UTC).date()
    failures: list[dict[str, Any]] = []
    parts: list[pd.DataFrame] = [existing] if not existing.empty else []
    refresh_summary: dict[str, Any] = {}

    for idx, universe_id in enumerate(universes, start=1):
        scoped_existing = existing[existing["universe_id"] == universe_id] if not existing.empty else pd.DataFrame()
        scoped_max = scoped_existing["date"].max() if not scoped_existing.empty else None
        if args.start_date:
            start_date = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        else:
            start_date = (scoped_max + pd.Timedelta(days=1)) if scoped_max is not None else DEFAULT_FIRST_HISTORY_DATE
            if hasattr(start_date, "date"):
                start_date = start_date.date()
        end_date = datetime.strptime(args.end_date, "%Y-%m-%d").date() if args.end_date else today

        fyers_symbol = symbol_master.to_fyers(universe_id)
        refresh_summary[universe_id] = {
            "fyers_symbol": fyers_symbol,
            "range_from": start_date.isoformat(),
            "range_to": end_date.isoformat(),
        }
        if args.dry_run or start_date > end_date:
            continue

        try:
            response = fetch_history(
                fyers_symbol=fyers_symbol,
                start_date=start_date,
                end_date=end_date,
                throttle_s=args.throttle,
            )
            candles = response.get("candles") if isinstance(response, dict) else None
            if candles:
                frame = candles_to_frame(universe_id, fyers_symbol, candles)
                if not frame.empty:
                    parts.append(frame)
                    refresh_summary[universe_id]["rows_added"] = int(len(frame))
                else:
                    refresh_summary[universe_id]["rows_added"] = 0
            else:
                failures.append({"universe_id": universe_id, "fyers_symbol": fyers_symbol, "response": response})
        except Exception as exc:
            failures.append({"universe_id": universe_id, "fyers_symbol": fyers_symbol, "error": str(exc)})
        if idx % 10 == 0:
            print(f"[fyers-index-daily] processed {idx}/{len(universes)}")
        time.sleep(args.throttle)

    combined = pd.concat(parts, ignore_index=True) if parts else existing
    if not combined.empty:
        combined["date"] = pd.to_datetime(combined["date"], errors="coerce").dt.date
        combined = (
            combined.dropna(subset=["date", "universe_id"])
            .drop_duplicates(subset=["date", "universe_id"], keep="last")
            .sort_values(["date", "universe_id"])
            .reset_index(drop=True)
        )
        combined.to_parquet(OUT_PARQUET, index=False)
        combined.to_csv(OUT_CSV, index=False)

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "universes_requested": universes,
        "universes_processed": len(universes),
        "rows_total": int(len(combined)),
        "universes_total": int(combined["universe_id"].nunique()) if not combined.empty else 0,
        "min_date": combined["date"].min().isoformat() if not combined.empty else None,
        "max_date": combined["date"].max().isoformat() if not combined.empty else None,
        "failed_universes": failures,
        "refresh_summary": refresh_summary,
        "output_parquet": str(OUT_PARQUET.relative_to(PROJECT_ROOT)),
        "output_csv": str(OUT_CSV.relative_to(PROJECT_ROOT)),
        "status": "dry_run" if args.dry_run else "completed",
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    SUMMARY_PATH.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

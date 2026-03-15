from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from app.services.fyers_client import get_fyers_client
from app.services.universe import get_universe_service
from app.services.symbol_master import symbol_master

PROJECT_ROOT = Path(__file__).resolve().parents[2]
ARCHIVE_ROOT = PROJECT_ROOT / "archive" / "fyers_intraday"
IST_TIMEZONE = "Asia/Kolkata"
INTRADAY_MAX_DAYS = 100


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Canonical FYERS adapter for refreshing consolidated intraday archive datasets."
    )
    parser.add_argument("--universe", type=str, default="NIFTY500")
    parser.add_argument("--symbols", type=str, default="", help="Comma-separated DB symbols")
    parser.add_argument("--timeframe", type=str, default="5")
    parser.add_argument("--start-year", type=int, default=2026)
    parser.add_argument("--years", type=int, default=1)
    parser.add_argument("--start-date", type=str, default="")
    parser.add_argument("--end-date", type=str, default="")
    parser.add_argument("--throttle", type=float, default=0.2)
    parser.add_argument("--limit-symbols", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def _parse_csv_list(raw: str) -> list[str]:
    return [item.strip().upper() for item in raw.split(",") if item and item.strip()]


def resolve_symbols(universe: str, explicit_symbols: list[str]) -> list[str]:
    if explicit_symbols:
        return sorted(set(explicit_symbols))
    service = get_universe_service()
    return sorted(set(service.get_symbols(universe.upper())))


def build_windows(args: argparse.Namespace) -> list[tuple[int, date, date]]:
    if args.start_date and args.end_date:
        range_from = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        range_to = datetime.strptime(args.end_date, "%Y-%m-%d").date()
        return [(range_from.year, range_from, range_to)]

    today = datetime.now(UTC).date()
    windows: list[tuple[int, date, date]] = []
    for year in range(args.start_year, args.start_year - args.years, -1):
        range_from = date(year, 1, 1)
        range_to = min(date(year, 12, 31), today) if year == today.year else date(year, 12, 31)
        if range_from <= range_to:
            windows.append((year, range_from, range_to))
    return windows


def iter_subranges(range_from: date, range_to: date, *, max_days: int = INTRADAY_MAX_DAYS):
    current = range_from
    step = timedelta(days=max_days - 1)
    while current <= range_to:
        chunk_end = min(current + step, range_to)
        yield current, chunk_end
        current = chunk_end + timedelta(days=1)


def fetch_history(
    fyers_symbol: str,
    timeframe: str,
    range_from: date,
    range_to: date,
    throttle_s: float,
    retries: int = 3,
) -> dict[str, Any]:
    client = get_fyers_client()
    if not client or not client.fyers or not client.validate_token():
        return {"s": "error", "message": "FYERS client unavailable or token invalid"}
    last_response: dict[str, Any] = {}
    for attempt in range(retries):
        last_response = client.get_historical_data(
            fyers_symbol,
            timeframe,
            range_from.isoformat(),
            range_to.isoformat(),
        )
        if isinstance(last_response, dict) and last_response.get("s") == "ok":
            return last_response
        time.sleep(throttle_s * (attempt + 1))
    return last_response


def candles_to_frame(symbol: str, fyers_symbol: str, candles: list[list[Any]]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame(columns=["symbol", "fyers_symbol", "timestamp", "open", "high", "low", "close", "volume"])
    frame = pd.DataFrame(candles, columns=["epoch", "open", "high", "low", "close", "volume"])
    frame["timestamp"] = (
        pd.to_datetime(frame["epoch"], unit="s", utc=True).dt.tz_convert(IST_TIMEZONE).dt.tz_localize(None)
    )
    frame["symbol"] = symbol
    frame["fyers_symbol"] = fyers_symbol
    frame = frame[["symbol", "fyers_symbol", "timestamp", "open", "high", "low", "close", "volume"]]
    return frame.drop_duplicates(subset=["symbol", "timestamp"]).sort_values(["timestamp", "symbol"]).reset_index(drop=True)


def main() -> int:
    args = parse_args()
    explicit_symbols = _parse_csv_list(args.symbols)
    symbols = resolve_symbols(args.universe, explicit_symbols)
    if args.limit_symbols > 0:
        symbols = symbols[: args.limit_symbols]
    windows = build_windows(args)
    scope_label = args.universe.upper() if not explicit_symbols else "explicit_symbols"
    out_root = ARCHIVE_ROOT / scope_label.lower() / f"timeframe={args.timeframe}min"

    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "scope": scope_label,
        "timeframe": args.timeframe,
        "symbol_count": len(symbols),
        "windows": [],
        "output_root": str(out_root.relative_to(PROJECT_ROOT)),
        "status": "dry_run" if args.dry_run else "completed",
    }
    if args.dry_run:
        for year, range_from, range_to in windows:
            summary["windows"].append({"year": year, "range_from": range_from.isoformat(), "range_to": range_to.isoformat()})
        print(json.dumps(summary, indent=2))
        return 0

    out_root.mkdir(parents=True, exist_ok=True)
    for year, range_from, range_to in windows:
        year_parts: list[pd.DataFrame] = []
        failures: list[dict[str, Any]] = []
        for idx, symbol in enumerate(symbols, start=1):
            fyers_symbol = symbol_master.to_fyers(symbol)
            all_chunks: list[pd.DataFrame] = []
            symbol_failed = False
            for chunk_from, chunk_to in iter_subranges(range_from, range_to):
                response = fetch_history(
                    fyers_symbol=fyers_symbol,
                    timeframe=args.timeframe,
                    range_from=chunk_from,
                    range_to=chunk_to,
                    throttle_s=args.throttle,
                )
                if not isinstance(response, dict) or response.get("s") != "ok":
                    failures.append(
                        {
                            "symbol": symbol,
                            "fyers_symbol": fyers_symbol,
                            "year": year,
                            "chunk_from": chunk_from.isoformat(),
                            "chunk_to": chunk_to.isoformat(),
                            "response": response,
                        }
                    )
                    symbol_failed = True
                    break
                frame = candles_to_frame(symbol, fyers_symbol, response.get("candles", []))
                if not frame.empty:
                    all_chunks.append(frame)
                time.sleep(args.throttle)
            if not symbol_failed and all_chunks:
                year_parts.append(pd.concat(all_chunks, ignore_index=True))
            if idx % 100 == 0:
                print(f"[fyers-intraday-archive] year={year} processed {idx}/{len(symbols)}")

        combined = pd.concat(year_parts, ignore_index=True) if year_parts else pd.DataFrame(
            columns=["symbol", "fyers_symbol", "timestamp", "open", "high", "low", "close", "volume"]
        )
        if not combined.empty:
            combined["timestamp"] = pd.to_datetime(combined["timestamp"], errors="coerce")
            combined = (
                combined.dropna(subset=["symbol", "timestamp"])
                .drop_duplicates(subset=["symbol", "timestamp"], keep="last")
                .sort_values(["timestamp", "symbol"])
                .reset_index(drop=True)
            )
        out_path = out_root / f"{scope_label.lower()}_{args.timeframe}min_{year}.parquet"
        combined.to_parquet(out_path, index=False)
        fail_path = out_root / f"{scope_label.lower()}_{args.timeframe}min_{year}_failures.json"
        fail_path.write_text(json.dumps(failures, indent=2), encoding="utf-8")
        summary["windows"].append(
            {
                "year": year,
                "range_from": range_from.isoformat(),
                "range_to": range_to.isoformat(),
                "rows": int(len(combined)),
                "symbols": int(combined["symbol"].nunique()) if not combined.empty else 0,
                "failed_symbols": len({item["symbol"] for item in failures}),
                "output_parquet": str(out_path.relative_to(PROJECT_ROOT)),
                "failure_log": str(fail_path.relative_to(PROJECT_ROOT)),
            }
        )

    manifest_path = out_root / "manifest.json"
    manifest_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

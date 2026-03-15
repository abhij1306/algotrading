"""
Deprecated one-shot FYERS stock history builder.

Canonical maintained command:
    python backend/scripts/refresh_fyers_equity_daily.py
"""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[2]
BHAVCOPY_DIR = PROJECT_ROOT / "data_system" / "01_sources" / "nse_bhavcopy"
OUT_DIR = PROJECT_ROOT / "data_system" / "01_sources" / "fyers_stock_prices"
OUT_YEAR_DIR = OUT_DIR / "yearly"
OUT_PARQUET = OUT_DIR / "stock_price_daily.parquet"
OUT_CSV = OUT_DIR / "stock_price_daily.csv"
MANIFEST_PATH = OUT_DIR / "download_manifest_stock_2016_2026.json"
FAILED_PATH = OUT_DIR / "failed_symbols_by_year.json"


@dataclass(frozen=True)
class YearWindow:
    year: int
    start: date
    end: date


def _parse_bhav_date_from_name(path: Path) -> date | None:
    # filename format: sec_bhavdata_full_DDMMYYYY.csv
    stem = path.stem
    token = stem.rsplit("_", 1)[-1]
    if len(token) != 8 or not token.isdigit():
        return None
    try:
        return datetime.strptime(token, "%d%m%Y").date()
    except ValueError:
        return None


def _load_yesterday_bhavcopy_symbols() -> tuple[list[str], Path, date]:
    files = list(BHAVCOPY_DIR.glob("sec_bhavdata_full_*.csv"))
    dated: list[tuple[Path, date]] = []
    for f in files:
        d = _parse_bhav_date_from_name(f)
        if d:
            dated.append((f, d))
    if len(dated) < 2:
        raise RuntimeError("Need at least 2 bhavcopy files to identify 'yesterday' universe")

    dated.sort(key=lambda x: x[1])
    latest_date = dated[-1][1]
    target = latest_date - timedelta(days=1)
    yesterday_file = next((f for f, d in reversed(dated) if d == target), None)
    if yesterday_file is None:
        # fallback: previous available bhavcopy before latest
        yesterday_file = dated[-2][0]
        target = dated[-2][1]

    df = pd.read_csv(yesterday_file)
    df.columns = [c.strip().upper() for c in df.columns]
    if "SERIES" not in df.columns or "SYMBOL" not in df.columns:
        raise RuntimeError(f"Unexpected bhavcopy schema in {yesterday_file}")
    eq = df[df["SERIES"].astype(str).str.strip().str.upper() == "EQ"].copy()
    symbols = sorted(set(eq["SYMBOL"].astype(str).str.strip().str.upper().tolist()))
    if not symbols:
        raise RuntimeError(f"No EQ symbols found in {yesterday_file}")
    return symbols, yesterday_file, target


def _fetch_history_with_retry(
    fyers_client: Any,
    fyers_symbol: str,
    start_date: date,
    end_date: date,
    retries: int = 3,
    sleep_s: float = 0.3,
) -> dict[str, Any]:
    payload = {
        "symbol": fyers_symbol,
        "resolution": "D",
        "date_format": "1",
        "range_from": start_date.isoformat(),
        "range_to": end_date.isoformat(),
        "cont_flag": "1",
    }
    last_resp: dict[str, Any] = {}
    for attempt in range(retries):
        try:
            resp = fyers_client.fyers.history(payload)
            if isinstance(resp, dict):
                last_resp = resp
                if resp.get("s") == "ok":
                    return resp
        except Exception:
            pass
        time.sleep(sleep_s * (attempt + 1))
    return last_resp


def _build_year_windows(year_start: int, year_end: int) -> list[YearWindow]:
    # descending: 2026, 2025, ... 2016
    today = datetime.now(UTC).date()
    windows: list[YearWindow] = []
    for y in range(year_end, year_start - 1, -1):
        start = date(y, 1, 1)
        end = date(y, 12, 31)
        if y == today.year:
            end = today
        windows.append(YearWindow(year=y, start=start, end=end))
    return windows


def run(year_start: int = 2016, year_end: int = 2026, throttle_s: float = 0.3) -> None:
    from backend.app.services.fyers_client import get_fyers_client
    from backend.app.services.symbol_master import symbol_master

    OUT_YEAR_DIR.mkdir(parents=True, exist_ok=True)

    symbols, bhav_file, bhav_date = _load_yesterday_bhavcopy_symbols()
    fyers = get_fyers_client()
    if not fyers or not fyers.fyers:
        raise RuntimeError("Fyers client unavailable. Ensure token is valid.")

    windows = _build_year_windows(year_start=year_start, year_end=year_end)
    summary: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "universe_source_file": str(bhav_file.relative_to(PROJECT_ROOT)),
        "universe_source_date": bhav_date.isoformat(),
        "symbol_count": len(symbols),
        "years": {},
    }
    failed: dict[str, list[str]] = {}

    all_parts: list[pd.DataFrame] = []
    for window in windows:
        year = window.year
        year_path = OUT_YEAR_DIR / f"stock_price_daily_{year}.parquet"
        year_rows = 0
        ok_symbols = 0
        failed_symbols: list[str] = []
        records: list[dict[str, Any]] = []

        print(f"[fyers-stock-build] Year {year} start ({window.start} -> {window.end}), symbols={len(symbols)}")
        for idx, symbol in enumerate(symbols, start=1):
            try:
                fyers_symbol = symbol_master.to_fyers(symbol)
            except Exception:
                failed_symbols.append(symbol)
                continue

            resp = _fetch_history_with_retry(
                fyers_client=fyers,
                fyers_symbol=fyers_symbol,
                start_date=window.start,
                end_date=window.end,
                retries=3,
                sleep_s=throttle_s,
            )
            candles = resp.get("candles") if isinstance(resp, dict) else None
            if not candles:
                failed_symbols.append(symbol)
            else:
                ok_symbols += 1
                for row in candles:
                    # [ts, open, high, low, close, volume]
                    if not isinstance(row, list) or len(row) < 6:
                        continue
                    ts = int(row[0])
                    d = datetime.fromtimestamp(ts, UTC).date()
                    records.append(
                        {
                            "date": d,
                            "symbol": symbol,
                            "open": float(row[1]),
                            "high": float(row[2]),
                            "low": float(row[3]),
                            "close": float(row[4]),
                            "volume": int(row[5] or 0),
                            "source": "fyers",
                            "fyers_symbol": fyers_symbol,
                            "year": year,
                        }
                    )
            if idx % 100 == 0:
                print(f"[fyers-stock-build] Year {year}: processed {idx}/{len(symbols)}")
            time.sleep(throttle_s)

        year_df = pd.DataFrame.from_records(records)
        if not year_df.empty:
            year_df = year_df.drop_duplicates(subset=["date", "symbol"], keep="last")
            year_df = year_df.sort_values(["date", "symbol"]).reset_index(drop=True)
            year_df.to_parquet(year_path, index=False)
            year_rows = int(len(year_df))
            all_parts.append(year_df)
        elif year_path.exists():
            year_path.unlink()

        failed[str(year)] = sorted(set(failed_symbols))
        summary["years"][str(year)] = {
            "range_from": window.start.isoformat(),
            "range_to": window.end.isoformat(),
            "rows": year_rows,
            "symbols_ok": ok_symbols,
            "symbols_failed": len(failed[str(year)]),
            "output_parquet": str(year_path.relative_to(PROJECT_ROOT)) if year_rows > 0 else None,
        }
        print(
            f"[fyers-stock-build] Year {year} done: rows={year_rows}, ok={ok_symbols}, failed={len(failed[str(year)])}"
        )

    # Consolidate all yearly partitions that exist, including prior runs
    partitions = sorted(OUT_YEAR_DIR.glob("stock_price_daily_*.parquet"))
    dfs = [pd.read_parquet(p) for p in partitions if p.exists()]
    if not dfs:
        raise RuntimeError("No yearly output generated.")
    combined = pd.concat(dfs, ignore_index=True)
    combined["date"] = pd.to_datetime(combined["date"], errors="coerce").dt.date
    combined = combined.dropna(subset=["date", "symbol"])
    combined = combined.drop_duplicates(subset=["date", "symbol"], keep="last")
    combined = combined.sort_values(["date", "symbol"]).reset_index(drop=True)
    combined.to_parquet(OUT_PARQUET, index=False)
    combined.to_csv(OUT_CSV, index=False)

    summary["consolidated"] = {
        "rows": int(len(combined)),
        "symbols": int(combined["symbol"].nunique()),
        "min_date": combined["date"].min().isoformat(),
        "max_date": combined["date"].max().isoformat(),
        "output_parquet": str(OUT_PARQUET.relative_to(PROJECT_ROOT)),
        "output_csv": str(OUT_CSV.relative_to(PROJECT_ROOT)),
    }
    MANIFEST_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    FAILED_PATH.write_text(json.dumps(failed, indent=2), encoding="utf-8")

    print(f"[fyers-stock-build] Done. consolidated_rows={summary['consolidated']['rows']}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Build Fyers stock historical dataset from bhavcopy universe")
    parser.add_argument("--year-start", type=int, default=2016)
    parser.add_argument("--year-end", type=int, default=2026)
    parser.add_argument("--throttle", type=float, default=0.3, help="Sleep seconds between requests")
    args = parser.parse_args()
    run(year_start=args.year_start, year_end=args.year_end, throttle_s=args.throttle)


if __name__ == "__main__":
    main()

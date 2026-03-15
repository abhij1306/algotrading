from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "data_system" / "01_sources" / "fyers_intraday_5min" / "nifty500"
ARCHIVE_ROOT = PROJECT_ROOT / "archive" / "fyers_intraday" / "nifty500" / "timeframe=5min"
MANIFEST_PATH = ARCHIVE_ROOT / "manifest.json"
logger = logging.getLogger(__name__)


def main() -> int:
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    summary: dict[str, object] = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source_root": str(SOURCE_ROOT.relative_to(PROJECT_ROOT)),
        "archive_root": str(ARCHIVE_ROOT.relative_to(PROJECT_ROOT)),
        "years": {},
    }

    year_dirs = sorted(SOURCE_ROOT.glob("year=*"))
    for year_dir in year_dirs:
        year_label = year_dir.name.replace("year=", "")
        files = sorted(year_dir.glob("*.parquet"))
        if not files:
            continue
        parts: list[pd.DataFrame] = []
        for path in files:
            try:
                parts.append(pd.read_parquet(path))
            except Exception as exc:
                logger.warning("Skipping unreadable parquet %s: %s", path, exc)
        if not parts:
            logger.warning("No readable intraday parquet files found for year=%s", year_label)
            summary["years"][year_label] = {
                "source_files": len(files),
                "rows": 0,
                "symbols": 0,
                "min_timestamp": None,
                "max_timestamp": None,
                "output_parquet": None,
                "status": "skipped_no_readable_parts",
            }
            continue
        combined = pd.concat(parts, ignore_index=True)
        combined["timestamp"] = pd.to_datetime(combined["timestamp"], errors="coerce")
        combined = (
            combined.dropna(subset=["symbol", "timestamp"])
            .drop_duplicates(subset=["symbol", "timestamp"], keep="last")
            .sort_values(["timestamp", "symbol"])
            .reset_index(drop=True)
        )
        out_path = ARCHIVE_ROOT / f"nifty500_5min_{year_label}.parquet"
        combined.to_parquet(out_path, index=False)
        summary["years"][year_label] = {
            "source_files": len(files),
            "rows": int(len(combined)),
            "symbols": int(combined["symbol"].nunique()),
            "min_timestamp": combined["timestamp"].min().isoformat() if not combined.empty else None,
            "max_timestamp": combined["timestamp"].max().isoformat() if not combined.empty else None,
            "output_parquet": str(out_path.relative_to(PROJECT_ROOT)),
        }
        print(
            f"[intraday-consolidate] year={year_label} files={len(files)} rows={len(combined)} output={out_path.name}"
        )

    MANIFEST_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

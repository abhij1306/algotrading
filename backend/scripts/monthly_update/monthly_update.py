"""
Monthly Update Orchestrator
=========================
Main script for monthly NSE index data updates.
"""

from __future__ import annotations

import argparse
import logging
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _parse_month(month: str) -> tuple[int, int]:
    if not MONTH_RE.match(month):
        raise ValueError(f"Invalid month format: {month}. Expected YYYY-MM format.")
    year_str, month_str = month.split("-")
    return int(year_str), int(month_str)


def _next_month_start(year: int, month: int) -> date:
    if month == 12:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


@dataclass(frozen=True)
class MonthlyUpdateConfig:
    month: str
    dry_run: bool
    validate: bool

    @property
    def year_month(self) -> tuple[int, int]:
        return _parse_month(self.month)

    @property
    def target_date(self) -> date:
        year, month = self.year_month
        return date(year, month, 1)

    @property
    def next_month(self) -> date:
        year, month = self.year_month
        return _next_month_start(year, month)

    @property
    def data_dir(self) -> Path:
        return Path(__file__).resolve().parents[2] / "data_system" / "03_universe" / "monthly_universe_raw" / self.month


class MonthlyUpdateOrchestrator:
    INDICES: list[str] = [
        "NIFTY50",
        "NIFTY100",
        "NIFTY200",
        "NIFTY500",
        "NIFTYMIDCAP50",
        "NIFTYMIDCAP100",
        "NIFTYSMLCAP100",
        "NIFTYBANK",
        "NIFTYIT",
        "NIFTYPHARMA",
        "NIFTYAUTO",
        "NIFTYMETAL",
        "NIFTYFMCG",
        "NIFTYENERGY",
        "NIFTYREALTY",
    ]

    def __init__(self, config: MonthlyUpdateConfig) -> None:
        self.config = config
        self.config.data_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict[str, Any]:
        logger.info("=" * 60)
        logger.info("NSE Monthly Update - %s", self.config.month)
        logger.info("=" * 60)

        results: dict[str, Any] = {
            "month": self.config.month,
            "dry_run": self.config.dry_run,
            "validate": self.config.validate,
            "downloaded": [],
            "parsed": [],
            "imported": [],
            "changes": {"additions": [], "removals": [], "weight_changes": []},
            "errors": [],
        }

        logger.info("Monthly update scaffold initialized at: %s", self.config.data_dir)
        return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    parser = argparse.ArgumentParser(description="Monthly NSE Index Update")
    parser.add_argument("--month", required=True, help="Month in YYYY-MM format (e.g., 2024-03)")
    parser.add_argument("--dry-run", action="store_true", help="Don't commit to database")
    parser.add_argument("--no-validate", action="store_true", help="Skip validation")
    args = parser.parse_args()

    config = MonthlyUpdateConfig(month=args.month, dry_run=args.dry_run, validate=not args.no_validate)
    orchestrator = MonthlyUpdateOrchestrator(config=config)
    orchestrator.run()


if __name__ == "__main__":
    main()

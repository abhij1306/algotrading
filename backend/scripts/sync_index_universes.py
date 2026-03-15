from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.database import ensure_universe_schema, get_db_session
from app.services.universe_sync import sync_index_universes_from_loader


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync index universes from canonical CSV files into DB.")
    parser.add_argument("--as-of-date", help="Effective date in YYYY-MM-DD format. Defaults to today.")
    parser.add_argument(
        "--skip-company-categories",
        action="store_true",
        help="Do not refresh companies.broad_market and companies.sector_index.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    as_of_date = datetime.strptime(args.as_of_date, "%Y-%m-%d").date() if args.as_of_date else None
    session = get_db_session()
    try:
        ensure_universe_schema()
        summary = sync_index_universes_from_loader(
            session,
            as_of_date=as_of_date,
            update_company_categories=not args.skip_company_categories,
        )
        print(json.dumps(summary.__dict__, indent=2))
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())

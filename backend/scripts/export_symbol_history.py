"""One-off export of the legacy `symbol_history` table to CSV (owner decision D68).

The legacy models and the table's other users are deleted; HAT rebuilds symbol renames from NSE
data (`app.hat.data.instruments.seed_symbol_history` takes an injected record source). Run this
once against the old database to keep the rows, then feed the CSV to that seed if wanted:

    python scripts/export_symbol_history.py --out symbol_history.csv

It only reads. If the table does not exist the script says so and writes nothing.
"""

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text  # noqa: E402

from app.database import engine  # noqa: E402

TABLE = "symbol_history"
COLUMNS = (
    "id",
    "old_symbol",
    "new_symbol",
    "old_company_name",
    "new_company_name",
    "change_type",
    "effective_from",
    "effective_to",
    "isin",
    "conversion_ratio",
    "notes",
    "source",
    "created_at",
    "updated_at",
)


def export(out: Path) -> int:
    """Write every row to `out` (oldest effective date first); return the row count, or -1 when
    the table is missing."""
    if TABLE not in inspect(engine).get_table_names():
        return -1
    present = {column["name"] for column in inspect(engine).get_columns(TABLE)}
    columns = [name for name in COLUMNS if name in present]
    select = f"SELECT {', '.join(columns)} FROM {TABLE} ORDER BY effective_from, id"  # noqa: S608
    with engine.connect() as connection, out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        count = 0
        for row in connection.execute(text(select)):
            writer.writerow(row)
            count += 1
    return count


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=Path("symbol_history.csv"))
    args = parser.parse_args()
    count = export(args.out)
    if count < 0:
        target = engine.url.render_as_string(hide_password=True)
        print(f"No {TABLE} table in {target}; nothing written.")
        return 1
    print(f"Wrote {count} rows to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

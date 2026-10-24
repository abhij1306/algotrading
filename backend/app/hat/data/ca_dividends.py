"""The one-off dividend import (CA3): equity dividends from NSE's corporate-actions feed, and the
value a bonus of debentures or preference shares hands out, through the checks and verdicts of
`ca_payouts`, stored in `hat_corporate_action` as price-neutral `dividend` rows (factor 1) with
the cash per share in `cash_amount`.

Inputs, all untrusted and read as text:
- the feed: a directory of `ca_YYYY-MM.json` files, each the JSON list NSE's corporate-actions
  API (`/api/corporates-corporateActions?index=equities`) answers for one month (every purpose
  type; only dividends are read). Pulled once by the operator; no HAT code calls NSE.
- optionally the research file f (`result_f_bonus_debentures.csv`): the value per share of a
  bonus of debentures, NCRPS or preference shares (D1 keeps them price-neutral in the view). A
  high or medium confidence row is a distribution of that value, a low one goes to review.
- or one night's `bc` file (`--bc`), the catch-up of a night the nightly import (`ca_nightly`)
  could not download it.

`--apply` writes in one transaction and journals each row as `corporate_action.imported`; a
repeat stores nothing.

A stored amount moves the total-return series the signals read (`store.bars_daily`) and pays
the cash a backtest position (and a live or shadow trade, `engine/trade_dividends.py`) earns on
the ex-date; the price basis (`bars_daily_cap`), the rescale and the GTT check read only the
price-changing rows. After `--apply`, run `lineage build` and `features --full`: the stored
features' inputs digest covers the dividends.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path

from ..money import exact
from .ca_bc import FeedRow, any_date, cell, read_bc
from .ca_gate import CONFLICT, REVIEW, STATUSES, STORED, CaFileError, nse_date
from .ca_nightly import bc_journal, nightly_window
from .ca_payouts import (
    KIND_BONUS,
    SOURCE_FEED,
    T_BONUS,
    T_FEED,
    T_NIGHTLY,
    TABLES,
    Outcome,
    Payout,
    Proposal,
    apply,
    evaluate,
    total,
)
from .store import DataStore

MAX_FILE_BYTES = 20_000_000
MAX_FILES = 400
MAX_ROWS = 5000  # research file f
BONUS_COLUMNS = (
    "symbol",
    "isin",
    "ex_date",
    "instrument",
    "value_per_share_rs",
    "value_basis",
    "confidence",
    "source_url",
    "quote",
    "notes",
)
SOURCE_DATE = "source_date"  # optional: when the research row's source was published (CA5)
STORED_CONFIDENCE = frozenset({"high", "medium"})


_SOME_HOLDERS = re.compile(
    r"non[- ]?promoter|promoters? excluded|only (?:to |for )?(?:the )?public"
)


def bonus_value(row: Mapping[str, str]) -> tuple[Payout, ...] | str:
    """A research file f row's distribution, or why it is not stored: `low_confidence`,
    `some_holders_only` (not every share received it, so it is no per-share distribution:
    MBLIMITED 2023's NCRPS went to non-promoters only), `bad_value`."""
    if row["confidence"].strip().lower() not in STORED_CONFIDENCE:
        return "low_confidence"
    if _SOME_HOLDERS.search(f"{row['instrument']} {row['notes']}".lower()):
        return "some_holders_only"
    value = exact(row["value_per_share_rs"].strip(), positive=True)
    if value is None:
        return "bad_value"
    return (Payout(KIND_BONUS, value),)


# --- input files --------------------------------------------------------------------------------


_FEED_FILE = re.compile(r"ca_\d{4}-\d{2}\.json")


def read_feed(directory: Path) -> tuple[list[FeedRow], str]:
    """Every row of the feed files in `directory` (`ca_YYYY-MM.json`), with the digest of the
    files read. A file too large, not a JSON list, or too many files is refused (`CaFileError`);
    an element that is not an object is skipped."""
    files = sorted(p for p in directory.iterdir() if _FEED_FILE.fullmatch(p.name))
    if not files:
        raise CaFileError("feed_missing")
    if len(files) > MAX_FILES:
        raise CaFileError("feed_too_large")
    sha = hashlib.sha256()
    rows: list[FeedRow] = []
    for path in files:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise CaFileError("feed_too_large")
        data = path.read_bytes()
        sha.update(path.name.encode() + b"\0" + data)
        try:
            items = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CaFileError("feed_unreadable") from exc
        if not isinstance(items, list):
            raise CaFileError("feed_unreadable")
        for item in items:
            if not isinstance(item, dict):
                continue
            rows.append(
                FeedRow(
                    cell(item.get("symbol")).upper(),
                    cell(item.get("isin")).upper(),
                    cell(item.get("series")).upper(),
                    nse_date(cell(item.get("exDate"))),
                    cell(item.get("subject")),
                )
            )
    return rows, sha.hexdigest()


def read_bonus_file(path: Path) -> list[dict[str, str]]:
    """Research file f's rows as text; refuses a file too large, not UTF-8 or missing a
    column."""
    if path.stat().st_size > MAX_FILE_BYTES:
        raise CaFileError("research_file_too_large")
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CaFileError("research_file_unreadable") from exc
    try:
        reader = csv.DictReader(io.StringIO(text))
        if not set(BONUS_COLUMNS) <= set(reader.fieldnames or ()):
            raise CaFileError("research_file_columns")
        out = []
        for row in reader:
            if len(out) >= MAX_ROWS:
                raise CaFileError("research_file_too_large")
            out.append({k: cell(row.get(k)) for k in (*BONUS_COLUMNS, SOURCE_DATE)})
    except csv.Error as exc:
        raise CaFileError("research_file_unreadable") from exc
    return out


# --- proposals ----------------------------------------------------------------------------------


def bonus_proposals(rows: Sequence[Mapping[str, str]]) -> list[Proposal]:
    out = []
    for line, row in enumerate(rows, start=2):  # line 1 is the header
        text = (
            f"{row['instrument'] or 'Bonus instrument'}: distribution Rs "
            f"{row['value_per_share_rs']} per share ({row['value_basis'] or 'value'}; research f "
            f"{row['confidence'].lower() or 'no confidence'})"
        )
        p = Proposal(
            T_BONUS,
            str(line),
            row["symbol"].upper(),
            row["isin"].upper(),
            any_date(row["ex_date"]),
            text,
            hint=f"{row['source_url'][:200]} {row['notes'][:200]}".strip(),
            source_day=any_date(row.get(SOURCE_DATE, "")),
        )
        parts = bonus_value(row)
        if p.ex_date is None:
            out.append(p.ends(REVIEW, "bad_ex_date"))
        elif isinstance(parts, str):
            out.append(p.ends(REVIEW, parts))
        else:
            out.append(replace(p, parts=parts))
    return out


# --- the report ---------------------------------------------------------------------------------


def report_lines(outcomes: Sequence[Outcome], *, applied: bool, everything: bool) -> list[str]:
    """Counts per input and status, the reasons, then the conflict and review rows (every row
    when `everything`, the stored ones too)."""
    counts: Counter[tuple[str, str]] = Counter((o.proposal.table, o.status) for o in outcomes)
    label = {STORED: "stored" if applied else "to_store"}
    lines = [
        "input               " + " ".join(f"{label.get(s, s):>9}" for s in STATUSES) + "    total"
    ]
    for table in TABLES:
        cells = [counts[(table, s)] for s in STATUSES]
        lines.append(f"{table:<20}" + " ".join(f"{c:>9}" for c in cells) + f" {sum(cells):>8}")
    added = sum(1 for o in outcomes if o.status == STORED and o.fill is not None)
    rows = sum(
        1 for o in outcomes if o.status == STORED and o.record is not None and o.fill is None
    )
    lines.append(
        f"rows {'written' if applied else 'to write'}: dividend {rows}, amounts added {added}"
    )
    for table in TABLES:
        reasons = Counter(
            (o.status, o.reason) for o in outcomes if o.proposal.table == table and o.reason
        )
        lines.append(
            f"reasons {table}: "
            + ", ".join(f"{s}/{r} {n}" for (s, r), n in sorted(reasons.items()))
        )
    shown = STATUSES if everything else (CONFLICT, REVIEW)
    for status in shown:
        for o in outcomes:
            if o.status != status:
                continue
            p = o.proposal
            amount = o.record.cash_amount if o.record is not None else total(p.parts)
            lines.append(
                "\t".join(
                    [
                        label.get(o.status, o.status).upper(),
                        o.reason,
                        p.table,
                        p.symbol,
                        p.isin,
                        p.ex_date.isoformat() if p.ex_date else "-",
                        f"{amount.normalize():f}" if amount else "-",
                        p.text[:160],
                        o.detail,
                        p.hint[:160],
                    ]
                )
            )
    return lines


# --- command line -------------------------------------------------------------------------------


def input_path(name: str, *, directory: bool = False, suffix: str = "") -> Path:
    """A path named on the command line, under the working directory (a directory, or a file with
    `suffix`): anything else is refused before it is opened."""
    base = Path.cwd().resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base) or (not directory and target.suffix.lower() != suffix):
        raise CaFileError("ca_file_outside_cwd")
    if not (target.is_dir() if directory else target.is_file()):
        raise CaFileError("ca_file_missing")
    return target


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.data.ca_dividends (--feed DIR [--bonus F.csv] | --bc F.csv --day DATE)
    [--apply] [--all]

    Reads NSE's corporate-actions feed (DIR of `ca_YYYY-MM.json`) and the research file f, or one
    night's `bc` file of NSE's PR archive (the catch-up of a night `ca.adjust` could not download
    it: the dividends of `--day` and the days before, as the nightly reads them), all under the
    working directory, read-only, and the raw bars under `HAT_DATA_ROOT`, and prints what it
    would store (a dry run) or, with `--apply`, stores it in one transaction. The report goes to
    stdout; `--all` lists every event."""
    parser = argparse.ArgumentParser(prog="python -m app.hat.data.ca_dividends")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--feed")
    source.add_argument("--bc", help="a night's bc file of NSE's PR archive (.csv)")
    parser.add_argument("--day", type=date.fromisoformat, help="the bc file's trade date")
    parser.add_argument("--bonus", help="research file f (.csv)")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args(argv)
    if args.bc and (args.day is None or args.bonus):
        parser.error("--bc takes --day and no --bonus")
    try:
        if args.bc:
            bc_path = input_path(args.bc, suffix=".csv")
            bc_text = bc_path.read_text(encoding="utf-8-sig", errors="replace")
            feed, feed_sha = nightly_window(read_bc(bc_text), args.day), ""
            bonus_path, bonus = None, []
        else:
            feed, feed_sha = read_feed(input_path(args.feed, directory=True))
            bonus_path = input_path(args.bonus, suffix=".csv") if args.bonus else None
            bonus = read_bonus_file(bonus_path) if bonus_path else []
    except CaFileError as exc:
        sys.stderr.write(f"refused: {exc.code}\n")
        return 2
    from ..settings import operator_settings

    settings = operator_settings()  # before the database module, which reads .env too
    from app.database import HatSessionLocal

    from ..time import utc_now

    store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
    table = T_NIGHTLY if args.bc else T_FEED
    with HatSessionLocal() as session:
        outcomes = evaluate(session, store, feed, bonus_proposals(bonus), table)
        if args.apply:
            journal = (
                bc_journal(bc_text, args.day)
                if args.bc
                else {"source": SOURCE_FEED, "feed_sha256": feed_sha}
            )
            if bonus_path is not None:
                journal["research_sha256"] = {
                    "f": hashlib.sha256(bonus_path.read_bytes()).hexdigest()
                }
            created, added = apply(session, outcomes, journal=journal, now=utc_now())
            session.commit()
            sys.stdout.write(f"applied: {created} rows, {added} amounts added\n")
        else:
            session.rollback()
    lines = report_lines(outcomes, applied=args.apply, everything=args.all)
    sys.stdout.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

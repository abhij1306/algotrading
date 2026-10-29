"""Data accuracy verification (W1, owner 2026-10-29): each check compares the stored data with
an independent reference, never with HAT's own output, and the run writes one report.

    python -m app.hat.data.verify [--check D1 ...] [--from YYYY-MM-DD] [--to YYYY-MM-DD]
        [--report DIR] [--ca-data DIR] [--notices DIR] [--kotak DIR] [--fetch]

The checks (one module each; each `run(ctx)` returns an audit check, `audit.check_result`):
- D1 `rawbars`: every stored raw daily bar against its row in NSE's bhavcopy file under `raw/`,
  read by a reader of its own (OHLC, previous close, volume, traded value, symbol, series), and
  every equity row of the file that is not stored, by why.
- D2 `adjustments`: every stored split, bonus, rights, demerger and dividend against NSE's
  corporate-action pulls (`--ca-data`); the price-basis series scanned for one-day moves over 20%
  that no action or results announcement explains; and a sample of NIFTY 500 stocks' yearly
  total return against a second adjusted-price source (Yahoo Finance).
- D3 `membership`: NIFTY 500 members per session, every change against the parsed NSE notices
  (`--notices`), and the stored members on each archived constituent list's date against it.
- D4 `bars5m`: the 5-minute reject days classified against the daily bar and the Kotak raw
  files (`--kotak`), coverage by year and the gap-day bias.
- D5 `filings`: each results filing's `public_at` against NSE's dissemination time
  (`--ca-data`).
- D6 `sessions`: the calendar against NSE's holiday lists and the bhavcopy days; the ISIN lineage
  chains and suspensions.
- D7 `series`: the stored index levels against NSE's daily index files, the TRI against
  niftyindices' kept answers (`index_history.verify_tri`), and the ETF store's closes against
  the bhavcopy.

Every check is read-only: the database under a read-only transaction, the stores as files. A
check that needs a public reference file not yet on disk downloads it only with `--fetch`, through
the data layer's fetchers and pacer, into `raw/reference/`.

The report (`verify.json`, `verify.md`) goes to `HAT_DATA_ROOT/audits/verify-<date>/` unless
`--report` names a folder. A backtest's data audit reads the newest report whose window covers its
own (`status_for`): a blocking check that failed, or none run, marks the run unverified.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..audit import FAIL, PASS, SKIPPED, WARN

__all__ = [
    "BLOCKING",
    "CHECKS",
    "REPORT_FORMAT",
    "covering_reports",
    "report_doc",
    "reports_dir",
    "status_for",
    "window_status",
    "write_report",
]

REPORT_FORMAT = "data-verify/1"
REPORT_JSON = "verify.json"
REPORT_MD = "verify.md"
CHECKS = {
    "D1": "Daily bars vs NSE bhavcopy",
    "D2": "Adjustments vs NSE corporate actions",
    "D3": "NIFTY 500 membership vs NSE notices and lists",
    "D4": "5-minute bars vs daily bars and Kotak raw",
    "D5": "Results filings vs NSE dissemination time",
    "D6": "Calendar, suspensions and ISIN lineage",
    "D7": "Index, TRI and ETF series vs NSE files",
}
# A failed blocking check marks every backtest of its window unverified; D4 only a run that reads
# 5-minute bars, D5 only one that reads results filings (the data audit cannot tell, so it counts).
BLOCKING = frozenset({"D1", "D2", "D3", "D5", "D6", "D7"})
_RANK = {PASS: 0, SKIPPED: 0, WARN: 1, FAIL: 2}


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def report_doc(
    checks: Mapping[str, Mapping[str, Any]], start: date, end: date, created: datetime
) -> dict[str, Any]:
    """The report: its window, each check's result under its id, the worst status and the
    digest (sha256 of everything but the creation time)."""
    status = max((c["status"] for c in checks.values()), key=_RANK.__getitem__, default=PASS)
    body = {
        "format": REPORT_FORMAT,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "status": status,
        "checks": {k: dict(checks[k]) for k in sorted(checks)},
    }
    digest = hashlib.sha256(_canonical(body).encode()).hexdigest()
    return {**body, "created": created.isoformat(timespec="seconds"), "digest": digest}


def reports_dir(root: Path) -> Path:
    return root / "audits"


def write_report(folder: Path, doc: Mapping[str, Any], markdown: str) -> tuple[Path, Path]:
    """`verify.json` and `verify.md` in `folder`, each written whole (a temporary name, then a
    rename), so a reader never sees half a report."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = (folder / REPORT_JSON, folder / REPORT_MD)
    for path, text in zip(paths, (json.dumps(doc, indent=1, default=str), markdown), strict=True):
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    return paths


def _read(path: Path) -> dict[str, Any] | None:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("format") == REPORT_FORMAT else None


def covering_reports(root: Path, start: date, end: date) -> list[tuple[dict[str, Any], Path]]:
    """The reports under `audits/verify-*/` whose window holds `start..end`, newest first (by
    creation time)."""
    found = []
    for path in reports_dir(root).glob(f"verify-*/{REPORT_JSON}"):
        doc = _read(path)
        window = (doc or {}).get("window") or {}
        try:
            first, last = date.fromisoformat(window["from"]), date.fromisoformat(window["to"])
        except (KeyError, TypeError, ValueError):
            continue
        if first <= start and end <= last:
            found.append((doc, path))
    return sorted(found, key=lambda f: str(f[0].get("created")), reverse=True)


def window_status(
    doc: Mapping[str, Any] | None, path: Path | None, *, used_5m: bool
) -> dict[str, Any]:
    """What a backtest of a window records of its verification: the report (its file, digest and
    each check's status), the blocking checks that failed or were not run, and `verified`: a
    report exists and every blocking check ran and did not fail."""
    blocking = sorted(BLOCKING | ({"D4"} if used_5m else set()))
    if doc is None:
        return {"report": None, "verified": False, "failed": [], "not_run": blocking}
    checks = doc.get("checks") or {}
    statuses = {k: (v or {}).get("status") for k, v in checks.items()}
    failed = [k for k in blocking if statuses.get(k) == FAIL]
    not_run = [k for k in blocking if statuses.get(k) in (None, SKIPPED)]
    return {
        "report": None if path is None else str(path),
        "digest": doc.get("digest"),
        "created": doc.get("created"),
        "window": doc.get("window"),
        "checks": statuses,
        "verified": not failed and not not_run,
        "failed": failed,
        "not_run": not_run,
    }


def status_for(root: Path, start: date, end: date, *, used_5m: bool) -> dict[str, Any]:
    """`window_status` of the newest report under `root` that covers `start..end`."""
    found = covering_reports(root, start, end)
    doc, path = found[0] if found else (None, None)
    return window_status(doc, path, used_5m=used_5m)


def seeded_sample(items: Sequence[Any], size: int, seed: int) -> list[Any]:
    """`size` of `items` chosen by the sha256 of (seed, item): the same items and seed give the
    same sample on every run and machine (a reproducible sample, no random generator)."""

    def rank(item: Any) -> str:  # noqa: ANN401 - any item with a stable text
        return hashlib.sha256(f"{seed}|{_canonical(item)}".encode()).hexdigest()

    return sorted(items, key=rank)[:size]


def worst(statuses: Sequence[str]) -> str:
    return max(statuses, key=_RANK.__getitem__, default=PASS)

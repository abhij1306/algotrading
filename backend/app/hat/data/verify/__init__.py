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
the data layer's fetchers and pacer, into `verify_reference/`.

A missing reference, or a span of the window a reference does not reach, is a failed finding or
limits the check's `coverage`; never a warning. A warning is something listed for a look, not a
gap in the proof.

The report (`verify.json`, `verify.md`) goes to `HAT_DATA_ROOT/audits/verify-<date>/` (a narrower
run: a folder naming its checks and window) unless `--report` names another `audits/verify-*`
folder. It records the data snapshot it judged (`snapshot`), taken before and after the checks:
data that moved during the run fails every check (`changed_during`). A backtest's data audit
judges each blocking check by its own report (`status_for`, `pick_reports`): the newest report
covering the window that ran the check and whose snapshot of its window is the data now, so a
narrow run never hides a wider report's other checks. The window starts at the first session the
run reads (`read_window`: the lookback before its period). On it (`check_status`) the check must
cover the window and none of its failed findings may concern it (a finding's `days` or `window`,
else the whole check). Otherwise the run is unverified.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any

from ..audit import FAIL, PASS, SKIPPED, WARN, finding
from .snapshot import snapshot as take_snapshot

__all__ = [
    "BLOCKING",
    "CHECKS",
    "REPORT_FORMAT",
    "check_status",
    "covered",
    "covering_reports",
    "dated",
    "report_doc",
    "report_folder",
    "reports_dir",
    "spanning",
    "status_for",
    "window_status",
    "write_report",
]

REPORT_FORMAT = "data-verify/2"
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
FIRST_DAY = date(2011, 6, 22)  # the first bhavcopy with ISINs: the store's first session


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def changed_during(
    results: Mapping[str, dict[str, Any]], before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    """The check results, each failed by `data_changed_during_run` (undated: every window) when
    the snapshot taken before the checks differs from the one after (the data moved under them,
    so no check judged one state of it); unchanged otherwise."""
    if before.get("digest") == after.get("digest"):
        return dict(results)
    moved = sorted(
        k for k, v in (after.get("parts") or {}).items() if before.get("parts", {}).get(k) != v
    )
    out = {}
    for check_id, result in results.items():
        found = finding("data_changed_during_run", FAIL, moved)
        out[check_id] = {
            **result,
            "status": FAIL,
            "findings": [*(result.get("findings") or ()), found],
        }
    return out


def report_doc(
    checks: Mapping[str, Mapping[str, Any]],
    start: date,
    end: date,
    created: datetime,
    snapshot: Mapping[str, Any],
) -> dict[str, Any]:
    """The report: its window, the data snapshot it judged (`snapshot.snapshot`), each check's
    result under its id, the worst status and the digest (sha256 of everything but the creation
    time)."""
    status = max((c["status"] for c in checks.values()), key=_RANK.__getitem__, default=PASS)
    body = {
        "format": REPORT_FORMAT,
        "window": {"from": start.isoformat(), "to": end.isoformat()},
        "snapshot": dict(snapshot),
        "status": status,
        "checks": {k: dict(checks[k]) for k in sorted(checks)},
    }
    digest = hashlib.sha256(_canonical(body).encode()).hexdigest()
    return {**body, "created": created.isoformat(timespec="seconds"), "digest": digest}


def reports_dir(root: Path) -> Path:
    return root / "audits"


def report_folder(root: Path, created: date, start: date, end: date, ids: Sequence[str]) -> Path:
    """The default report folder: `verify-<date>` for every check over the whole history, a
    distinct name for a narrower run (its checks and window) so it never replaces the full one."""
    if set(ids) == set(CHECKS) and start <= FIRST_DAY:
        return reports_dir(root) / f"verify-{created.isoformat()}"
    scope = "-".join(sorted(ids))
    return reports_dir(root) / f"verify-{created.isoformat()}-{scope}-{start:%Y%m%d}-{end:%Y%m%d}"


def report_target(root: Path, folder: Path) -> Path:
    """`folder` resolved inside the reports folder as a `verify-*` folder (where `status_for`
    reads reports); any other folder is refused before anything is written."""
    base = reports_dir(root).resolve()
    target = (base / folder).resolve()
    if not target.is_relative_to(base) or target.parent != base:
        raise ValueError("report_folder_outside_audits")
    if not target.name.startswith("verify-"):
        raise ValueError("report_folder_not_verify")
    return target


def write_report(
    root: Path, folder: Path, doc: Mapping[str, Any], markdown: str
) -> tuple[Path, Path]:
    """`verify.json` and `verify.md` in `folder` (inside the reports folder, `report_target`),
    each written whole (a temporary name, then a rename), so a reader never sees half a report."""
    folder = report_target(root, folder)
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


def _span(value: object) -> tuple[date, date] | None:
    try:
        return date.fromisoformat(value["from"]), date.fromisoformat(value["to"])  # type: ignore[index]
    except (KeyError, TypeError, ValueError):
        return None


def covering_reports(root: Path, start: date, end: date) -> list[tuple[dict[str, Any], Path]]:
    """The reports under `audits/verify-*/` whose window holds `start..end`, newest first (by
    creation time)."""
    found = []
    for path in reports_dir(root).glob(f"verify-*/{REPORT_JSON}"):
        doc = _read(path)
        span = _span((doc or {}).get("window"))
        if span is not None and span[0] <= start and end <= span[1]:
            found.append((doc, path))
    return sorted(found, key=lambda f: str(f[0].get("created")), reverse=True)


def dated(found: dict[str, Any], days: Iterable[date | str]) -> dict[str, Any]:
    """A finding with the days it concerns (`days`, ISO, every one of them, not only the listed
    items), so a backtest is failed by it only when one of them is in its window."""
    return {**found, "days": sorted({d if isinstance(d, str) else d.isoformat() for d in days})}


def spanning(found: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    """A finding about `start..end` only (`window`): a gap in a reference's or the data's span."""
    return {**found, "window": {"from": start.isoformat(), "to": end.isoformat()}}


def covered(result: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    """A check result that judged only `start..end` (`coverage`): a backtest reaching outside it
    finds the check not run."""
    return {**result, "coverage": {"from": start.isoformat(), "to": end.isoformat()}}


def failing_in(found: Mapping[str, Any], start: date, end: date) -> bool:
    """Whether a failed finding concerns `start..end`: one of its days is in it, its window meets
    it, or it names neither (it concerns the whole check)."""
    if found.get("severity") != FAIL:
        return False
    if "days" in found:
        return any(start.isoformat() <= d <= end.isoformat() for d in found["days"])
    span = _span(found.get("window"))
    return span is None or (span[0] <= end and start <= span[1])


def check_status(result: Mapping[str, Any] | None, start: date, end: date) -> str:
    """One check's verdict on `start..end`: `not_run` (absent, skipped, or its coverage does not
    hold the window), `failed` (a failed finding concerns the window) or `verified`."""
    if not result or result.get("status") == SKIPPED:
        return "not_run"
    span = _span(result.get("coverage"))
    if span is not None and not (span[0] <= start and end <= span[1]):
        return "not_run"
    if any(failing_in(f, start, end) for f in result.get("findings") or ()):
        return "failed"
    return "verified"


def pick_reports(
    found: Sequence[tuple[Mapping[str, Any], Path | None]],
    check_id: str,
    current: Callable[[Mapping[str, Any]], str | None],
) -> tuple[Mapping[str, Any], Path | None] | None | str:
    """The report that judges `check_id` for a window: the newest of `found` (newest first) that
    ran the check and whose snapshot is the data now (`current`, the snapshot digest of the
    report's window); `"stale"` when every report that ran it judged data since changed, None
    when none ran it. A narrow run never hides the check of an older, wider report."""
    ran = [
        (doc, path)
        for doc, path in found
        if ((doc.get("checks") or {}).get(check_id) or {}).get("status") not in (None, SKIPPED)
    ]
    for doc, path in ran:
        if (doc.get("snapshot") or {}).get("digest") == current(doc):
            return doc, path
    return "stale" if ran else None


def window_status(
    found: Sequence[tuple[Mapping[str, Any], Path | None]],
    start: date,
    end: date,
    current: Callable[[Mapping[str, Any]], str | None],
    *,
    used_5m: bool,
) -> dict[str, Any]:
    """What a backtest of `start..end` records of its verification. Each blocking check is judged
    by its own report (`pick_reports` over the covering reports `found`, newest first) on this
    window (`check_status`): `failed`, `not_run` (no report ran it, or its coverage misses the
    window) or `stale` (every report that ran it judged data since changed). `verified`: every
    blocking check verified. The digest names the reports used, so a new one changes it."""
    blocking = sorted(BLOCKING | ({"D4"} if used_5m else set()))
    if not found:
        return {
            "report": None,
            "verified": False,
            "stale": [],
            "failed": [],
            "not_run": blocking,
        }
    verdicts: dict[str, str] = {}
    used: dict[str, tuple[Mapping[str, Any], Path | None]] = {}
    for check_id in blocking:
        pick = pick_reports(found, check_id, current)
        if pick is None or pick == "stale":
            verdicts[check_id] = pick or "not_run"
            continue
        assert not isinstance(pick, str)
        used[check_id] = pick
        verdicts[check_id] = check_status(pick[0]["checks"][check_id], start, end)
    named = sorted(f"{k}={doc.get('digest')}" for k, (doc, _) in used.items())
    newest_doc, newest_path = found[0]
    return {
        "report": str(newest_path) if newest_path is not None else newest_doc.get("digest"),
        "reports": {k: None if p is None else str(p) for k, (_, p) in used.items()},
        "digest": hashlib.sha256("\n".join(named).encode()).hexdigest() if used else None,
        "created": max((str(doc.get("created")) for doc, _ in used.values()), default=None),
        "checks": {k: doc["checks"][k].get("status") for k, (doc, _) in used.items()},
        "verdicts": verdicts,
        "stale": [k for k, v in verdicts.items() if v == "stale"],
        "verified": all(v == "verified" for v in verdicts.values()),
        "failed": [k for k, v in verdicts.items() if v == "failed"],
        "not_run": [k for k, v in verdicts.items() if v == "not_run"],
    }


def read_window(snapshot: Mapping[str, Any], start: date, end: date) -> tuple[date, date]:
    """The sessions a backtest of `start..end` reads: from the first session its market loads
    (`store_first`, the lookback before `start`), so data it reads before its period is judged
    too."""
    first = snapshot.get("store_first")
    return (min(date.fromisoformat(first), start) if first else start), end


def status_for(
    root: Path,
    session: Any,  # noqa: ANN401 - a SQLAlchemy session
    start: date,
    end: date,
    *,
    used_5m: bool,
) -> dict[str, Any]:
    """`window_status` of the reports under `root` covering `start..end`, each against the
    snapshot of its own window taken now (once a window)."""
    found = covering_reports(root, start, end)
    taken: dict[tuple[date, date], str] = {}

    def current(doc: Mapping[str, Any]) -> str | None:
        first, last = _span(doc.get("window")) or (start, end)
        if (first, last) not in taken:
            taken[first, last] = take_snapshot(root, session, first, last)["digest"]
        return taken[first, last]

    return window_status(found, start, end, current, used_5m=used_5m)


def seeded_sample(items: Sequence[Any], size: int, seed: int) -> list[Any]:
    """`size` of `items` chosen by the sha256 of (seed, item): the same items and seed give the
    same sample on every run and machine (a reproducible sample, no random generator)."""

    def rank(item: Any) -> str:  # noqa: ANN401 - any item with a stable text
        return hashlib.sha256(f"{seed}|{_canonical(item)}".encode()).hexdigest()

    return sorted(items, key=rank)[:size]


def worst(statuses: Sequence[str]) -> str:
    return max(statuses, key=_RANK.__getitem__, default=PASS)

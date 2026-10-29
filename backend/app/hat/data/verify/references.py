"""Public reference files the checks read: kept once under `raw/reference/<group>/<name>`
(`store.keep_once`, never overwritten), downloaded only when the run allows it (`--fetch`),
through the data layer's fetcher (`bhavcopy.http_fetcher`) and one pacer for the run."""

from __future__ import annotations

import csv
import io
import json
import os
import uuid
from collections.abc import Callable, Collection
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from ..bhavcopy import BhavcopyError, FetchResult, archive_url
from ..pacing import Pacer
from ..store import keep_once
from .context import Context

MIN_INTERVAL_S = 2.0  # politely: one request every two seconds, to any public host
_PACER = Pacer(min_interval=MIN_INTERVAL_S)
Fetch = Callable[[str], FetchResult]
SYMBOL_CHANGES = "symbolchange.csv"
SYMBOL_CHANGES_URL = "https://nsearchives.nseindia.com/content/equities/symbolchange.csv"


@dataclass(frozen=True)
class Rename:
    old: str
    new: str
    day: date


def read_symbol_changes(data: bytes) -> tuple[list[Rename], int]:
    """NSE's `symbolchange.csv`, read here by column position (company, old symbol, new symbol,
    effective date `30-OCT-2019`), not by the importer's parser: (the renames, the rows that
    could not be read). The header row is not counted."""
    renames, unread = [], 0
    rows = csv.reader(io.StringIO(data.decode("utf-8-sig", "replace")))
    for n, row in enumerate(rows):
        cells = [c.strip() for c in row]
        if n == 0 or not any(cells):
            continue
        try:
            day = datetime.strptime(cells[3].title(), "%d-%b-%Y").date()  # noqa: DTZ007
            old, new = cells[1].upper(), cells[2].upper()
        except (IndexError, ValueError):
            unread += 1
            continue
        if not old or not new:
            unread += 1
            continue
        if old == new:  # a row that renames nothing
            continue
        renames.append(Rename(old, new, day))
    return renames, unread


def safe_name(name: str) -> bool:
    return bool(name) and name == Path(name).name and not name.startswith(".")


def reference_file(
    ctx: Context,
    group: str,
    name: str,
    url: str,
    fetch: Fetch | None = None,
    valid: Callable[[bytes], bool] = bool,
) -> bytes | None:
    """The kept file `raw/reference/<group>/<name>`; when absent and the run may fetch, `url`
    downloaded once and kept when `valid` (a site's "not found" page is not kept). None when
    there is no file (and none could be fetched)."""
    if not safe_name(group) or not safe_name(name):
        raise ValueError("verify_bad_reference_name")
    path = ctx.reference_dir(group) / name
    if path.is_file():
        return path.read_bytes()
    if not ctx.fetch:
        return None
    if fetch is None:
        from ..bhavcopy import http_fetcher  # noqa: PLC0415 - the network, only when fetching

        fetch = http_fetcher()
    _PACER.wait()
    result = fetch(url)
    if result.status != "ok" or not result.data or not valid(result.data):
        ctx.unfetched[group] += 1
        return None
    keep_once(path, result.data)
    return result.data


def kept_bytes(ctx: Context) -> int:
    """The bytes kept under `raw/reference/` (the report says what the downloads cost)."""
    base = ctx.root / "raw" / "reference"
    return sum(p.stat().st_size for p in base.rglob("*") if p.is_file()) if base.is_dir() else 0


PROBES = "bhavcopy_probes.json"
FOUND, NOT_FOUND = "found", "not_found"


def probe_days(
    ctx: Context, days: Collection[date], fetch: Fetch | None = None
) -> dict[date, str]:  # pragma: no cover - the network
    """Whether NSE's archive holds a bhavcopy for each of `days` (`FOUND` or `NOT_FOUND`), asked
    of the archive itself (`bhavcopy.archive_url`), not read from `raw/`: the answers are kept
    in `raw/reference/nse/bhavcopy_probes.json` and only the unasked days are requested, with
    `--fetch`, one every `MIN_INTERVAL_S`. A day with no answer (not fetched, a network error,
    a day before the archive's formats) is left out."""
    path = ctx.reference_dir("nse") / PROBES
    try:
        kept = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        kept = {}
    todo = sorted(d for d in days if d.isoformat() not in kept)
    if ctx.fetch and todo:
        if fetch is None:
            from ..bhavcopy import http_fetcher  # noqa: PLC0415 - the network, only when fetching

            fetch = http_fetcher()
        for day in todo:
            try:
                url = archive_url(day)
            except BhavcopyError:
                continue
            _PACER.wait()
            result = fetch(url)
            if result.status == "ok":
                kept[day.isoformat()] = FOUND
            elif result.status == "not_found":
                kept[day.isoformat()] = NOT_FOUND
            else:
                ctx.unfetched["nse"] += 1
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(dict(sorted(kept.items())), indent=0), encoding="utf-8")
        os.replace(tmp, path)
    return {d: kept[d.isoformat()] for d in days if d.isoformat() in kept}

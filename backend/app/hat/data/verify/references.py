"""Public reference files the checks read: kept once under `raw/reference/<group>/<name>`
(`store.keep_once`, never overwritten), downloaded only when the run allows it (`--fetch`),
through the data layer's fetcher (`bhavcopy.http_fetcher`) and one pacer for the run."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..bhavcopy import FetchResult
from ..pacing import Pacer
from ..store import keep_once
from .context import Context

MIN_INTERVAL_S = 2.0  # politely: one request every two seconds, to any public host
_PACER = Pacer(min_interval=MIN_INTERVAL_S)
Fetch = Callable[[str], FetchResult]


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

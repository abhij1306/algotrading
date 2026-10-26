"""The golden sample check (docs/backtests/golden-sample.md): a frozen, independently verified
sample of store values, weighted to the risky spots (ex-dates, renames, delistings, index
rebalances), looked up again in the current stores and compared within each item's tolerance.

    python -m app.hat.data.golden check [--file F]
    python -m app.hat.data.golden rebase --reason "<text>" (--item ID [--item ID ...] |
        --all-drifted) [--file F]

Golden files live in `HAT_DATA_ROOT/audits/golden/` as `YYYY-MM-DD[.N].json` (data, never
committed; validated against `docs/backtests/golden-sample.schema.json`); a check reads the newest
unless `--file` names one there and writes its result under `results/` there. `rebase` writes a new
golden file with the current store values of drifted items, each with its reason; a golden file
is never edited. The data audit runs the same check (`audit.check_golden`). Read-only on the
database and the stores.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

import polars as pl
from sqlalchemy import select

from app.paths import GOLDEN_SCHEMA

from ..errors import HatError
from ..models import (
    HatCorporateAction,
    HatIndexMember,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatIsinLineage,
)
from ..time import IST
from .bars_store import BarStore
from .store import (
    BREADTH_SCHEMA,
    DATASET_BREADTH,
    DATASET_FEATURES,
    DATASET_MARKET_BREADTH,
    MARKET_BREADTH_SCHEMA,
    DataStore,
)

if TYPE_CHECKING:
    import duckdb
    from sqlalchemy.orm import Session

__all__ = [
    "FORMAT",
    "GoldenError",
    "Stores",
    "check",
    "current",
    "failed",
    "golden_dir",
    "load_stores",
    "lookup",
    "matches",
    "newest",
    "rebase_doc",
    "resolve_instrument",
    "result_doc",
    "validate",
]

FORMAT = "golden-sample/1"
RESULT_FORMAT = "golden-check/1"
PASS, DRIFT, MISSING, UNRESOLVED = "pass", "drift", "missing", "unresolved"
# A recorded mismatch: the store still holds the wrong value it held then (`open`), now holds the
# verified one (`fixed`), or neither (`drift`).
OPEN, FIXED = "open", "fixed"
ALL_DRIFTED = "all-drifted"
DEFAULT_INDEX = "NIFTY 500"
_NAME = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:\.(\d+))?\.json$")
# The adjusted views by the names a key may give them (`store.py`).
_VIEWS = {
    "bars_daily": "bars_daily",
    "bars_daily_adj": "bars_daily",
    "tr": "bars_daily",
    "total_return": "bars_daily",
    "bars_daily_cap": "bars_daily_cap",
    "bars_daily_cap_adj": "bars_daily_cap",
    "cap": "bars_daily_cap",
    "price": "bars_daily_cap",
}
_BREADTH_FIELDS = {f.name for f in BREADTH_SCHEMA}
_MARKET_BREADTH_FIELDS = {f.name for f in MARKET_BREADTH_SCHEMA}
_AGG_FIELDS = ("open", "high", "low", "close", "volume", "bars")
_AGG_ALIASES = {
    "first_open": "open",
    "max_high": "high",
    "min_low": "low",
    "last_close": "close",
    "sum_volume": "volume",
    "count": "bars",
}


class GoldenError(HatError):
    """A golden file or rebase request was refused; `code` is stable and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Unresolved(Exception):  # noqa: N818 - an outcome, not a failure of the code
    """The key names no single store value (absent, ambiguous or malformed); `status` is
    `missing` when the key is well formed and the store holds no such row."""

    def __init__(self, status: str, note: str) -> None:
        super().__init__(note)
        self.status = status
        self.note = note


# --- files ---------------------------------------------------------------------------------


def golden_dir(root: Path) -> Path:
    return root / "audits" / "golden"


def _order(path: Path) -> tuple[str, int] | None:
    found = _NAME.match(path.name)
    return None if found is None else (found[1], int(found[2] or 1))


def newest(root: Path) -> Path | None:
    """The newest golden file (by its date, then its `.N` sequence), None when there is none."""
    base = golden_dir(root)
    named = [(o, p) for p in base.glob("*.json") if (o := _order(p))] if base.is_dir() else []
    return max(named)[1] if named else None


def next_path(root: Path, day: date) -> Path:
    """The name of a new golden file written on `day`: `day.json`, or `day.N.json` after it."""
    base = golden_dir(root)
    n = 1
    while (path := base / (f"{day}.json" if n == 1 else f"{day}.{n}.json")).exists():
        n += 1
    return path


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def current(root: Path) -> str | None:
    """The sha256 of the newest golden file, None when there is none."""
    path = newest(root)
    return None if path is None else file_digest(path)


@cache
def _validator() -> Any:  # noqa: ANN401 - jsonschema's validator
    from jsonschema import Draft202012Validator  # noqa: PLC0415 - only the check needs it

    return Draft202012Validator(json.loads(GOLDEN_SCHEMA.read_text(encoding="utf-8")))


def validate(doc: object) -> list[str]:
    """The schema errors of a golden document (empty: valid), each `path: message`, and every
    item id held twice."""
    errors = [
        f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
        for e in _validator().iter_errors(doc)
    ]
    if not errors and isinstance(doc, Mapping):
        ids = Counter(i["id"] for part in ("items", "mismatches") for i in doc.get(part, ()))
        errors.extend(f"items: id {i!r} held {n} times" for i, n in sorted(ids.items()) if n > 1)
    return errors


def golden_path(root: Path, given: Path | str | None) -> Path | None:
    """The golden file a command reads: `given` (a name, or a path) inside `audits/golden/`,
    else the newest. A path outside that directory is refused: a check reads only golden
    files."""
    if given is None:
        return newest(root)
    base = golden_dir(root).resolve()
    path = (base / given).resolve()
    if not path.is_relative_to(base) or not path.is_file():
        raise GoldenError("golden_file_outside_dir")
    return path


def read_golden(path: Path) -> dict[str, Any]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoldenError("golden_file_unreadable") from exc
    if validate(doc):
        raise GoldenError("golden_file_invalid")
    return doc


# --- comparing -----------------------------------------------------------------------------


def _number(value: object) -> bool:
    return isinstance(value, (int, float, Decimal)) and not isinstance(value, bool)


def matches(expected: object, actual: object, tolerance: Mapping[str, float] | None) -> bool:
    """Whether the store's `actual` is the golden `expected`: numbers within `{"abs": x}` or
    `{"rel": x}` (of the expected value), or equal when `tolerance` is None; anything else
    equal (a date as its ISO string, a bool as a bool)."""
    if _number(expected) and _number(actual):
        e, a = float(expected), float(actual)  # type: ignore[arg-type]
        if not tolerance:
            return e == a
        bound = tolerance["abs"] if "abs" in tolerance else tolerance["rel"] * abs(e)
        return abs(e - a) <= bound + 1e-12 * max(1.0, abs(e))  # the decimal-to-binary error
    if isinstance(expected, bool) or isinstance(actual, bool):  # True == 1 in Python
        return expected is actual
    if expected == "" and actual is None:  # an empty string stands for a NULL column
        return True
    return _plain(expected) == _plain(actual)


def _plain(value: object) -> object:
    """A store value as JSON holds it: a date as its ISO string, a Decimal as a float, a uuid as
    its string."""
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value


# --- resolving keys ------------------------------------------------------------------------


@dataclass(frozen=True)
class Stores:
    """The store rows a golden file's items read, as plain data (`load_stores`).

    `instruments` holds (id, isin, symbol) of every instrument, `history` (id, symbol, from, to)
    of every dated symbol, `chain` each linked older instrument's chain id. The rest are keyed by
    the instrument a store files the row under: `raw` (instrument, day), `adjusted` (view, chain,
    day), `features` (chain, day), `bars_5m` (instrument, day): a list of (bar_start, open, high,
    low, close, volume); `actions` and `members` are table rows, `lineage` rows with the ISINs
    of both sides, `index_closes` (index, day), `breadth` (dataset, day)."""

    instruments: Sequence[tuple[str, str, str]] = ()
    history: Sequence[tuple[str, str, date, date | None]] = ()
    chain: Mapping[str, str] = field(default_factory=dict)
    raw: Mapping[tuple[str, date], Mapping[str, Any]] = field(default_factory=dict)
    adjusted: Mapping[tuple[str, str, date], Mapping[str, Any]] = field(default_factory=dict)
    actions: Sequence[Mapping[str, Any]] = ()
    members: Sequence[Mapping[str, Any]] = ()
    lineage: Sequence[Mapping[str, Any]] = ()
    index_closes: Mapping[tuple[str, date], Mapping[str, Any]] = field(default_factory=dict)
    features: Mapping[tuple[str, date], Mapping[str, Any]] = field(default_factory=dict)
    breadth: Mapping[tuple[str, date], Mapping[str, Any]] = field(default_factory=dict)
    bars_5m: Mapping[tuple[str, date], Sequence[tuple]] = field(default_factory=dict)


def _first(key: Mapping[str, Any], *names: str) -> Any:  # noqa: ANN401 - a JSON value
    return next((key[n] for n in names if key.get(n) not in (None, "")), None)


def _day(key: Mapping[str, Any], *names: str) -> date | None:
    value = _first(key, *names, "date", "trade_date", "day")
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise Unresolved(UNRESOLVED, f"bad date {value!r}") from exc


def _need_day(key: Mapping[str, Any], *names: str) -> date:
    day = _day(key, *names)
    if day is None:
        raise Unresolved(UNRESOLVED, "no date in key")
    return day


def _one(ids: Iterable[str], what: str) -> str:
    found = sorted(set(ids))
    if not found:
        raise Unresolved(MISSING, f"no instrument for {what}")
    if len(found) > 1:
        raise Unresolved(UNRESOLVED, f"ambiguous: {what} names {len(found)} instruments")
    return found[0]


def resolve_instrument(
    key: Mapping[str, Any], day: date | None, stores: Stores, prefix: str = ""
) -> str:
    """The instrument a key names: its `instrument_id`, else its `isin`, else its `symbol` as of
    `day` (the dated symbol history, else the current symbol). A `prefix` (`old_`, `new_`) reads
    one side of a lineage key."""
    given = _first(key, f"{prefix}instrument_id")
    if given is not None:
        if not any(i == str(given) for i, _, _ in stores.instruments):
            raise Unresolved(MISSING, f"no instrument {given}")
        return str(given)
    isin = _first(key, f"{prefix}isin")
    if isin is not None:
        return _one((i for i, s, _ in stores.instruments if s == isin), f"isin {isin}")
    symbol = _first(key, f"{prefix}symbol", f"{prefix}tradingsymbol")
    if symbol is None:
        raise Unresolved(UNRESOLVED, f"no {prefix}instrument_id, {prefix}isin or {prefix}symbol")
    if day is not None:
        dated = [
            i
            for i, s, lo, hi in stores.history
            if s == symbol and lo <= day and (hi is None or day < hi)
        ]
        if dated:
            return _one(dated, f"symbol {symbol} on {day}")
    return _one((i for i, _, s in stores.instruments if s == symbol), f"symbol {symbol}")


def _field(key: Mapping[str, Any], default: str | None = None) -> str:
    name = _first(key, "field", "column", "feature") or default
    if name is None:
        raise Unresolved(UNRESOLVED, "no field in key")
    return str(name)


def _get(row: Mapping[str, Any], name: str) -> Any:  # noqa: ANN401 - a store value
    if name not in row:
        raise Unresolved(UNRESOLVED, f"unknown field {name}")
    return row[name]


def _row(rows: Mapping[Any, Mapping[str, Any]], at: tuple, what: str) -> Mapping[str, Any]:
    found = rows.get(at)
    if found is None:
        raise Unresolved(MISSING, f"no {what} row")
    return found


def _view(key: Mapping[str, Any]) -> str:
    # the first word: a key may describe the view after its name ("bars_daily_cap view (...)")
    name = str(_first(key, "view", "store", "table", "basis") or "bars_daily").split()[0]
    if name not in _VIEWS:
        raise Unresolved(UNRESOLVED, f"unknown adjusted view {name}")
    return _VIEWS[name]


def _bar_raw(key: Mapping[str, Any], stores: Stores) -> object:
    day = _need_day(key)
    instrument = resolve_instrument(key, day, stores)
    return _get(_row(stores.raw, (instrument, day), "bars_daily_raw"), _field(key, "close"))


def _bar_adjusted(key: Mapping[str, Any], stores: Stores) -> object:
    day = _need_day(key)
    chain = stores.chain.get(i := resolve_instrument(key, day, stores), i)
    view = _view(key)
    return _get(_row(stores.adjusted, (view, chain, day), view), _field(key, "close"))


def _corporate_action(key: Mapping[str, Any], stores: Stores) -> object:
    day = _need_day(key, "ex_date")
    instrument = resolve_instrument(key, day, stores)
    kind = _first(key, "action_type", "type")
    rows = [
        r
        for r in stores.actions
        if r["instrument_id"] == instrument
        and r["ex_date"] == day
        and (kind is None or r["action_type"] == kind)
    ]
    name = _field(key, "adjustment_factor")
    if name == "exists":
        return bool(rows)
    if not rows:
        raise Unresolved(MISSING, "no hat_corporate_action row")
    if len(rows) > 1:
        types = ", ".join(sorted(r["action_type"] for r in rows))
        raise Unresolved(UNRESOLVED, f"ambiguous: {len(rows)} actions on the ex-date ({types})")
    return _get(rows[0], name)


def _membership(key: Mapping[str, Any], stores: Stores) -> object:
    """Membership on the key's `date`, or the span starting on its `effective_from`."""
    starts = _day(key, "effective_from")
    day = _day(key) if key.get("effective_from") is None else None
    if starts is None:
        raise Unresolved(UNRESOLVED, "no date or effective_from in key")
    instrument = resolve_instrument(key, starts, stores)
    index = str(_first(key, "index", "index_name") or DEFAULT_INDEX)
    spans = [
        r
        for r in stores.members
        if r["index_name"] == index
        and r["instrument_id"] == instrument
        and (
            r["effective_from"] == starts
            if day is None
            else r["effective_from"] <= day
            and (r["effective_to"] is None or day < r["effective_to"])
        )
    ]
    name = _field(key, "member")
    if name in ("member", "is_member"):
        return bool(spans)
    if not spans:
        raise Unresolved(MISSING, f"no {index} span of the instrument at {starts}")
    return _get(spans[0], name)


def _isin_lineage(key: Mapping[str, Any], stores: Stores) -> object:
    name = _field(key, "status")
    if name in ("chain_id", "chain_isin"):  # the chain an instrument's bars read as
        chain = stores.chain.get(i := resolve_instrument(key, _day(key), stores), i)
        if name == "chain_id":
            return chain
        return next(s for c, s, _ in stores.instruments if c == chain)
    old = resolve_instrument(key, _day(key), stores, "old_")
    new = resolve_instrument(key, _day(key), stores, "new_")
    rows = [r for r in stores.lineage if r["old_instrument_id"] == old]
    row = next((r for r in rows if r["new_instrument_id"] == new), None)
    if name == "linked":
        return row is not None and row["status"] == "linked"
    if row is None:
        raise Unresolved(MISSING, "no hat_isin_lineage row")
    return _get(row, name)


def _index_close(key: Mapping[str, Any], stores: Stores) -> object:
    day = _need_day(key)
    index = _first(key, "index", "index_name", "symbol", "name")
    if index is None:
        raise Unresolved(UNRESOLVED, "no index in key")
    return _get(_row(stores.index_closes, (str(index), day), "index_daily"), _field(key, "close"))


def _feature(key: Mapping[str, Any], stores: Stores) -> object:
    day = _need_day(key)
    chain = stores.chain.get(i := resolve_instrument(key, day, stores), i)
    return _get(_row(stores.features, (chain, day), DATASET_FEATURES), _field(key))


def _breadth_dataset(key: Mapping[str, Any], name: str) -> str:
    given = _first(key, "dataset", "store", "table")
    if given is not None:
        if given not in (DATASET_BREADTH, DATASET_MARKET_BREADTH):
            raise Unresolved(UNRESOLVED, f"unknown breadth dataset {given}")
        return str(given)
    return DATASET_BREADTH if name in _BREADTH_FIELDS else DATASET_MARKET_BREADTH


def _breadth(key: Mapping[str, Any], stores: Stores) -> object:
    day = _need_day(key)
    name = _field(key, "breadth_pct")
    dataset = _breadth_dataset(key, name)
    return _get(_row(stores.breadth, (dataset, day), dataset), name)


def aggregate_5m(bars: Sequence[tuple]) -> dict[str, Any]:
    """A day's 5-minute bars, (bar_start, open, high, low, close, volume), as one daily bar."""
    ordered = sorted(bars, key=lambda b: b[0])
    return {
        "open": ordered[0][1],
        "high": max(b[2] for b in ordered),
        "low": min(b[3] for b in ordered),
        "close": ordered[-1][4],
        "volume": sum(b[5] for b in ordered),
        "bars": len(ordered),
    }


def _bar_5m_daily_agg(key: Mapping[str, Any], stores: Stores) -> object:
    day = _need_day(key)
    instrument = resolve_instrument(key, day, stores)
    bars = stores.bars_5m.get((instrument, day)) or stores.bars_5m.get(
        (stores.chain.get(instrument, instrument), day)
    )
    name = _field(key, "close")
    name = _AGG_ALIASES.get(name, name)
    if name not in _AGG_FIELDS:
        raise Unresolved(UNRESOLVED, f"unknown field {name}")
    if not bars:
        raise Unresolved(MISSING, "no bars_5m rows")
    return aggregate_5m(bars)[name]


_LOOKUPS = {
    "bar_raw": _bar_raw,
    "bar_adjusted": _bar_adjusted,
    "corporate_action": _corporate_action,
    "membership": _membership,
    "isin_lineage": _isin_lineage,
    "index_close": _index_close,
    "feature": _feature,
    "breadth": _breadth,
    "bar_5m_daily_agg": _bar_5m_daily_agg,
}


def lookup(item: Mapping[str, Any], stores: Stores) -> object:
    """The current store value an item's key names; `Unresolved` when there is none."""
    return _plain(_LOOKUPS[item["kind"]](item["key"], stores))


# --- the check -----------------------------------------------------------------------------


def _outcome(item: Mapping[str, Any], stores: Stores, *, recorded: bool) -> dict[str, Any]:
    """One item's result: `pass` or `drift` (a `recorded` mismatch: `fixed`, `open` or `drift`),
    or `missing`/`unresolved` with a note."""
    out = {k: item[k] for k in ("id", "kind", "risk", "key", "value", "tolerance")}
    if recorded:
        out["recorded_store_value"] = item["store_value"]
    try:
        actual = lookup(item, stores)
    except Unresolved as exc:
        return out | {"status": exc.status, "store_value": None, "note": exc.note}
    tolerance = item["tolerance"]
    if matches(item["value"], actual, tolerance):
        status = FIXED if recorded else PASS
    elif recorded and matches(item["store_value"], actual, tolerance):
        status = OPEN
    else:
        status = DRIFT
    return out | {"status": status, "store_value": actual}


def _counts(outcomes: Sequence[Mapping[str, Any]], by: str) -> dict[str, dict[str, int]]:
    counts: dict[str, Counter] = defaultdict(Counter)
    for o in outcomes:
        counts[o[by]][o["status"]] += 1
    return {k: dict(sorted(v.items())) for k, v in sorted(counts.items())}


def check(doc: Mapping[str, Any], stores: Stores) -> dict[str, Any]:
    """Every item and recorded mismatch of a golden document looked up in `stores`: the
    outcomes, the counts per status, kind and risk, and the drift list (every item that is not
    `pass` and every mismatch that is neither `open` nor `fixed`)."""
    items = [_outcome(i, stores, recorded=False) for i in doc["items"]]
    recorded = [_outcome(i, stores, recorded=True) for i in doc.get("mismatches", ())]
    drift = [o for o in items if o["status"] != PASS] + [
        o for o in recorded if o["status"] not in (OPEN, FIXED)
    ]
    return {
        "counts": dict(sorted(Counter(o["status"] for o in items).items())),
        "mismatch_counts": dict(sorted(Counter(o["status"] for o in recorded).items())),
        "by_kind": _counts(items, "kind"),
        "by_risk": _counts(items, "risk"),
        "drift": drift,
        "items": items,
        "mismatches": recorded,
    }


def failed(result: Mapping[str, Any]) -> bool:
    return bool(result.get("drift"))


def result_doc(
    path: Path, digest: str, golden: Mapping[str, Any], body: Mapping[str, Any], at: datetime
) -> dict[str, Any]:
    """A check's result document: the golden file it read (name, sha256, created) and the
    check's body."""
    return {
        "format": RESULT_FORMAT,
        "status": "fail" if failed(body) else "pass",
        "golden_file": path.name,
        "golden_digest": digest,
        "golden_created": golden.get("created"),
        "data_audit_digest": golden.get("data_audit_digest"),
        "checked_at": at.isoformat(),
        **body,
    }


def summary(result: Mapping[str, Any], path: Path | None = None) -> dict[str, Any]:
    """What the command line prints: the status, counts and the drift list (shortened)."""
    keep = ("id", "kind", "risk", "status", "value", "store_value", "note")
    return {
        k: result.get(k)
        for k in ("status", "golden_file", "golden_digest", "counts", "mismatch_counts")
    } | {
        "result_file": None if path is None else str(path),
        "by_kind": result.get("by_kind"),
        "by_risk": result.get("by_risk"),
        "drift": [{k: o.get(k) for k in keep if k in o} for o in result.get("drift", ())[:50]],
        "drift_count": len(result.get("drift", ())),
    }


# --- rebase --------------------------------------------------------------------------------


def rebase_doc(
    doc: Mapping[str, Any],
    result: Mapping[str, Any],
    wanted: Sequence[str] | str,
    reason: str,
    today: date,
    source: tuple[str, str],
) -> dict[str, Any]:
    """A new golden document: `doc` with the current store value of each named drifted item
    (`wanted`, or every drifted item for `all-drifted`), each with the reason, the value it
    replaces, the file it came from and the day; `rebased_from` names the old file and its
    sha256. Refused without a reason, with nothing to rebase, or for an item that did not
    drift (a missing or unresolved one has no store value to take)."""
    if not reason.strip():
        raise GoldenError("golden_rebase_reason_required")
    drifted = {o["id"]: o for o in result["items"] if o["status"] == DRIFT}
    ids = sorted(drifted) if wanted == ALL_DRIFTED else list(dict.fromkeys(wanted))
    if not ids:
        raise GoldenError("golden_rebase_nothing_drifted")
    if any(i not in drifted for i in ids):
        raise GoldenError("golden_rebase_item_not_drifted")
    name, digest = source
    new = copy.deepcopy(dict(doc))
    new |= {
        "created": today.isoformat(),
        "created_by": "golden rebase",
        "rebased_from": {"file": name, "digest": digest},
    }
    for item in new["items"]:
        if item["id"] in ids:
            item["rebase"] = {
                "reason": reason.strip(),
                "previous_value": item["value"],
                "from_file": name,
                "on": today.isoformat(),
            }
            item["value"] = drifted[item["id"]]["store_value"]
            item["verified_from"] = f"store value at rebase on {today}: {reason.strip()}"
    if validate(new):
        raise GoldenError("golden_rebase_invalid")
    return new


# --- reading the stores --------------------------------------------------------------------

_ADJUSTED_SQL = """
SELECT * FROM {view}
WHERE date = ? AND instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
"""


def _needs(items: Sequence[Mapping[str, Any]], base: Stores) -> dict[str, dict]:
    """What each kind reads, by day: the instruments (or chains) and the adjusted views,
    indexes and breadth datasets the keys name; a key that does not resolve reads nothing."""
    needs: dict[str, dict] = defaultdict(lambda: defaultdict(set))
    for item in items:
        kind, key = item["kind"], item["key"]
        try:
            if kind == "index_close":
                needs[kind][_need_day(key)].add(None)
                continue
            if kind == "breadth":
                dataset = _breadth_dataset(key, _field(key, "breadth_pct"))
                needs[kind][_need_day(key)].add(dataset)
                continue
            if kind == "isin_lineage":
                continue
            names = {"corporate_action": "ex_date", "membership": "effective_from"}
            day = _need_day(key, names.get(kind, "date"))
            instrument = resolve_instrument(key, day, base)
        except Unresolved:
            continue
        chain = base.chain.get(instrument, instrument)
        if kind == "bar_adjusted":
            needs[kind][day].add((_view(key), chain))
        else:
            needs[kind][day].update({instrument, chain})
    return needs


def load_stores(
    items: Sequence[Mapping[str, Any]],
    session: Session,
    store: DataStore,
    con: duckdb.DuckDBPyConnection,
    chain: Mapping[str, str],
) -> Stores:
    """The rows `items` read: the instruments and symbol history, then per day only the rows of
    the instruments the keys resolve to. `con` is a `store.connect` connection with the stored
    adjustment, `chain` its lineage."""
    instruments = [
        (str(i), isin, symbol)
        for i, isin, symbol in session.execute(
            select(HatInstrument.id, HatInstrument.isin, HatInstrument.symbol)
        )
    ]
    history = [
        (str(i), s, lo, hi)
        for i, s, lo, hi in session.execute(
            select(
                HatInstrumentSymbolHistory.instrument_id,
                HatInstrumentSymbolHistory.symbol,
                HatInstrumentSymbolHistory.valid_from,
                HatInstrumentSymbolHistory.valid_to,
            )
        )
    ]
    base = Stores(instruments=instruments, history=history, chain=dict(chain))
    needs = _needs(items, base)
    raw = {}
    for day, ids in needs["bar_raw"].items():
        for row in store.read_bars(day).filter(pl.col("instrument_id").is_in(ids)).to_dicts():
            raw[(row["instrument_id"], day)] = row
    adjusted = {}
    for day, wanted in needs["bar_adjusted"].items():
        for view in {v for v, _ in wanted}:
            chains = sorted(c for v, c in wanted if v == view)
            sql = _ADJUSTED_SQL.format(view=view)  # a name from `_VIEWS`
            for row in con.execute(sql, [day, chains]).pl().to_dicts():
                adjusted[(view, row["instrument_id"], day)] = row
    features = {}
    for day, ids in needs["feature"].items():
        part = store.read_partition(DATASET_FEATURES, day)
        for row in part.filter(pl.col("instrument_id").is_in(ids)).to_dicts():
            features[(row["instrument_id"], day)] = row
    breadth = {}
    for day, datasets in needs["breadth"].items():
        for dataset in datasets:
            rows = store.read_partition(dataset, day).to_dicts()
            if rows:
                breadth[(dataset, day)] = rows[0]
    index_closes = {
        (r.index_name, day): {"open": r.open, "high": r.high, "low": r.low, "close": r.close}
        for day in needs["index_close"]
        for r in store.read_index(day)
    }
    bars_5m: dict[tuple[str, date], list[tuple]] = defaultdict(list)
    five = BarStore(store.root)
    for day, ids in needs["bar_5m_daily_agg"].items():
        for r in five.read(day):
            if r.instrument_id in ids:
                bars_5m[(r.instrument_id, day)].append(
                    (r.bar_start, r.open, r.high, r.low, r.close, r.volume)
                )
    acted = sorted({i for ids in needs["corporate_action"].values() for i in ids})
    actions = [
        {c.name: getattr(r, c.name) for c in HatCorporateAction.__table__.columns}
        | {"instrument_id": str(r.instrument_id)}
        for r in session.scalars(
            select(HatCorporateAction).where(HatCorporateAction.instrument_id.in_(acted))
        )
    ]
    listed = sorted({i for ids in needs["membership"].values() for i in ids})
    members = [
        {c.name: getattr(r, c.name) for c in HatIndexMember.__table__.columns}
        | {"instrument_id": str(r.instrument_id)}
        for r in session.scalars(
            select(HatIndexMember).where(HatIndexMember.instrument_id.in_(listed))
        )
    ]
    lineage = [
        {
            c.name: getattr(r, c.name)
            for c in HatIsinLineage.__table__.columns
            if c.name not in ("evidence", "decided_at")
        }
        | {
            "old_instrument_id": str(r.old_instrument_id),
            "new_instrument_id": str(r.new_instrument_id),
        }
        for r in session.scalars(select(HatIsinLineage))
    ]
    return Stores(
        instruments=instruments,
        history=history,
        chain=dict(chain),
        raw=raw,
        adjusted=adjusted,
        actions=actions,
        members=members,
        lineage=lineage,
        index_closes=index_closes,
        features=features,
        breadth=breadth,
        bars_5m=dict(bars_5m),
    )


def run_check(
    path: Path,
    store: DataStore,
    session: Session,
    con: duckdb.DuckDBPyConnection,
    chain: Mapping[str, str],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The golden document at `path` and its result against the current stores."""
    golden = read_golden(path)
    stores = load_stores(
        [*golden["items"], *golden.get("mismatches", ())], session, store, con, chain
    )
    body = check(golden, stores)
    return golden, result_doc(path, file_digest(path), golden, body, datetime.now(UTC))


def write_result(root: Path, result: Mapping[str, Any]) -> Path:
    """The result under `audits/golden/results/`, named by its golden file and time."""
    stamp = str(result["checked_at"]).replace(":", "").replace("+0000", "Z")
    path = golden_dir(root) / "results" / f"{Path(result['golden_file']).stem}.{stamp}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=1, default=_plain), encoding="utf-8")
    return path


# --- the command line ----------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """`check` prints the summary and exits 2 on drift or an unresolvable key; `rebase` writes
    a new golden file and prints its name."""
    from sqlalchemy import text  # noqa: PLC0415

    from app.database import db_session_scope  # noqa: PLC0415

    from ..settings import operator_settings  # noqa: PLC0415
    from .chains import stored_adjustment  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.data.golden")
    sub = parser.add_subparsers(dest="command", required=True)
    checking = sub.add_parser("check")
    checking.add_argument("--file")
    rebasing = sub.add_parser("rebase")
    rebasing.add_argument("--file")
    rebasing.add_argument("--reason", required=True)
    named = rebasing.add_mutually_exclusive_group(required=True)
    named.add_argument("--item", action="append", dest="items")  # an id may hold a comma
    named.add_argument("--all-drifted", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "rebase" and not args.reason.strip():
        sys.stderr.write("golden rebase refused: golden_rebase_reason_required\n")
        return 2
    settings = operator_settings()
    root = settings.data_root
    try:
        path = golden_path(root, args.file)
    except GoldenError as exc:
        sys.stderr.write(f"golden {args.command} refused: {exc.code}\n")
        return 2
    if path is None:
        sys.stderr.write("golden check refused: golden_file_missing\n")
        return 2
    store = DataStore(root, memory_limit=settings.duckdb_memory)
    try:
        with db_session_scope() as session:
            session.execute(
                text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatInstrument}
            )
            adjustment = stored_adjustment(session)
            with store.connect(adjustment) as con:
                golden, result = run_check(path, store, session, con, adjustment.lineage)
    except GoldenError as exc:
        sys.stderr.write(f"golden {args.command} refused: {exc.code}\n")
        return 2
    written = write_result(root, result)
    if args.command == "check":
        sys.stdout.write(json.dumps(summary(result, written), indent=2, default=_plain) + "\n")
        return 2 if failed(result) else 0
    wanted = ALL_DRIFTED if args.all_drifted else args.items
    today = datetime.now(UTC).astimezone(IST).date()
    try:
        new = rebase_doc(golden, result, wanted, args.reason, today, (path.name, file_digest(path)))
    except GoldenError as exc:
        sys.stderr.write(f"golden rebase refused: {exc.code}\n")
        return 2
    target = next_path(root, today)
    with target.open("x", encoding="utf-8") as out:  # never over an existing file
        json.dump(new, out, indent=1, default=_plain)
    rebased = [i["id"] for i in new["items"] if i.get("rebase", {}).get("from_file") == path.name]
    sys.stdout.write(json.dumps({"written": str(target), "rebased": rebased}, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

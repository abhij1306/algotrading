"""What the features partitions were written for (`features_daily/_state.json`, DB5): the
adjustment as text, the inputs digest, the per-day bars fingerprints, and from them the days and
chains a run must recompute (`stale_days`, `affected_chains`). Pure functions of their inputs
but for the state file itself; `features` runs the recomputation."""

from __future__ import annotations

import hashlib
import json
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

from ..money import require_exact
from .isin import ORDINARY_EQUITY
from .store import (
    DATASET_BARS,
    DATASET_BREADTH,
    DATASET_FEATURES,
    ActionFactor,
    Adjustment,
    DataStore,
    DividendCash,
    atomic_replace,
)

LOOKBACK_ROWS = 252  # rows before a date that any feature of the date can read
STATE_FILE = "_state.json"
STATE_VERSION = 2
# The ranked universe (D257) is the rows whose own ISIN is an equity share (`isin.ORDINARY_EQUITY`):
# point in time, with no reference data (a symbol suffix is a convention; `hat_instrument` keeps
# no security type and lacks some short-lived entitlements).
# What else besides the adjustment changes every date: the universe rule and the formulas. A
# change here rebuilds every date (the state records it).
FEATURES_RULE = f"v3|universe:{ORDINARY_EQUITY}"
DIVIDEND_LINE = "dividend:"  # a dividend's value in `adjustment_lines`


def adjustment_lines(adjustment: Adjustment) -> tuple[list[str], list[str]]:
    """The adjustment as sorted text: `instrument|ex_date|factor` per factor of the adjusted
    view and `instrument|ex_date|dividend:amount` per dividend (both keyed by chain id), and
    `old>chain` per linked ISIN. The state keeps them, so a later run knows which chains a change
    touches and can rebuild their old series. A dividend imported for past dates changes the
    returns before it; one that goes ex later changes no stored date (`LEVEL_FEATURES`)."""
    factors = sorted(
        [f"{a.instrument_id}|{a.ex_date.isoformat()}|{a.factor}" for a in adjustment.factors]
        + [
            f"{d.instrument_id}|{d.ex_date.isoformat()}|{DIVIDEND_LINE}{d.amount}"
            for d in adjustment.dividends
        ]
    )
    lineage = sorted(f"{old}>{new}" for old, new in adjustment.lineage.items())
    return factors, lineage


def inputs_digest(adjustment: Adjustment) -> str:
    """Order-independent sha256 of what changes old dates without changing their bars: the
    (instrument, ex-date, factor) set the adjusted view uses, its dividends, its ISIN lineage, and
    the ranked universe and formulas (`FEATURES_RULE`). No lineage or dividend leaves the digest
    of the factors alone."""
    factors, lineage = adjustment_lines(adjustment)
    lines = [*factors, *(f"lineage:{line}" for line in lineage)]
    return hashlib.sha256(("\n".join(lines) + "\nrule:" + FEATURES_RULE).encode()).hexdigest()


@dataclass
class FeaturesState:
    """`features_daily/_state.json`: the inputs the partitions were written for and, per day,
    the bars fingerprint (`DataStore.fingerprints`) the day's partitions were computed from. A
    day without a record, or whose bars now have another fingerprint, is stale. A run removes
    the records of the days it is about to rewrite before it writes any, and records each day
    once written, so a run that dies part-way (a `--full` included) leaves the rest unrecorded
    and the next run resumes there."""

    inputs_digest: str
    rule: str
    factors: list[str]
    lineage: list[str]
    days: dict[date, str] = field(default_factory=dict)


def _state_path(store: DataStore) -> Path:
    return store.root / DATASET_FEATURES / STATE_FILE


def read_state(store: DataStore) -> FeaturesState | None:
    """The recorded state; None when there is none or it is unreadable or of an older format
    (every date is then recomputed)."""
    try:
        raw = json.loads(_state_path(store).read_text(encoding="utf-8"))
        if raw.get("version") != STATE_VERSION:
            return None
        return FeaturesState(
            str(raw["inputs_digest"]),
            str(raw["rule"]),
            [str(line) for line in raw["factors"]],
            [str(line) for line in raw["lineage"]],
            {date.fromisoformat(day): str(fp) for day, fp in raw["days"].items()},
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def write_state(store: DataStore, state: FeaturesState) -> None:
    text = json.dumps(
        {
            "version": STATE_VERSION,
            "inputs_digest": state.inputs_digest,
            "rule": state.rule,
            "factors": state.factors,
            "lineage": state.lineage,
            "days": {day.isoformat(): fp for day, fp in sorted(state.days.items())},
        },
        sort_keys=True,
    )
    atomic_replace(_state_path(store), lambda tmp: tmp.write_text(text, encoding="utf-8"))


def features_current(
    store: DataStore, digest: str, days: Iterable[date], read: Mapping[date, str] | None = None
) -> bool:
    """True when every one of `days` has features written for the inputs `digest` from the bars
    the store holds now (the backtest's gate; the caller holds the features lock). A rebuild in
    progress, or bars corrected since, leaves some day unrecorded or with another fingerprint;
    a day missing its features or its breadth file is not current.
    `read` is the fingerprints a caller took before reading the bars itself: they must be the
    recorded ones and still the store's, so features never pair with bars read before a change."""
    state = read_state(store)
    if state is None or state.inputs_digest != digest:
        return False
    prints = store.fingerprints(DATASET_BARS)
    seen = prints if read is None else read
    return all(
        day in prints
        and state.days.get(day) == prints[day] == seen.get(day)
        and store.partition_files(DATASET_FEATURES, day)
        and store.partition_files(DATASET_BREADTH, day)
        for day in days
    )


def stale_days(
    days: Sequence[date],
    dirty: Collection[date],
    presence: Callable[[date], Collection[str]],
    lost: Collection[date] = (),
) -> set[date]:
    """The days to recompute when the bars of `dirty` (days of `days`) changed or were never
    featured, or a recorded part file of `lost` disappeared.

    A feature of day x reads the instrument's own last `LOOKBACK_ROWS` rows before x, so a
    changed day d moves, besides d itself, every later day on which an instrument with a row
    on d has one of its next `LOOKBACK_ROWS` rows (counted per instrument: one that trades
    rarely reaches further than `LOOKBACK_ROWS` store days), and with it the percentiles and
    breadth of those days; the `LOOKBACK_ROWS` store days after d are stale in any case. A lost
    day (removed, or one of its recorded files removed or rewritten) no longer lists the
    instruments it had, so every day from it on is stale. `presence(day)` is the ids with a bar
    on a day of `days`, as the adjusted view names them."""
    stale: set[date] = set()
    if lost:
        first_lost = min(lost)
        stale.update(d for d in days if d >= first_lost)
    if not dirty:
        return stale
    remaining: dict[str, int] = {}  # rows still to come that read a dirty day, per instrument
    floor = -1  # the last index stale by the store-day rule
    last_dirty = max(dirty)
    for i in range(bisect_left(days, min(dirty)), len(days)):
        day = days[i]
        if day in dirty:
            stale.add(day)
            floor = max(floor, i + LOOKBACK_ROWS)
            remaining.update(dict.fromkeys(presence(day), LOOKBACK_ROWS))
            continue
        if not remaining and i > floor:
            if day > last_dirty:
                break
            continue
        hit = [x for x in presence(day) if x in remaining] if remaining else []
        if hit or i <= floor:
            stale.add(day)
        for x in hit:
            remaining[x] -= 1
            if not remaining[x]:
                del remaining[x]
    return stale


def fingerprint_parts(fingerprint: str) -> set[str]:
    """The `name:size` parts of a `DataStore.fingerprints` entry."""
    return {part for part in fingerprint.split(";") if part}


def affected_chains(
    old: tuple[Sequence[str], Sequence[str]], new: tuple[Sequence[str], Sequence[str]]
) -> set[str]:
    """The ids whose adjusted series may differ between two adjustments given as
    `adjustment_lines`: a factor line belongs to its chain, a lineage line `old>chain` to both
    ids (the old ISIN's bars leave its own series and join the chain's, or the reverse)."""

    def groups(lines: tuple[Sequence[str], Sequence[str]]) -> dict[str, set[str]]:
        out: dict[str, set[str]] = defaultdict(set)
        for line in lines[0]:
            out[line.split("|", 1)[0]].add(line)
        for line in lines[1]:
            member, chain = line.split(">", 1)
            out[member].add(line)
            out[chain].add(line)
        return out

    before, after = groups(old), groups(new)
    return {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}


def adjustment_of(
    factors: Sequence[str], lineage: Sequence[str], chains: Collection[str]
) -> Adjustment:
    """The part of an adjustment (as `adjustment_lines`) that the series of `chains` read: their
    factors and every link into or out of them. A link out of a chain to an untouched one keeps
    that ISIN's bars under the other id, as in the whole adjustment."""
    picked, paid = [], []
    for line in factors:
        instrument, ex_date, value = line.split("|")
        if instrument not in chains:
            continue
        day = date.fromisoformat(ex_date)
        if value.startswith(DIVIDEND_LINE):
            paid.append(
                DividendCash(
                    instrument, day, require_exact(value.removeprefix(DIVIDEND_LINE), "dividend")
                )
            )
        else:
            picked.append(ActionFactor(instrument, day, Decimal(value)))
    links = {}
    for line in lineage:
        member, chain = line.split(">", 1)
        if member in chains or chain in chains:
            links[member] = chain
    return Adjustment(tuple(picked), links, tuple(paid))

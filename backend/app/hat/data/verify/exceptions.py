"""Reviewed exceptions of D2, D3, D4, D6 and D7: a failure that is explained and known, listed
with why.

The file (CSV) has one row per exception: `code` (the finding's code), `isin` (the item's own ISIN:
a stored action's instrument, a reference action's ISIN, a link's old ISIN, a member, a 5-minute
day's instrument; D7's items have none, so its file names the index in a `key` column instead),
`symbol` (for the reader), `day` (the item's day: an ex-date, a link's date, the last bar), `pin`
(`field=value`, several joined by `;`: the values the item was reviewed at, such as `stored=0.9424`
or `bars=62;scale=1.0`) and `reason`; every row must give all but the symbol, and no two rows may
name the same code, key and day. A check whose items carry their key and day under other fields
gives its own `key_of`. There is no wildcard: an exception names one item at its reviewed value, so
a later change to the item (another factor, another amount) fails again, and a rule that two items
match (by key and day, whatever their values) excepts neither. An excepted item moves to the
`excepted` finding (warn) with its reason; an exception no failure uses is listed as
`exception_unused` (warn), so the list cannot go stale unseen. A row whose reason starts with
`OPEN:` excepts nothing: it marks an item reviewed without a cause found, and moves it to the
`open_defect` finding (warn, every one listed, with what was searched), so a known defect stays in
view.
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..audit import WARN, finding

_ISIN = re.compile(r"^IN[A-Z0-9]{10}$")

KeyOf = Callable[[str, Mapping[str, Any]], tuple[str | None, str | None]]  # code, item -> key, day
DAY_FIELDS = ("ex_date", "effective", "date", "last_bar")
PIN_TOLERANCE = 1e-6  # relative: a pinned number read back from the report's text
OPEN = "OPEN:"  # a reason with this prefix is an open defect, not an explanation


@dataclass(frozen=True)
class Excepted:
    code: str
    isin: str
    day: str
    pin: str  # `field=value`: the item's value when it was reviewed
    reason: str
    symbol: str = ""

    @property
    def open(self) -> bool:
        """Whether this row is an open defect (no cause found), not an exception."""
        return self.reason.startswith(OPEN)

    def pins(self, item: Mapping[str, Any]) -> bool:
        """Whether `item` still has every reviewed value (numbers within `PIN_TOLERANCE`)."""
        return all(_pinned(item, part) for part in self.pin.split(";"))


def _pinned(item: Mapping[str, Any], pin: str) -> bool:
    name, _, want = pin.partition("=")
    got = item.get(name.strip())
    if got is None:
        return False
    try:
        a, b = float(got), float(want)
    except (TypeError, ValueError):
        return str(got).strip() == want.strip()
    return abs(a - b) <= PIN_TOLERANCE * max(abs(a), abs(b), 1.0)


def read_exceptions(path: Path, check: str) -> list[Excepted]:
    """The reviewed exceptions in `path`; a row without a reason, an ISO day or a pin, and a
    second row for the same code, key and day, are refused. A missing file is no exceptions."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [
            Excepted(
                r["code"].strip(),
                (r.get("isin") or r.get("key") or "").strip().upper(),
                r["day"].strip(),
                r.get("pin", "").strip(),
                r["reason"].strip(),
                r.get("symbol", "").strip(),
            )
            for r in csv.DictReader(handle)
        ]
    bad = [
        r
        for r in rows
        if not (r.reason and _is_day(r.day) and r.isin and r.code and _pins_read(r.pin))
    ]
    if bad:
        raise ValueError(f"{check} exceptions without a code, ISIN, day, pin or reason: {bad[:3]}")
    twice = [k for k, n in Counter((r.code, r.isin, r.day) for r in rows).items() if n > 1]
    if twice:
        raise ValueError(f"{check} exceptions listed twice: {twice[:3]}")
    return rows


def _pins_read(pin: str) -> bool:
    return all("=" in part and part.partition("=")[0].strip() for part in pin.split(";"))


def _is_day(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


def item_key(item: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """An item's own ISIN (`isin`, else the one ISIN among a reference action's `keys`: NSE's
    row names one) and its day (the first of `DAY_FIELDS` it has as text: a move's `ex_date` is
    a flag)."""
    isin = item.get("isin")
    if not isin:
        named = [k for k in item.get("keys") or () if _ISIN.match(str(k).upper())]
        isin = named[0] if len(named) == 1 else None
    day = next((item[f] for f in DAY_FIELDS if isinstance(item.get(f), str) and item[f]), None)
    return (str(isin).upper() if isin else None), day


@dataclass
class Exceptions:
    """The reviewed exceptions of one check run, and what they matched."""

    rules: Sequence[Excepted] = ()
    used: set[Excepted] = field(default_factory=set)
    excepted: list[dict[str, Any]] = field(default_factory=list)
    open: list[dict[str, Any]] = field(default_factory=list)
    key_of: KeyOf = lambda _code, item: item_key(item)

    def split(self, code: str, items: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """The items of finding `code` no exception covers; the covered ones (excepted, or open
        defects) are kept, with their reason, for `findings`. A rule whose key and day two items
        share excepts neither, whatever their values."""
        rules = {(r.isin, r.day): r for r in self.rules if r.code == code}
        items = list(items)
        matched: dict[int, Excepted] = {}
        for n, item in enumerate(items):
            key, day = self.key_of(code, item)
            rule = rules.get((None if key is None else str(key).upper(), day))
            if rule is not None:
                matched[n] = rule
        twice = {r for r, k in Counter(matched.values()).items() if k > 1}
        kept = []
        for n, item in enumerate(items):
            rule = matched.get(n)
            if rule is None or rule in twice or not rule.pins(item):
                kept.append(item)
            else:
                self.used.add(rule)
                (self.open if rule.open else self.excepted).append(
                    {"code": code, **item, "reason": rule.reason}
                )
        return kept

    def findings(self) -> list[dict[str, Any]]:
        """`open_defect` (every one listed), `excepted` (every excepted item) and
        `exception_unused`, all warn."""
        out = []
        if self.open:
            out.append({**finding("open_defect", WARN, ()), "count": len(self.open)})
            out[-1]["items"] = list(self.open)
        if self.excepted:
            out.append(finding("excepted", WARN, self.excepted, count=len(self.excepted)))
        unused = [asdict(r) for r in self.rules if r not in self.used]
        if unused:
            out.append(finding("exception_unused", WARN, unused))
        return out

    def summary(self) -> dict[str, int]:
        return dict(sorted(Counter(i["code"] for i in self.excepted).items()))

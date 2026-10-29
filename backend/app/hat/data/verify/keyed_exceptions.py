"""The reviewed exceptions of D3, D4 and D7 (`app.paths.VERIFY_EXCEPTIONS`): one CSV per check,
each row a failed finding's code, what it is about (`key`: a symbol, an index name), its day and
why it is known and accepted. A failed item a row names is reported under `excepted` (warn) with
that reason, and a row no failure needs under `exception_unused` (warn: the list is out of
date). There is no wildcard: a new failure is always reported."""

from __future__ import annotations

import csv
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from ..audit import WARN, finding

KeyOf = Callable[[str, Mapping[str, Any]], tuple[str, str]]  # (code, item) -> (key, ISO day)


@dataclass(frozen=True)
class Excepted:
    code: str
    key: str
    day: str
    reason: str


def read_exceptions(path: Path) -> list[Excepted]:
    """The rows of one exceptions file (CSV: code, key, day, reason); every row needs a reason
    and a day (YYYY-MM-DD). A missing file is no exceptions."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [
            Excepted(r["code"].strip(), r["key"].strip().upper(), r["day"].strip(), r["reason"])
            for r in csv.DictReader(handle)
        ]
    bad = [r for r in rows if not r.reason.strip() or not _is_day(r.day)]
    if bad:
        raise ValueError(f"exceptions without a reason or a day: {bad[:3]}")
    return rows


def _is_day(text: str) -> bool:
    try:
        date.fromisoformat(text)
    except ValueError:
        return False
    return True


@dataclass
class Exceptions:
    """The rows of one check, applied finding by finding (`split`), then reported (`findings`)."""

    rules: Sequence[Excepted]
    key_of: KeyOf
    excepted: list[dict[str, Any]]
    used: set[Excepted]

    @classmethod
    def of(cls, rules: Sequence[Excepted], key_of: KeyOf) -> Exceptions:
        return cls(rules, key_of, [], set())

    def split(self, code: str, items: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
        """`items` of the failed finding `code` that no row covers; the covered ones are kept
        under `excepted`, each with its row's reason."""
        rules = {(r.key, r.day): r for r in self.rules if r.code == code}
        if not rules:
            return list(items)
        kept = []
        for item in items:
            key, day = self.key_of(code, item)
            rule = rules.get((key.upper(), day))
            if rule is None:
                kept.append(item)
            else:
                self.used.add(rule)
                self.excepted.append({"code": code, **item, "reason": rule.reason})
        return kept

    def findings(self) -> list[dict[str, Any]]:
        """`excepted` and `exception_unused` (both warn), when they hold anything."""
        out = []
        if self.excepted:
            out.append(finding("excepted", WARN, self.excepted))
        unused = [asdict(r) for r in self.rules if r not in self.used]
        if unused:
            out.append(finding("exception_unused", WARN, unused))
        return out


__all__ = ["Excepted", "Exceptions", "read_exceptions"]

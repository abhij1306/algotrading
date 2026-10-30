"""The known coverage gaps of the 5-minute history (`app.paths.BARS5M_GAPS`): spans of sessions
whose stored bars a vendor's copy does not cover in full, each named by vendor, first and last
session, the instrument's ISIN (`*`: every instrument) and why. A replay refuses the 5-minute
bars of a session inside a gap (`in_gap`) and decides it on the daily estimate, so a truncated
copy is refused by where it is known to be short, never by how its prices moved.

The rows in force: the sessions of 2026-08-03..2026-10-28 where the Kotak export of 2026-10-28
ends at the 15:10 bar and the Fyers refetch (`bars5m_refetch`) could not make the stored day
whole (16 sessions of 11 instruments, each row with the refetch's outcome), and
NSE's special sessions in the history's span ran shorter than the regular session (2024-03-02
and 2024-05-18, the disaster-recovery site's live sessions)."""

from __future__ import annotations

import csv
import hashlib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path

EVERY = "*"
REFETCH_DIR = "bars5m_refetch"  # under the data root: one folder per `bars5m_refetch` run
APPLIED = "applied.csv"  # in a run folder: the (instrument id, day) its `apply` wrote


@dataclass(frozen=True)
class Gap:
    vendor: str
    first: date
    last: date
    isin: str  # `EVERY`: every instrument
    reason: str


def read_gaps(path: Path) -> list[Gap]:
    """The gaps in `path` (CSV: vendor, first, last, isin, symbol, reason; the symbol is for the
    reader). A row without a vendor, an ISIN or a reason, with a span that ends before it
    starts, or listed twice is refused. A missing file is no gaps."""
    if not path.is_file():
        return []
    with path.open(encoding="utf-8", newline="") as handle:
        rows = [
            Gap(
                r["vendor"].strip(),
                date.fromisoformat(r["first"].strip()),
                date.fromisoformat(r["last"].strip()),
                r["isin"].strip().upper(),
                r["reason"].strip(),
            )
            for r in csv.DictReader(handle)
        ]
    bad = [g for g in rows if not (g.vendor and g.isin and g.reason) or g.last < g.first]
    if bad:
        raise ValueError(f"5-minute gaps without a vendor, ISIN, reason or span: {bad[:3]}")
    twice = [k for k, n in Counter((g.vendor, g.isin, g.first) for g in rows).items() if n > 1]
    if twice:
        raise ValueError(f"5-minute gaps listed twice: {twice[:3]}")
    return rows


def gap_spans(gaps: Iterable[Gap]) -> dict[str, tuple[tuple[date, date], ...]]:
    """The gaps' spans by ISIN (`EVERY` for the ones of every instrument)."""
    out: dict[str, list[tuple[date, date]]] = defaultdict(list)
    for g in gaps:
        out[g.isin].append((g.first, g.last))
    return {isin: tuple(sorted(spans)) for isin, spans in out.items()}


def spans_digest(spans: Mapping[str, tuple[tuple[date, date], ...]]) -> str:
    """A digest of `spans` (`gap_spans`), whatever their order or the file's layout: the gaps a
    run's replay refused sessions by, as its conventions record them."""
    text = repr(
        sorted((isin, [(a.isoformat(), b.isoformat()) for a, b in s]) for isin, s in spans.items())
    )
    return hashlib.sha256(text.encode()).hexdigest()


def in_gap(spans: Mapping[str, tuple[tuple[date, date], ...]], isin: str | None, day: date) -> bool:
    """Whether `day` lies in a gap of the instrument of `isin` or of every instrument."""
    found = (*spans.get(EVERY, ()), *(spans.get(isin.upper(), ()) if isin else ()))
    return any(first <= day <= last for first, last in found)


__all__ = ["EVERY", "Gap", "gap_spans", "in_gap", "read_gaps", "spans_digest"]


def refetched_days(root: Path) -> set[tuple[str, date]]:
    """The (instrument id, day) every applied `bars5m_refetch` run under the data root `root`
    wrote from Fyers' history (each run folder's `applied.csv`): days a vendor's short copy no
    longer stands for."""
    out: set[tuple[str, date]] = set()
    for path in sorted((root / REFETCH_DIR).glob(f"*/{APPLIED}")):
        with path.open(encoding="utf-8", newline="") as handle:
            out |= {
                (r["instrument_id"], date.fromisoformat(r["day"])) for r in csv.DictReader(handle)
            }
    return out

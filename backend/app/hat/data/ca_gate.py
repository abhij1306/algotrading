"""The shared kernel of the corporate-action importers (CA1, CA3, CA4): the outcome statuses,
the instrument reference a row is matched against (its ISIN, its symbol now or around the
ex-date, and their lineage chains), the raw-bar windows around an ex-date and the price gate
that confirms a factor from the ex-date gap. The file import (`ca_import`), the research import
(`ca_research`), the dividend imports (`ca_payouts`) and the point-in-time checks read it.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import HatInstrument, HatInstrumentSymbolHistory
from .chains import chain_members, stored_links
from .store import DataStore

EVENT_IMPORTED = "corporate_action.imported"
# The gate: NSE's widest daily price band is 20%, so the ex-date open of a real action is within
# 20% of the previous close times its factor. Measured on the 707 split and bonus factors HAT
# had stored with bars on both days (2026-10-24): the median miss is 2.6%, 90% are within 10%
# and 95% within 16%.
GATE_TOL = 0.20
BAR_DAYS = 14  # the previous session and the ex session must fall this close to the ex-date
SAME_FACTOR_TOL = Fraction(1, 20000)  # half a unit in the 4th place
TEXT_MAX = 2000  # cells are cut to this length
STORED = "stored"  # a new row (with --apply; "to store" in a dry run)
ALREADY = "already"  # confirmed, and the same factor is stored
CONFLICT = "conflict"  # confirmed or not, a different factor is stored for the same event
REVIEW = "review"  # not confirmed, ambiguous, unparsed or without bars: the owner decides
SKIPPED = "skipped"  # nothing to store: price-neutral, InvIT units, an SME row with no instrument
STATUSES = (STORED, ALREADY, CONFLICT, REVIEW, SKIPPED)


class CaFileError(HatError):
    """The file cannot be read as the expected tables; `code` is stable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


_MONTHS = "jan feb mar apr may jun jul aug sep oct nov dec".split()
_NSE_DATE = re.compile(r"(\d{1,2})-([A-Za-z]{3})-(\d{4})")


def nse_date(raw: str) -> date | None:
    """`04-Sep-2014` as a date; anything else (`-`, empty, a bad day) is None."""
    found = _NSE_DATE.fullmatch(raw.strip())
    if found is None or found.group(2).lower() not in _MONTHS:
        return None
    try:
        return date(int(found.group(3)), _MONTHS.index(found.group(2).lower()) + 1, int(found[1]))
    except ValueError:
        return None


@dataclass(frozen=True)
class Verdict:
    """The gate's answer: `reason` is "" when the bars confirm, else `off_factor` or
    `ambiguous`; `gap` is the ex-date open over the previous close."""

    reason: str
    gap: Fraction

    @property
    def confirmed(self) -> bool:
        return not self.reason


def gate(expected: Fraction, previous_close: Fraction, ex_open: Fraction) -> Verdict:
    """Do the bars confirm `expected`? The gap (ex open / previous close) must be within
    `GATE_TOL` of it, and the gap's distance from it (in log terms) at most half its distance
    from 1: otherwise a plain session move could explain the gap as well (`ambiguous`)."""
    if expected <= 0 or previous_close <= 0 or ex_open <= 0:
        raise ValueError("gate needs positive prices and factor")
    gap = ex_open / previous_close
    off = abs(math.log(gap / expected))
    move = abs(math.log(gap))
    if off > math.log1p(GATE_TOL):
        return Verdict("off_factor", gap)
    if 2 * off > move:
        return Verdict("ambiguous", gap)
    return Verdict("", gap)


@dataclass(frozen=True)
class Instrument:
    id: str
    isin: str
    symbol: str
    name: str


@dataclass
class Reference:
    """HAT's instruments as the import matches them."""

    instruments: dict[str, Instrument]
    by_isin: dict[str, str]
    by_symbol: dict[str, set[str]]  # current symbols
    history: dict[str, list[tuple[str, date, date | None]]]  # symbol -> (id, from, to)
    chain: dict[str, frozenset[str]]  # a linked instrument -> every member of its chain
    successors: set[str]  # the newer side of a linked pair: not a fresh listing

    def ids(self, symbol: str, isin: str, day: date) -> set[str]:
        """The instruments the row can mean: its ISIN, its symbol now or around `day`, and the
        linked chains of those."""
        found = set(self.by_symbol.get(symbol, ()))
        if isin in self.by_isin:
            found.add(self.by_isin[isin])
        slack = timedelta(days=BAR_DAYS)
        for iid, start, end in self.history.get(symbol, ()):
            if start <= day + slack and (end is None or end >= day - slack):
                found.add(iid)
        for iid in list(found):
            found |= self.chain.get(iid, set())
        return found


def load_reference(session: Session) -> Reference:
    instruments = {
        str(i.id): Instrument(str(i.id), i.isin, i.symbol, i.name)
        for i in session.scalars(select(HatInstrument))
    }
    by_symbol: dict[str, set[str]] = defaultdict(set)
    for i in instruments.values():
        by_symbol[i.symbol].add(i.id)
    history: dict[str, list[tuple[str, date, date | None]]] = defaultdict(list)
    for h in session.scalars(select(HatInstrumentSymbolHistory)):
        history[h.symbol].append((str(h.instrument_id), h.valid_from, h.valid_to))
    links = stored_links(session)
    return Reference(
        instruments,
        {i.isin: i.id for i in instruments.values()},
        dict(by_symbol),
        dict(history),
        chain_members(links),
        {new for _, new in links},
    )


@dataclass(frozen=True)
class Bar:
    instrument_id: str
    day: date
    isin: str
    open: Decimal
    close: Decimal


# The latest ingest run's row of each instrument and day, for the instruments each proposal
# can mean, within BAR_DAYS of its ex-date (an equi-join on the instrument first, so DuckDB
# never compares every bar with every proposal).
_WINDOW_SQL = """
SELECT w.k, r.instrument_id, r.date, arg_max(r.isin, r.ingest_run_id),
       arg_max(r.open, r.ingest_run_id), arg_max(r.close, r.ingest_run_id)
FROM want w JOIN bars_daily_raw r ON r.instrument_id = w.instrument_id
WHERE r.date BETWEEN w.ex - ? AND w.ex + ?
GROUP BY w.k, r.instrument_id, r.date
"""


_FIRST_SQL = """
WITH span AS (SELECT instrument_id, min(date) AS first_day FROM bars_daily_raw GROUP BY 1)
SELECT r.instrument_id, r.date, arg_max(r.close, r.ingest_run_id)
FROM bars_daily_raw r JOIN span s ON r.instrument_id = s.instrument_id AND r.date = s.first_day
GROUP BY r.instrument_id, r.date
"""


def read_windows(
    store: DataStore, wanted: Mapping[int, tuple[date, set[str]]]
) -> dict[int, list[Bar]]:
    """The bars around each wanted (ex-date, instruments), by key."""
    out: dict[int, list[Bar]] = defaultdict(list)
    rows = [(k, iid, ex) for k, (ex, ids) in wanted.items() for iid in sorted(ids)]
    if not rows:
        return out
    with closing(store.connect()) as con:
        con.execute("CREATE TABLE want (k INTEGER, instrument_id VARCHAR, ex DATE)")
        con.executemany("INSERT INTO want VALUES (?, ?, ?)", rows)
        for k, iid, day, isin, open_, close in con.execute(
            _WINDOW_SQL, [BAR_DAYS, BAR_DAYS]
        ).fetchall():
            out[k].append(Bar(str(iid), day, isin, open_, close))
    return out


def read_first_closes(store: DataStore) -> dict[str, tuple[date, Decimal]]:
    """Each instrument's first session and close in the bars."""
    with closing(store.connect()) as con:
        return {str(i): (d, c) for i, d, c in con.execute(_FIRST_SQL).fetchall()}


def around(bars: Iterable[Bar], ex_date: date, isin: str) -> tuple[Bar, Bar] | str:
    """The last bar before the ex-date and the first on or after it, or why there is no pair:
    `no_bars`, or `ambiguous_bars` when two instruments trade on that day and the row's ISIN
    names neither. A suspension may put the pair a few sessions apart (within `BAR_DAYS`)."""
    before = [b for b in bars if b.day < ex_date]
    after = [b for b in bars if b.day >= ex_date]
    if not before or not after:
        return "no_bars"
    picked = []
    for side, day in ((before, max(b.day for b in before)), (after, min(b.day for b in after))):
        found = [b for b in side if b.day == day]
        if len(found) > 1:
            found = [b for b in found if b.isin == isin]
        if len(found) != 1:
            return "ambiguous_bars"
        picked.append(found[0])
    return picked[0], picked[1]


def fmt4(value: Fraction | Decimal) -> str:
    return f"{float(value):.4f}"


def same_factor(stored: Decimal, factor: Fraction) -> bool:
    """A stored factor is the same as `factor` up to the rounding of its source (the MCP's are
    rounded to 4 places: 0.6667 is a 1:2 bonus)."""
    return abs(Fraction(stored) - factor) <= SAME_FACTOR_TOL

"""The lineage review list and the adjusted series' unexplained jumps (PR 74, CA4, CA7):
`python -m app.hat.data.lineage report` and `show`.

A jump is a day whose adjusted open is more than `JUMP_TOL` from the previous adjusted close:
a price-changing action with no stored factor (a demerger ex-date shows here), a wrong link or a
bad bar. The report lists the review cases of `hat_isin_lineage`, the chain keys whose stored
copies disagree, the dividends without an amount and the jumps; `show` prints one symbol's chain.
Both read every factor and link as stored today (`HINDSIGHT`).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatInstrument, HatIsinLineage
from .ca_nightly import missing_amounts
from .chains import (
    LINKED,
    REVIEW,
    adjustment,
    chain_factors,
    chains,
    stored_actions,
    stored_dividends,
)
from .store import DATASET_BARS, Adjustment, DataStore

JUMP_TOL = Decimal("0.25")
JUMP_MAX_DAYS = 14  # a relisting after a longer suspension may jump on its own
# What a jump is, from its ISIN and raw prices (`jump_kind`, CA7): a rights entitlement (security
# type `20`, the `-RE` symbols) and an ETF or fund unit (`INF`) trade on their own terms; a raw
# move of at most two Rs 0.05 ticks passes `JUMP_TOL` only at a price of Rs 0.40 or less, where
# one tick is the move (2,505 of the 2,835 jumps 2011-2026). `other` is what needs a reason.
JUMP_RIGHTS, JUMP_ETF, JUMP_TICK, JUMP_OTHER = "rights_entitlement", "etf", "tick", "other"
JUMP_KINDS = (JUMP_OTHER, JUMP_TICK, JUMP_RIGHTS, JUMP_ETF)
TICK_MOVE = Decimal("0.10")


@dataclass(frozen=True)
class Jump:
    """A day whose adjusted open is more than `JUMP_TOL` from the previous adjusted close: a
    price-changing action with no stored factor (a missing split or bonus, a demerger, a
    capital reduction) or a wrong one."""

    instrument_id: str
    previous_day: date
    day: date
    previous_close: Decimal
    open: Decimal
    isin: str = ""  # the day's own ISIN
    previous_raw_close: Decimal | None = None
    raw_open: Decimal | None = None

    @property
    def ratio(self) -> Decimal:
        return self.open / self.previous_close

    @property
    def kind(self) -> str:
        return jump_kind(self.isin, self.previous_raw_close, self.raw_open)


def jump_kind(isin: str, previous_raw_close: Decimal | None, raw_open: Decimal | None) -> str:
    """What a jump is (`JUMP_KINDS`): a rights entitlement or an ETF unit by its ISIN, else a
    tick when the raw open is within `TICK_MOVE` of the raw previous close, else `other`."""
    if isin[7:9] == "20":
        return JUMP_RIGHTS
    if isin.startswith("INF"):
        return JUMP_ETF
    if (
        previous_raw_close is not None
        and raw_open is not None
        and abs(raw_open - previous_raw_close) <= TICK_MOVE
    ):
        return JUMP_TICK
    return JUMP_OTHER


def jump_counts(jumps: Iterable[Jump]) -> tuple[dict[int, int], dict[str, int]]:
    """The jumps per year of their day and per kind, each in order."""
    years: dict[int, int] = defaultdict(int)
    kinds: dict[str, int] = dict.fromkeys(JUMP_KINDS, 0)
    for j in jumps:
        years[j.day.year] += 1
        kinds[j.kind] += 1
    return dict(sorted(years.items())), kinds


# One year at a time, from the latest run's rows filtered by date first (a window over the
# whole view holds every bar in memory); the lookback gives the year's first day its previous
# close. The raw open is the adjusted one over the day's factor (close / raw close).
_JUMPS_SQL = """
WITH latest AS (
    SELECT instrument_id, date, isin, open, close, raw_close FROM bars_daily_adj
    WHERE date BETWEEN ? AND ?
    QUALIFY row_number() OVER (PARTITION BY instrument_id, date ORDER BY ingest_run_id DESC) = 1
),
s AS (
    SELECT instrument_id, date, isin, open, close, raw_close,
           lag(close) OVER w AS previous_close, lag(date) OVER w AS previous_day,
           lag(raw_close) OVER w AS previous_raw_close
    FROM latest WINDOW w AS (PARTITION BY instrument_id ORDER BY date)
)
SELECT instrument_id, previous_day, date, previous_close, open, isin, previous_raw_close,
       CASE WHEN close > 0 THEN CAST(open * raw_close / close AS DECIMAL(18, 4)) END AS raw_open
FROM s
WHERE date >= ? AND previous_close > 0 AND date - previous_day <= ?
  AND abs(open - previous_close) > ? * previous_close
"""


def unexplained_jumps(
    store: DataStore, adjust: Adjustment, since: date, until: date | None = None
) -> list[Jump]:
    """Every jump in the adjusted series HAT reads, from `since` on (to `until`, when given),
    newest first."""
    days = [d for d in store.days(DATASET_BARS) if d >= since and (until is None or d <= until)]
    found: list[Jump] = []
    if not days:
        return found
    with closing(store.connect(adjust)) as con:
        for year in range(days[0].year, days[-1].year + 1):
            first = max(since, date(year, 1, 1))
            lookback = first - timedelta(days=JUMP_MAX_DAYS)
            params = [lookback, min(date(year, 12, 31), days[-1]), first, JUMP_MAX_DAYS, JUMP_TOL]
            found += [
                Jump(
                    str(r[0]),
                    r[1],
                    r[2],
                    r[3],
                    r[4],
                    r[5] or "",
                    r[6],
                    r[7],
                )
                for r in con.execute(_JUMPS_SQL, params).fetchall()
            ]
    return sorted(found, key=lambda j: (j.day, j.instrument_id), reverse=True)


def _instruments(session: Session) -> dict[str, HatInstrument]:
    return {str(i.id): i for i in session.scalars(select(HatInstrument))}


def _label(instruments: Mapping[str, HatInstrument], instrument_id: str) -> str:
    found = instruments.get(instrument_id)
    return f"{found.symbol} {found.isin}" if found is not None else instrument_id


HINDSIGHT = "hindsight: every factor and link as stored today, not as of any date (CA6)"


def review_report(
    session: Session,
    store: DataStore,
    since: date,
    limit: int,
    out: Callable[[str], Any],
    kinds: Collection[str] = JUMP_KINDS,
) -> None:
    """Print the review list: the successions not linked, the chains whose action copies
    disagree and the unexplained jumps since `since` (the newest `limit` of each, of `kinds`),
    with their count per year and per kind."""
    instruments = _instruments(session)
    rows = session.scalars(
        select(HatIsinLineage).order_by(HatIsinLineage.effective_date.desc())
    ).all()
    linked = [r for r in rows if r.status == LINKED]
    review = [r for r in rows if r.status == REVIEW]
    out(HINDSIGHT)
    out(f"lineage: {len(linked)} linked, {len(review)} for review")
    reasons: dict[str, int] = defaultdict(int)
    for r in review:
        reasons[r.reason] += 1
    out("review reasons: " + ", ".join(f"{k} {v}" for k, v in sorted(reasons.items())))
    for r in review[:limit]:
        e = r.evidence
        out(
            f"  REVIEW {r.reason}: {e.get('old_symbol')} {e.get('old_isin')} "
            f"(last {e.get('old_last_day')}, close {e.get('old_last_close')}) -> "
            f"{e.get('new_symbol')} {e.get('new_isin')} (first {e.get('new_first_day')}, "
            f"prev {e.get('new_prev_close')}, open {e.get('new_first_open')}), "
            f"gap {e.get('gap_sessions')}, factor {e.get('gap_factor')}"
            + (f", {'; '.join(e['restructuring'])}" if e.get("restructuring") else "")
        )
    actions = stored_actions(session)
    links = [(str(r.old_instrument_id), str(r.new_instrument_id)) for r in linked]
    _, conflicts = chain_factors(actions, chains(links))
    out(f"action copies that disagree within a chain: {len(conflicts)} (the current ISIN's kept)")
    for chain, ex_date, kind in conflicts[:limit]:
        out(f"  CONFLICT {_label(instruments, chain)} {ex_date} {kind}")
    # a dividend stored without its cash (the MCP lookup, a review case) stays price-neutral:
    # its ex-date drop is in the total-return series (CA4)
    missing = missing_amounts(session)
    out(f"dividends without an amount (price-neutral in the total-return series): {len(missing)}")
    for m in missing[:limit]:
        out(f"  NO_AMOUNT {m.symbol} {m.isin} {m.ex_date} ({m.source})")
    # the series the signals read: a dividend with its cash explains its ex-date drop (CA7)
    jumps = unexplained_jumps(store, adjustment(actions, links, stored_dividends(session)), since)
    out(
        f"unexplained jumps since {since} (total-return open vs previous close beyond "
        f"{JUMP_TOL:.0%}, no factor or dividend; demerger ex-dates show here): {len(jumps)}"
    )
    years, by_kind = jump_counts(jumps)
    out("  by year: " + ", ".join(f"{y} {n}" for y, n in years.items()))
    out(
        "  by kind: "
        + ", ".join(f"{k} {n}" for k, n in by_kind.items())
        + f" (tick: a raw move of at most Rs {TICK_MOVE}, past {JUMP_TOL:.0%} only below Rs 0.40)"
    )
    for j in [j for j in jumps if j.kind in kinds][:limit]:
        out(
            f"  JUMP {_label(instruments, j.instrument_id)} {j.previous_day} close "
            f"{j.previous_close:.2f} -> {j.day} open {j.open:.2f} (x{j.ratio:.3f}) {j.kind}"
        )


def show_chains(
    session: Session, store: DataStore, symbols: Sequence[str], out: Callable[[str], Any]
) -> None:
    """Each instrument that ever traded under one of `symbols`, with its links both ways and
    the unexplained jumps of its chain."""
    from ..models import HatInstrumentSymbolHistory

    wanted = {s.upper() for s in symbols}
    ids = {
        str(i)
        for i in session.scalars(
            select(HatInstrumentSymbolHistory.instrument_id).where(
                HatInstrumentSymbolHistory.symbol.in_(wanted)
            )
        )
    } | {
        str(i)
        for i in session.scalars(select(HatInstrument.id).where(HatInstrument.symbol.in_(wanted)))
    }
    instruments = _instruments(session)
    rows = session.scalars(select(HatIsinLineage)).all()
    links = [
        (str(r.old_instrument_id), str(r.new_instrument_id)) for r in rows if r.status == LINKED
    ]
    heads = chains(links)
    actions = stored_actions(session)
    jumps = unexplained_jumps(store, adjustment(actions, links), date.min)
    out(HINDSIGHT)
    for instrument_id in sorted(ids, key=lambda i: (instruments[i].symbol, instruments[i].isin)):
        head = heads.get(instrument_id, (instrument_id, 0))[0]
        found = instruments[instrument_id]
        out(
            f"{found.symbol} {found.isin} {found.name!r} active={found.active} "
            f"chain={_label(instruments, head)}"
        )
        for r in rows:
            if instrument_id in (str(r.old_instrument_id), str(r.new_instrument_id)):
                e = r.evidence
                out(
                    f"  {r.status} {r.reason}: {e['old_isin']} ({e['old_symbol']}, last "
                    f"{e['old_last_day']}) -> {e['new_isin']} ({e['new_symbol']}, first "
                    f"{e['new_first_day']}) gap {e['gap_sessions']} "
                    f"prev x{e.get('prev_close_ratio')} open x{e.get('open_ratio')} "
                    f"factor {e['gap_factor']}"
                )
        if head == instrument_id:
            for j in jumps:
                if j.instrument_id == head:
                    out(
                        f"  jump {j.previous_day} close {j.previous_close:.2f} -> {j.day} open "
                        f"{j.open:.2f} (x{j.ratio:.3f})"
                    )

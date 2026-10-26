"""The universe database operations (HAT-PORT-020..022): universes, their versions and the
point-in-time membership intervals.

Every function changes state in the caller's open transaction; none commits. The rules they apply
are the pure ones in `universe`.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.confighash import config_hash
from app.hat.configs.records import Bot
from app.hat.configs.unit import configs_of
from app.hat.data.isin import ordinary_equity
from app.hat.data.reference import IndustryRow
from app.hat.data.symbol_history import resolve_symbol
from app.hat.models import HatUniverse, HatUniverseMember, HatUniverseVersion
from app.hat.risk.sessions import SessionCalendar, SessionState

from . import universe
from .common import PortfolioError


def create_universe(
    session: Session, *, user_id: uuid.UUID, market_id: uuid.UUID, name: str, kind: str = "manual"
) -> HatUniverse:
    row = HatUniverse(user_id=user_id, market_id=market_id, name=name, kind=kind)
    session.add(row)
    session.flush()
    return row


def universe_named(
    session: Session, *, user_id: uuid.UUID, market_id: uuid.UUID, name: str
) -> HatUniverse:
    """The user's universe of that name in the market, created (manual) when missing."""
    row = session.scalars(
        select(HatUniverse).where(
            HatUniverse.user_id == user_id,
            HatUniverse.market_id == market_id,
            HatUniverse.name == name,
        )
    ).first()
    return row or create_universe(session, user_id=user_id, market_id=market_id, name=name)


def resolve_symbols(
    session: Session,
    market_id: uuid.UUID,
    symbols: Collection[str],
    on: date,
    isins: Mapping[str, str] | None = None,
) -> tuple[frozenset[uuid.UUID], tuple[str, ...]]:
    """The instruments the symbols name on `on`, and the symbols that name none (sorted). When
    `isins` gives a symbol's ISIN (an index CSV does), an instrument with another ISIN does not
    count: the symbol is reported as `SYMBOL(ISIN)` rather than resolved to another security."""
    found: set[uuid.UUID] = set()
    unknown: list[str] = []
    for symbol in sorted({s.strip().upper() for s in symbols if s.strip()}):
        instrument = resolve_symbol(session, market_id, symbol, on)
        isin = (isins or {}).get(symbol, "").strip().upper()
        if instrument is None:
            unknown.append(symbol)
        elif isin and instrument.isin.upper() != isin:
            unknown.append(f"{symbol}({isin})")
        else:
            found.add(instrument.id)
    return frozenset(found), tuple(unknown)


def stock_rows(
    rows: Sequence[IndustryRow], excluded: Collection[str]
) -> tuple[list[IndustryRow], tuple[str, ...]]:
    """The index list's rows to resolve: the excluded symbols' rows dropped first (an exclude may
    name a row no instrument matches, such as an index provider's placeholder), then every row
    whose ISIN is not an ordinary equity share's (decisions.md "Stocks only": a REIT, InvIT,
    ETF or fund unit, or a placeholder), which are also returned, by symbol, sorted. A row
    without an ISIN is kept: its instrument's ISIN is classed when it is scanned."""
    kept: list[IndustryRow] = []
    not_stocks: list[str] = []
    for row in rows:
        if row.symbol in excluded:
            continue
        if row.isin and not ordinary_equity(row.isin):
            not_stocks.append(row.symbol)
        else:
            kept.append(row)
    return kept, tuple(sorted(not_stocks))


def assign_universe(
    session: Session,
    *,
    bot_id: uuid.UUID,
    universe_version_id: uuid.UUID,
    calendar: SessionCalendar,
    now: datetime,
) -> Bot:
    """Point a sleeve at a live universe version (its scans, backtests and evidence read the
    universe it belongs to), outside market hours only, so a session's scans never change under
    it. A draft or retired version is refused."""
    if calendar.state(now) is not SessionState.CLOSED:
        raise PortfolioError("market_hours")
    bot = configs_of(session).get(Bot, bot_id)
    if bot is None:
        raise PortfolioError("bot_unknown")
    version = session.get(HatUniverseVersion, universe_version_id)
    if version is None or version.status != "live":
        raise PortfolioError("universe_version_not_live")
    bot.universe_version_id = version.id
    return bot


def membership_rows(session: Session, universe_id: uuid.UUID) -> list[universe.Interval]:
    return [
        universe.Interval(m.instrument_id, m.effective_from, m.effective_to)
        for m in session.scalars(
            select(HatUniverseMember).where(HatUniverseMember.universe_id == universe_id)
        )
    ]


def members_of(session: Session, universe_id: uuid.UUID, day: date) -> frozenset[uuid.UUID]:
    """The members of `universe_id` on `day` (point in time, HAT-PORT-021)."""
    return universe.members_on(membership_rows(session, universe_id), day)


def apply_membership(
    session: Session,
    universe_id: uuid.UUID,
    target: Collection[uuid.UUID],
    day: date,
    *,
    reasons: dict[uuid.UUID, str] | None = None,
) -> tuple[int, int]:
    """Make the members on `day` equal `target`: new intervals for additions, a closed interval
    for removals. Returns (added, removed). A removal never touches an open position: leaving
    the universe stops entries only (HAT-PORT-023)."""
    rows = membership_rows(session, universe_id)
    to_add, to_remove = universe.plan_sync(rows, target, day)
    for instrument_id in sorted(to_add):
        universe.add_member(rows, instrument_id, day)  # refuses an edit into recorded history
        session.add(
            HatUniverseMember(
                universe_id=universe_id,
                instrument_id=instrument_id,
                effective_from=day,
                reason=(reasons or {}).get(instrument_id, "constituent"),
            )
        )
    for instrument_id in sorted(to_remove):
        universe.remove_member(rows, instrument_id, day)
        member = session.scalars(
            select(HatUniverseMember).where(
                HatUniverseMember.universe_id == universe_id,
                HatUniverseMember.instrument_id == instrument_id,
                HatUniverseMember.effective_to.is_(None),
            )
        ).one()
        member.effective_to = day
    session.flush()
    return len(to_add), len(to_remove)


def seed_universe_version(
    session: Session,
    universe_id: uuid.UUID,
    *,
    constituents: Collection[uuid.UUID],
    include: Collection[uuid.UUID] = (),
    exclude: Collection[uuid.UUID] = (),
    as_of: date,
    index: str | None = None,
) -> HatUniverseVersion:
    """A new draft version of the MVP universe: the index constituents plus the includes minus
    the excludes as of `as_of` (HAT-PORT-022). `index` names the index the constituents are of:
    a backtest of the universe reads that index's members point in time (`hat_index_member`)
    with the same includes and excludes, and refuses a version that names none. The draft only
    records its member list: the universe's membership intervals change when the version is
    published, so a draft is never seen by `members_of`. Each edit of the lists is a new
    version (HAT-PORT-020)."""
    target = universe.effective_members(constituents, include, exclude)
    reasons = sorted(str(i) for i in include if i not in set(constituents))
    previous = session.scalars(
        select(HatUniverseVersion.version).where(HatUniverseVersion.universe_id == universe_id)
    ).all()
    definition = {
        "kind": "manual",
        "source": "index_constituents",
        "as_of": as_of.isoformat(),
        "include": sorted(str(i) for i in include),
        "exclude": sorted(str(i) for i in exclude),
        "members": len(target),
        "member_ids": sorted(str(i) for i in target),
        "included_by_owner": reasons,
        **({} if index is None else {"index": index}),
    }
    row = HatUniverseVersion(
        universe_id=universe_id,
        version=max(previous, default=0) + 1,
        status="draft",
        definition=definition,
        config_hash=config_hash(definition),
    )
    session.add(row)
    session.flush()
    return row


def publish_version(session: Session, version_id: uuid.UUID, now: datetime) -> HatUniverseVersion:
    """Make a universe version live and retire the one that was (one live version per universe,
    HAT-PORT-020). The old one is retired first, so the partial unique index never sees two. The
    version's member list becomes the universe's membership from its `as_of` date (history before
    it is never touched)."""
    version = session.get(HatUniverseVersion, version_id)
    if version is None:
        raise PortfolioError("universe_version_unknown")
    for other in session.scalars(
        select(HatUniverseVersion).where(
            HatUniverseVersion.universe_id == version.universe_id,
            HatUniverseVersion.status == "live",
            HatUniverseVersion.id != version.id,
        )
    ):
        other.status = "retired"
    session.flush()
    definition = version.definition
    if "member_ids" in definition:
        owner_included = set(definition.get("included_by_owner", ()))
        apply_membership(
            session,
            version.universe_id,
            [uuid.UUID(i) for i in definition["member_ids"]],
            date.fromisoformat(definition["as_of"]),
            reasons={uuid.UUID(i): "include" for i in owner_included},
        )
    version.status = "live"
    version.promoted_at = now
    session.flush()
    return version

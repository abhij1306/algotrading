"""The quality quarantine, read and cleared by the owner (HAT-DATA-062, PR 33).

The quality gate quarantines an instrument whose move of over 20% no stored corporate action
explains (`quality.standing_quarantine`, `quality.quarantine`). The gate releases a quarantine
itself once a corporate action with ex-date `since` is stored for the instrument. A move with no
corporate action behind it (real news, a bad print) stays quarantined until the owner clears it
here:

    python -m app.hat.data.quarantine list
    python -m app.hat.data.quarantine clear SYMBOL

`clear` releases every open quarantine of the instruments with that symbol, active or not
(`released_by = owner`). The instrument is back in the next night's scans. `ca.adjust` looks up
the actions of every open quarantine's instrument on its `since` date each night, so a split
whose lookup failed on the day is found later and releases the quarantine at the next gate.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import HatInstrument, HatQualityQuarantine
from .quality import RELEASED_BY_OWNER

__all__ = ["OpenQuarantine", "clear", "main", "open_quarantines"]


@dataclass(frozen=True)
class OpenQuarantine:
    instrument_id: uuid.UUID
    symbol: str
    since: date


def open_quarantines(session: Session) -> list[OpenQuarantine]:
    """Every quarantine not yet released, oldest first."""
    rows = session.execute(
        select(HatQualityQuarantine.instrument_id, HatInstrument.symbol, HatQualityQuarantine.since)
        .join(HatInstrument, HatInstrument.id == HatQualityQuarantine.instrument_id)
        .where(HatQualityQuarantine.released_at.is_(None))
        .order_by(HatQualityQuarantine.since, HatInstrument.symbol)
    )
    return [OpenQuarantine(i, s, d) for i, s, d in rows]


def clear(session: Session, symbol: str, now: datetime) -> int:
    """Release the open quarantines of the instruments named `symbol` (active or not: `list`
    shows them all), in the caller's transaction. The number released (0: nothing was
    quarantined under that symbol)."""
    ids = select(HatInstrument.id).where(HatInstrument.symbol == symbol.strip().upper())
    rows = session.scalars(
        select(HatQualityQuarantine).where(
            HatQualityQuarantine.instrument_id.in_(ids), HatQualityQuarantine.released_at.is_(None)
        )
    ).all()
    for row in rows:
        row.released_at = now
        row.released_by = RELEASED_BY_OWNER
    session.flush()
    return len(rows)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    import argparse  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from app.database import db_session_scope  # noqa: PLC0415

    from ..settings import operator_settings  # noqa: PLC0415
    from ..time import utc_now  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.data.quarantine")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="the open quarantines")
    cleared = commands.add_parser("clear", help="release the quarantine of a symbol")
    cleared.add_argument("symbol")
    args = parser.parse_args(argv)
    operator_settings()  # loads .env, as every data command line does
    with db_session_scope() as session:
        if args.command == "list":
            for row in open_quarantines(session):
                sys.stdout.write(f"{row.since.isoformat()}  {row.symbol}  {row.instrument_id}\n")
            return 0
        released = clear(session, args.symbol, utc_now())
    sys.stdout.write(f"released {released} quarantine(s) of {args.symbol.strip().upper()}\n")
    return 0 if released else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

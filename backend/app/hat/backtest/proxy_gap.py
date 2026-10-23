"""The gap between a bot's shadow trades and the backtest's proxy for them (G15, G36 part, PR 40).

A shadow (paper) trade runs on live ticks; the backtest replays the same setup on daily bars with
proxies (the trigger at the level, close exits at the official close, a gap over filled at the
open: the run's `known_gaps`). The proxy of a closed shadow trade is the run's trade of the same
playbook, instrument and entry date. Per matched trade the report gives the entry price gap in
basis points of the proxy's entry, the R gap (shadow minus proxy) and whether the exit reasons
and the watchlist scores (PR 65) agree; per bot, their means and the R gap by the shadow exit
reason. A trade on one side only is counted, not matched: paper and the backtest took different
trades. A key held by more than one trade on a side is ambiguous and matches nothing, and a
proxy with no positive entry price is left out. Scores agree only when both sides have one.

Left out of both sides: the playbooks the run cannot have (`evidence.unbacktested`, EP until PR 70)
and the keys of the run's trades it closed at the end of its period, whose exit is the run's,
not the setup's.

Only shadow trades (a virtual position of a `shadow` signal) of the run's strategy version are
read; real trades and the veto trades of skipped confirm signals are not paper.
"""

from __future__ import annotations

import uuid
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..errors import HatError
from ..models import (
    HatBacktestRun,
    HatBot,
    HatPosition,
    HatSignal,
    HatTradeResult,
    HatWatchlistEntry,
)
from ..money import NotExactError, require_exact
from .evidence import COMPLETED, RunMetrics, family_key, unbacktested, universe_of
from .report import END_OF_PERIOD
from .runs import trades_path

_PLACES = Decimal("0.000001")
_BPS = Decimal(10000)
_COLUMNS = ("playbook", "instrument_id", "entry_date", "entry_price", "r_multiple", "exit_reason")
_SCORE = "score"  # a run before PR 65 wrote no score column

Key = tuple[str, str, date]  # (playbook, instrument id, entry date)


class GapError(HatError):
    """The run cannot be compared with the bot's shadow trades; `code` says why."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Leg:
    """One closed trade, shadow or proxy, as the comparison reads it."""

    playbook: str
    instrument_id: str
    entry_date: date
    entry_price: Decimal
    r_multiple: Decimal
    exit_reason: str
    score: Decimal | None

    @property
    def key(self) -> Key:
        return (self.playbook, self.instrument_id, self.entry_date)


@dataclass(frozen=True)
class TradeGap:
    """A shadow trade beside its proxy."""

    key: Key
    entry_gap_bps: Decimal
    r_gap: Decimal
    shadow_exit: str
    proxy_exit: str
    same_score: bool


@dataclass(frozen=True)
class GapReport:
    gaps: tuple[TradeGap, ...]
    shadow_only: tuple[Key, ...]
    proxy_only: tuple[Key, ...]

    def summary(self) -> dict[str, Any]:
        """The per-bot numbers the owner reads; a mean of no trades is None."""
        by_reason: dict[str, list[Decimal]] = defaultdict(list)
        for gap in self.gaps:
            by_reason[gap.shadow_exit].append(gap.r_gap)
        return {
            "matched": len(self.gaps),
            "shadow_only": len(self.shadow_only),
            "proxy_only": len(self.proxy_only),
            "mean_entry_gap_bps": _mean(g.entry_gap_bps for g in self.gaps),
            "mean_r_gap": _mean(g.r_gap for g in self.gaps),
            "same_exit": sum(g.shadow_exit == g.proxy_exit for g in self.gaps),
            "same_score": sum(g.same_score for g in self.gaps),
            "r_gap_by_exit": {
                reason: {"trades": len(rs), "mean_r_gap": _mean(rs)}
                for reason, rs in sorted(by_reason.items())
            },
        }


def _mean(values: Iterable[Decimal]) -> str | None:
    items = list(values)
    if not items:
        return None
    return str((sum(items, Decimal(0)) / len(items)).quantize(_PLACES))


def _same_score(a: Decimal | None, b: Decimal | None) -> bool:
    return a is not None and b is not None and a.quantize(_PLACES) == b.quantize(_PLACES)


def compare(shadow: Sequence[Leg], proxy: Sequence[Leg]) -> GapReport:
    """Each shadow trade beside the proxy with its key; the rest counted on its own side."""
    proxy = [leg for leg in proxy if leg.entry_price > 0]
    shadow_count = Counter(leg.key for leg in shadow)
    proxy_count = Counter(leg.key for leg in proxy)
    proxies = {leg.key: leg for leg in proxy if proxy_count[leg.key] == 1}
    gaps: list[TradeGap] = []
    shadow_only: list[Key] = []
    for leg in shadow:
        twin = proxies.get(leg.key) if shadow_count[leg.key] == 1 else None
        if twin is None:
            shadow_only.append(leg.key)
            continue
        gaps.append(
            TradeGap(
                key=leg.key,
                entry_gap_bps=(
                    (leg.entry_price - twin.entry_price) * _BPS / twin.entry_price
                ).quantize(_PLACES),
                r_gap=(leg.r_multiple - twin.r_multiple).quantize(_PLACES),
                shadow_exit=leg.exit_reason,
                proxy_exit=twin.exit_reason,
                same_score=_same_score(leg.score, twin.score),
            )
        )
    matched = {gap.key for gap in gaps}
    proxy_only = [leg.key for leg in proxy if leg.key not in matched]
    return GapReport(tuple(gaps), tuple(sorted(shadow_only)), tuple(sorted(proxy_only)))


def window(
    period_start: date, period_end: date, start: date | None, end: date | None
) -> tuple[date, date]:
    """The days compared: from `start` to `end` (the run's period where not given), never beyond
    the run's period."""
    return max(start or period_start, period_start), min(end or period_end, period_end)


def comparable(
    shadow: Sequence[Leg], proxy: Sequence[Leg], left_out: Collection[str]
) -> tuple[list[Leg], list[Leg]]:
    """Both sides without the playbooks in `left_out` (those the run cannot have) and the setups
    of the trades the run closed at the end of its period, whose exit is the run's, not the
    setup's."""
    ended = {leg.key for leg in proxy if leg.exit_reason == END_OF_PERIOD}

    def kept(leg: Leg) -> bool:
        return leg.playbook not in left_out and leg.key not in ended

    return [leg for leg in shadow if kept(leg)], [leg for leg in proxy if kept(leg)]


def shadow_legs(
    session: Session, bot_id: uuid.UUID, version_id: uuid.UUID, start: date, end: date
) -> list[Leg]:
    """The bot's closed shadow trades of strategy version `version_id` entered from `start` to
    `end`, with the score of their watchlist row (None unless exactly one score)."""
    rows = session.execute(
        select(
            HatSignal.playbook,
            HatSignal.instrument_id,
            HatSignal.trade_date,
            HatPosition.avg_entry,
            HatTradeResult.r_multiple,
            HatTradeResult.exit_reason,
        )
        .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
        .join(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(
            HatPosition.bot_id == bot_id,
            HatPosition.is_virtual.is_(True),
            HatSignal.mode == "shadow",
            HatSignal.strategy_version_id == version_id,
            HatSignal.trade_date >= start,
            HatSignal.trade_date <= end,
        )
    ).all()
    scores: dict[Key, set[Decimal]] = defaultdict(set)
    for playbook, instrument, day, score in session.execute(
        select(
            HatWatchlistEntry.playbook,
            HatWatchlistEntry.instrument_id,
            HatWatchlistEntry.trade_date,
            HatWatchlistEntry.score,
        ).where(
            HatWatchlistEntry.bot_id == bot_id,
            HatWatchlistEntry.strategy_version_id == version_id,
            HatWatchlistEntry.trade_date >= start,
            HatWatchlistEntry.trade_date <= end,
        )
    ):
        if score is not None:
            scores[(playbook, str(instrument), day)].add(Decimal(score))
    legs = []
    for playbook, instrument, day, entry, r, reason in rows:
        found = scores.get((playbook, str(instrument), day), set())
        legs.append(
            Leg(
                playbook=playbook,
                instrument_id=str(instrument),
                entry_date=day,
                entry_price=Decimal(entry),
                r_multiple=Decimal(r),
                exit_reason=reason,
                score=next(iter(found)) if len(found) == 1 else None,
            )
        )
    return legs


def columns_to_read(names: Collection[str]) -> list[str]:
    """The `trades.parquet` columns the comparison reads: the trade's, and its score when the
    file has one (a run before PR 65 has none, and its trades have no score). A file without a
    trade column is refused."""
    if not set(_COLUMNS) <= set(names):
        raise GapError("gap_no_trades_file")
    return [*_COLUMNS, _SCORE] if _SCORE in names else list(_COLUMNS)


def proxy_legs(root: Path, run_id: uuid.UUID, start: date, end: date) -> list[Leg]:
    """The run's trades (`trades.parquet`) entered from `start` to `end`."""
    path = trades_path(root, run_id)
    try:
        columns = columns_to_read(pq.read_schema(path).names)
        table = pq.read_table(
            path,
            columns=columns,
            filters=[("entry_date", ">=", start), ("entry_date", "<=", end)],
        )
    except (OSError, pa.ArrowException):
        raise GapError("gap_no_trades_file") from None
    try:
        return [
            Leg(
                playbook=row["playbook"],
                instrument_id=row["instrument_id"],
                entry_date=row["entry_date"],
                entry_price=require_exact(row["entry_price"], "entry_price"),
                r_multiple=require_exact(row["r_multiple"], "r_multiple"),
                exit_reason=row["exit_reason"],
                # the scorer's float (`TRADE_SCHEMA`); null without a watchlist row
                score=None
                if row.get("score") is None
                else require_exact(row["score"], "score", floats=True),
            )
            for row in table.to_pylist()
        ]
    except NotExactError:  # a trade the file cannot price is no trade to compare
        raise GapError("gap_no_trades_file") from None


def shadow_gap(
    session: Session,
    bot: HatBot,
    run: HatBacktestRun,
    *,
    root: Path,
    start: date | None = None,
    end: date | None = None,
) -> GapReport:
    """The gap between `bot`'s shadow trades and `run`'s trades entered in the window (the run's
    period by default, never beyond it), without the playbooks the run cannot have and the
    trades it closed at the end of its period. The run must be a completed run of the bot's
    strategy on its universe."""
    if run.status != COMPLETED:
        raise GapError("gap_run_not_completed")
    universe_id = universe_of(session, bot)
    if universe_id is None or run.family_key != family_key(bot.strategy_id, universe_id):
        raise GapError("gap_run_not_of_bot")
    first, last = window(run.period_start, run.period_end, start, end)
    return compare(
        *comparable(
            shadow_legs(session, bot.id, run.strategy_version_id, first, last),
            proxy_legs(root, run.id, first, last),
            unbacktested(RunMetrics.from_json(run.metrics)),
        )
    )


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.backtest.proxy_gap --bot <id> --run <id> [--from D] [--to D]

    Prints the gap between the bot's shadow trades and the run's trades as JSON: the per-bot
    summary and each matched trade's gaps."""
    import argparse  # noqa: PLC0415
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from app.database import db_session_scope  # noqa: PLC0415

    from ..settings import operator_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.backtest.proxy_gap")
    parser.add_argument("--bot", type=uuid.UUID, required=True)
    parser.add_argument("--run", type=uuid.UUID, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, default=None)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, default=None)
    args = parser.parse_args(argv)
    settings = operator_settings()
    with db_session_scope() as session:
        bot = session.get(HatBot, args.bot)
        run = session.get(HatBacktestRun, args.run)
        if bot is None or run is None:
            sys.stderr.write("proxy gap refused: gap_unknown_bot_or_run\n")
            return 2
        try:
            report = shadow_gap(
                session, bot, run, root=settings.data_root, start=args.start, end=args.end
            )
        except GapError as exc:
            sys.stderr.write(f"proxy gap refused: {exc.code}\n")
            return 2
    summary = {
        "bot_id": str(bot.id),
        "run_id": str(run.id),
        **report.summary(),
        "trades": [
            {
                "playbook": gap.key[0],
                "instrument_id": gap.key[1],
                "entry_date": gap.key[2].isoformat(),
                "entry_gap_bps": str(gap.entry_gap_bps),
                "r_gap": str(gap.r_gap),
                "shadow_exit": gap.shadow_exit,
                "proxy_exit": gap.proxy_exit,
                "same_score": gap.same_score,
            }
            for gap in report.gaps
        ],
    }
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

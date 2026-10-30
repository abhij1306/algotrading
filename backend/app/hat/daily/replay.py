"""The shadow-day replay: `python -m app.hat.daily.replay --date D [--bot <id> ...]`.

For a past session D it builds, for each active bot (or each `--bot`), what the live daily path
decides on D's close and what the backtest engine decides for the same night on the same store
and config, and lists every difference with its cause. Each side builds its inputs with its own
loader; both then run the same decision code:

- **Watchlist and signals**: `scan.scan_bot` on the live scan's inputs (`market.scan_inputs`,
  without the quality gate's exclusions, the bot's tradable members on D) and on the backtest's
  (`load_market` and a `MarketView` clocked at the next session's open, so it refuses any read
  past D: the as-of guard of `backtest/data.py`; the members of the run's universe rows as the
  scan reads them, `scan_night_members`). Both scan an empty book (no held names, no used
  pivots): the replay compares data and code, not books. A row's planned entry (its pivot and
  planned stop, the levels its signal triggers and stops at) is compared as a share of its own
  side's close on D, since live reads bars on the store's latest scale and the backtest on D's.
  The intraday trigger on 5-minute bars is the shared `TriggerMonitor` code and is not replayed.
- **Rotation targets**: on a rebalance night, `rotation_live.night_ranking` (live) and the
  backtest loop's `_rotation_plan` inputs (the trade date's members, `TrCloses`, the
  factor-pending names), each ranked by `rotation.ranking` and selected by `rotation.targets`
  from an empty book with every name buyable; the regime gate of each.
- **Exits**: each position the live book held at D's close (no veto trade, no rotation holding),
  evaluated at the close phase by `exits.evaluate` on its stored state (the same on both sides)
  and a view from each side's bars through D (`exits.view_from_bars`), its prices put on that
  side's scale of D (`close / raw_close` of D).

Only data available at D is read: the live side's frames are checked to hold nothing after D
(`backtest.data.looks_ahead`, refused `replay_lookahead`), the backtest's view refuses itself.
The database is read in a read-only transaction; nothing is written.

The comparison (`compare`) is pure: a difference's cause is the first input of the instrument
that differs between the sides (`Facts`: universe membership, the quality gate's exclusion, a
factor pending, no bars, different bars), else `same_inputs` (the code paths decide apart).
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from typing import Any

import polars as pl
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..backtest.data import MarketView, load_market, looks_ahead, scan_night_members
from ..backtest.rotation import TrCloses
from ..configs.records import Bot, StrategyVersion
from ..configs.unit import configs_of
from ..data.identity import series_as_of
from ..data.index_history import NIFTY_500_TRI
from ..data.isin import ordinary_equity
from ..data.quality import excluded_for
from ..data.store import DataStore
from ..engine import exits, rotation
from ..engine.portfolio.universe import members_on
from ..engine.positions import position_state
from ..engine.regime_gate import regime_allows_longs
from ..engine.rotation_live import night_ranking
from ..engine.scan import ScanInputs, scan_bot, universe_passers
from ..engine.strategy.config import ExitRule, StrategyConfig, parse_config
from ..engine.watchlist import bot_universe_members
from ..errors import HatError
from ..governance import Mode
from ..models import HatEvent, HatPosition, HatSignal
from ..money import exact, require_exact
from ..time import day_bounds
from .market import scan_inputs
from .nightly_deps import next_session

__all__ = [
    "Difference",
    "ExitCheck",
    "Facts",
    "ReplayError",
    "RotationNight",
    "Side",
    "Watch",
    "compare",
    "facts_of",
    "exit_check",
    "main",
    "no_later",
    "replay",
    "rotation_night",
]

LOOKAHEAD = "replay_lookahead"
RATIO = Decimal("0.0001")  # a level's share of the close, compared to 4 places
SCORE = Decimal("0.000001")  # a score, compared to 6 places
RETURN_SESSIONS = 20  # `Facts.ret`: the return over the last sessions through D
SAME_INPUTS = "same_inputs"


class ReplayError(HatError):
    """The replay could not be built (the code names why)."""


# --- what each side decided -------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    """One instrument's inputs on one side: whether it is a member of the bot's universe for
    the scan (`member`) and for a rotation (`rotation_member`), excluded by the quality gate,
    factor pending, how many bars it has through D and its return over the last
    `RETURN_SESSIONS` of them (scale-free, so the two sides' adjustments compare)."""

    member: bool = False
    rotation_member: bool = False
    excluded: bool = False
    pending: bool = False
    bars: int = 0
    ret: Decimal | None = None


@dataclass(frozen=True)
class Watch:
    """A watchlist row: its score, and its pivot and planned stop as shares of the close on D."""

    score: Decimal
    level: Decimal | None = None
    stop: Decimal | None = None


@dataclass(frozen=True)
class RotationNight:
    """A rotation's night: the regime gate, the ranking (best first, scores to `SCORE`) and the
    target list from an empty book."""

    gate_on: bool
    ranking: tuple[tuple[str, Decimal], ...]
    target: tuple[str, ...]


@dataclass(frozen=True)
class ExitCheck:
    """A position's close-phase evaluation: its sales (`kind:rule ids`) and its new stop as a
    share of the close on D (None: unchanged)."""

    actions: tuple[str, ...] = ()
    new_stop: Decimal | None = None


@dataclass
class Side:
    """What one side decided on D: by bot the regime gate, the watchlist rows by (bot, playbook,
    instrument), the rotation nights by bot, the exits by position (None: no bar on D, nothing
    evaluated) with each position's instrument, and each instrument's facts."""

    gates: dict[str, bool] = field(default_factory=dict)
    watchlist: dict[tuple[str, str, str], Watch] = field(default_factory=dict)
    rotation: dict[str, RotationNight] = field(default_factory=dict)
    exits: dict[str, ExitCheck | None] = field(default_factory=dict)
    held: dict[str, str] = field(default_factory=dict)  # position -> instrument
    facts: dict[str, Facts] = field(default_factory=dict)


@dataclass(frozen=True)
class Difference:
    kind: str  # gate, watchlist, signal, rotation_gate, rotation_rank, rotation_target, exit
    key: str
    field: str
    live: str | None
    backtest: str | None
    cause: str

    def to_json(self) -> dict[str, str | None]:
        return {
            "kind": self.kind,
            "key": self.key,
            "field": self.field,
            "live": self.live,
            "backtest": self.backtest,
            "cause": self.cause,
        }


# --- the comparison (pure) --------------------------------------------------------------------


def cause_of(instrument_id: str, live: Side, backtest: Side, *, rotation_: bool = False) -> str:
    """The first input of `instrument_id` that differs between the sides, else `same_inputs`."""
    a = live.facts.get(instrument_id, Facts())
    b = backtest.facts.get(instrument_id, Facts())
    member = (a.rotation_member, b.rotation_member) if rotation_ else (a.member, b.member)
    if member[0] != member[1]:
        return "membership"
    if a.excluded != b.excluded:
        return "quality_excluded"
    if a.pending != b.pending:
        return "factor_pending"
    if (a.bars == 0) != (b.bars == 0):
        return "no_bars"
    if a.bars != b.bars or a.ret != b.ret:
        return "bars_differ"
    return SAME_INPUTS


def _text(value: object) -> str | None:
    return None if value is None else str(value)


def _watch_differences(live: Side, backtest: Side) -> list[Difference]:
    out = []
    for key in sorted({*live.watchlist, *backtest.watchlist}):
        bot, playbook, instrument_id = key
        a, b = live.watchlist.get(key), backtest.watchlist.get(key)
        label = f"{bot}:{playbook}:{instrument_id}"
        cause = cause_of(instrument_id, live, backtest)
        if a is None or b is None:
            here = "listed" if a is not None else None
            there = "listed" if b is not None else None
            out.append(Difference("watchlist", label, "row", here, there, cause))
            continue
        if a.score != b.score:
            out.append(Difference("watchlist", label, "score", str(a.score), str(b.score), cause))
        for name in ("level", "stop"):
            x, y = getattr(a, name), getattr(b, name)
            if x != y:
                out.append(Difference("signal", label, name, _text(x), _text(y), cause))
    return out


def _rotation_differences(
    bot: str, a: RotationNight, b: RotationNight, live: Side, backtest: Side
) -> list[Difference]:
    out = []
    if a.gate_on != b.gate_on:
        out.append(
            Difference(
                "rotation_gate", bot, "gate_on", str(a.gate_on), str(b.gate_on), "regime_inputs"
            )
        )
    ranks_a = {name: (n, score) for n, (name, score) in enumerate(a.ranking, start=1)}
    ranks_b = {name: (n, score) for n, (name, score) in enumerate(b.ranking, start=1)}
    passers_differ = set(ranks_a) != set(ranks_b)
    for name in sorted({*ranks_a, *ranks_b}):
        x, y = ranks_a.get(name), ranks_b.get(name)
        cause = cause_of(name, live, backtest, rotation_=True)
        if x is None or y is None:
            out.append(
                Difference(
                    "rotation_rank",
                    f"{bot}:{name}",
                    "ranked",
                    _text(x and x[0]),
                    _text(y and y[0]),
                    cause,
                )
            )
            continue
        if x != y:
            # riskadj is a cross-sectional z-score: another name's inputs move this one's score
            why = "passers_differ" if passers_differ and cause == SAME_INPUTS else cause
            out.append(
                Difference(
                    "rotation_rank",
                    f"{bot}:{name}",
                    "rank:score",
                    f"{x[0]}:{x[1]}",
                    f"{y[0]}:{y[1]}",
                    why,
                )
            )
    for name in sorted(set(a.target) ^ set(b.target)):
        cause = cause_of(name, live, backtest, rotation_=True)
        if cause == SAME_INPUTS and ranks_a.get(name, (None,))[0] != ranks_b.get(name, (None,))[0]:
            cause = "ranking_differs"
        here = "target" if name in a.target else None
        there = "target" if name in b.target else None
        out.append(Difference("rotation_target", f"{bot}:{name}", "target", here, there, cause))
    return out


def compare(live: Side, backtest: Side) -> list[Difference]:
    """Every difference between what the two sides decided on D, each with its cause."""
    out = []
    for bot in sorted({*live.gates, *backtest.gates}):
        a, b = live.gates.get(bot), backtest.gates.get(bot)
        if a != b:
            out.append(Difference("gate", bot, "gate_on", _text(a), _text(b), "regime_inputs"))
    out.extend(_watch_differences(live, backtest))
    for bot in sorted({*live.rotation, *backtest.rotation}):
        a, b = live.rotation.get(bot), backtest.rotation.get(bot)
        if a is None or b is None:
            here, there = _text(a and "planned"), _text(b and "planned")
            out.append(Difference("rotation_rank", bot, "night", here, there, SAME_INPUTS))
            continue
        out.extend(_rotation_differences(bot, a, b, live, backtest))
    for position in sorted({*live.exits, *backtest.exits}):
        a, b = live.exits.get(position), backtest.exits.get(position)
        if a == b:
            continue
        instrument_id = live.held.get(position) or backtest.held.get(position) or ""
        cause = cause_of(instrument_id, live, backtest)
        if a is None or b is None:
            here, there = _text(a and "evaluated"), _text(b and "evaluated")
            out.append(Difference("exit", position, "view", here, there, cause))
            continue
        if a.actions != b.actions:
            out.append(
                Difference(
                    "exit", position, "actions", ",".join(a.actions), ",".join(b.actions), cause
                )
            )
        if a.new_stop != b.new_stop:
            out.append(
                Difference(
                    "exit", position, "new_stop", _text(a.new_stop), _text(b.new_stop), cause
                )
            )
    return out


# --- building a side from bars (pure over a frame) --------------------------------------------


def _decimal(value: object) -> Decimal | None:
    return exact(value, floats=True) if value is not None else None


def facts_of(frame: pl.DataFrame, day: date) -> dict[str, tuple[int, Decimal | None]]:
    """Each instrument's (bars through `day`, return over its last `RETURN_SESSIONS` bars to
    `RATIO`'s places of a share) in `frame` (`instrument_id`, `date`, `close`)."""
    out: dict[str, tuple[int, Decimal | None]] = {}
    if frame.height == 0:
        return out
    upto = frame.filter(pl.col("date") <= day).sort("instrument_id", "date")
    for key, part in upto.partition_by("instrument_id", as_dict=True).items():
        closes = part["close"].cast(pl.Float64).to_list()
        tail = closes[-(RETURN_SESSIONS + 1) :]
        first, last = _decimal(tail[0]), _decimal(tail[-1])
        ret = (last / first - 1).quantize(RATIO) if first and last and len(tail) > 1 else None
        out[str(key[0])] = (len(closes), ret)
    return out


def _ratio(value: object, close: Decimal | None) -> Decimal | None:
    number = _decimal(value)
    if number is None or close is None or close <= 0:
        return None
    return (number / close).quantize(RATIO)


def watch_rows(
    config: StrategyConfig, inputs: ScanInputs, bot: str
) -> tuple[bool, dict[tuple[str, str, str], Watch]]:
    """(the regime gate, the watchlist rows) `scan_bot` gives for `inputs` on an empty book,
    each row's levels as shares of its close on `inputs.as_of`."""
    result = scan_bot(config, replace(inputs, held=frozenset(), used_pivots=frozenset()))
    closes = _closes_on(inputs.bars, inputs.as_of)
    rows = {}
    for row in result.rows:
        close = closes.get(row.instrument_id)
        rows[(bot, row.playbook, row.instrument_id)] = Watch(
            score=require_exact(row.score, "score", floats=True).quantize(SCORE),
            level=_ratio(row.pivot, close),
            stop=_ratio(row.planned_stop, close),
        )
    return result.gate_on, rows


def _closes_on(frame: pl.DataFrame, day: date) -> dict[str, Decimal]:
    if frame.height == 0:
        return {}
    on = frame.filter(pl.col("date") == day)
    out = {}
    for name, close in zip(
        on["instrument_id"].cast(pl.String).to_list(), on["close"].to_list(), strict=True
    ):
        value = _decimal(close)
        if value is not None:
            out[name] = value
    return out


def rotation_night(
    config: StrategyConfig, ranks: Sequence[tuple[str, float]], gate_on: bool
) -> RotationNight:
    """A ranking's night: its scores to `SCORE` and the targets from an empty book."""
    rules = config.rotation
    if rules is None:
        raise ReplayError("replay_not_a_rotation")
    target, _ = rotation.targets(ranks, set(), rules, gate_on=gate_on, can_buy=lambda _n: True)
    ranking = tuple(
        (name, require_exact(score, "score", floats=True).quantize(SCORE)) for name, score in ranks
    )
    return RotationNight(gate_on, ranking, target)


def exit_check(
    frame: pl.DataFrame,
    instrument_id: str,
    entry_day: date,
    day: date,
    rules: Sequence[ExitRule],
    state: exits.PositionState,
) -> ExitCheck | None:
    """The close-phase evaluation of a position on `day` from the instrument's bars in `frame`
    (`date`, `close`, `low`, `high`, optional `raw_close`), its prices put on the frame's scale
    of `day`; None without a bar on `day`."""
    rows = frame.filter(
        (pl.col("instrument_id").cast(pl.String) == instrument_id) & (pl.col("date") <= day)
    ).sort("date")
    if rows.height == 0 or rows["date"][-1] != day:
        return None
    close = _decimal(rows["close"][-1])
    if close is None or close <= 0:
        return None
    raw = _decimal(rows["raw_close"][-1]) if "raw_close" in rows.columns else None
    scale = close / raw if raw is not None and raw > 0 else Decimal(1)
    before = rows.filter(pl.col("date") < day).tail(exits.history_sessions(rules))
    held = rows.filter((pl.col("date") >= entry_day) & (pl.col("date") < day))

    def prices(frame_: pl.DataFrame, column: str) -> list[Decimal]:
        return [v for v in (_decimal(x) for x in frame_[column].to_list()) if v is not None]

    view = exits.view_from_bars(
        close,
        rules,
        rows.filter(pl.col("date") > entry_day).height,
        prices(before, "close"),
        prices(before, "low"),
        prices(before, "high"),
        prices(held, "high") if exits.reads_peak(rules) else None,
    )
    scaled = replace(
        state,
        avg_entry=state.avg_entry * scale,
        initial_stop=state.initial_stop * scale,
        current_stop=state.current_stop * scale,
    )
    found = exits.evaluate(rules, scaled, view, "close")
    actions = tuple(f"{a.kind}:{'+'.join(a.rule_ids)}" for a in found.actions)
    new_stop = None if found.new_stop is None else (found.new_stop / close).quantize(RATIO)
    return ExitCheck(actions, new_stop)


def no_later(frame: pl.DataFrame, day: date, session_: date) -> pl.DataFrame:
    """`frame` unchanged when it holds nothing dated after `day`; else `replay_lookahead`."""
    if frame.height and looks_ahead(max(frame["date"].to_list()), session_, day, inclusive=True):
        raise ReplayError(LOOKAHEAD)
    return frame


# --- the two sides (the database and the store, read only) ------------------------------------


@dataclass(frozen=True)
class _Bot:
    id: str
    bot: Bot
    config: StrategyConfig


@dataclass(frozen=True)
class _Held:
    position_id: str
    bot_id: str
    instrument_id: str
    entry_day: date
    rules: tuple[ExitRule, ...]
    state: exits.PositionState


def _bots(session: Session, wanted: Iterable[uuid.UUID]) -> list[_Bot]:
    asked = set(wanted)
    unit = configs_of(session)
    out = []
    for bot in unit.all(
        Bot,
        lambda b: (
            b.live_version_id is not None and (b.id in asked if asked else b.status == "active")
        ),
    ):
        version = unit.get(StrategyVersion, bot.live_version_id)
        if version is not None:
            out.append(_Bot(str(bot.id), bot, parse_config(version.config)))
    return sorted(out, key=lambda b: b.id)


def _held(session: Session, bot_ids: Sequence[str], day: date) -> list[_Held]:
    """The positions the live book held at `day`'s close: open by then and not closed before
    it ended, of the bots asked, neither a veto trade nor a rotation holding."""
    end = day_bounds(day)[1]
    rows = session.execute(
        select(HatPosition, HatSignal)
        .join(HatSignal, HatSignal.id == HatPosition.signal_id)
        .where(
            HatPosition.bot_id.in_([uuid.UUID(b) for b in bot_ids]),
            HatPosition.opened_at < end,
            (HatPosition.closed_at.is_(None)) | (HatPosition.closed_at >= end),
            (HatPosition.is_virtual.is_(False)) | (HatSignal.mode == Mode.SHADOW),
            HatSignal.playbook != rotation.PLAYBOOK,
        )
    ).all()
    out = []
    for position, signal in rows:
        rules, state = position_state(session, position)
        out.append(
            _Held(
                str(position.id),
                str(position.bot_id),
                str(position.instrument_id),
                signal.trade_date,
                tuple(rules),
                state,
            )
        )
    return out


def _add_exits(side: Side, held: Sequence[_Held], frame: pl.DataFrame, day: date) -> None:
    for h in held:
        side.held[h.position_id] = h.instrument_id
        side.exits[h.position_id] = exit_check(
            frame, h.instrument_id, h.entry_day, day, h.rules, h.state
        )


def _set_facts(
    side: Side,
    frame: pl.DataFrame,
    day: date,
    members: set[str],
    rotation_members: set[str],
    excluded: set[str],
    pending: frozenset[str],
) -> None:
    counted = facts_of(frame, day)
    for name in {*counted, *members, *rotation_members, *excluded, *pending}:
        bars, ret = counted.get(name, (0, None))
        side.facts[name] = Facts(
            name in members, name in rotation_members, name in excluded, name in pending, bars, ret
        )


def live_side(
    session: Session,
    store: DataStore,
    bots: Sequence[_Bot],
    held: Sequence[_Held],
    day: date,
    trade_date: date,
) -> Side:
    """What the live path decides on `day`'s close (module docstring)."""
    side = Side()
    excluded = excluded_for(session, day)
    inputs: dict[str, ScanInputs] = {}
    members: set[str] = set()
    rotation_members: set[str] = set()
    frame = pl.DataFrame()
    for b in bots:
        index = b.config.regime.index
        if index not in inputs:
            found = scan_inputs(store, session, day, index)
            frame = no_later(found.bars, day, trade_date)  # exits read every bar
            if excluded and found.bars.height:
                found = replace(
                    found,
                    bars=found.bars.filter(
                        ~pl.col("instrument_id").cast(pl.String).is_in(sorted(excluded))
                    ),
                )
            inputs[index] = found
        mine = {str(m) for m in bot_universe_members(session, b.bot, day)}
        if b.config.rotation is not None:
            rotation_members |= {str(m) for m in bot_universe_members(session, b.bot, trade_date)}
            if rotation.is_rebalance(day, trade_date, b.config.rotation.rebalance):
                night = night_ranking(session, b.bot, b.config, inputs[index], trade_date)
                if night is not None:
                    side.rotation[b.id] = rotation_night(b.config, night[1], night[2])
            continue
        members |= mine
        gate_on, rows = watch_rows(b.config, replace(inputs[index], members=mine), b.id)
        side.gates[b.id] = gate_on
        side.watchlist.update(rows)
    _add_exits(side, held, frame, day)
    _set_facts(side, frame, day, members, rotation_members, excluded, frozenset())
    return side


def backtest_side(
    session: Session,
    store: DataStore,
    bots: Sequence[_Bot],
    held: Sequence[_Held],
    day: date,
    trade_date: date,
) -> Side:
    """What the backtest engine decides on `day`'s close for `trade_date` (module docstring)."""
    side = Side()
    members: set[str] = set()
    rotation_members: set[str] = set()
    pending: frozenset[str] = frozenset()
    frame = pl.DataFrame()
    for b in bots:
        if b.bot.universe_version_id is None:
            continue
        index = b.config.regime.index
        data, _ = load_market(
            store,
            session,
            universe_version_id=b.bot.universe_version_id,
            index=index,
            start=trade_date,
            end=trade_date,
        )
        view = MarketView(data, index)
        view.open(trade_date)  # the clock: nothing after `day`'s close is read
        bars = view.window(day)
        pending = view.pending_on(day)
        frame = (
            bars
            if frame.height == 0
            else pl.concat([frame, bars], how="diagonal_relaxed").unique(["instrument_id", "date"])
        )
        benchmark = [(d, float(c)) for d, c in data.index_closes.get(NIFTY_500_TRI, ()) if d <= day]
        inputs = ScanInputs(
            as_of=day,
            members=(),
            bars=bars,
            regime_closes=view.closes_through(day),
            breadth=view.breadth_through(day),
            features=view.features_on(day),
            series=series_as_of(data.series, day),
            benchmark=benchmark,
        )
        if b.config.rotation is not None:
            rules = b.config.rotation
            mine = {str(m) for m in members_on(data.members, trade_date)}
            rotation_members |= mine
            passers = universe_passers(
                b.config, replace(inputs, members=mine, features=None, benchmark=())
            )
            view.as_of(day)
            closes = TrCloses(data.bars, view.days).window(
                passers, view.index[day], rotation.window_rows(rules)
            )
            ranks = rotation.ranking(passers, closes, rules, pending)
            gate_on = regime_allows_longs(b.config.regime, inputs.regime_closes, inputs.breadth)
            if rotation.is_rebalance(day, trade_date, rules.rebalance):
                side.rotation[b.id] = rotation_night(b.config, ranks, gate_on)
            continue
        night = members_on(scan_night_members(data.members, data.days), day)
        mine = {str(m) for m in night if ordinary_equity(data.isins.get(str(m), ""))}
        members |= mine
        gate_on, rows = watch_rows(b.config, replace(inputs, members=mine), b.id)
        side.gates[b.id] = gate_on
        side.watchlist.update(rows)
    _add_exits(side, held, frame, day)
    _set_facts(side, frame, day, members, rotation_members, set(), pending)
    return side


def replay(
    session: Session, store: DataStore, day: date, wanted: Iterable[uuid.UUID] = ()
) -> dict[str, Any]:
    """The replay of session `day` (module docstring) as a JSON document."""
    trade_date = next_session(session, day)
    if trade_date is None:
        raise ReplayError("replay_no_next_session")
    bots = _bots(session, wanted)
    if not bots:
        raise ReplayError("replay_no_bot")
    held = _held(session, [b.id for b in bots], day)
    live = live_side(session, store, bots, held, day, trade_date)
    backtest = backtest_side(session, store, bots, held, day, trade_date)
    found = compare(live, backtest)
    counts: dict[str, int] = {}
    for d in found:
        counts[f"{d.kind}:{d.cause}"] = counts.get(f"{d.kind}:{d.cause}", 0) + 1
    return {
        "date": day.isoformat(),
        "trade_date": trade_date.isoformat(),
        "bots": [b.id for b in bots],
        "compared": {
            "watchlist": [len(live.watchlist), len(backtest.watchlist)],
            "rotation_nights": [len(live.rotation), len(backtest.rotation)],
            "positions": len(held),
        },
        "counts": dict(sorted(counts.items())),
        "differences": [d.to_json() for d in found],
    }


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    from app.database import db_session_scope

    from ..settings import operator_settings

    parser = argparse.ArgumentParser(prog="python -m app.hat.daily.replay")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--bot", action="append", default=[], type=uuid.UUID)
    args = parser.parse_args(argv)
    settings = operator_settings()
    with db_session_scope() as session:
        # nothing is written: hat_live is read-only already, hat_state is made so
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
        try:
            report = replay(session, store, args.date, args.bot)
        except HatError as refused:
            sys.stdout.write(json.dumps({"refused": str(refused)}) + "\n")
            return 2
    sys.stdout.write(json.dumps(report, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

"""The backtest day loop (09 §1-§2, HAT-BT-010..024, HAT-DEC-T12): one engine for backtest and live.

The loop has no strategy, risk or exit logic of its own. Each simulated trade date d runs the
code live trading runs, in the same order:

1. the nightly scan of d's previous session, published as d's watchlist
   (`watchlist.scan_and_publish`, which calls `scan.scan_bot`);
2. open positions: the nightly exit rules at d's open, then the live rules along the day's path
   (`trades.evaluate_position`, which calls `exits.evaluate`);
3. entries: each watchlist row whose level the day breaks becomes a trigger, the stop comes from
   `stops.plan_stop`, and `trades.open_entry` decides it with `evaluate_signal` (caps, max chase,
   sizing) and fills it with the shadow engine's virtual fills;
4. the 15:25 close rules at the official close, then the session-end expiry of the watchlist
   (`trades.expire_watchlist`).

Those functions work on a database session, so the loop keeps a private ledger: an in-memory
SQLite database holding the run's one bot, its pinned strategy version, its universe (point in
time) and the virtual signals, orders, fills, positions and trade results the engine writes. The
ledger is the shadow engine's own schema; it never touches the HAT database and nothing in it
reaches a broker.

What the backtest adds is only what live trading gets from the market: prices. Daily bars cannot
show where in a day a level broke, so a day is read as the path open -> low -> high -> close (the
adverse move first, `app.hat.backtest.fills`): an entry breaks its level on the way up, a stop is
hit on the way down, a profit rule fires at the first tick price on the way up at which
`exits.evaluate` acts, and the close rules see the official close. Fills go through the cost
model (`app.hat.backtest.costs`): slippage against the trade, then statutory charges.

Fill conventions (PR 62, HAT-BT-020..022, O5): pessimistic by default. An entry needs the high to
clear the level by one tick and fills at that price (`penetration_1_tick`; `touch` is a variant a
run may choose), a stop gapped through fills at min(open, stop), and the slippage defaults to
20 bps (`DEFAULT_SLIPPAGE_BPS`). A run flagged for promotion (`RunSpec.for_promotion`, the only
kind `publish_version` accepts and the sleeve's evidence reads) is refused at a slippage of 0,
under `touch` or under a slot order other than `time` (`check_conventions`). Every run's
metrics record the fill path, the entry fill rule, the gap-through rule, the trigger proxy, the
slippage, the cost model and the slot order, and `config_digest` hashes them, so two runs
under different conventions never read as comparable.

The exit search along a day's way up (`first_trigger`) assumes a monotone rule stack: above the
stop, every live rule that acts at a price acts at any higher one. The built-in rules are
thresholds (targets, R levels, breakeven moves), and the close-phase rules
(`partial_after_days`, the breakeven moves) see one price; a custom rule that acts only inside a
band of prices would need a linear scan of the tick grid instead of the bisection.

The entry day (PR 60, HAT-BT-020): an entry breaks its level after the day's pre-break low, read
from the session's 5-minute bars when the run has them and otherwise estimated as
max(day low, min(open, level x (1 - adr20 / 100))) with `adr20` as of the session before; a low-of-
day stop is planned from it, as live plans it from the bars up to the trigger. Whatever of the
day's low lies below the pre-break low came after the break, and is taken before the high (the
adverse order): a stop it reaches fills that day, and it seeds the trade's MAE. A gap-over entry
with a low-of-day stop needs the opening range's low, so without 5-minute bars it is skipped
(`gap_over_no_intraday`). The rule is recorded in every run's metrics (`entry_day_low`).

Entry filters (PR 61, HAT-BT-025): at each trigger the playbook's entry filters run as live runs
them (`trigger.evaluate_entry_filters`): `adtr_filter` on the planned stop and ADTR(20) as of the
session before, and `max_chase` with the upper-circuit state, inferred from a bar pinned at an NSE
band all day (`upper_circuit_locked`). A failing filter is a Rejected signal, as live writes it.

Slot order (PR 63, HAT-BT-026): live takes a day's triggers in the order they fire, so on a capped
day the first breaks get the open slots. The loop takes the day's watchlist rows in their
estimated trigger order (`slot_order` `time`, the default, `app.hat.backtest.slots`): gap overs
first, then (level - open) / open ascending, ties by score. `score` (the PR 12 order) and
`random:K` are variants; the rule is part of the run's conventions and `config_digest`.

Path pessimism (PR 64, HAT-BT-021): a stop the way up raised was placed after the
low on the assumed path, though the low may have come after the high. A day whose low reaches
the raised stop while its close stays above it is ambiguous: every run counts these days
(`ambiguous_days`), and under `ambiguous` `pessimistic` the raised stop fills at its price that
day; the default, `optimistic`, holds it (the run's `ambiguous` convention says which). A session
pinned at its lower band all day (open = high = low = close within a tick of an NSE band's lower
price, `lower_circuit_locked`) sells nothing: its stop and exits wait for the next session that
is not pinned, which fills the stop at min(open, stop) (`lower_circuit_days` counts them). A
position whose instrument has no bar (suspended, not delisted) is carried at its last close for
`suspension_sessions` sessions, then marked 5% lower per further session, in the equity curve
and at the period end. All three rules are in the run's conventions.

The per-instrument cooldown (PR 58, HAT-RISK-036) is the risk policy's `cooldown_sessions`, which
`evaluate_signal` reads through `trades.open_entry` from the ledger's hard-stop closes, as live
does; a run may replace it (`with_cooldown`, `--cooldown-sessions`) and records the value used.

Known gaps, recorded by fixed code in every run's metrics (`KNOWN_GAPS`, 09 §7 HAT-BT-070) and
measured in shadow (PLAN Q4): among them, the trigger is the break of the level alone
(`volume_pace` and the opening range need intraday bars, HAT-DEC-I4), the episodic-pivot playbook
is not backtested, close-based exits fill at the official close, kill switches are not simulated
(the exposure ladder is), and a sleeve's allocation uses regime and health multipliers of 1.

The command line runs a sleeve's strategy version over the Parquet store and records the run
(`app.hat.backtest.runs`): `python -m app.hat.engine.backtest --bot <id> --from YYYY-MM-DD
--to YYYY-MM-DD`.
"""

from __future__ import annotations

import io
import math
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from ...base import Base
from ..backtest import metrics as bt_metrics
from ..backtest.costs import DEFAULT_SLIPPAGE_BPS, BacktestFills
from ..backtest.dsr import ReturnStats, return_stats
from ..backtest.fills import (
    AMBIGUOUS_OPTIMISTIC,
    AMBIGUOUS_PESSIMISTIC,
    AMBIGUOUS_RULES,
    ENTRY_FILL_RULES,
    FILL_PATH,
    GAP_THROUGH_RULE,
    PENETRATION,
    SUSPENSION_BAND_PCT,
    SUSPENSION_SESSIONS,
    TOUCH,
    TRIGGER_PROXY,
    entry_fill,
    raised_stop_dip,
    stop_fill,
    suspended_mark,
)
from ..backtest.slots import SLOT_ORDER_TIME, is_slot_order, order_slots, trigger_rank
from ..confighash import config_hash
from ..data.sessions import IST
from ..data.ticks import tick_for_date
from ..errors import HatError
from ..models import (
    HatBot,
    HatFill,
    HatIndustry,
    HatMarket,
    HatOrder,
    HatPosition,
    HatSignal,
    HatStrategy,
    HatStrategyVersion,
    HatTradeResult,
    HatUniverse,
    HatUniverseMember,
    HatUniverseVersion,
    HatWatchlistEntry,
)
from ..risk.decision import SignalContext
from ..risk.exposure import LadderState, apply_outcome, is_win
from ..risk.kill import KillState
from ..risk.policy import PolicyError, PolicyVersion, parse_policy
from ..risk.sessions import SessionCalendar, SessionDay
from ..time import as_utc, trade_date_of
from . import exits, lifecycle, trades, trigger, watchlist
from .portfolio.universe import Interval
from .regime_gate import regime_allows_longs
from .scan import EP_PLAYBOOK, ScanInputs
from .stops import (
    SKIP_BAD_INPUT,
    SKIP_TOO_WIDE,
    StopInputs,
    check_width,
    mean_true_range,
    plan_stop,
)
from .strategy.config import (
    Playbook,
    StopAtrMultiple,
    StopBelowFinalContractionLow,
    StopLowOfDay,
    StopSwingLow,
    StrategyConfig,
)

__all__ = [
    "INITIAL_EQUITY",
    "KNOWN_GAPS",
    "LOOKBACK_SESSIONS",
    "LOWER_CIRCUIT_RULE",
    "TOUCH_GAP",
    "TRADE_SCHEMA",
    "BacktestError",
    "BacktestResult",
    "FillRow",
    "MarketData",
    "RunSpec",
    "SignalRow",
    "TradeRow",
    "WatchRow",
    "check_conventions",
    "compute_metrics",
    "config_digest",
    "conventions",
    "first_trigger",
    "known_gaps",
    "pre_break_low",
    "lower_circuit_locked",
    "upper_circuit_locked",
    "run_backtest",
    "trades_parquet",
]

INITIAL_EQUITY = Decimal(1_000_000)
# The scan reads at most 652 sessions back (a 400-session SMA compared 252 sessions earlier);
# the window it is given holds more, so its watchlist equals a scan of the full history.
LOOKBACK_SESSIONS = 700
# What the daily-bar loop does not model, by fixed code (09 §7, HAT-BT-070); measured in shadow.
KNOWN_GAPS = (
    "trigger_is_level_break_only",  # volume_pace and the opening range need intraday bars
    "episodic_pivot_not_backtested",
    "close_exits_at_official_close",
    "kill_switches_not_simulated",
    "gap_over_fills_at_open",  # live fills at the first tick after the open
    "upper_circuit_inferred_from_bar",  # O=H=C at an NSE band (lower: O=H=L=C, PR 64)
    "multipliers_fixed_at_one",  # regime and health multipliers are 1
    "entry_day_low_estimated",  # without bars_5m the pre-break low is the adr20 estimate
)
# Reported, besides `KNOWN_GAPS`, by a run under the `touch` entry rule (PR 62).
TOUCH_GAP = "entry_fills_on_touch"  # a high at the level fills; live needs a trade above it
# A stop on a session pinned at its lower band waits for the next open with a bar not pinned.
LOWER_CIRCUIT_RULE = "carry_to_next_unlocked_open"
MAX_SUSPENSION_SESSIONS = 250  # about a year of sessions
# The NSE price bands an upper circuit is inferred at when the run has no band table (PR 61).
NSE_BANDS = (Decimal(2), Decimal(5), Decimal(10), Decimal(20))
END_OF_PERIOD = "end_of_period"
DELISTED = "delisted"
SKIP_NO_LEVEL = "no_trigger_level"
SKIP_GAP_NO_INTRADAY = "gap_over_no_intraday"
# How the loop reads the entry day's low before and after the break (HAT-BT-020, PR 60).
ENTRY_DAY_LOW = "bars_5m_else_max_low_min_open_level_less_adr20"
_SANDBOX_USER = uuid.UUID(int=1)
_PRICE_PLACES = Decimal("0.0001")
_ZERO = Decimal(0)
_FINEST_TICK = Decimal("0.01")  # the NSE tick below ₹250 (`data.ticks`), the finest slab
# The instants of a simulated day (IST): a later step of the day has a later instant.
_AT_NIGHT = time(16, 0)
_AT_OPEN = time(9, 15)
_AT_LOW = time(9, 20)
_AT_ENTRY = time(9, 30)
_AT_DIP = time(9, 45)  # the entry day's low after the entry
_AT_HIGH = time(10, 0)
_AT_FALL = time(15, 20)
_AT_CLOSE = time(15, 25)
_AT_END = time(15, 29)


class BacktestError(HatError):
    """A run was refused before it started; `code` is a fixed word and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- inputs -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class MarketData:
    """Everything a run reads, point in time.

    `days`: every session of the store, sorted. `bars`: adjusted daily bars (`instrument_id`
    text, `date`, `open`, `high`, `low`, `close`, `volume`, `traded_value`, as doubles).
    `features`: the daily features (`date`, `instrument_id`, the 02 §7 columns). `index_closes`:
    per index name, (date, close) oldest first. `members`: the universe's membership intervals,
    delisted instruments included. `industries`: each instrument's industry, either one name
    or its classifications as (effective_from, industry), read point in time. `last_traded`: the
    last session of each instrument delisted by the period's end.
    """

    days: tuple[date, ...]
    bars: pl.DataFrame
    features: pl.DataFrame
    index_closes: Mapping[str, Sequence[tuple[date, Decimal]]]
    members: tuple[Interval, ...]
    industries: Mapping[str, str | Sequence[tuple[date, str]]] = field(default_factory=dict)
    series: Mapping[str, str] = field(default_factory=dict)
    last_traded: Mapping[str, date] = field(default_factory=dict)
    # 5-minute bars of a session, keyed (instrument_id, date): (high, low) per bar in time order,
    # the 09:15 bar first, on the daily bars' (adjusted) scale. A session without them uses the
    # daily estimate (PR 60).
    intraday: Mapping[tuple[str, date], Sequence[tuple[Decimal, Decimal]]] = field(
        default_factory=dict
    )


@dataclass(frozen=True)
class RunSpec:
    """One run: a strategy version, one risk-policy version, a period and a fill model."""

    config: StrategyConfig
    strategy_version_id: uuid.UUID
    policy: PolicyVersion
    start: date
    end: date
    fills: BacktestFills
    initial_equity: Decimal = INITIAL_EQUITY
    entry_fill_rule: str = PENETRATION  # HAT-BT-020: `touch` is the variant
    for_promotion: bool = False  # the run may evidence a version (`publish_version`)
    slot_order: str = SLOT_ORDER_TIME  # HAT-BT-026: `score` and `random:K` are variants
    ambiguous: str = AMBIGUOUS_OPTIMISTIC  # HAT-BT-021: `pessimistic` fills a raised stop's dip
    suspension_sessions: int = SUSPENSION_SESSIONS  # bar-less sessions before the markdown


# --- outputs ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class WatchRow:
    trade_date: date
    playbook: str
    instrument_id: str
    score: Decimal
    pivot: Decimal | None
    planned_stop: Decimal | None


@dataclass(frozen=True)
class SignalRow:
    trade_date: date
    playbook: str
    instrument_id: str
    state: str  # the state right after the decision: Open or Rejected
    reason: str | None
    trigger_price: Decimal
    stop_price: Decimal
    qty: int
    strategy_version_id: uuid.UUID
    risk_policy_version_id: uuid.UUID


@dataclass(frozen=True)
class FillRow:
    day: date
    instrument_id: str
    purpose: str
    side: str
    qty: int
    price: Decimal
    fees: Decimal


@dataclass(frozen=True)
class TradeRow:
    entry_date: date
    exit_date: date
    playbook: str
    instrument_id: str
    qty: int
    entry_price: Decimal
    initial_stop: Decimal
    exit_reason: str
    realized_pnl: Decimal
    fees: Decimal
    r_multiple: Decimal
    mfe_r: Decimal
    mae_r: Decimal
    holding_days: int
    strategy_version_id: uuid.UUID
    risk_policy_version_id: uuid.UUID


@dataclass(frozen=True)
class BacktestResult:
    trades: tuple[TradeRow, ...]
    equity: tuple[tuple[date, Decimal], ...]  # mark to market at each session's close
    watchlists: tuple[WatchRow, ...]
    signals: tuple[SignalRow, ...]
    fills: tuple[FillRow, ...]
    skips: Mapping[str, int]  # triggers that never became a signal, by reason
    metrics: dict[str, Any]


# --- small helpers ----------------------------------------------------------------------------


def _price(value: object) -> Decimal | None:
    """A stored price as an exact Decimal of the store's 4 places; None when absent."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        number = Decimal(repr(value))
    else:
        return None
    return number.quantize(_PRICE_PLACES) if number.is_finite() else None


def _at(day: date, moment: time, offset: int = 0) -> datetime:
    """The aware UTC instant of `moment` IST on `day`, plus `offset` seconds."""
    local = datetime.combine(day, moment, tzinfo=IST)
    return as_utc(local.replace(second=offset % 60, minute=moment.minute + offset // 60))


@dataclass(frozen=True)
class _Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class _EntryPlan:
    """A trigger's entry: price, stop, the day's low after the entry (None: none below the
    break's), the upper-circuit state and the first failing entry filter (None: all passed)."""

    price: Decimal
    stop: Decimal
    dip: Decimal | None
    locked: bool | None
    skip: str | None
    filters: tuple[dict[str, object], ...] = ()


@dataclass
class _Held:
    """What the loop tracks of an open position beyond the ledger: its instrument, entry day and
    the extremes the price made since entry (for the result's MFE and MAE), and the stop's last
    raise on a way up (for an ambiguous day)."""

    instrument_id: str
    entry_day: date
    high: Decimal
    low: Decimal
    # The day the way up last raised the stop, and the stop before it (PR 64).
    raised: tuple[date, Decimal] | None = None


# --- the ledger -------------------------------------------------------------------------------


_ALWAYS = date(1900, 1, 1)


def _classifications(data: MarketData, instrument_id: str) -> list[tuple[date, str]]:
    """The instrument's industry classifications, oldest first."""
    found = data.industries.get(instrument_id, "unknown")
    if isinstance(found, str):
        return [(_ALWAYS, found)]
    return sorted(found) or [(_ALWAYS, "unknown")]


def industry_on(data: MarketData, instrument_id: str, day: date) -> str:
    """The industry in force on `day`: the newest classification effective on or before it."""
    rows = [name for start, name in _classifications(data, instrument_id) if start <= day]
    return rows[-1] if rows else "unknown"


def _ledger(spec: RunSpec, data: MarketData) -> tuple[Session, uuid.UUID]:
    """A private in-memory database seeded with the run's bot, version and universe."""
    engine = create_engine(
        "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session = Session(engine)
    market = HatMarket(
        code="NSE",
        timezone="Asia/Kolkata",
        currency="INR",
        session_open=time(9, 15),
        session_close=time(15, 30),
    )
    session.add(market)
    session.flush()
    strategy = HatStrategy(
        user_id=_SANDBOX_USER, market_id=market.id, name=spec.config.bot, persona="trader"
    )
    universe = HatUniverse(user_id=_SANDBOX_USER, market_id=market.id, name="run", kind="manual")
    session.add_all([strategy, universe])
    session.flush()
    version = HatStrategyVersion(
        id=spec.strategy_version_id,
        strategy_id=strategy.id,
        version=1,
        status="shadow",
        config=spec.config.to_json(),
        config_hash=spec.config.hash(),
    )
    universe_version = HatUniverseVersion(
        universe_id=universe.id, version=1, status="live", definition={}, config_hash="run"
    )
    session.add_all([version, universe_version])
    session.flush()
    for interval in data.members:
        session.add(
            HatUniverseMember(
                universe_id=universe.id,
                instrument_id=interval.instrument_id,
                effective_from=interval.effective_from,
                effective_to=interval.effective_to,
                reason="constituent",
            )
        )
    for instrument_id in sorted({str(i.instrument_id) for i in data.members}):
        # the live lookup reads the classification in force on the trade date
        for effective_from, industry in _classifications(data, instrument_id):
            session.add(
                HatIndustry(
                    instrument_id=uuid.UUID(instrument_id),
                    sector="",
                    industry=industry,
                    effective_from=effective_from,
                )
            )
    bot = HatBot(
        id=uuid.uuid4(),
        user_id=_SANDBOX_USER,
        strategy_id=strategy.id,
        live_version_id=version.id,
        broker_account_id=uuid.UUID(int=2),
        execution_mode="shadow",
        status="active",
        universe_version_id=universe_version.id,
        lifecycle_state="research",
        risk_cluster="run",
    )
    session.add(bot)
    session.flush()
    return session, bot.id


# --- the loop ---------------------------------------------------------------------------------


class _Run:
    """One run of the day loop over `MarketData` (see the module docstring)."""

    def __init__(self, spec: RunSpec, data: MarketData) -> None:
        self.spec = spec
        self.data = data
        self.config = spec.config
        self.policy = spec.policy.policy
        # Each playbook's configured max chase, as the trigger reads it (HAT-CRIT-122).
        self.chase = {
            b.name: trigger.last_configured_chase([self.config], b.name)
            for b in self.config.playbooks
        }
        self.days = tuple(sorted(set(data.days)))
        self.period = tuple(d for d in self.days if spec.start <= d <= spec.end)
        if not self.period:
            raise BacktestError("backtest_empty_period")
        self.index = {d: n for n, d in enumerate(self.days)}
        self.session, self.bot_id = _ledger(spec, data)
        self.calendar = SessionCalendar(
            {
                d: SessionDay(
                    True, _at(d, _AT_OPEN), as_utc(datetime.combine(d, time(15, 30), IST))
                )
                for d in self.period
            }
        )
        self.bars = data.bars.sort("instrument_id", "date")
        self.bar = self._bars_by_day()
        self.history = self._history_by_instrument()
        self.by_day = self._bars_by_session()
        self.feature_rows: dict[date, dict[str, dict[str, Any]]] = {}
        self.features = {
            key[0]: part for key, part in data.features.partition_by("date", as_dict=True).items()
        }
        self.closes = list(data.index_closes.get(self.config.regime.index, ()))
        self.ladder = LadderState()
        self.held: dict[uuid.UUID, _Held] = {}
        self.seen_results: set[uuid.UUID] = set()
        self.last_close: dict[str, Decimal] = {}
        self.last_bar: dict[str, date] = {}  # each instrument's last session with a bar
        self.ambiguous_days: set[date] = set()
        self.lower_circuit_days: set[date] = set()
        self.equity: list[tuple[date, Decimal]] = []
        self.skips: dict[str, int] = {}
        self.gate: dict[date, bool] = {}
        self.signal_rows: list[SignalRow] = []

    # --- market views ------------------------------------------------------------------------

    def _bars_by_day(self) -> dict[tuple[str, date], _Bar]:
        found: dict[tuple[str, date], _Bar] = {}
        for row in self.bars.select("instrument_id", "date", "open", "high", "low", "close").rows():
            prices = [_price(v) for v in row[2:]]
            if any(p is None or p <= 0 for p in prices):
                continue  # a bar without four positive prices cannot be traded on
            found[(str(row[0]), row[1])] = _Bar(*prices)  # type: ignore[arg-type]
        return found

    def _history_by_instrument(self) -> dict[str, tuple[list[date], list[_Bar]]]:
        found: dict[str, tuple[list[date], list[_Bar]]] = {}
        for (instrument_id, day), bar in sorted(self.bar.items(), key=lambda item: item[0]):
            dates, bars = found.setdefault(instrument_id, ([], []))
            dates.append(day)
            bars.append(bar)
        return found

    def _bars_by_session(self) -> dict[date, list[tuple[str, _Bar]]]:
        found: dict[date, list[tuple[str, _Bar]]] = {}
        for (instrument_id, day), bar in sorted(self.bar.items(), key=lambda item: item[0]):
            found.setdefault(day, []).append((instrument_id, bar))
        return found

    def _prev(self, day: date) -> date | None:
        n = self.index[day]
        return self.days[n - 1] if n > 0 else None

    def _closes_through(self, day: date) -> list[Decimal]:
        return [c for d, c in self.closes if d <= day]

    def _feature(self, day: date, instrument_id: str, name: str) -> Decimal | None:
        rows = self.feature_rows.get(day)
        if rows is None:
            frame = self.features.get(day)
            rows = {} if frame is None else {str(r["instrument_id"]): r for r in frame.to_dicts()}
            self.feature_rows[day] = rows
        return _price(rows.get(instrument_id, {}).get(name))

    def _bars_through(self, instrument_id: str, day: date, *, inclusive: bool = True) -> list[_Bar]:
        """The instrument's bars dated on or before `day` (before it, if not `inclusive`)."""
        dates, bars = self.history.get(instrument_id, ([], []))
        end = bisect_right(dates, day) if inclusive else bisect_left(dates, day)
        return bars[:end]

    def _tick(self, instrument_id: str, day: date) -> Decimal | None:
        """The instrument's NSE tick on `day` (HAT-EXEC-053), from its closes before `day`, as
        the live trigger prices it; None without an earlier close."""
        bars = self._bars_through(instrument_id, day, inclusive=False)
        dates = self.history.get(instrument_id, ([], []))[0][: len(bars)]
        return tick_for_date(dates, [b.close for b in bars], day)

    def _sma(self, instrument_id: str, day: date, length: int) -> Decimal | None:
        bars = self._bars_through(instrument_id, day)
        if len(bars) < length:
            return None
        return sum((b.close for b in bars[-length:]), _ZERO) / length

    def _swing_low(self, instrument_id: str, before: date, lookback: int) -> Decimal | None:
        """The lowest low of the `lookback` sessions the instrument traded before `before`."""
        bars = self._bars_through(instrument_id, before, inclusive=False)
        return min(b.low for b in bars[-lookback:]) if len(bars) >= lookback else None

    def _atr(self, instrument_id: str, day: date, length: int) -> Decimal | None:
        """The mean true range of the `length` sessions up to `day`."""
        window = self._bars_through(instrument_id, day)[-(length + 1) :]
        return mean_true_range(
            [b.high for b in window], [b.low for b in window], [b.close for b in window], length
        )

    def _window(self, as_of: date) -> pl.DataFrame:
        first = self.days[max(0, self.index[as_of] - LOOKBACK_SESSIONS)]
        return self.bars.filter((pl.col("date") >= first) & (pl.col("date") <= as_of))

    # --- the run -----------------------------------------------------------------------------

    def run(self) -> BacktestResult:
        for day in self.period:
            prev = self._prev(day)
            if prev is not None:
                self._publish(prev, day)
            self._morning(day, prev)
            self._sync_ladder()
            if prev is not None:
                self._entries(day, prev)
            self._close_phase(day)
            self._delistings(day)
            self._sync_ladder()
            trades.expire_watchlist(self.session, day)
            self._mark(day)
        self._finish(self.period[-1])
        return self._result()

    def _publish(self, prev: date, day: date) -> None:
        inputs = ScanInputs(
            as_of=prev,
            members=(),  # replaced by the bot's universe on `prev`
            bars=self._window(prev),
            regime_closes=self._closes_through(prev),
            features=self.features.get(prev),
            series=dict(self.data.series),
        )
        published = watchlist.scan_and_publish(
            self.session, self.bot_id, inputs, day, _at(prev, _AT_NIGHT)
        )
        self.gate[day] = published.gate_on

    def _open_positions(self) -> list[HatPosition]:
        rows = self.session.scalars(
            select(HatPosition).where(
                HatPosition.bot_id == self.bot_id, HatPosition.closed_at.is_(None)
            )
        ).all()
        return sorted(
            rows, key=lambda p: (self.held[p.id].entry_day, self.held[p.id].instrument_id)
        )

    def _evaluate(
        self, position: HatPosition, view: exits.ExitView, phase: exits.Phase, at: datetime
    ) -> None:
        held = self.held[position.id]
        trades.evaluate_position(
            self.session,
            position.id,
            view,
            phase,
            at,
            high=held.high,
            low=held.low,
            fills=self.spec.fills,
        )

    def _down_leg(
        self, position: HatPosition, bar: _Bar, day: date, moment: time = _AT_LOW
    ) -> None:
        """The way down from the open to the low (at `moment`): the stop fills at min(open, stop).
        A position the stop closes never saw the rest of the fall, so its excursion stops at the
        fill."""
        held = self.held[position.id]
        if position.closed_at is not None:
            return
        fill = stop_fill(bar.open, bar.low, Decimal(position.current_stop))
        if fill is not None:
            held.low = min(held.low, fill)
            self._evaluate(position, exits.ExitView(fill), "live", _at(day, moment))
        if position.closed_at is None:
            held.low = min(held.low, bar.low)

    def _up_leg(self, position: HatPosition, low: Decimal, high: Decimal, day: date) -> None:
        """The way up to the high: each profit rule fills at the first tick price at which
        `exits.evaluate` acts, as many times as the stack keeps acting. A position closed on
        the way up never saw the rest of the rise, so its excursion stops at the fill."""
        held = self.held[position.id]
        before = Decimal(position.current_stop)
        # A held instrument always has the closes before its entry; the finest NSE tick stands
        # in only if it did not, which keeps the search exact on a finer grid.
        tick = self._tick(held.instrument_id, day) or _FINEST_TICK
        for step in range(len(self.config.exits) + 2):
            if position.closed_at is not None:
                return
            rules, state = trades.position_state(self.session, position)
            price = first_trigger(rules, state, low, high, tick)
            if price is None:
                break
            held.high = max(held.high, price)
            self._evaluate(position, exits.ExitView(price), "live", _at(day, _AT_HIGH, step))
            low = price
        if position.closed_at is None:
            held.high = max(held.high, high)
            if Decimal(position.current_stop) > before:
                held.raised = (day, before)

    def _morning(self, day: date, prev: date | None) -> None:
        regime_off = prev is not None and not regime_allows_longs(
            self.config.regime, self._closes_through(prev)
        )
        for position in self._open_positions():
            held = self.held[position.id]
            bar = self.bar.get((held.instrument_id, day))
            if bar is None:
                continue  # no session for this instrument today: nothing can fill
            held.high, held.low = max(held.high, bar.open), min(held.low, bar.open)
            if self._lower_locked(held.instrument_id, day, bar):
                # Pinned at the lower band all day: no buyer, so nothing sells; a stop waits for
                # the next open that is not pinned and fills there at min(open, stop).
                self.lower_circuit_days.add(day)
                continue
            nightly = exits.ExitView(bar.open, regime_off=regime_off)
            self._evaluate(position, nightly, "nightly", _at(day, _AT_OPEN))
            if position.closed_at is None:
                self._evaluate(position, exits.ExitView(bar.open), "live", _at(day, _AT_OPEN, 1))
            self._down_leg(position, bar, day)
            self._up_leg(position, bar.open, bar.high, day)

    # --- entries -----------------------------------------------------------------------------

    def _watchlist(self, day: date) -> list[HatWatchlistEntry]:
        rows = self.session.scalars(
            select(HatWatchlistEntry).where(
                HatWatchlistEntry.bot_id == self.bot_id,
                HatWatchlistEntry.trade_date == day,
                HatWatchlistEntry.status == lifecycle.WATCHLISTED,
            )
        ).all()
        return sorted(rows, key=lambda r: (-r.score, r.playbook, str(r.instrument_id)))

    def _skip(self, reason: str) -> None:
        self.skips[reason] = self.skips.get(reason, 0) + 1

    def _stop(
        self, playbook: Playbook, row: HatWatchlistEntry, entry: Decimal, low: Decimal, prev: date
    ) -> tuple[Decimal | None, str | None]:
        """The initial stop for an entry at `entry` (HAT-BOT-062), by the playbook's rule, from
        what is known when the trigger fires: `low` is the day's low before the entry."""
        rule = playbook.stop
        instrument_id = str(row.instrument_id)
        tick = self._tick(instrument_id, row.trade_date)
        if tick is None:
            return None, SKIP_BAD_INPUT
        if isinstance(rule, StopBelowFinalContractionLow):
            # The contraction stop does not move with the entry; only its width is checked again.
            if row.planned_stop is None:
                # The scan leaves the plan empty when the stop at the pivot is too wide.
                return None, SKIP_TOO_WIDE
            plan = check_width(
                entry,
                Decimal(row.planned_stop),
                Decimal(repr(rule.max_pct)),
                Decimal(repr(rule.min_pct)),
            )
            return plan.stop, plan.skip
        inputs = StopInputs(entry=entry, tick=tick)
        if isinstance(rule, StopLowOfDay):
            inputs = StopInputs(entry=entry, tick=tick, day_lows=[low])
        elif isinstance(rule, StopAtrMultiple):
            inputs = StopInputs(
                entry=entry, tick=tick, atr=self._atr(instrument_id, prev, rule.days)
            )
        elif isinstance(rule, StopSwingLow):
            older = self._bars_through(instrument_id, prev)
            lows = [b.low for b in older[-rule.lookback_days :]]
            inputs = StopInputs(
                entry=entry, tick=tick, swing_lows=lows if len(lows) == rule.lookback_days else []
            )
        plan = plan_stop(rule, inputs)
        return plan.stop, plan.skip

    def _equity_now(self) -> tuple[Decimal, Decimal]:
        """(equity at the last close, free cash now)."""
        cash = self._cash()
        equity = self.equity[-1][1] if self.equity else self.spec.initial_equity
        return equity, cash

    def _context(self, row: HatWatchlistEntry, plan: _EntryPlan, prev: date) -> SignalContext:
        equity, cash = self._equity_now()
        instrument_id = str(row.instrument_id)
        return SignalContext(
            action="entry",
            strategy_key=self.config.bot,
            industry=industry_on(self.data, instrument_id, row.trade_date),
            entry_price=plan.price,
            stop_price=plan.stop,
            trigger_level=Decimal(row.pivot),
            ltp=plan.price,
            locked_upper_circuit=plan.locked,
            configured_max_chase_pct=self.chase[row.playbook],
            equity=equity,
            allocation_risk_pct=self.policy.risk_pct,
            regime_mult=Decimal(1),
            health_mult=Decimal(1),
            free_cash=cash,
            adv20=self._feature(prev, instrument_id, "adv20"),
            total_equity=equity,
            policy_versions=[self.spec.policy],
            ladder=self.ladder,
            gate_on=self.gate.get(row.trade_date, False),
            feed_stale=False,
            quality_failed=False,
            kill=KillState(),
            calendar=self.calendar,
        )

    def _slots(self, day: date) -> list[HatWatchlistEntry]:
        """The day's watchlist in the order its triggers take the open slots (HAT-BT-026)."""

        def rank(row: HatWatchlistEntry) -> tuple[int, Decimal]:
            bar = self.bar.get((str(row.instrument_id), day))
            pivot = None if row.pivot is None else Decimal(row.pivot)
            return trigger_rank(None if bar is None else bar.open, pivot, self.spec.entry_fill_rule)

        return order_slots(self._watchlist(day), self.spec.slot_order, day, rank)

    def _entries(self, day: date, prev: date) -> None:
        for n, row in enumerate(self._slots(day)):
            if row.playbook == EP_PLAYBOOK:
                continue
            instrument_id = str(row.instrument_id)
            bar = self.bar.get((instrument_id, day))
            if bar is None:
                continue  # no session today: the level cannot break
            price = self._fill(row, bar, day)
            if isinstance(price, str):
                self._skip(price)
                continue
            if price is None:
                continue  # the level did not break: the row expires at the session end
            level = Decimal(row.pivot)
            playbook = self.config.playbook(row.playbook)
            lows = self._entry_lows(playbook, instrument_id, bar, level, price, (day, prev))
            if isinstance(lows, str):
                self._skip(lows)
                continue
            before, dip = lows
            stop, skip = self._stop(playbook, row, price, before, prev)
            if stop is None:
                self._skip(skip or SKIP_BAD_INPUT)
                continue
            at = _at(day, _AT_ENTRY, n)
            locked = self._locked(instrument_id, price, bar.high, day)
            failed, filters = self._entry_filters(
                playbook, instrument_id, (price, stop, level, locked), (at, prev)
            )
            plan = _EntryPlan(price, stop, dip, locked, failed, filters)
            self._enter(row, bar, plan, prev, at)

    def _fill(self, row: HatWatchlistEntry, bar: _Bar, day: date) -> Decimal | str | None:
        """The entry fill of the row's level under the run's entry fill rule, None when the
        level did not break, or the skip reason when there is no level or no tick to penetrate
        by (HAT-BT-020)."""
        if row.pivot is None:
            return SKIP_NO_LEVEL
        level = Decimal(row.pivot)
        rule = self.spec.entry_fill_rule
        # The tick matters only to a penetration from below; read it only then.
        tick = None
        if rule == PENETRATION and bar.open <= level < bar.high:
            tick = self._tick(str(row.instrument_id), day)
            if tick is None:
                return SKIP_BAD_INPUT  # no tick to penetrate by (nor to plan a stop on)
        return entry_fill(bar.open, bar.high, level, tick, rule)

    def _prev_close(self, instrument_id: str, day: date) -> Decimal | None:
        dates, bars = self.history.get(instrument_id, ([], []))
        at = bisect_left(dates, day)
        return bars[at - 1].close if at > 0 else None

    def _locked(self, instrument_id: str, price: Decimal, high: Decimal, day: date) -> bool | None:
        """Whether the entry at `price` is at the upper circuit (`upper_circuit_locked`)."""
        prev_close = self._prev_close(instrument_id, day)
        return upper_circuit_locked(price, high, prev_close, self._tick(instrument_id, day))

    def _lower_locked(self, instrument_id: str, day: date, bar: _Bar) -> bool:
        """Whether the instrument sat at its lower circuit all of `day` (`lower_circuit_locked`);
        an unknown state (no previous close or tick) is not locked."""
        if not bar.open == bar.high == bar.low == bar.close:
            return False  # the common case, without reading the tick
        ohlc = (bar.open, bar.high, bar.low, bar.close)
        prev_close = self._prev_close(instrument_id, day)
        return lower_circuit_locked(ohlc, prev_close, self._tick(instrument_id, day)) is True

    def _entry_filters(
        self,
        playbook: Playbook,
        instrument_id: str,
        entry: tuple[Decimal, Decimal, Decimal, bool | None],
        when: tuple[datetime, date],
    ) -> tuple[str | None, tuple[dict[str, object], ...]]:
        """(the type of the first failing entry filter or None, each filter's outcome as live
        records it on the signal) for the playbook's entry filters (`adtr_filter`, `max_chase`)
        at the trigger, evaluated as live evaluates them (HAT-BT-025): `entry` is (price, stop,
        level, locked), with ADTR(20) as of `prev`, the session before the trigger's instant
        `at`."""
        price, stop, level, locked = entry
        at, prev = when
        ctx = trigger.entry_filter_context(
            at=at,
            entry=price,
            stop=stop,
            adtr20=self._feature(prev, instrument_id, "adtr20"),
            level=level,
            locked_upper_circuit=locked,
        )
        failed, outcomes = trigger.evaluate_entry_filters(
            playbook, ctx, configured_pct=self.chase[playbook.name], strategy_key=self.config.bot
        )
        return failed, tuple({"stage": "entry_filter", **o.to_json()} for o in outcomes)

    def _entry_lows(
        self,
        playbook: Playbook,
        instrument_id: str,
        bar: _Bar,
        level: Decimal,
        price: Decimal,
        days: tuple[date, date],
    ) -> tuple[Decimal, Decimal | None] | str:
        """(the entry day's low before the break, its low after the entry or None), or the skip
        reason when a low-of-day stop cannot know the low before the break (HAT-BT-020, PR 60)."""
        day, prev = days
        intraday = self.data.intraday.get((instrument_id, day), ())
        adr20 = self._feature(prev, instrument_id, "adr20")
        low_of_day = isinstance(playbook.stop, StopLowOfDay)
        gapped = price == bar.open
        if low_of_day and not intraday:
            if gapped:
                # A gap over enters at the open: its stop needs the opening range's low.
                return SKIP_GAP_NO_INTRADAY
            if adr20 is None or adr20 < 0:
                return SKIP_BAD_INPUT  # the whole day's low would be read before the break
        # The break is at the entry price (level + 1 tick under penetration); a gap over at the
        # open breaks on the first bar either way.
        broke = level if gapped else price
        before, after = pre_break_low(bar.open, bar.low, broke, adr20, intraday)
        if gapped and not low_of_day:
            # A gap over enters at the open and then sees the whole day's way down.
            return before, bar.low
        return before, after

    def _enter(
        self,
        row: HatWatchlistEntry,
        bar: _Bar,
        plan: _EntryPlan,
        prev: date,
        at: datetime,
    ) -> None:
        """Open the entry, or record it Rejected with the failing entry filter as live does. The
        plan's `dip` is taken before the high, so a stop it reaches fills that day and it seeds
        the MAE."""
        instrument_id = str(row.instrument_id)
        price, dip = plan.price, plan.dip
        held_prices = {
            # marked down as the equity is, after sessions without a bar (PR 64)
            uuid.UUID(h.instrument_id): self._mark_price(
                h.instrument_id, row.trade_date, self.last_close[h.instrument_id]
            )
            for h in self.held.values()
            if h.instrument_id in self.last_close
        }
        request = trades.EntryRequest(
            bot_id=self.bot_id,
            playbook=row.playbook,
            instrument_id=row.instrument_id,
            trade_date=row.trade_date,
            triggered_at=at,
            context=self._context(row, plan, prev),
            last_prices=held_prices,
            skip_reason=plan.skip,
            criteria=plan.filters,
        )
        outcome = trades.open_entry(self.session, request, fills=self.spec.fills)
        signal = self.session.get(HatSignal, outcome.signal_id)
        if signal is None or outcome.duplicate:
            return
        self.signal_rows.append(
            SignalRow(
                trade_date=row.trade_date,
                playbook=row.playbook,
                instrument_id=instrument_id,
                state=signal.state,
                reason=signal.risk_decision.get("reason"),
                trigger_price=Decimal(signal.trigger_price),
                stop_price=Decimal(signal.stop_price),
                qty=signal.qty,
                strategy_version_id=signal.strategy_version_id,
                risk_policy_version_id=signal.risk_policy_version_id,
            )
        )
        if outcome.position_id is None:
            return
        position = self.session.get(HatPosition, outcome.position_id)
        if position is None:  # pragma: no cover - open_entry returned the id it wrote
            return
        day = row.trade_date
        self.held[position.id] = _Held(instrument_id, day, price, price)
        if dip is not None and dip < price:
            # From the entry down to the low: a stop it reaches fills at min(entry, stop).
            self._down_leg(position, _Bar(price, bar.high, dip, bar.close), day, _AT_DIP)
        self._up_leg(position, price, bar.high, day)

    # --- the close, delistings, the period end ------------------------------------------------

    def _close_view(self, position: HatPosition, bar: _Bar, day: date) -> exits.ExitView:
        held = self.held[position.id]
        return exits.view_from_rules(
            bar.close,
            self.config.exits,
            self.index[day] - self.index[held.entry_day],
            lambda length: self._sma(held.instrument_id, day, length),
            lambda lookback: self._swing_low(held.instrument_id, day, lookback),
        )

    def _close_phase(self, day: date) -> None:
        for position in self._open_positions():
            held = self.held[position.id]
            bar = self.bar.get((held.instrument_id, day))
            if bar is None:
                continue
            held.low = min(held.low, bar.close)
            if self._lower_locked(held.instrument_id, day, bar):
                # Nothing sells at a locked lower band, a close rule's sale included; a position
                # entered today (a gap over at the band) counts the day here.
                self.lower_circuit_days.add(day)
                continue
            stop = Decimal(position.current_stop)
            if bar.close <= stop or self._ambiguous_dip(position, bar, day):
                # Falling from the high to the close (or, on an ambiguous day under
                # `pessimistic`, to the low) through a stop raised during the day.
                self._evaluate(position, exits.ExitView(stop), "live", _at(day, _AT_FALL))
            if position.closed_at is None:
                self._evaluate(
                    position, self._close_view(position, bar, day), "close", _at(day, _AT_CLOSE)
                )
        for instrument_id, bar in self.by_day.get(day, ()):
            self.last_close[instrument_id] = bar.close
            self.last_bar[instrument_id] = day

    def _ambiguous_dip(self, position: HatPosition, bar: _Bar, day: date) -> bool:
        """Whether the position's day is ambiguous (`raised_stop_dip`: the way up raised its
        stop, the low reaches the raised stop, the close is above it), counting the day; True
        when the run's rule is `pessimistic`, so the raised stop fills at its price."""
        raised = self.held[position.id].raised
        if raised is None or raised[0] != day:
            return False
        if not raised_stop_dip(bar.low, bar.close, raised[1], Decimal(position.current_stop)):
            return False
        self.ambiguous_days.add(day)
        return self.spec.ambiguous == AMBIGUOUS_PESSIMISTIC

    def _close(self, position: HatPosition, price: Decimal, reason: str, at: datetime) -> None:
        held = self.held[position.id]
        trades.close_position(
            self.session,
            position.id,
            price,
            reason,
            at,
            high=held.high,
            low=held.low,
            fills=self.spec.fills,
        )

    def _delistings(self, day: date) -> None:
        """An instrument's last traded session closes its open position at that close
        (HAT-BT-012): a delisted stock's loss stays in the results."""
        for position in self._open_positions():
            held = self.held[position.id]
            if self.data.last_traded.get(held.instrument_id) != day:
                continue
            bar = self.bar.get((held.instrument_id, day))
            price = bar.close if bar is not None else self.last_close.get(held.instrument_id)
            if price is not None:
                self._close(position, price, DELISTED, _at(day, _AT_END))

    def _finish(self, last: date) -> None:
        for position in self._open_positions():
            held = self.held[position.id]
            price = self._mark_price(held.instrument_id, last, Decimal(position.avg_entry))
            self._close(position, price, END_OF_PERIOD, _at(last, _AT_END, 30))
        self.session.flush()
        if self.equity:
            self.equity[-1] = (last, self._marked(last))

    def _sync_ladder(self) -> None:
        """Step the exposure ladder for each trade closed since the last step, in close order:
        the backtest counts its trades as real ones."""
        rows = self.session.execute(
            select(HatTradeResult.position_id, HatTradeResult.r_multiple, HatPosition.closed_at)
            .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
            .where(HatPosition.bot_id == self.bot_id)
        ).all()
        fresh = sorted(
            (r for r in rows if r[0] not in self.seen_results),
            key=lambda r: (as_utc(r[2]), self.held[r[0]].instrument_id),
        )
        for position_id, r_multiple, _ in fresh:
            self.seen_results.add(position_id)
            self.ladder = apply_outcome(self.ladder, is_win(Decimal(r_multiple)), self.policy)

    def _cash(self) -> Decimal:
        cash = self.spec.initial_equity
        for side, value, fees in self._exact_flows():
            cash += value if side == "sell" else -value
            cash -= fees
        return cash

    def _exact_flows(self) -> list[tuple[str, Decimal, Decimal]]:
        """Per side, the traded value and fees of every fill, summed exactly in Decimal."""
        totals: dict[str, tuple[Decimal, Decimal]] = {}
        for side, qty, price, fees in self.session.execute(
            select(HatOrder.side, HatFill.qty, HatFill.price, HatFill.fees).join(
                HatOrder, HatOrder.id == HatFill.order_id
            )
        ):
            value, fee = totals.get(side, (_ZERO, _ZERO))
            totals[side] = (value + qty * Decimal(price), fee + Decimal(fees))
        return [(side, value, fee) for side, (value, fee) in sorted(totals.items())]

    def _mark_price(self, instrument_id: str, day: date, fallback: Decimal) -> Decimal:
        """The instrument's mark on `day`: its last close, marked down one band per session
        after `suspension_sessions` sessions without a bar (`suspended_mark`, PR 64); `fallback`
        before its first close."""
        last = self.last_close.get(instrument_id)
        seen = self.last_bar.get(instrument_id)
        if last is None or seen is None:
            return fallback
        sessions = self.index[day] - self.index[seen]
        after = self.spec.suspension_sessions
        if sessions <= after:
            return last
        tick = self._tick(instrument_id, day) or _FINEST_TICK
        return suspended_mark(last, sessions, after, SUSPENSION_BAND_PCT, tick)

    def _marked(self, day: date) -> Decimal:
        equity = self._cash()
        for position in self._open_positions():
            held = self.held[position.id]
            price = self._mark_price(held.instrument_id, day, Decimal(position.avg_entry))
            equity += position.qty_open * price
        return equity

    def _mark(self, day: date) -> None:
        self.equity.append((day, self._marked(day)))

    # --- the result --------------------------------------------------------------------------

    def _result(self) -> BacktestResult:
        trade_rows = self._trades()
        result = BacktestResult(
            trades=trade_rows,
            equity=tuple(self.equity),
            watchlists=self._watch_rows(),
            signals=tuple(self.signal_rows),
            fills=self._fill_rows(),
            skips=dict(sorted(self.skips.items())),
            metrics={},
        )
        metrics = compute_metrics(trade_rows, result.equity, self.spec)
        metrics["skips"] = dict(result.skips)
        metrics["signals"] = len(self.signal_rows)
        metrics["rejections"] = _count_by(r.reason or "" for r in self.signal_rows if r.qty == 0)
        metrics["ambiguous_days"] = len(self.ambiguous_days)  # HAT-BT-021 (PR 64)
        metrics["lower_circuit_days"] = len(self.lower_circuit_days)
        self.session.close()
        return BacktestResult(**{**result.__dict__, "metrics": metrics})

    def _trades(self) -> tuple[TradeRow, ...]:
        rows = []
        query = (
            select(HatPosition, HatTradeResult, HatSignal)
            .join(HatTradeResult, HatTradeResult.position_id == HatPosition.id)
            .join(HatSignal, HatSignal.id == HatPosition.signal_id)
            .where(HatPosition.bot_id == self.bot_id)
        )
        for position, result, signal in self.session.execute(query).all():
            qty = self.session.scalar(
                select(func.sum(HatFill.qty))
                .join(HatOrder, HatOrder.id == HatFill.order_id)
                .where(HatOrder.signal_id == signal.id, HatOrder.purpose == "entry")
            )
            rows.append(
                TradeRow(
                    entry_date=signal.trade_date,
                    exit_date=trade_date_of(as_utc(position.closed_at)),
                    playbook=signal.playbook,
                    instrument_id=str(signal.instrument_id),
                    qty=int(qty or 0),
                    entry_price=Decimal(position.avg_entry),
                    initial_stop=Decimal(position.initial_stop),
                    exit_reason=result.exit_reason,
                    realized_pnl=Decimal(result.realized_pnl),
                    fees=Decimal(result.fees),
                    r_multiple=Decimal(result.r_multiple),
                    mfe_r=Decimal(result.mfe_r),
                    mae_r=Decimal(result.mae_r),
                    holding_days=result.holding_days,
                    strategy_version_id=signal.strategy_version_id,
                    risk_policy_version_id=signal.risk_policy_version_id,
                )
            )
        rows.sort(key=lambda t: (t.exit_date, t.entry_date, t.instrument_id, t.playbook))
        return tuple(rows)

    def _watch_rows(self) -> tuple[WatchRow, ...]:
        rows = [
            WatchRow(
                trade_date=r.trade_date,
                playbook=r.playbook,
                instrument_id=str(r.instrument_id),
                score=Decimal(r.score),
                pivot=None if r.pivot is None else Decimal(r.pivot),
                planned_stop=None if r.planned_stop is None else Decimal(r.planned_stop),
            )
            for r in self.session.scalars(
                select(HatWatchlistEntry).where(HatWatchlistEntry.bot_id == self.bot_id)
            )
        ]
        rows.sort(key=lambda w: (w.trade_date, -w.score, w.playbook, w.instrument_id))
        return tuple(rows)

    def _fill_rows(self) -> tuple[FillRow, ...]:
        rows = [
            FillRow(
                day=trade_date_of(as_utc(fill.filled_at)),
                instrument_id=str(signal.instrument_id),
                purpose=order.purpose,
                side=order.side,
                qty=fill.qty,
                price=Decimal(fill.price),
                fees=Decimal(fill.fees),
            )
            for fill, order, signal in self.session.execute(
                select(HatFill, HatOrder, HatSignal)
                .join(HatOrder, HatOrder.id == HatFill.order_id)
                .join(HatSignal, HatSignal.id == HatOrder.signal_id)
            ).all()
        ]
        rows.sort(key=lambda f: (f.day, f.instrument_id, f.purpose, f.side, f.qty, f.price))
        return tuple(rows)


def _count_by(values: Iterable[str]) -> dict[str, int]:
    found: dict[str, int] = {}
    for value in values:
        found[value] = found.get(value, 0) + 1
    return dict(sorted(found.items()))


# --- the profit-rule search -------------------------------------------------------------------


def _acts(rules: Sequence[Any], state: exits.PositionState, price: Decimal) -> bool:
    result = exits.evaluate(rules, state, exits.ExitView(price), "live")
    return bool(result.actions or result.new_stop is not None or result.fired)


def pre_break_low(
    bar_open: Decimal,
    bar_low: Decimal,
    level: Decimal,
    adr20: Decimal | None,
    intraday: Sequence[tuple[Decimal, Decimal]] = (),
) -> tuple[Decimal, Decimal | None]:
    """(the day's low before an entry at `level` broke, its low after the break or None) on an
    entry day (HAT-BT-020, PR 60). With the session's 5-minute bars: the lowest low of the bars
    up to and including the first one whose high reaches the level, as live reads it, and the
    lowest low of the bars after that one (or the day low, when the bars miss it). Without
    them: max(day low, min(open, level x (1 - adr20 / 100))), and the day low as the low after
    the break when it is below that; with no `adr20` the whole day's low comes before the
    break."""
    if intraday:
        lows = [low for _, low in intraday]
        at = next((i for i, (high, _) in enumerate(intraday) if high >= level), len(lows) - 1)
        # The trigger bar's low counts before the break (the stop is planned from it); a day low
        # the bars miss is taken as after it.
        later = [*lows[at + 1 :], *([bar_low] if bar_low < min(lows) else [])]
        return min(lows[: at + 1]), (min(later) if later else None)
    if adr20 is None or adr20 < 0:
        return bar_low, None
    estimate = min(bar_open, level * (1 - adr20 / 100))
    before = max(bar_low, estimate)
    return before, (bar_low if bar_low < before else None)


def upper_circuit_locked(
    price: Decimal, bar_high: Decimal, prev_close: Decimal | None, tick: Decimal | None
) -> bool | None:
    """Whether an entry at `price` is at the upper circuit (HAT-BT-025, PR 61), live's rule
    (`trigger.locked_upper_circuit`: the price at or above the band's upper price) read from a
    daily bar: the price is the day's high (nothing traded above it), above the previous close,
    and within one tick of an NSE band's upper price (`trigger.upper_circuit_price` at 2, 5, 10
    or 20%) over the previous close. The run has no band table, so the band is the nearest of
    these whose upper price is above the previous close; a name with a wider band entered at
    exactly a narrower one's price reads as locked. None without a previous close or a tick (live
    fails closed on an unknown circuit state)."""
    if prev_close is None or prev_close <= 0 or tick is None or tick <= 0:
        return None
    if price != bar_high or price <= prev_close:
        return False
    for band in NSE_BANDS:
        daily = trigger.DailyInputs(
            prev_close=prev_close, tick=tick, band_known=True, band_pct=band
        )
        upper = trigger.upper_circuit_price(daily)
        if upper is not None and upper > prev_close and abs(price - upper) <= tick:
            return True
    return False


def lower_circuit_locked(
    ohlc: tuple[Decimal, Decimal, Decimal, Decimal],
    prev_close: Decimal | None,
    tick: Decimal | None,
) -> bool | None:
    """Whether a session sat at its lower circuit all day (HAT-BT-021, PR 64), read from its
    daily bar as `upper_circuit_locked` reads the upper one: one price all day (open = high =
    low = close), below the previous close and within one tick of an NSE band's lower price
    (`trigger.lower_circuit_price` at 2, 5, 10 or 20%) under the previous close. Nothing traded
    above the band, so a seller found no buyer: a stop that day carries to the next session not
    pinned. None without a previous close or a tick."""
    if prev_close is None or prev_close <= 0 or tick is None or tick <= 0:
        return None
    open_, high, low, close = ohlc
    if not open_ == high == low == close or close >= prev_close:
        return False
    for band in NSE_BANDS:
        daily = trigger.DailyInputs(
            prev_close=prev_close, tick=tick, band_known=True, band_pct=band
        )
        lower = trigger.lower_circuit_price(daily)
        if lower is not None and lower < prev_close and abs(close - lower) <= tick:
            return True
    return False


def first_trigger(
    rules: Sequence[Any],
    state: exits.PositionState,
    low: Decimal,
    high: Decimal,
    tick: Decimal,
) -> Decimal | None:
    """The lowest price in [low, high] (`low`, `high` or a tick price between) at which the live
    rules of the stack act, or None when they do not act even at `high`.

    Above the stop every live rule that acts at a price also acts at any higher one (profit
    targets and R levels are thresholds), so the search is a bisection over the tick grid. The
    rules themselves are only ever evaluated by `exits.evaluate`."""
    if high < low or not _acts(rules, state, high):
        return None
    if _acts(rules, state, low):
        return low
    first = (low / tick).to_integral_value(rounding=ROUND_FLOOR) + 1
    last = (high / tick).to_integral_value(rounding=ROUND_CEILING) - 1
    lo, hi = int(first), int(last) + 1  # index hi stands for `high` itself
    while lo < hi:
        mid = (lo + hi) // 2
        if _acts(rules, state, mid * tick):
            hi = mid
        else:
            lo = mid + 1
    return high if lo > int(last) else lo * tick


# --- metrics ----------------------------------------------------------------------------------


def _ratio(value: Decimal) -> str:
    return str(value.quantize(Decimal("0.000001")))


def _float(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _stats_json(stats: ReturnStats | None) -> dict[str, Any] | None:
    if stats is None:
        return None
    return {"sr": stats.sr, "t": stats.t, "skew": stats.skew, "kurtosis": stats.kurtosis}


def compute_metrics(
    trades_: Sequence[TradeRow],
    equity: Sequence[tuple[date, Decimal]],
    spec: RunSpec,
) -> dict[str, Any]:
    """The run's metrics (HAT-BT-040, HAT-BT-041): R statistics in Decimal, the standard
    drawdown, return and Sharpe figures from `app.hat.backtest.metrics`, and the return moments
    the deflated Sharpe ratio reads. The DSR itself depends on the family's trials and is added
    when the run is recorded."""
    rs = [t.r_multiple for t in trades_]
    count = len(rs)
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r <= 0]
    total_r = sum(rs, _ZERO)
    curve = [spec.initial_equity, *(value for _, value in equity)]
    floats = pl.Series([float(v) for v in curve], dtype=pl.Float64)
    returns = (floats / floats.shift(1) - 1.0).slice(1)
    drawdown = bt_metrics.max_drawdown(bt_metrics.drawdown_series(floats))
    stats = return_stats([float(r) for r in returns])
    trade_stats = bt_metrics.trade_stats([float(r) for r in rs])
    final = curve[-1]
    net = sum((t.realized_pnl for t in trades_), _ZERO)
    return {
        "trades": count,
        "sum_r": _ratio(total_r),
        "expectancy_r": _ratio(total_r / count) if count else None,
        "win_rate": trade_stats.win_rate,
        "avg_win_r": _ratio(sum(wins, _ZERO) / len(wins)) if wins else None,
        "avg_loss_r": _ratio(sum(losses, _ZERO) / len(losses)) if losses else None,
        "avg_mfe_r": _ratio(sum((t.mfe_r for t in trades_), _ZERO) / count) if count else None,
        "avg_mae_r": _ratio(sum((t.mae_r for t in trades_), _ZERO) / count) if count else None,
        "net_pnl": str(net),
        "fees": str(sum((t.fees for t in trades_), _ZERO)),
        "initial_equity": str(spec.initial_equity),
        "final_equity": str(final),
        "total_return": _float(bt_metrics.total_return(float(final), float(spec.initial_equity))),
        "max_drawdown": _float(-drawdown) if drawdown else 0.0,
        "sharpe_annual": _float(bt_metrics.sharpe_mean_std(returns)),
        "returns": _stats_json(stats),
        "sessions": len(equity),
        "strategy_version_id": str(spec.strategy_version_id),
        "risk_policy_version_id": str(spec.policy.version_id),
        "cooldown_sessions": spec.policy.policy.cooldown_sessions,
        "known_gaps": known_gaps(spec),
        "entry_day_low": ENTRY_DAY_LOW,
        **conventions(spec),
        "for_promotion": spec.for_promotion,
        "config_digest": config_digest(spec),
    }


def known_gaps(spec: RunSpec) -> list[str]:
    """The run's known gaps (HAT-BT-070): `KNOWN_GAPS`, and the touch fill under `touch`."""
    return [*KNOWN_GAPS, *([TOUCH_GAP] if spec.entry_fill_rule == TOUCH else [])]


def conventions(spec: RunSpec) -> dict[str, Any]:
    """The fill conventions a run used (PR 62, O5): recorded in its metrics."""
    return {
        "fill_path": FILL_PATH,
        "entry_fill_rule": spec.entry_fill_rule,
        "gap_through_rule": GAP_THROUGH_RULE,
        "trigger_proxy": TRIGGER_PROXY,
        # normalised, so 20 and 20.0 bps record (and hash) alike
        "slippage_bps": format(abs(spec.fills.slippage_bps).normalize(), "f"),  # -0 is 0
        "cost_model": spec.fills.model.to_json(),
        "slot_order": spec.slot_order,  # HAT-BT-026
        "ambiguous": spec.ambiguous,  # HAT-BT-021 (PR 64)
        "lower_circuit": LOWER_CIRCUIT_RULE,
        "suspension_markdown": {
            "after_sessions": spec.suspension_sessions,
            "band_pct": str(SUSPENSION_BAND_PCT),
        },
    }


def config_digest(spec: RunSpec) -> str:
    """The canonical hash of the run's fill conventions (`conventions`): runs with equal
    digests were filled, slipped and charged alike."""
    return config_hash(conventions(spec))


# --- trades.parquet ---------------------------------------------------------------------------

_MONEY = pa.decimal128(18, 4)
_R = pa.decimal128(18, 6)
TRADE_SCHEMA = pa.schema(
    [
        ("entry_date", pa.date32()),
        ("exit_date", pa.date32()),
        ("playbook", pa.string()),
        ("instrument_id", pa.string()),
        ("qty", pa.int64()),
        ("entry_price", _MONEY),
        ("initial_stop", _MONEY),
        ("exit_reason", pa.string()),
        ("realized_pnl", _MONEY),
        ("fees", _MONEY),
        ("r_multiple", _R),
        ("mfe_r", _R),
        ("mae_r", _R),
        ("holding_days", pa.int64()),
        ("strategy_version_id", pa.string()),
        ("risk_policy_version_id", pa.string()),
    ]
)


def trades_parquet(trades_: Sequence[TradeRow]) -> bytes:
    """The run's `trades.parquet`, one row per closed trade in exit order (HAT-BT-032)."""
    rows = [
        {
            **{name: getattr(t, name) for name in TRADE_SCHEMA.names},
            "strategy_version_id": str(t.strategy_version_id),
            "risk_policy_version_id": str(t.risk_policy_version_id),
        }
        for t in trades_
    ]
    table = pa.Table.from_pylist(rows, schema=TRADE_SCHEMA)
    buffer = io.BytesIO()
    pq.write_table(table, buffer)
    return buffer.getvalue()


# --- entry point ------------------------------------------------------------------------------


def with_cooldown(policy: PolicyVersion, sessions: int) -> PolicyVersion:
    """`policy` with its `cooldown_sessions` set to `sessions`, the backtest's sweep of the
    per-instrument cooldown (HAT-RISK-036, PR 58). The run records the value it used; a value
    the policy schema refuses raises `BacktestError`."""
    try:
        changed = parse_policy({**policy.policy.to_json(), "cooldown_sessions": sessions})
    except PolicyError:
        raise BacktestError("backtest_bad_cooldown") from None
    return PolicyVersion(
        policy.version_id, policy.version, policy.effective_from, policy.superseded_at, changed
    )


def run_backtest(spec: RunSpec, data: MarketData) -> BacktestResult:
    """Run `spec` over `data` and return its trades, equity curve and metrics."""
    if spec.start > spec.end:
        raise BacktestError("backtest_bad_period")
    if not spec.initial_equity.is_finite() or spec.initial_equity <= 0:
        raise BacktestError("backtest_bad_equity")
    check_conventions(
        spec.entry_fill_rule,
        spec.fills.slippage_bps,
        spec.for_promotion,
        spec.slot_order,
        ambiguous=spec.ambiguous,
        suspension_sessions=spec.suspension_sessions,
    )
    return _Run(spec, data).run()


def check_conventions(
    entry_fill_rule: str,
    slippage_bps: Decimal,
    for_promotion: bool,
    slot_order: str = SLOT_ORDER_TIME,
    *,
    ambiguous: str = AMBIGUOUS_OPTIMISTIC,
    suspension_sessions: int = SUSPENSION_SESSIONS,
) -> None:
    """Refuse a run's fill conventions (PR 62, O5): an unknown entry fill rule or slot order
    (PR 63), an unknown ambiguous-day rule or a bar-less session count that is not a whole
    number in 0..`MAX_SUSPENSION_SESSIONS` (PR 64), and for a run flagged for promotion,
    perfect fills (a slippage of 0), the optimistic `touch` rule, a slot order other than the
    estimated trigger time or a suspension markdown later than `SUSPENSION_SESSIONS`."""
    if entry_fill_rule not in ENTRY_FILL_RULES:
        raise BacktestError("backtest_bad_fill_rule")
    if not is_slot_order(slot_order):
        raise BacktestError("backtest_bad_slot_order")
    if ambiguous not in AMBIGUOUS_RULES:
        raise BacktestError("backtest_bad_ambiguous_rule")
    if (
        isinstance(suspension_sessions, bool)
        or not isinstance(suspension_sessions, int)
        or not 0 <= suspension_sessions <= MAX_SUSPENSION_SESSIONS
    ):
        raise BacktestError("backtest_bad_suspension")
    if for_promotion and slippage_bps == 0:
        raise BacktestError("backtest_promotion_without_slippage")
    if for_promotion and entry_fill_rule != PENETRATION:
        raise BacktestError("backtest_promotion_on_touch")
    if for_promotion and slot_order != SLOT_ORDER_TIME:
        raise BacktestError("backtest_promotion_off_time_order")
    if for_promotion and suspension_sessions > SUSPENSION_SESSIONS:
        raise BacktestError("backtest_promotion_late_suspension")


# --- command line -----------------------------------------------------------------------------


def _slot_order_arg(text: str) -> str:  # pragma: no cover - the command line
    import argparse  # noqa: PLC0415

    if not is_slot_order(text):
        raise argparse.ArgumentTypeError("time, score or random:K (K a whole number)")
    return text


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - needs the database
    """python -m app.hat.engine.backtest --bot <id> --from YYYY-MM-DD --to YYYY-MM-DD
    [--version <id>] [--slippage-bps N] [--cost-model NAME] [--cooldown-sessions N]
    [--entry-fill penetration_1_tick|touch] [--slot-order time|score|random:K]
    [--ambiguous optimistic|pessimistic] [--suspension-sessions N] [--for-promotion]

    Backtests the sleeve's live strategy version (or `--version`) on its universe over the
    Parquet store under `HAT_DATA_ROOT`, records the run as a trial of its family and writes
    `backtests/run_id=<id>/trades.parquet`. Prints the run id, status, trade count and DSR.
    The slippage defaults to 20 bps; `--for-promotion` marks a run `publish_version` may take as
    a version's evidence, and is refused at a slippage of 0 (PR 62). A capped day's slots go in
    the estimated trigger order (`--slot-order time`, PR 63). `--ambiguous pessimistic` fills
    a raised stop the day's low reached; a bar-less position is marked down after
    `--suspension-sessions` sessions (PR 64).
    """
    import argparse  # noqa: PLC0415
    import json  # noqa: PLC0415
    import sys  # noqa: PLC0415

    from app.database import db_session_scope  # noqa: PLC0415

    from ..backtest.runs import RunError, backtest_bot  # noqa: PLC0415
    from ..data.store import DataStore  # noqa: PLC0415
    from ..settings import operator_settings  # noqa: PLC0415

    parser = argparse.ArgumentParser(prog="python -m app.hat.engine.backtest")
    parser.add_argument("--bot", type=uuid.UUID, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--version", type=uuid.UUID, default=None)
    parser.add_argument("--slippage-bps", type=Decimal, default=DEFAULT_SLIPPAGE_BPS)
    parser.add_argument("--cost-model", default=None)
    parser.add_argument("--cooldown-sessions", type=int, default=None)
    parser.add_argument("--entry-fill", choices=ENTRY_FILL_RULES, default=PENETRATION)
    parser.add_argument("--slot-order", type=_slot_order_arg, default=SLOT_ORDER_TIME)
    parser.add_argument("--ambiguous", choices=AMBIGUOUS_RULES, default=AMBIGUOUS_OPTIMISTIC)
    parser.add_argument("--suspension-sessions", type=int, default=SUSPENSION_SESSIONS)
    parser.add_argument("--for-promotion", action="store_true")
    args = parser.parse_args(argv)
    settings = operator_settings()
    extra: dict[str, Any] = {} if args.cost_model is None else {"cost_model": args.cost_model}
    if args.cooldown_sessions is not None:
        extra["cooldown_sessions"] = args.cooldown_sessions
    refused: str | None = None
    summary: dict[str, Any] = {}
    # A refusal is caught inside the session, so a run the engine refused commits as a failed
    # trial of its family instead of rolling back with the error.
    with db_session_scope() as session:
        try:
            run = backtest_bot(
                session,
                DataStore(settings.data_root),
                args.bot,
                args.start,
                args.end,
                root=settings.data_root,
                slippage_bps=args.slippage_bps,
                version_id=args.version,
                entry_fill_rule=args.entry_fill,
                for_promotion=args.for_promotion,
                slot_order=args.slot_order,
                ambiguous=args.ambiguous,
                suspension_sessions=args.suspension_sessions,
                **extra,
            )
        except RunError as exc:
            refused = exc.code
        else:
            summary = {
                "run_id": str(run.id),
                "status": run.status,
                "family_key": run.family_key,
                "trades": run.metrics.get("trades"),
                "expectancy_r": run.metrics.get("expectancy_r"),
                "dsr": run.metrics.get("dsr"),
            }
    if refused is not None:
        sys.stderr.write(f"backtest refused: {refused}\n")
        return 2
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""The backtest day loop (HAT-BT-010..024, HAT-DEC-T12): one engine for backtest and live.

The loop has no strategy, risk or exit logic of its own. Each simulated trade date d runs the
code live trading runs, in the same order:

1. the nightly scan of d's previous session, published as d's watchlist
   (`watchlist.scan_and_publish`, which calls `scan.scan_bot`);
2. open positions: the nightly exit rules at d's open, then the live rules along the day's path
   (`shadow.evaluate_position`, which calls `exits.evaluate`);
3. entries: the watchlist rows and the pre-open playbooks' gap candidates (`_gappers`, as live's
   09:16 gap scan picks them), each decided by the live `TriggerMonitor` on the session's stored
   5-minute bars (`intraday.replay`) or, without them, on the daily estimate below; the stop comes
   from `trigger.entry_stop`, and `entries.open_entry` decides each trigger, in the order they
   fired, with `evaluate_signal` (caps, max chase, sizing) and fills it with the shadow engine's
   virtual fills; each pre-open candidate's path through the EP funnel is logged (`ep_log`,
   `ep_candidates.csv` and the `episodic_pivot` breakdowns of run.json);
4. the 15:05-15:10 close rules at the day's official close, then the session-end expiry of the
   watchlist
   (`shadow.expire_watchlist`).

Those functions work on a database session, so the loop keeps a private ledger (`ledger`): an
in-memory SQLite database holding the run's one bot, its pinned strategy version, its universe
(point in time) and the virtual signals, orders, fills, positions and trade results the engine
writes. It never touches the HAT database and nothing in it reaches a broker.

What the backtest adds is only what live trading gets from the market: prices. Where the store
has a session's 5-minute bars the entry is live's own decision on them; otherwise daily bars
cannot show where in a day a level broke, so a day is read as the path open -> low -> high ->
close (the adverse move first, `app.hat.backtest.fills`): an entry breaks its level on the way
up, a stop is hit on the way down, a profit rule fires at the first tick price on the way up at
which `exits.evaluate` acts, and the close rules see the official close. Fills go through the
cost model (`app.hat.costs`): slippage against the trade, then statutory charges.

Fill conventions (PR 62, HAT-BT-020..022, O5): pessimistic by default. An entry needs the high to
clear the level by one tick and fills at that price (`penetration_1_tick`; `touch` is a variant a
run may choose), a stop gapped through fills at min(open, stop), and the slippage defaults to
20 bps (`DEFAULT_SLIPPAGE_BPS`). A run flagged for promotion (`RunSpec.for_promotion`, the only
kind `publish_version` accepts and the sleeve's evidence reads) is refused at a slippage of 0,
under `touch` or under a slot order other than `time` (`RunConventions`). Every run's
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

Entry timing: a playbook whose level trigger says `entry_timing: daily_close` enters after the
close phase, at the day's close, when the close is above the level (no penetration rule: the close
is the decision and the fill), and with `volume_vs_avg` set only when the day's volume, known at
the close, is at least that multiple of the mean of the `volume_avg_days` sessions before
(`breakout_volume_low`, `breakout_volume_missing` skips); its stop is planned from the whole day's
low and the day's path is behind it. The official close stands in for the price live would act on
before the 15:15 cut-off, and live does not trade this timing: the known gap
`daily_close_entry_not_live` names the playbook, which blocks its promotion.

Slot order (PR 63, HAT-BT-026): live takes a day's triggers in the order they fire, so on a capped
day the first breaks get the open slots. The loop takes the day's watchlist rows in their
estimated trigger order (`slot_order` `time`, the default, `app.hat.backtest.slots`): gap overs
first, then (level - open) / open ascending, ties by score. `score` (the PR 12 order), `rs`
(the signal night's `rs_weighted`, highest first: the S1 book's rule) and `random:K` are
variants; the rule is part of the run's conventions and `config_digest`.

Volume pace: a session decided on 5-minute bars judges an enabled `volume_pace` as live does, on
the pace of the volume up to each bar. The daily estimate cannot: it judges it on the full day's
volume against `vs_avg50` x the mean of the 50 sessions through the previous one
(`volume_pace_low`, `volume_pace_missing` skips), a look-ahead the run records as the known gap
`volume_pace_on_full_day_volume`. Under the `volume_pace` convention `full_day` the 5-minute
sessions take that rule too, the replay deciding the trigger without the pace.

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
and at the period end. All three rules are in the run's conventions. A position in a stock that
stopped trading closes at its last close on its last session (`MarketData.last_traded`,
HAT-BT-012), one held into a demerger whose record date was announced before the last session
before its ex-date closes at that session's close (`MarketData.demerger_exits`, CA8, exit reason
`demerger`), and nothing trades or marks within a few sessions of an open review jump
(`MarketData.skipped`, audit H7), without those sessions counting as a suspension. A name
factor-pending (CA5, `MarketData.pending`) is not scanned that night nor entered that day; a
position held in it keeps its marks, stops and exits. Nor is a name entered on its demerger exit
session (Q5): the factor-pending days then cover it from the ex-date.

Dividends (CA3): the run trades and marks the price basis, the prices a broker shows, and a
position open at the start of an ex-date session earns its dividends that day, `qty_open` times
the cash per share (`MarketData.dividends`, `dividend_credit`), into the account's cash; a buy on
the ex-date earns nothing. The signals read the total-return series (`MarketView`), so the ex-date
drop is neither a breakdown nor a loss: it is paid back as cash, once. The ledger holds the same
dividends (`ledger._seed_dividends`), so a closed trade's P&L and R include the credits it earned
(R-DIV, `positions.write_result`); the metrics count them too (`dividend_income`,
`dividend_credits`).

The run's one risk-policy version (`RunSpec.policy`) is in force over the whole period, from its
first session, whenever the stored version took effect: a backtest of years before the policy
existed sizes under it as if it had always been the policy.

The per-instrument cooldown (PR 58, HAT-RISK-036) is the risk policy's `cooldown_sessions`, which
`evaluate_signal` reads through `entries.open_entry` from the ledger's hard-stop closes, as live
does; a run may replace it (`with_cooldown`, `--cooldown-sessions`) and records the value used.

Known gaps, recorded by fixed code in every run's metrics (`KNOWN_GAPS`, HAT-BT-070) and
measured in shadow, besides the estimates the run took (`ESTIMATE_GAPS`: on a session without
5-minute bars the trigger is the daily high's break of the level, without the opening range,
with `volume_pace` on the full day's volume, and a pre-open playbook's the calibrated
`intraday.EP_ESTIMATE`;
`catalyst_attached` is never judged): close-based exits decide on and fill at the daily close
(the auction price), which stands in for the price live acts on before the 15:15 cut-off (owner
2026-10-25), kill switches are not simulated (the exposure ladder is), a sleeve's health
multiplier is 1 (its regime multiplier too, unless the run applies the policy's), and the run is
one sleeve, so the account-level caps never bind across sleeves.

Sizing (PR 68, HAT-BT-014): each night before a trade date d the loop allocates the run's one sleeve
with the live allocator (`portfolio.allocator.allocate`) over the account's equity at the night's
close, at the run's sleeve weight (`sleeve_weight`, by default the policy's base weight of the
sleeve), and d's entries are sized from that allocation as live sizes them: the sleeve's capital as
equity and the allocation's risk per trade. The free cash an entry is sized on is the account's cash
net of the entry's slippage and charges (`BacktestFills.affordable`), and a buy whose value plus
charges exceeds the cash is refused as the broker refuses it, so cash never goes negative and an
entry after it runs out is a cash rejection (`size_zero_cash`). The regime multiplier is 1
(`regime_mult` `1`, the default; the allocator clamps it to the policy's bounds) or the live regime
engine's (`policy`: v1's inputs, 3-day confirmation and default table, warmed up over the sessions
before the period, then the allocator's smoothing); a night whose regime inputs are missing makes no
allocation, so the next day's entries are rejected for the missing input, as live rejects them.
Percentage metrics are of the whole account, of which the sleeve is `sleeve_weight`; the run records
the weight and the rule beside them (`pct_basis`), and the mean share of the account held in
positions (`invested_share`). R statistics do not depend on either.

An overlap run (`RunConventions.overlap`, backtest only) allocates with the rotation leg left out
of the whole-book cap, so it keeps its weight beside the setup sleeves'. A setup entry sizes on
the free cash plus what selling the rotation's holdings would net at its instant (`_funding_quote`)
and, when it takes more than the free cash, the rotation sells for the rest as it fills
(`RotationBook.fund`); the rotation's next rebalance sizes on `rotation.overlap_capital`.

The command line runs a sleeve's strategy version over the Parquet store and records the run
(`app.hat.backtest.runs`): `python -m app.hat.backtest --bot <id> --from YYYY-MM-DD --to
YYYY-MM-DD`.
"""

from __future__ import annotations

import math
import uuid
from bisect import bisect_left, bisect_right
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from fractions import Fraction
from typing import Any

import polars as pl
from sqlalchemy import select

from ..configs.records import Bot
from ..configs.unit import configs_of
from ..costs import BacktestFills, CostModel
from ..data.bars import Bar as DataBar
from ..data.identity import series_as_of
from ..data.preopen import meets_gap
from ..engine import (
    entries,
    exits,
    fill_0920,
    lifecycle,
    positions,
    rotation,
    shadow,
    trigger,
    watchlist,
)
from ..engine.confidence import BACKTEST_CONFIDENCE
from ..engine.live_entry import EP_UNIVERSE, ep_gate, ep_stock_log, gap_floor, session_open
from ..engine.portfolio import regime as regime_v1
from ..engine.portfolio.allocator import LIVE, Allocation, SleeveInput, allocate, sleeve_key
from ..engine.portfolio.health import Health
from ..engine.portfolio.universe import members_on
from ..engine.regime_gate import regime_allows_longs
from ..engine.scan import SESSIONS_AHEAD, ScanInputs, WindowKey, universe_passers, window_key
from ..engine.stops import SKIP_BAD_INPUT, StopPlan, history_inputs
from ..engine.strategy.config import Playbook, Rotation, StopLowOfDay, StrategyConfig
from ..models import (
    HatPosition,
    HatSignal,
    HatTradeResult,
    HatWatchlistEntry,
)
from ..money import dividend_credit, exact
from ..risk.decision import SignalContext
from ..risk.exposure import LadderState, apply_outcome, is_win
from ..risk.kill import KillState
from ..risk.sessions import SessionCalendar, SessionDay
from ..time import IST, as_utc
from . import ep_log
from .auction_days import auction_risk_days
from .data import (
    HINDSIGHT_DELISTED_EXIT,
    HINDSIGHT_EP_CLOSE,
    HINDSIGHT_EP_VOLUME,
    HINDSIGHT_FULL_DAY_PACE,
    Bar,
    MarketData,
    MarketView,
    industry_on,
    symbol_on,
)
from .fillrules import (
    SKIP_PACE_LOW,
    SKIP_PACE_MISSING,
    breakout_volume_ok,
    close_lower_locked,
    first_trigger,
    full_day_pace_skip,
    inferred_band,
    lower_circuit_locked,
    pre_break_low,
    upper_circuit_locked,
)
from .fills import (
    AMBIGUOUS_PESSIMISTIC,
    PENETRATION,
    SUSPENSION_BAND_PCT,
    entry_fill,
    raised_stop_dip,
    stop_fill,
    suspended_mark,
)
from .intraday import (
    EP_ESTIMATE,
    NO_CATALYST_MODE,
    after_trigger,
    replay,
    session_admitted,
    session_bars,
    trace,
    traded_low_after,
)
from .ledger import exact_flows, fill_rows, fills_on, ledger, set_series
from .ledger import trade_rows as ledger_trade_rows
from .ledger import watch_rows as ledger_watch_rows
from .overlay import (
    INSUFFICIENT_CREDIT,
    EtfData,
    Overlay,
    OverlayError,
    benchmarks,
    refusal_reason,
)
from .reconcile import NO_OVERLAY, Held, OverlayJournal, SessionBook, reconcile
from .report import (
    DELISTED,
    DEMERGER,
    END_OF_PERIOD,
    BacktestResult,
    FillRow,
    SessionRow,
    SignalRow,
    TradeRow,
    WatchRow,
    auction_risk_summary,
    compute_metrics,
    flag_auction_risk,
)
from .rotation import (
    Market,
    Plan,
    Quote,
    RotationBook,
    TrCloses,
    bar_ending_at,
    funding_sale,
    overlap_capital,
)
from .signal_features import SignalFeatures
from .slots import order_slots, trigger_rank
from .spec import (
    ANALYST_BEAT_NOT_EVALUATED,
    BASIS_5M,
    BASIS_CLOSE,
    BASIS_DAILY,
    CATALYST_NOT_EVALUATED,
    ENTRY_BARS_5M,
    ENTRY_DAY_LOW_ESTIMATED,
    EP_TRIGGER_ESTIMATED,
    GAP_OVER_ESTIMATED,
    LEVEL_BREAK_ESTIMATED,
    OVERLAP_FUNDING_ESTIMATED,
    REGIME_MULT_ONE,
    VOLUME_PACE_ESTIMATED,
    VOLUME_PACE_FULL_DAY,
    BacktestError,
    RunSpec,
    sleeve_weight,
)
from .standard import exit_sma, fwd_r, planned_target

__all__ = ["run_backtest"]

SKIP_NO_LEVEL = "no_trigger_level"
SKIP_FACTOR_PENDING = "factor_pending"  # CA5: the day's adjusted history is not point in time
SKIP_GAP_NO_INTRADAY = "gap_over_no_intraday"
SKIP_DEMERGER_PENDING = "demerger_pending"  # the demerger exit session (`data/demerger_notice.py`)
SKIP_LOW_VOLUME = "breakout_volume_low"  # a close above the level on too little volume
SKIP_NO_VOLUME = "breakout_volume_missing"  # the volumes the breakout volume test reads
VOLUME_SKIPS = frozenset({SKIP_LOW_VOLUME, SKIP_NO_VOLUME})


def consumes(reason: str) -> bool:
    """Whether a skip after a row's level broke ends its setup's window (decisions.md "S1
    validity window"): every skip but one on volume or the volume pace, without which live's
    trigger never fires, so live writes no signal either."""
    return reason not in VOLUME_SKIPS and reason not in (SKIP_PACE_LOW, SKIP_PACE_MISSING)


# The day's gates of a watchlist row after the scan's (`scan.funnel_gates`), in order.
TRIGGER_GATES = ("trigger", "breakout_volume", "stop", "max_chase")
MAX_CHASE = "max_chase"
SKIP_LIMIT_NOT_REACHED = "limit_not_reached"  # a 5-minute entry LIMIT nothing traded at
# The sleeve's id in its allocations: the ledger's bot id is new each run, a result's is fixed.
_SANDBOX_SLEEVE = uuid.UUID(int=2)
_ZERO = Decimal(0)
# The trigger types a pre-open playbook's daily estimate stands in for (`EP_ESTIMATE`): the
# opening-range break, and the catalyst no backtest can judge.
_EP_ESTIMATED_TYPES = frozenset({trigger.PRICE_BREAKS_LEVEL, trigger.CATALYST})
# The trigger types the estimate leaves to its own checks: those, and `volume_pace`, which has no
# elapsed share of a session on a daily bar and is judged on the full day (`_full_day_pace`).
_EP_SEPARATE_TYPES = _EP_ESTIMATED_TYPES | {trigger.VOLUME_PACE}
# The estimated trigger times (seconds after midnight IST) of the daily estimate under the `time`
# slot order: a gap over at the first trigger bar's end (09:20, ahead of a 5-minute trigger
# then), a break at 09:30, ordered by its distance, and a pre-open playbook's at 09:35, the
# calibration's median.
_GAP_SECONDS = 9 * 3600 + 20 * 60 - 1
_BREAK_SECONDS = 9 * 3600 + 30 * 60
_EP_SECONDS = 9 * 3600 + 35 * 60
_AT_OPEN_SECONDS = 9 * 3600 + 15 * 60


# The extra acts a day's way up allows: a step trail acts once per step the rise reaches.
_STEP_TRAIL_MOVES = 100
_FINEST_TICK = Decimal("0.01")  # the NSE tick below ₹250 (`data.ticks`), the finest slab
# The instants of a simulated day (IST): a later step of the day has a later instant.
_AT_NIGHT = time(16, 0)
_AT_OPEN = time(9, 15)
_AT_LOW = time(9, 20)
_AT_ENTRY = time(9, 30)
_AT_HIGH = time(10, 0)
# The close phase at live's times (`exits.close` 15:05-15:10, before the 15:15 cut-off), deciding
# and filling on the daily close, which stands in for the price live acts on then (owner
# 2026-10-25, `close_rules_decided_on_official_close`).
_AT_FALL = time(15, 6)  # after the last 5-minute trigger (15:05) and its entry's legs
_AT_CLOSE = time(15, 10)
# A close entry (`entry_timing: daily_close`): after the close phase, before the 15:15 cut-off.
_AT_CLOSE_ENTRY = time(15, 11)
_AT_END = time(15, 14)
# What an overlap run's funding sale was priced from (`_funding_quote`): the 5-minute bar at the
# entry's instant, the daily close at a close entry, the 09:20 price at 09:20, or a stale 09:20.
_FUNDING_BAR, _FUNDING_CLOSE = "bar_5m", "close"
_FUNDING_AT_0920, _FUNDING_0920 = "at_0920", "stale_0920"


@dataclass(frozen=True)
class _History:
    """The `stops.StopHistory` of one instrument over the sessions through `through`."""

    view: MarketView
    instrument_id: str
    through: date

    def atr(self, length: int) -> Decimal | None:
        return self.view.atr(self.instrument_id, self.through, length)

    def lows(self, count: int) -> list[Decimal]:
        bars = self.view.bars_through(self.instrument_id, self.through, count, since_break=True)
        return [b.low for b in bars]


def _at(day: date, moment: time, offset: int = 0) -> datetime:
    """The aware UTC instant of `moment` IST on `day`, plus `offset` seconds."""
    return as_utc(datetime.combine(day, moment, tzinfo=IST) + timedelta(seconds=offset))


@dataclass(frozen=True)
class _EntryPlan:
    """A trigger's entry: price, stop, the day's low after the entry (None: none below the
    break's), the upper-circuit state, the first failing entry filter (None: all passed), the
    level that broke and whether it is a close entry (the day's path is behind it)."""

    price: Decimal
    stop: Decimal
    dip: Decimal | None
    locked: bool | None
    skip: str | None
    level: Decimal
    filters: tuple[dict[str, object], ...] = ()
    at_close: bool = False
    # the highest price after the entry (None: the day's high, the daily estimate's path)
    high: Decimal | None = None
    basis: str = BASIS_DAILY  # what decided it: the session's 5-minute bars or the daily bar


@dataclass(frozen=True)
class _Row:
    """What an entry reads of its candidate: a watchlist row, or a pre-open candidate of the
    day's gap scan, which has no row (score 0, no levels)."""

    playbook: str
    instrument_id: uuid.UUID
    trade_date: date
    score: Decimal = _ZERO
    pivot: Decimal | None = None
    criteria_results: Mapping[str, Any] = field(default_factory=dict)

    @staticmethod
    def of(row: HatWatchlistEntry) -> _Row:
        return _Row(
            row.playbook,
            row.instrument_id,
            row.trade_date,
            Decimal(row.score),
            None if row.pivot is None else Decimal(row.pivot),
            row.criteria_results or {},
        )


@dataclass(frozen=True)
class _Decision:
    """A candidate that triggered on `day`, its entry plan, its daily bar, its rank under the
    `time` slot order (seconds after midnight of its trigger, then the distance to its level)
    and the instant of the trigger on 5-minute bars (None: the daily estimate's)."""

    row: _Row
    plan: _EntryPlan
    bar: Bar
    rank: tuple[int, Decimal]
    at: datetime | None = None
    leg: int = 0  # the strategy it belongs to (`_Run.legs`)


def _estimated_rank(rank: tuple[int, Decimal]) -> tuple[int, Decimal]:
    """A daily estimate's `slots.trigger_rank` as (seconds, distance), so it orders with the
    triggers decided on 5-minute bars."""
    kind, distance = rank
    return (_GAP_SECONDS if kind == 0 else _BREAK_SECONDS), distance


def _has(playbook: Playbook, code: str) -> bool:
    return any(i.enabled and i.type == code for i in playbook.trigger)


def _levels(row: _Row) -> dict[str, Decimal | None]:
    """The setup levels the scan stored with the row (`trigger.watchlist_levels`)."""
    return trigger.watchlist_levels((row.criteria_results or {}).get("levels", {}))


@dataclass
class _Held:
    """What the loop tracks of an open position beyond the ledger: its instrument, entry day and
    the extremes the price made since entry (for the result's MFE and MAE), and the stop's last
    raise on a way up (for an ambiguous day)."""

    instrument_id: str
    entry_day: date
    high: Decimal
    low: Decimal
    playbook: str = ""  # the playbook it entered under: its exit stack (`exits_for`)
    # The day the way up last raised the stop, and the stop before it (PR 64).
    raised: tuple[date, Decimal] | None = None
    leg: int = 0  # the strategy it entered under (`_Run.legs`)


@dataclass
class _Leg:
    """One strategy of the run (the run's own first, then each co-sleeve, `RunSpec.co_sleeves`):
    its config, its bot in the ledger, its weight and regime table, and what the loop keeps per
    strategy: each playbook's configured max chase, its gate by trade date, its nightly
    allocations, its exposure ladder (a bot's own, HAT-RISK-040), its scan's memo, its funnel,
    its EP log and its regime-off nights. `prefix` names its playbooks in a run of more than
    one strategy (`<bot>:`), so their rows and breakdowns never merge."""

    config: StrategyConfig
    bot_id: uuid.UUID
    weight: Decimal
    regime_table: Mapping[str, Decimal]
    prefix: str = ""
    chase: dict[str, Decimal | None] = field(default_factory=dict)
    gate: dict[date, bool] = field(default_factory=dict)
    allocations: dict[date, Allocation | None] = field(default_factory=dict)
    ladder: LadderState = field(default_factory=LadderState)
    scan_memo: dict[Any, Any] = field(default_factory=dict)
    # the setups whose trigger was skipped after the level broke (`_skip`), closed by the scan
    consumed: set[WindowKey] = field(default_factory=set)
    funnel: dict[str, dict[str, int]] = field(default_factory=dict)
    ep_records: dict[tuple[date, str, str], ep_log.EpRecord] = field(default_factory=dict)
    regime_off_nights: int = 0


class _Sleeve:
    """The run's sleeves (one per strategy, `_Leg`), allocated each night together by the live
    allocator (HAT-BT-014): Live, health 1, base weight each strategy's weight (the run's own
    `sleeve_weight`), each its own cluster, at the regime multiplier 1 or, under `policy`, the
    live regime engine's from each strategy's table, each last multiplier carried for the
    smoothing."""

    def __init__(
        self, spec: RunSpec, data: MarketData, days: tuple[date, ...], legs: Sequence[_Leg]
    ) -> None:
        self.spec = spec
        self.days = days
        self.index = {d: n for n, d in enumerate(days)}
        self.breadth = data.breadth
        self.legs = legs
        self.keys = [sleeve_key(leg.config.bot) for leg in legs]
        # an overlap run's rotation sleeve, which the whole-book cap leaves out
        self.overlap = next(
            (
                key
                for key, leg in zip(self.keys, legs, strict=True)
                if spec.conventions.overlap and leg.config.rotation is not None
            ),
            None,
        )
        self.policy = replace(
            spec.policy.policy,
            base_weight={k: leg.weight for k, leg in zip(self.keys, legs, strict=True)},
        )
        self.regime_closes = dict(data.index_closes.get(regime_v1.INDEX_NAME, ()))
        self.vix = dict(data.index_closes.get(regime_v1.VIX_NAME, ()))
        # the index days, as the regime job reads the last `HISTORY_DAYS` of them
        self.index_days = sorted({*self.regime_closes, *self.vix})
        self.confirmation: regime_v1.Confirmation | None = None
        # each sleeve's last allocation's multiplier, which smoothing reads
        self.regime_mult: list[Decimal | None] = [None] * len(legs)
        self.nights = 0  # the sessions the regime has been read for

    def allocate(self, night: date, equity: Decimal) -> list[Allocation] | None:
        """The sleeves' allocations of `night` over `equity`, the account's equity at the
        night's close, in leg order; None when the regime inputs of the night are missing, as
        the live allocation then fails. Under `policy` the regime engine first reads every
        earlier session of the run's store (the warm-up of the 3-day rule and the smoothing),
        each allocated over the initial equity."""
        if self.spec.conventions.regime_mult == REGIME_MULT_ONE:
            return self._allocate([Decimal(1)] * len(self.legs), equity)
        allocation = None
        while self.nights <= self.index[night]:
            day = self.days[self.nights]
            self.nights += 1
            allocation = self._regime_night(
                day, equity if day == night else self.spec.initial_equity
            )
        return allocation

    def _regime_night(self, day: date, equity: Decimal) -> list[Allocation] | None:
        """Read `day`'s regime and allocate at its confirmed state's multiplier, or None."""
        end = bisect_right(self.index_days, day)
        window = self.index_days[max(0, end - regime_v1.HISTORY_DAYS) : end]
        closes = {d: self.regime_closes[d] for d in window if d in self.regime_closes}
        vix = {d: self.vix[d] for d in window if d in self.vix}
        try:
            inputs = regime_v1.compute_inputs(day, closes, vix, self.breadth.get(day))
        except regime_v1.RegimeError:
            return None  # live's regime job fails: the night has no allocation
        self.confirmation = regime_v1.confirm_step(
            self.confirmation, regime_v1.classify_inputs(inputs)
        )
        confirmed = self.confirmation.confirmed
        values = [regime_v1.multiplier_for(confirmed, leg.regime_table) for leg in self.legs]
        return self._allocate(values, equity)

    def _allocate(self, table_values: Sequence[Decimal], equity: Decimal) -> list[Allocation]:
        found = allocate(
            [
                SleeveInput(
                    bot_id=uuid.UUID(int=_SANDBOX_SLEEVE.int + n),
                    sleeve=key,
                    cluster=key,
                    lifecycle_state=LIVE,
                    regime_table_value=value,
                    health=Health(Decimal(1), probation=False),
                    previous_regime_mult=self.regime_mult[n],
                )
                for n, (key, value) in enumerate(zip(self.keys, table_values, strict=True))
            ],
            self.policy,
            equity,
            overlap=self.overlap,
        )
        self.regime_mult = [a.regime_mult for a in found]  # one per sleeve, in their order
        return found


class _Run:
    """One run of the day loop over `MarketData` (see the module docstring)."""

    def __init__(self, spec: RunSpec, data: MarketData, etf: EtfData | None = None) -> None:
        self.spec = spec
        self.data = data
        self.policy = spec.policy.policy
        # The run's policy version is in force over the whole period: the version in force now
        # may have taken effect after the period starts (on a fresh install, `seed-policy` makes
        # v1 effective today), and `open_entry` decides under the version in force at the
        # trigger. Its id, and so the recorded `risk_policy_version_id`, are unchanged.
        self.policy_version = replace(
            spec.policy,
            effective_from=as_utc(datetime.combine(spec.start, time.min, IST)),
            superseded_at=None,
        )
        self.view = MarketView(data, spec.config.regime.index)
        self.days, self.index = self.view.days, self.view.index
        self.period = tuple(d for d in self.days if spec.start <= d <= spec.end)
        if not self.period:
            raise BacktestError("backtest_empty_period")
        self.session, bot_ids = ledger(spec, data)
        sleeves = [
            (spec.config, sleeve_weight(spec), spec.regime_table),
            *((c.config, c.weight, c.regime_table) for c in spec.co_sleeves),
        ]
        many = len(sleeves) > 1
        self.legs = [
            _Leg(
                config,
                bot_id,
                weight,
                table,
                prefix=f"{config.bot}:" if many else "",
                # each playbook's configured max chase, as the trigger reads it (HAT-CRIT-122)
                chase={
                    b.name: trigger.last_configured_chase([config], b.name)
                    for b in config.playbooks
                },
            )
            for (config, weight, table), bot_id in zip(sleeves, bot_ids, strict=True)
        ]
        self.leg = self.legs[0]  # the strategy the entry phase is deciding (`_use`)
        versions = [spec.strategy_version_id, *(c.strategy_version_id for c in spec.co_sleeves)]
        # each rotation strategy's book (`rotation`), by leg: it trades outside the ledger
        # the account's (day, instrument) sales so far: one DP charge per scrip per sell day
        self.dp_sold: set[tuple[date, str]] = set()
        self.books = {
            n: RotationBook(
                leg.config,
                versions[n],
                spec.policy.version_id,
                spec.fills,
                leg.prefix,
                self.dp_sold,
            )
            for n, leg in enumerate(self.legs)
            if leg.config.rotation is not None
        }
        self.tr_closes = TrCloses(data.bars, self.days) if self.books else None
        # an overlap run's rotation book, which funds the setup entries, and per setup position
        # it funded the share of its entry cost the sales covered; its funding sales by what
        # their price was read from (`_FUNDING_BAR`, ...)
        self.funder = next(iter(self.books.values()), None) if spec.conventions.overlap else None
        self.funded: dict[uuid.UUID, Decimal] = {}
        self.funding_fills: dict[str, int] = {}
        self._funding_sources: dict[tuple[str, date], str] = {}
        self._funding_bars: tuple[date | None, dict[str, tuple[DataBar, ...]]] = (None, {})
        self.rotation_dividends = _ZERO
        # each rotation fill by what its 09:20 price was read from (`fill_0920.REAL`, `ESTIMATED`)
        self.rotation_fills: dict[str, int] = {}
        self._fill_sources: dict[tuple[str, date], str] = {}
        self._firsts_day: date | None = None
        self._firsts: dict[str, DataBar | None] = {}
        # the account's equity each night's allocation read: a portfolio run's global open-risk
        # cap is of it, as live's (`portfolio.jobs`); a one-strategy run keeps its sleeve's
        # capital there (the known gap `single_sleeve_caps`)
        self.account_equity: dict[date, Decimal] = {}
        # the ETF overlay (`overlay`), off unless the run has one
        self.overlay = (
            None
            if spec.overlay is None
            else Overlay(spec.overlay, _etf_data(etf), spec.fills.schedule)
        )
        if self.overlay is not None:  # the sleeve is bought at the first session's open
            self.overlay.clock(self.view.as_of)
            self.overlay.plan = self.overlay.first_plan(self.period[0], spec.initial_equity)
        self.calendar = SessionCalendar(
            {
                d: SessionDay(
                    True, _at(d, _AT_OPEN), as_utc(datetime.combine(d, time(15, 30), IST))
                )
                for d in self.period
            }
        )
        self.held: dict[uuid.UUID, _Held] = {}
        self.seen_results: set[uuid.UUID] = set()
        self.closed: date | None = None  # the last session whose close phase has run
        self.ambiguous_days: set[date] = set()
        self.lower_circuit_days: set[date] = set()
        self.dividend_cash = _ZERO  # the dividends the held positions earned (CA3)
        # the ledger reconciliation's inputs (`reconcile`): each session's book and the cash
        # dividends credited since the last mark
        self.books_log: list[SessionBook] = []
        self.day_dividends = _ZERO
        self.dividend_credits = 0
        self.equity: list[tuple[date, Decimal]] = []
        self.skips: dict[str, int] = {}
        # every skip and funnel count with its session, (day, kind, key): the truncation check's
        self.decisions: list[tuple[date, str, str]] = []
        self.today: date | None = None
        self.signal_rows: list[SignalRow] = []
        self.invested: list[Decimal] = []
        # The sleeves' nightly allocations (HAT-BT-014), by the trade date they size.
        self.sleeve = _Sleeve(spec, data, self.days, self.legs)
        # what the result files report (`folder`): each session's book, each signal's features
        self.sessions: list[SessionRow] = []
        self.signal_features = SignalFeatures(data, self.view)
        # the estimate gaps the run took (`spec.ESTIMATE_GAPS`) and its entries by year and basis
        self.estimated: set[str] = set()
        self.entry_bars: dict[int, dict[str, int]] = {}
        self.night_window: tuple[date, pl.DataFrame] | None = None  # the last scan's bars
        # the NIFTY 500's closes the scan's relative correction reads, a cache the scan keeps
        # across the nights (`EvalContext.memo`), and the funnel: per playbook the stock-days
        # (scan) and watchlist rows (day loop) that passed each gate
        self.benchmark = [(d, float(c)) for d, c in data.index_closes.get(regime_v1.INDEX_NAME, ())]
        self.benchmark_days = [d for d, _ in self.benchmark]

    # --- the strategy being decided: the active leg's (`_use`) -----------------------------------

    def _use(self, leg: _Leg) -> None:
        self.leg = leg

    @property
    def config(self) -> StrategyConfig:
        return self.leg.config

    @property
    def bot_id(self) -> uuid.UUID:
        return self.leg.bot_id

    @property
    def chase(self) -> dict[str, Decimal | None]:
        return self.leg.chase

    @property
    def gate(self) -> dict[date, bool]:
        return self.leg.gate

    @property
    def allocations(self) -> dict[date, Allocation | None]:
        return self.leg.allocations

    @property
    def ladder(self) -> LadderState:
        return self.leg.ladder

    @property
    def funnel(self) -> dict[str, dict[str, int]]:
        return self.leg.funnel

    @property
    def ep_records(self) -> dict[tuple[date, str, str], ep_log.EpRecord]:
        """The EP log: each pre-open candidate-day of the gap scan (`ep_log`)."""
        return self.leg.ep_records

    def _leg_of(self, position: HatPosition) -> _Leg:
        return self.legs[self.held[position.id].leg]

    def run(self) -> BacktestResult:
        for day in self.period:
            self.view.open(day)
            self.today = day
            prev = self.view.prev(day)
            if prev is not None:
                for leg in self.legs:
                    if leg.config.rotation is None:  # a rotation has no watchlist
                        self._use(leg)
                        self._publish(prev, day)
                account = self._equity_now()[0]
                self.account_equity[day] = account
                allocations = self.sleeve.allocate(prev, account)
                for n, leg in enumerate(self.legs):
                    leg.allocations[day] = None if allocations is None else allocations[n]
            if self.overlay is not None:
                self.overlay.open_session(day, self._free_cash())
            self._morning(day, prev)
            self._rotate(day, prev)
            self._sync_ladder()
            if prev is not None:
                self._entries(day, prev)
            self.view.close(day)  # the close phase decides on the daily close (decisions.md)
            self._close_phase(day)
            if prev is not None:
                self._entries(day, prev, at_close=True)
            self._demergers(day)
            self._delistings(day)
            self._sync_ladder()
            shadow.expire_watchlist(self.session, day)
            if self.overlay is not None and day != self.period[-1]:
                self._overlay_close(day, prev)
            self._mark(day)
        self._finish(self.period[-1])
        self.view.release()
        return self._result()

    def _publish(self, prev: date, day: date) -> None:
        series = series_as_of(self.data.series, prev)
        set_series(self.session, series)
        self.night_window = (prev, self.view.window(prev))
        inputs = ScanInputs(
            as_of=prev,
            members=(),  # replaced by the bot's universe on `prev`
            bars=self.night_window[1],
            regime_closes=self.view.closes_through(prev),
            breadth=self.view.breadth_through(prev),
            features=self.view.features_on(prev),
            series=series,
            benchmark=self.benchmark[: bisect_right(self.benchmark_days, prev)],
            memo=self.leg.scan_memo,
            triggered=frozenset(self.leg.consumed),
            # the calendar's sessions from `day` on (a validity window's last session)
            sessions_ahead=self.days[bisect_left(self.days, day) :][:SESSIONS_AHEAD],
        )
        published = watchlist.scan_and_publish(
            self.session, self.bot_id, inputs, day, _at(prev, _AT_NIGHT)
        )
        self.gate[day] = published.gate_on
        if not published.gate_on and published.skipped is None:
            self.leg.regime_off_nights += 1
        for playbook, counts in published.funnel.items():
            mine = self.funnel.setdefault(playbook, {})
            for gate, count in counts.items():
                mine[gate] = mine.get(gate, 0) + count
                self.decisions.append((day, "funnel", f"{playbook}:{gate}:{count}"))

    def _passed(self, playbook: str, gate: str) -> None:
        """One watchlist row of `playbook` passed the day's `gate` (`TRIGGER_GATES`)."""
        mine = self.funnel.setdefault(playbook, {})
        mine[gate] = mine.get(gate, 0) + 1
        if self.today is not None:
            self.decisions.append((self.today, "funnel", f"{playbook}:{gate}:1"))

    def _open_positions(self) -> list[HatPosition]:
        """Every strategy's open positions, by entry day and instrument."""
        bots = [leg.bot_id for leg in self.legs]
        rows = self.session.scalars(
            select(HatPosition).where(HatPosition.bot_id.in_(bots), HatPosition.closed_at.is_(None))
        ).all()
        return sorted(
            rows, key=lambda p: (self.held[p.id].entry_day, self.held[p.id].instrument_id)
        )

    def _evaluate(
        self, position: HatPosition, view: exits.ExitView, phase: exits.Phase, at: datetime
    ) -> None:
        held = self.held[position.id]
        shadow.evaluate_position(
            self.session,
            position.id,
            view,
            phase,
            at,
            high=held.high,
            low=held.low,
            fills=AccountFills(self.spec.fills, self.dp_sold, held.instrument_id),
        )

    def _down_leg(self, position: HatPosition, bar: Bar, at: datetime) -> None:
        """The way down from the open to the low (at `at`): the stop fills at min(open, stop).
        A position the stop closes never saw the rest of the fall, so its excursion stops at the
        fill."""
        held = self.held[position.id]
        if position.closed_at is not None:
            return
        fill = stop_fill(bar.open, bar.low, Decimal(position.current_stop))
        if fill is not None:
            held.low = min(held.low, fill)
            self._evaluate(position, exits.ExitView(fill), "live", at)
        if position.closed_at is None:
            held.low = min(held.low, bar.low)

    def _up_leg(
        self, position: HatPosition, low: Decimal, high: Decimal, day: date, at: datetime
    ) -> None:
        """The way up to the high, from `at`: each profit rule fills at the first tick price at
        which `exits.evaluate` acts (a millisecond apart), as many times as the stack keeps
        acting. A position closed on the way up never saw the rest of the rise, so its excursion
        stops at the fill."""
        held = self.held[position.id]
        before = Decimal(position.current_stop)
        # A held instrument always has the closes before its entry; the finest NSE tick stands
        # in only if it did not, which keeps the search exact on a finer grid.
        tick = self.view.tick(held.instrument_id, day) or _FINEST_TICK
        stack = self.legs[held.leg].config.exits_for(held.playbook)
        for step in range(len(stack) + 2 + _STEP_TRAIL_MOVES):
            if position.closed_at is not None:
                return
            rules, state = positions.position_state(self.session, position)
            price = first_trigger(rules, state, low, high, tick)
            if price is None:
                break
            held.high = max(held.high, price)
            step_at = at + timedelta(milliseconds=step)
            self._evaluate(position, exits.ExitView(price), "live", step_at)
            low = price
        if position.closed_at is None:
            held.high = max(held.high, high)
            if Decimal(position.current_stop) > before:
                held.raised = (day, before)

    def _top_signal(self, held: _Held, prev: date | None) -> bool:
        """Whether the held stack's `top_signal` rule saw its signal on `prev`'s close
        (`exits.top_signal_seen`, on the closes and volumes through it)."""
        rules = self.legs[held.leg].config.exits_for(held.playbook)
        count = exits.top_signal_sessions(rules)
        if prev is None or count == 0:
            return False
        bars = self.view.bars_through(held.instrument_id, prev, count)
        volumes = self.view.volumes_through(held.instrument_id, prev, count)
        return exits.top_signal_seen(rules, [b.close for b in bars], volumes)

    def _morning(self, day: date, prev: date | None) -> None:
        regime_off = [
            prev is not None
            and not regime_allows_longs(
                leg.config.regime, self.view.closes_through(prev), self.view.breadth_through(prev)
            )
            for leg in self.legs
        ]
        for position in self._open_positions():
            held = self.held[position.id]
            # Held into the ex-date: the shares earn the dividend, a session or not.
            per_share = self.view.dividends_on(held.instrument_id, day)
            if per_share and position.qty_open > 0:
                credit = dividend_credit(position.qty_open, per_share)
                self.dividend_cash += credit
                self.day_dividends += credit
                self.dividend_credits += 1
                if self.overlay is not None:
                    self.overlay.dividend(day, credit)
            bar = self.view.bar(held.instrument_id, day)
            if bar is None:
                continue  # no session for this instrument today: nothing can fill
            held.high, held.low = max(held.high, bar.open), min(held.low, bar.open)
            if self._lower_locked(held.instrument_id, day, bar):
                # Pinned at the lower band all day: no buyer, so nothing sells; a stop waits for
                # the next open that is not pinned and fills there at min(open, stop).
                self.lower_circuit_days.add(day)
                continue
            nightly = exits.ExitView(
                bar.open,
                regime_off=regime_off[held.leg],
                top_signal=self._top_signal(held, prev),
            )
            self._evaluate(position, nightly, "nightly", _at(day, _AT_OPEN))
            if position.closed_at is None:
                self._evaluate(position, exits.ExitView(bar.open), "live", _at(day, _AT_OPEN, 1))
            self._down_leg(position, bar, _at(day, _AT_LOW))
            self._up_leg(position, bar.open, bar.high, day, _at(day, _AT_HIGH))

    # --- rotation (`rotation`) ----------------------------------------------------------------

    def _rotate(self, day: date, prev: date | None) -> None:
        """Each rotation book's open (`_rotate_book`), in leg order."""
        for n, book in self.books.items():
            self._rotate_book(self.legs[n], book, day, prev)

    def _rotate_book(self, leg: _Leg, book: RotationBook, day: date, prev: date | None) -> None:
        """A rotation book's open: its dividends, its pending sales, and the rebalance its
        `prev` close decided, on the sleeve's allocation of that night (the account's equity of
        that close times the allocator's final weight, after the regime multiplier and the caps,
        as every leg is sized); a night without an allocation makes no rebalance, as it rejects
        the other legs' entries. Buys the cash cannot pay draw on the overlay's same-day credit,
        sold as they fill."""
        credit = book.dividends(lambda i: self.view.dividends_on(i, day))
        if credit:
            self.rotation_dividends += credit
            self.day_dividends += credit
            self.dividend_credits += 1
            if self.overlay is not None:
                self.overlay.dividend(day, credit)
        book.see(lambda i: None if (b := self.view.bar(i, day)) is None else (b.high, b.low))
        pending = [n for n, h in book.holdings.items() if h.exit is not None]
        if pending:
            self._prefetch_first(pending, day)
        book.sell_pending(day, lambda i: self._quote(i, day, "sell"), self._symbol(day))
        rules = leg.config.rotation
        if prev is None or rules is None:
            self._count_fills(book, day)
            return
        if not rotation.is_rebalance(prev, day, rules.rebalance):
            if rules.scale_down is not None and not book.scaled and self._scale_down(rules, prev):
                self._prefetch_first(list(book.holdings), day)
                book.scale_down(
                    day,
                    prev,
                    rotation.capital_share(rules, True),
                    lambda i: self._quote(i, day, "sell"),
                    self._symbol(day),
                )
            self._count_fills(book, day)
            return
        allocation = leg.allocations.get(day)
        if allocation is None:
            self._count_fills(book, day)
            return
        plan = self._rotation_plan(book, leg, prev, day)
        fires = rules.scale_down is not None and self._scale_down(rules, prev)
        book.scaled = fires
        funded = _at(day, _AT_LOW)  # the open's trades: the overlay sells at its 09:15 bar
        overlay = self.overlay

        def cash() -> Decimal:
            credit = _ZERO if overlay is None else overlay.credit_for(day, funded)
            return self._free_cash() + credit

        market = Market(
            # sized on the price at 09:20 (the buy side's, `rotation.fill_0920`), as traded
            opens={
                name: price
                for name in {*plan.target, *book.holdings}
                if (bar := self.view.bar(name, day)) is not None
                and (price := self._sizing_price(name, day, bar)) is not None
            },
            sell=lambda i: self._quote(i, day, "sell"),
            buy=lambda i: self._quote(i, day, "buy"),
            cash=cash,
            symbol=self._symbol(day),
            entry=lambda i, qty, price, _rank, score: self._rotation_signal(
                leg, i, day, prev, qty, price, score
            ),
        )
        # the allocator's weight over the equity it allocated (`allocation.capital` unrounded)
        equity = self.account_equity[day]
        capital = equity * allocation.final_weight
        if book is self.funder:  # less what its sales still fund (`overlap_capital`)
            capital = overlap_capital(allocation.final_weight, equity, self._funded_open(day))
        share = rotation.capital_share(rules, fires)  # the scale-down's fraction when it fires
        book.rebalance(day, plan, capital * share, market)
        self._count_fills(book, day)
        if overlay is not None and self._free_cash() < 0:
            overlay.fund(day, funded, -self._free_cash())

    def _scale_down(self, rules: Rotation, prev: date) -> bool:
        """Whether the rotation's scale-down fires on `prev`'s close: its checks on the run's
        regime index closes and the breadth through that close, as the regime gate reads them."""
        if rules.scale_down is None:  # pragma: no cover - the callers check
            return False
        return rotation.scale_down_fires(
            rules.scale_down, self.view.closes_through(prev), self.view.breadth_through(prev)
        )

    def _rotation_plan(self, book: RotationBook, leg: _Leg, prev: date, day: date) -> Plan:
        """The rebalance `prev`'s close decides for trade date `day` (`RotationBook.plan`): the
        members of `day` (the scan's rule, `scan_night_members`) that pass the universe stage on
        `prev`, scored on their total-return closes through `prev`."""
        rules = leg.config.rotation
        if rules is None or self.tr_closes is None:  # pragma: no cover - a rotation leg's book
            raise BacktestError("backtest_not_a_rotation")
        inputs = ScanInputs(
            as_of=prev,
            members=sorted(str(m) for m in members_on(self.data.members, day)),
            bars=self.view.window(prev),
            regime_closes=self.view.closes_through(prev),
            breadth=self.view.breadth_through(prev),
            series=series_as_of(self.data.series, prev),
            memo=leg.scan_memo,
        )
        passers = universe_passers(leg.config, inputs)
        self._prefetch_first([*passers, *book.holdings], day)  # their prices at 09:20
        self.view.as_of(prev)  # the total-return closes through `prev`, known at its close
        closes = self.tr_closes.window(passers, self.index[prev], rotation.window_rows(rules))
        gate_on = regime_allows_longs(leg.config.regime, inputs.regime_closes, inputs.breadth)
        leg.gate[day] = gate_on
        return book.plan(
            prev,
            passers,
            closes,
            pending=self.view.pending_on(prev),
            gate_on=gate_on,
            can_buy=lambda i: self._quote(i, day, "buy")[0] is not None,
            industry=lambda i: industry_on(self.data, i, day),
        )

    def _quote(self, instrument_id: str, day: date, side: str) -> Quote:
        """The price a rotation order in `instrument_id` fills at on `day` at 09:20
        (`fill_0920.fill_0920`: the 09:15 bar's close where its five-minute session may be read,
        `_first_bar`, else the open moved by the median to 09:20), else None and why not: no
        bar (`no_bar`), a sale pinned at the lower band (`lower_circuit`: the 09:15 bar, else
        the whole day), a buy at the upper band (`upper_circuit`: the 09:15 bar's close at its
        high and the band, else the open at the day's high; an unknown circuit state too, live
        fails closed), a buy on the name's demerger exit session or factor-pending day
        (`demerger_pending`, `factor_pending`). What the price was read from is kept for the
        run's counts (`_fill_sources`)."""
        bar = self.view.bar(instrument_id, day)
        if bar is None:
            return None, "no_bar"
        first = self._first_bar(instrument_id, day)
        price, source = fill_0920.fill_0920(
            day, side, None if first is None else (first,), bar.open, low=bar.low, high=bar.high
        )
        self._fill_sources[(instrument_id, day)] = source
        if side == "sell":
            if self._lower_locked_0920(instrument_id, day, bar, first):
                return None, "lower_circuit"
            return price, None
        if (instrument_id, day) in self.data.demerger_exits:
            return None, SKIP_DEMERGER_PENDING
        if instrument_id in self.view.pending_on(day):
            return None, SKIP_FACTOR_PENDING
        locked = (
            self._locked(instrument_id, bar.open, bar.high, day)
            if first is None
            else self._locked(instrument_id, first.close, first.high, day)
        )
        if locked is not False:
            return None, "upper_circuit"
        return price, None

    def _sizing_price(self, instrument_id: str, day: date, bar: Bar) -> Decimal:
        """The price a rebalance sizes `instrument_id` on: its buy-side price at 09:20."""
        first = self._first_bar(instrument_id, day)
        bars = None if first is None else (first,)
        return fill_0920.fill_0920(day, "buy", bars, bar.open, low=bar.low, high=bar.high)[0]

    def _lower_locked_0920(
        self, instrument_id: str, day: date, bar: Bar, first: DataBar | None
    ) -> bool:
        """Whether a sale at 09:20 finds the name locked at its lower band: its 09:15 bar closing
        at its low at the band (`close_lower_locked`, as a buy is refused on that bar's close at
        its high at the upper band), else (no five-minute session) the whole day pinned there
        (`_lower_locked`)."""
        if first is None:
            return self._lower_locked(instrument_id, day, bar)
        if first.close != first.low:
            return False  # the common case, without reading the tick
        prev_close = self.view.prev_close(instrument_id, day)
        tick = self.view.tick(instrument_id, day)
        return close_lower_locked(first.close, first.low, prev_close, tick) is True

    def _first_bar(self, instrument_id: str, day: date) -> DataBar | None:
        """The 09:15 five-minute bar of `instrument_id` on `day` on the run's price basis, when
        its session may be read (`_session_bars`: it fits its daily bar, lacks no refused
        bucket, lies in no coverage gap); None otherwise. Read once per name and day."""
        if self._firsts_day != day:
            self._firsts_day, self._firsts = day, {}
        if instrument_id not in self._firsts:
            self._prefetch_first({instrument_id}, day)
        return self._firsts.get(instrument_id)

    def _prefetch_first(self, names: Collection[str], day: date) -> None:
        """Read the 09:15 bars of `names` on `day` in one pass of the day's file."""
        if self._firsts_day != day:
            self._firsts_day, self._firsts = day, {}
        wanted = {n for n in names if n not in self._firsts}
        if not wanted:
            return
        sessions = self._session_bars(day, wanted)
        for name in wanted:
            found = fill_0920.price_0920(day, sessions.get(name, ()))
            self._firsts[name] = None if found is None else found[1]

    def _count_fills(self, book: RotationBook, day: date) -> None:
        """Count the book's fills of `day`'s 09:20 by what their price was read from."""
        for fill in book.fills_on(day):
            source = self._fill_sources.get((fill.instrument_id, day))
            if source is not None:
                self.rotation_fills[source] = self.rotation_fills.get(source, 0) + 1

    def _symbol(self, day: date) -> Any:  # noqa: ANN401 - a callable
        return lambda instrument_id: symbol_on(self.data, instrument_id, day)

    def _rotation_signal(
        self,
        leg: _Leg,
        instrument_id: str,
        day: date,
        prev: date,
        qty: int,
        price: Decimal,
        score: Decimal | None,
    ) -> SignalRow:
        """A rotation trade's entry as a taken signal (the result files' features): no stop."""
        open_count, open_risk = self._book()
        return SignalRow(
            trade_date=day,
            playbook=self.books[self.legs.index(leg)].playbook,
            instrument_id=instrument_id,
            state=lifecycle.OPEN,
            reason=None,
            trigger_price=price,
            stop_price=_ZERO,
            qty=qty,
            strategy_version_id=self.books[self.legs.index(leg)].version,
            risk_policy_version_id=self.spec.policy.version_id,
            entry_price=price,
            scan_date=prev,
            open_positions=open_count,
            open_risk=open_risk,
            equity=self._equity_now()[0],
            regime_on=leg.gate.get(day),
            breadth_pct=self.view.breadth_on(prev),
            features=self.signal_features.at(
                instrument_id,
                day,
                prev,
                entry=price,
                stop=_ZERO,
                score=score,
                pivot=None,
                criteria_results=None,
            ),
        )

    def _funded_open(self, day: date) -> list[tuple[Decimal, int, Decimal]]:
        """Each setup position the rotation funded: (the share of its entry cost the sales
        covered, the shares still open, its mark on `day`, `_mark_price`); a closed position is
        dropped."""
        out: list[tuple[Decimal, int, Decimal]] = []
        for position_id, share in list(self.funded.items()):
            position = self.session.get(HatPosition, position_id)
            if position is None or position.closed_at is not None:
                del self.funded[position_id]
                continue
            mark = self._mark_price(str(position.instrument_id), day, Decimal(position.avg_entry))
            out.append((share, position.qty_open, mark))
        return out

    def _funding_quote(self, instrument_id: str, day: date, at: datetime) -> Quote:
        """The price an overlap run's funding sale of a rotation holding fills at for a setup
        entry at `at`, never before 09:20: blocked as a rotation sale is (`_quote`), else at a
        close entry the daily close, else the close of the name's 5-minute bar that ended in the
        five minutes up to then where its session may be read, each blocked when that bar
        closed locked at the lower band (`funding_sale`), else the 09:20 price (stale
        after 09:20, `OVERLAP_FUNDING_ESTIMATED`). What the price was read from is kept for
        `funding_fills`."""
        price, blocked = self._quote(instrument_id, day, "sell")
        if price is None:
            return None, blocked
        key = (instrument_id, day)
        bar = self.view.bar(instrument_id, day)
        prev_close = self.view.prev_close(instrument_id, day)
        tick = self.view.tick(instrument_id, day)
        if at.astimezone(IST).time() >= _AT_CLOSE and bar is not None:
            self._funding_sources[key] = _FUNDING_CLOSE
            return funding_sale(bar.close, bar.low, prev_close, tick)
        when = max(at, _at(day, _AT_LOW))
        if self._funding_bars[0] != day and self.funder is not None:
            self._funding_bars = (day, self._session_bars(day, set(self.funder.holdings)))
        found = bar_ending_at(self._funding_bars[1].get(instrument_id, ()), when)
        if found is not None:
            self._funding_sources[key] = _FUNDING_BAR
            return funding_sale(found.close, found.low, prev_close, tick)
        self._funding_sources[key] = _FUNDING_0920 if when > _at(day, _AT_LOW) else _FUNDING_AT_0920
        return price, None

    def _rotation_mark(self, day: date) -> Any:  # noqa: ANN401 - a callable
        return lambda instrument_id, fallback: self._mark_price(instrument_id, day, fallback)

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

    def _skip(self, reason: str, row: _Row | None = None) -> None:
        """Count a skip; with the `row` whose level broke, a skip that ends its setup's window
        (`consumes`) is kept for the next nights' scans (`ScanInputs.triggered`), as live's
        signal of the skipped trigger closes it."""
        key = (
            None
            if row is None
            else window_key(row.playbook, str(row.instrument_id), row.criteria_results)
        )
        if key is not None and consumes(reason):
            self.leg.consumed.add(key)
        self.skips[reason] = self.skips.get(reason, 0) + 1
        if self.today is not None:
            self.decisions.append((self.today, "skip", reason))

    def _daily(
        self,
        playbook: Playbook,
        instrument_id: str,
        day: date,
        prev: date,
        bar: Bar | None = None,
    ) -> trigger.DailyInputs:
        """The nightly inputs of an entry on `day` as live reads them (`DailyData.daily`): the
        previous close, the day's tick, the mean volume of the 50 sessions through `prev` (None
        with fewer, or one missing), ADV(20) and ADTR(20) as of `prev`, and the ATR and swing
        lows of the playbook's own stop rule over the sessions through `prev`. With the day's
        `bar` (a trigger replayed on 5-minute bars) the price band is the one its high reached
        (`fillrules.inferred_band`): the run has no band table."""
        atr, swing = history_inputs(playbook.stop, _History(self.view, instrument_id, prev))
        recent = [
            exact(v, floats=True)
            for v in self.view.volumes_through(instrument_id, prev, trigger.AVG_VOLUME_DAYS)
        ]
        avg50 = (
            sum((v for v in recent if v is not None), _ZERO) / trigger.AVG_VOLUME_DAYS
            if len(recent) == trigger.AVG_VOLUME_DAYS and None not in recent
            else None
        )
        prev_close = self.view.prev_close(instrument_id, day)
        tick = self.view.tick(instrument_id, day)
        return trigger.DailyInputs(
            prev_close=prev_close,
            avg50_volume=avg50,
            adv20=self.view.feature(prev, instrument_id, "adv20"),
            adtr20=self.view.feature(prev, instrument_id, "adtr20"),
            tick=tick,
            band_known=bar is not None,
            band_pct=None if bar is None else inferred_band(bar.high, prev_close, tick),
            atr=atr,
            swing_lows=tuple(swing),
        )

    def _stop(
        self,
        playbook: Playbook,
        row: _Row,
        daily: trigger.DailyInputs,
        entry: Decimal,
        low: Decimal,
    ) -> StopPlan:
        """The initial stop for an entry at `entry` (HAT-BOT-062), planned as live plans it
        (`trigger.entry_stop`): `low` is the day's low before the entry."""
        contraction = _levels(row).get(trigger.CONTRACTION_LOW)
        return trigger.entry_stop(playbook, daily, entry, (low,), contraction)

    def _equity_now(self) -> tuple[Decimal, Decimal]:
        """(equity at the last close, free cash now)."""
        cash = self._cash()
        equity = self.equity[-1][1] if self.equity else self.spec.initial_equity
        return equity, cash

    def _context(
        self, row: _Row, plan: _EntryPlan, prev: date, credit: Decimal = _ZERO
    ) -> SignalContext:
        """The entry's context as live builds it (`daily.entries`): sized from the night's
        allocation, the book being the run's one sleeve; without one, the allocation inputs are
        missing and the entry is rejected for it. Under the overlay the free cash counts what
        selling the sleeve would credit today (`credit`)."""
        # The free cash net of the entry's slippage and charges: the cash the shares it can
        # afford take at the entry price, so no size the cash term allows overdraws the account
        # (a broker refuses that order); with no cash left the entry is a cash rejection.
        free = self._free_cash() + credit
        cash = self.spec.fills.affordable(free, plan.price, row.trade_date) * plan.price
        allocation = self.allocations.get(row.trade_date)
        capital = None if allocation is None else allocation.capital
        instrument_id = str(row.instrument_id)
        return SignalContext(
            action="entry",
            strategy_key=self.config.bot,
            industry=industry_on(self.data, instrument_id, row.trade_date),
            entry_price=plan.price,
            stop_price=plan.stop,
            trigger_level=plan.level,
            locked_upper_circuit=plan.locked,
            configured_max_chase_pct=self.chase[row.playbook],
            equity=capital,
            allocation_risk_pct=None if allocation is None else allocation.risk_pct,
            regime_mult=None if allocation is None else allocation.regime_mult,
            health_mult=None if allocation is None else allocation.health_mult,
            free_cash=cash,
            adv20=self.view.feature(prev, instrument_id, "adv20"),
            total_equity=(
                self.account_equity.get(row.trade_date, capital)
                if len(self.legs) > 1 and capital is not None
                else capital
            ),
            policy_versions=[self.policy_version],
            ladder=self.ladder,
            gate_on=self.gate.get(row.trade_date, False),
            feed_stale=False,
            quality_failed=False,
            kill=KillState(),
            calendar=self.calendar,
        )

    def _rows(self, day: date, *, at_close: bool) -> list[_Row]:
        """The day's watchlist rows of the intraday-break playbooks, or with `at_close` of the
        daily-close ones, in the watchlist's score order."""
        return [
            _Row.of(row)
            for row in self._watchlist(day)
            if self.config.playbook(row.playbook).at_close == at_close
        ]

    def _entries(self, day: date, prev: date, *, at_close: bool = False) -> None:
        """The day's entries in the order their triggers take the open slots (HAT-BT-026): the
        intraday breaks and the pre-open playbooks' candidates, each decided on the session's
        5-minute bars when it has them (`_on_bars`) and on the daily estimate otherwise; or with
        `at_close` the close entries of the `daily_close` playbooks, after the close phase (they
        all trigger at the close, so they keep the score order under `time`). In a run of more
        than one strategy each strategy decides its own candidates and they all take the slots
        in one order, the account's (the strategies in leg order at the close)."""
        if at_close:
            n = 0
            for leg in self.legs:
                self._use(leg)
                for row in self._rows(day, at_close=True):
                    found = self._decide_daily(row, day, prev, at_close=True)
                    if found is not None:
                        self._enter(
                            found.row, found.bar, found.plan, prev, _at(day, _AT_CLOSE_ENTRY, n)
                        )
                    n += 1
            return
        decisions: list[_Decision] = []
        for leg_n, leg in enumerate(self.legs):
            self._use(leg)
            decisions.extend(replace(d, leg=leg_n) for d in self._leg_decisions(day, prev))
        # the watchlist's score order, which the `score` and `random` slot orders keep
        decisions.sort(
            key=lambda d: (-d.row.score, d.row.playbook, str(d.row.instrument_id), d.leg)
        )
        rule = self.spec.conventions.slot_order

        def strength(d: _Decision) -> Decimal | None:
            return self.view.feature(prev, str(d.row.instrument_id), "rs_weighted", rounded=False)

        def leader(d: _Decision) -> bool:
            setup = d.row.criteria_results.get("setup")
            return isinstance(setup, Mapping) and setup.get("early_leader") is True

        ordered = order_slots(decisions, rule, day, lambda d: d.rank, strength, leader)
        for n, found in enumerate(ordered):
            at = (
                _at(day, _AT_ENTRY, n) if found.at is None else found.at + timedelta(microseconds=n)
            )
            self._use(self.legs[found.leg])
            self._enter(found.row, found.bar, found.plan, prev, at)

    def _leg_decisions(self, day: date, prev: date) -> list[_Decision]:
        """The active strategy's triggers of the day: its watchlist rows and pre-open
        candidates, each on the session's 5-minute bars when it has them, else the daily
        estimate."""
        rows = [r for r in self._rows(day, at_close=False) if self._open_for(r, day)]
        gappers = self._gappers(day, prev)
        bars = self._session_bars(day, {str(r.instrument_id) for r in (*rows, *gappers)})
        decisions = self._on_bars(day, prev, (*rows, *gappers), bars)
        for row in rows:
            if str(row.instrument_id) not in bars:
                decisions.extend(self._decided(self._decide_daily(row, day, prev)))
        for row in gappers:
            if str(row.instrument_id) not in bars:
                decisions.extend(self._decided(self._decide_ep(row, day, prev)))
        return decisions

    @staticmethod
    def _decided(found: _Decision | None) -> list[_Decision]:
        return [] if found is None else [found]

    def _open_for(self, row: _Row, day: date) -> bool:
        """Whether a candidate may be entered on `day` at all: not factor-pending (CA5), not on
        its demerger exit session (Q5) and with a session that day."""
        instrument_id = str(row.instrument_id)
        if instrument_id in self.view.pending_on(day):
            self._skip(SKIP_FACTOR_PENDING)
            return False
        if (instrument_id, day) in self.data.demerger_exits:
            self._skip(SKIP_DEMERGER_PENDING)  # sold at this close (Q5)
            return False
        return self.view.bar(instrument_id, day) is not None

    def _decide_daily(
        self, row: _Row, day: date, prev: date, *, at_close: bool = False
    ) -> _Decision | None:
        """A watchlist row decided on its daily bar (the estimate of the live trigger, or a
        close entry); None when it did not trigger or was skipped (the skip counted)."""
        if at_close and not self._open_for(row, day):
            return None
        instrument_id = str(row.instrument_id)
        bar = self.view.bar(instrument_id, day)
        if bar is None:
            return None  # no session today: the level cannot break
        level = self._level(row)
        if level is None:
            self._skip(SKIP_NO_LEVEL)
            return None
        playbook = self.config.playbook(row.playbook)
        if at_close:
            price = self._close_fill(playbook, instrument_id, bar, day, level)
        else:
            price = self._fill(row, bar, day, level)
        if isinstance(price, str):
            self._passed(row.playbook, "trigger")  # broke, without a fill to take
            if price not in VOLUME_SKIPS:
                self._passed(row.playbook, "breakout_volume")
            self._skip(price, row)
            return None
        if price is None:
            return None  # the level did not break: the row expires at the session end
        self._passed(row.playbook, "trigger")
        if not at_close and (paced := self._full_day_pace(playbook, instrument_id, day)):
            self._skip(paced)
            return None
        self._passed(row.playbook, "breakout_volume")
        # A close entry comes after the whole day's path: its low is the day's low.
        lows = (
            (bar.low, None)
            if at_close
            else self._entry_lows(playbook, instrument_id, bar, level, price, (day, prev))
        )
        if isinstance(lows, str):
            self._skip(lows, row)
            return None
        before, dip = lows
        daily = self._daily(playbook, instrument_id, day, prev)
        planned = self._stop(playbook, row, daily, price, before)
        stop = planned.stop
        if stop is None:
            self._skip(planned.skip or SKIP_BAD_INPUT, row)
            return None
        self._passed(row.playbook, "stop")
        at = _at(day, _AT_CLOSE_ENTRY if at_close else _AT_ENTRY)
        locked = self._locked(instrument_id, price, bar.high, day)
        failed, filters = self._entry_filters(
            playbook, row, daily, (price, stop, level, locked), at
        )
        if failed != MAX_CHASE:
            self._passed(row.playbook, MAX_CHASE)
        if not at_close:
            self.estimated.update((LEVEL_BREAK_ESTIMATED, ENTRY_DAY_LOW_ESTIMATED))
            if bar.open > level:
                self.estimated.add(GAP_OVER_ESTIMATED)
        basis = BASIS_CLOSE if at_close else BASIS_DAILY
        plan = _EntryPlan(price, stop, dip, locked, failed, level, filters, at_close, None, basis)
        rank = trigger_rank(bar.open, level, self.spec.conventions.entry_fill_rule)
        return _Decision(row, plan, bar, _estimated_rank(rank))

    def _full_day_pace(self, playbook: Playbook, instrument_id: str, day: date) -> str | None:
        """The skip reason when the playbook's enabled `volume_pace` fails on the full day's
        volume (`full_day_pace_skip`), None when it passes or the playbook has none; a judged
        pace records the look-ahead gap `volume_pace_on_full_day_volume`."""
        multiple = trigger.volume_pace_multiple(playbook)
        if multiple is None:
            return None
        self.estimated.add(VOLUME_PACE_ESTIMATED)
        # the day's own volume before its close: the known gap `VOLUME_PACE_ESTIMATED`
        with self.view.hindsight(HINDSIGHT_FULL_DAY_PACE):
            volumes = self.view.volumes_through(instrument_id, day, trigger.AVG_VOLUME_DAYS + 1)
        return full_day_pace_skip(volumes, multiple)

    def _paced_live(self, playbook: Playbook) -> Playbook:
        """The playbook the 5-minute replay decides with: as configured, or under the
        `volume_pace` convention `full_day` without its `volume_pace` trigger, which the full
        day's volume judges after the replay fires (`_full_day_pace`)."""
        if self.spec.conventions.volume_pace != VOLUME_PACE_FULL_DAY:
            return playbook
        return playbook.model_copy(
            update={
                "trigger": [
                    i.model_copy(update={"enabled": False}) if i.type == trigger.VOLUME_PACE else i
                    for i in playbook.trigger
                ]
            }
        )

    def _session_bars(
        self, day: date, instruments: Collection[str]
    ) -> dict[str, tuple[DataBar, ...]]:
        """The stored 5-minute bars of `day` of `instruments`, on the price basis, of each
        session that fits its daily bar (`intraday.on_basis`), lacks no refused bucket
        (`intraday.lacks_refused`) and lies in no known coverage gap (`bars5m_gaps.in_gap`);
        none in a `daily` run or without a `bars_5m` store."""
        store = self.data.intraday
        if store is None or self.spec.conventions.entry_bars != ENTRY_BARS_5M:
            return {}
        self.view.as_of(day, inclusive=False)  # the session in progress, replayed bar by bar
        scales = {
            i: scale
            for i in instruments
            if (scale := self.view.price_scale(i, day)) is not None and scale > 0
        }
        return {
            i: bars
            for i, bars in session_bars(store, day, scales).items()
            if (bar := self.view.bar(i, day)) is not None
            and session_admitted(
                day,
                bars,
                bar.open,
                self.data.screened_5m.get((i, day), ()),
                self.data.gaps_5m,
                self.data.isins.get(i),
            )
        }

    def _candidate(self, row: _Row, day: date, prev: date) -> trigger.Candidate:
        """The live trigger's candidate of `row` on `day`, from the same nightly inputs live
        reads (`_daily`), its setup levels and, for a pre-open playbook, the day's open."""
        instrument_id = str(row.instrument_id)
        playbook = self.config.playbook(row.playbook)
        bar = self.view.bar(instrument_id, day)
        daily = self._daily(playbook, instrument_id, day, prev, bar)
        return trigger.Candidate(
            bot_id=self.bot_id,
            strategy_key=self.config.bot,
            mode=NO_CATALYST_MODE,
            playbook=self._paced_live(playbook),
            instrument_id=row.instrument_id,
            symbol=instrument_id,
            trade_date=day,
            daily=daily,
            levels=_levels(row),
            final_contraction_low=_levels(row).get(trigger.CONTRACTION_LOW),
            open_price=bar.open if bar is not None and playbook.preopen else None,
            configured_max_chase_pct=self.chase[playbook.name],
            daily_results=trigger.daily_trigger_results(
                playbook, self._setup_bars(instrument_id, prev), day
            ),
            results=self.view.results_on(instrument_id, day) if playbook.preopen else (),
            results_since=session_open(prev) if playbook.preopen else None,
        )

    def _on_bars(
        self,
        day: date,
        prev: date,
        rows: Sequence[_Row],
        bars: Mapping[str, Sequence[DataBar]],
    ) -> list[_Decision]:
        """The candidates whose session has 5-minute bars, decided by the live trigger replayed
        on them (`intraday.replay`): the level with the opening range, the close confirmation,
        `volume_pace`, the LIMIT entry, the low-of-day stop from the bars up to the trigger and
        the entry filters are live's (under the `volume_pace` convention `full_day` the pace is
        judged on the full day's volume once the replay fires). A trigger whose stop cannot be
        planned is skipped, as on the daily estimate."""
        candidates = [self._candidate(r, day, prev) for r in rows if str(r.instrument_id) in bars]
        by_key = {(r.playbook, str(r.instrument_id)): r for r in rows}
        for row in rows:
            # the level broke that day: what the funnel's trigger counts on the 5-minute days,
            # whose replay fires only on the break with its confirmation and volume pace
            level, bar = self._level(row), self.view.bar(str(row.instrument_id), day)
            if str(row.instrument_id) in bars and level is not None and bar is not None:
                if bar.high > level and not self.config.playbook(row.playbook).preopen:
                    self._passed(row.playbook, "trigger")
        for candidate in candidates:
            if candidate.playbook.preopen:
                self._ep_trace(day, candidate, bars[candidate.symbol])
        out: list[_Decision] = []
        for fired in replay(day, candidates, bars):
            candidate, planned = fired.candidate, fired.plan
            row = by_key[(candidate.playbook.name, candidate.symbol)]
            if candidate.playbook.preopen:
                self._ep_fired(row, planned.skip, planned.stop is not None)
            if self.spec.conventions.volume_pace == VOLUME_PACE_FULL_DAY and (
                paced := self._full_day_pace(
                    self.config.playbook(row.playbook), candidate.symbol, day
                )
            ):
                self._skip(paced)
                continue
            preopen = candidate.playbook.preopen
            if not preopen:
                self._passed(row.playbook, "breakout_volume")
            if planned.entry is None or planned.level is None or planned.stop is None:
                self._skip(planned.skip or SKIP_BAD_INPUT, row)
                continue
            if not preopen:
                self._passed(row.playbook, "stop")
                if planned.skip != MAX_CHASE:
                    self._passed(row.playbook, MAX_CHASE)
            bar = self.view.bar(candidate.symbol, day)
            if bar is None:  # pragma: no cover - `_open_for` and the scan read the same bars
                continue
            dip, high = after_trigger(bars[candidate.symbol], fired.at, bar.low)
            traded = traded_low_after(bars[candidate.symbol], fired.at)
            if planned.skip is None and (traded is None or traded > planned.entry):
                # live's LIMIT rests at the trigger bar's close: no bar after the trigger traded
                # at or below it, so nothing fills
                self._skip(SKIP_LIMIT_NOT_REACHED, row)
                continue
            criteria = tuple(
                {"stage": stage, **outcome.to_json()}
                for stage, outcomes in (
                    ("trigger", fired.evaluation.outcomes),
                    ("entry_filter", planned.filters),
                )
                for outcome in outcomes
            )
            plan = _EntryPlan(
                planned.entry,
                planned.stop,
                dip,
                planned.locked_upper_circuit,
                planned.skip,
                planned.level,
                criteria,
                high=high if high is not None else planned.entry,
                basis=BASIS_5M,
            )
            if candidate.playbook.preopen:
                self._catalyst_gaps(candidate.playbook)
            local = fired.at.astimezone(IST)
            seconds = local.hour * 3600 + local.minute * 60 + local.second
            out.append(_Decision(row, plan, bar, (seconds, _ZERO), as_utc(fired.at)))
        return out

    def _catalyst_gaps(self, playbook: Playbook) -> None:
        """The catalyst gaps a pre-open entry records: `catalyst_not_evaluated` for an enabled
        `catalyst_attached` unless `earnings_catalyst` judges the catalyst, and
        `analyst_beat_not_evaluated` with `earnings_catalyst` (no estimates to beat)."""
        if _has(playbook, ep_log.EARNINGS):
            self.estimated.add(ANALYST_BEAT_NOT_EVALUATED)
        elif _has(playbook, trigger.CATALYST):
            self.estimated.add(CATALYST_NOT_EVALUATED)

    def _ep_trace(self, day: date, candidate: trigger.Candidate, bars: Sequence[DataBar]) -> None:
        """The EP log of a pre-open candidate decided on its 5-minute bars (`intraday.trace`)."""
        record = self.ep_records.get((day, candidate.symbol, candidate.playbook.name))
        if record is None:
            return
        order = ep_log.trigger_gates(candidate.playbook)
        found = trace(day, candidate, bars, order)
        record.basis = BASIS_5M
        record.through(found.depth, order)
        if found.depth < len(order):
            gate = order[found.depth]
            outcome = found.outcomes.get(gate)
            record.failed(None if outcome is None else outcome.reason, gate)
        record.at_trigger(
            found.at,
            found.cum_volume,
            candidate.daily.adv20,
            candidate.results,
            candidate.results_since,
        )

    def _ep_fired(self, row: _Row, skip: str | None, stop_planned: bool) -> None:
        """A pre-open trigger fired: its stop and entry filters in the EP log (the first filter
        that failed is `skip`; a stop that could not be planned fails the first filter)."""
        record = self._ep_of(row)
        if record is None:
            return
        playbook = self.config.playbook(row.playbook)
        record.through(len(ep_log.trigger_gates(playbook)), ep_log.trigger_gates(playbook))
        if not stop_planned:
            record.failed(skip or SKIP_BAD_INPUT)
            return
        for gate in record.gates[record.gates.index(record.reached) + 1 :]:
            if gate == ep_log.FILLED:
                break
            if gate == skip:
                record.failed(skip, gate)
                return
            record.passed(gate)

    def _gappers(self, day: date, prev: date) -> list[_Row]:
        """The day's candidates of the pre-open playbooks, as live's 09:16 gap scan picks them
        (HAT-BOT-052): a member of the bot's universe on `day`, open at least the scan's floor
        over the previous close (`live_entry.gap_floor` of the run's config; a stock's band caps
        its gap, so the band rule needs no band table here), passing the playbook's effective
        universe (its own or the bot's, `StrategyConfig.universe_for`) and its setup on the bars
        to the previous session (`live_entry.ep_gate`). Each candidate in a playbook's universe
        gets an EP log record (`ep_log`) with the gate it failed. The regime gate is not applied
        here: `evaluate_signal` refuses each of their signals while it is off, as live's gate
        board does."""
        preopen = [p for p in self.config.playbooks if p.preopen]
        floor = gap_floor([self.config])
        if not preopen or floor is None:
            return []
        bot = configs_of(self.session).get(Bot, self.bot_id)
        # the run's universe rows are the scan nights' (`data.scan_night_members`): read on
        # `prev` they are the members of `day`; read on `day` they would be the next session's
        members = () if bot is None else watchlist.bot_universe_members(self.session, bot, prev)
        series = series_as_of(self.data.series, prev)
        found: list[_Row] = []
        for member in sorted(members, key=str):
            instrument_id = str(member)
            bar = self.view.bar(instrument_id, day)
            prev_close = self.view.prev_close(instrument_id, day)
            if bar is None or prev_close is None or prev_close <= 0:
                continue
            if not meets_gap(bar.open, prev_close, floor):
                continue
            history = self._setup_bars(instrument_id, prev)
            for playbook in preopen:
                universe = self.config.universe_for(playbook.name)
                failed = ep_gate(playbook, history, day, prev, universe, series.get(instrument_id))
                if failed == EP_UNIVERSE:
                    continue  # not in the playbook's universe: not in its gap scan
                row = _Row(playbook.name, member, day)
                record = self._ep_record(row, history, prev, 100 * (bar.open / prev_close - 1))
                gap_min = playbook.gap_min_pct
                if gap_min is not None and bar.open * 100 < prev_close * (100 + gap_min):
                    record.failed(ep_log.GAP_TOO_SMALL)
                    continue
                record.passed(ep_log.GAP_UP)
                if failed is not None:
                    record.failed(failed, failed if failed in record.gates else None)
                    continue
                for instance in playbook.setup.criteria:
                    record.passed(instance.type)
                if self._open_for(row, day):
                    found.append(row)
                else:
                    record.failed("not_open")
        return found

    def _ep_record(
        self, row: _Row, history: pl.DataFrame, prev: date, gap: Decimal
    ) -> ep_log.EpRecord:
        """A new EP log record of a pre-open candidate in the gap scan (`ep_log`)."""
        instrument_id = str(row.instrument_id)
        record = ep_log.EpRecord(
            row.trade_date,
            instrument_id,
            symbol_on(self.data, instrument_id, row.trade_date),
            row.playbook,
            ep_log.ep_gates(self.config.playbook(row.playbook)),
        )
        record.columns.update(ep_stock_log(history, prev), gap_pct=round(float(gap), 4))
        self.ep_records[(row.trade_date, instrument_id, row.playbook)] = record
        return record

    def _ep_of(self, row: _Row) -> ep_log.EpRecord | None:
        return self.ep_records.get((row.trade_date, str(row.instrument_id), row.playbook))

    def _setup_bars(self, instrument_id: str, prev: date) -> pl.DataFrame:
        """The instrument's bars of the night's scan window (`MarketView.window` of `prev`)."""
        if self.night_window is None or self.night_window[0] != prev:
            self.night_window = (prev, self.view.window(prev))
        frame = self.night_window[1]
        return frame.filter(pl.col("instrument_id").cast(pl.String) == instrument_id)

    def _decide_ep(self, row: _Row, day: date, prev: date) -> _Decision | None:
        """A pre-open candidate without 5-minute bars, on the daily estimate calibrated on the
        5-minute years (`intraday.EP_ESTIMATE`): the playbook's `gap_up`, `early_volume` (the
        day's whole volume standing in for the volume at the trigger) and daily-bar trigger
        criteria pass, the bar fires the estimate, and the entry, its level and the low before
        it are the estimate's; the stop, the entry filters and the circuit state as on any
        daily estimate. None when it does not fire or is skipped."""
        instrument_id = str(row.instrument_id)
        playbook = self.config.playbook(row.playbook)
        bar = self.view.bar(instrument_id, day)
        daily = self._daily(playbook, instrument_id, day, prev)
        record = self._ep_of(row)
        if bar is None or daily.tick is None or daily.prev_close is None:
            self._skip(SKIP_BAD_INPUT)
            if record is not None:
                record.failed(SKIP_BAD_INPUT)
            return None
        if not self._ep_fires(playbook, row, bar, daily, day, prev):
            return None
        if paced := self._full_day_pace(playbook, instrument_id, day):
            self._skip(paced)
            if record is not None:
                record.failed(paced, trigger.VOLUME_PACE)
            return None
        price = trigger.entry_limit(EP_ESTIMATE.entry(bar.open, bar.high), bar.open, daily.tick)
        if price > bar.high:
            if record is not None:
                record.failed("limit_above_high")
            return None  # the LIMIT is above anything the day traded
        level = EP_ESTIMATE.level(price)
        before = EP_ESTIMATE.low_before(bar.open, bar.low)
        planned = trigger.entry_stop(playbook, daily, price, (before,), None)
        if planned.stop is None:
            self._skip(planned.skip or SKIP_BAD_INPUT)
            self._ep_fired(row, planned.skip, False)
            return None
        at = _at(day, _AT_ENTRY)
        locked = self._locked(instrument_id, price, bar.high, day)
        failed, filters = self._entry_filters(
            playbook, row, daily, (price, planned.stop, level, locked), at
        )
        self.estimated.add(EP_TRIGGER_ESTIMATED)
        self._catalyst_gaps(playbook)
        self._ep_fired(row, failed, True)
        dip = bar.low if bar.low < before else None
        plan = _EntryPlan(price, planned.stop, dip, locked, failed, level, filters)
        return _Decision(row, plan, bar, (_EP_SECONDS, _ZERO))

    def _ep_fires(
        self,
        playbook: Playbook,
        row: _Row,
        bar: Bar,
        daily: trigger.DailyInputs,
        day: date,
        prev: date,
    ) -> bool:
        """The estimate's trigger: the bar fires `EP_ESTIMATE`, and every enabled `gap_up`,
        `early_volume`, `earnings_catalyst` and daily-bar trigger criterion passes on the daily
        values at the estimate's trigger instant (`volume_pace` is judged on the full day's volume
        after, `_full_day_pace`); the outcome goes into the EP log."""
        instrument_id = str(row.instrument_id)
        # the day's volume at the estimate's trigger: the known gap `EP_TRIGGER_ESTIMATED`
        with self.view.hindsight(HINDSIGHT_EP_VOLUME):
            volumes = self.view.volumes_through(instrument_id, day, 1)
        volume = int(volumes[-1]) if volumes and math.isfinite(volumes[-1]) else None
        at = _at(day, time(0), _EP_SECONDS)
        results = self.view.results_on(instrument_id, day)
        ctx = replace(
            trigger.daily_context(at, daily, {}),
            open_price=bar.open,
            cum_volume=volume,
            minutes=Fraction(_EP_SECONDS - _AT_OPEN_SECONDS, 60),
            results=results,
            results_since=session_open(prev),
        )
        rest = [i for i in playbook.trigger if i.type not in _EP_SEPARATE_TYPES]
        daily_results = trigger.daily_trigger_results(
            playbook, self._setup_bars(instrument_id, prev), day
        )
        evaluation = trigger.evaluate_trigger(
            playbook.model_copy(update={"trigger": rest}), ctx, NO_CATALYST_MODE, daily_results
        )
        # the session's whole bar at the estimate's trigger: the known gap `EP_TRIGGER_ESTIMATED`
        self.view.count_hindsight(HINDSIGHT_EP_CLOSE)
        fires = EP_ESTIMATE.fires(bar.open, bar.high, bar.low, bar.close)
        record = self._ep_of(row)
        if record is not None:
            outcomes = {o.type: o.result for o in evaluation.outcomes}
            order = ep_log.trigger_gates(playbook)
            # the pace is judged on the full day's volume after the estimate fires
            judged = {ep_log.LEVEL: fires, trigger.VOLUME_PACE: True}
            passed = {k: v.passed for k, v in outcomes.items()} | judged
            depth = ep_log.trigger_depth(order, passed)
            record.basis = BASIS_DAILY
            record.through(depth, order)
            if depth < len(order):
                outcome = outcomes.get(order[depth])
                reason = "estimate_not_fired" if outcome is None else outcome.reason
                record.failed(reason, order[depth])
            record.at_trigger(at, volume, daily.adv20, results, session_open(prev))
        return fires and all(o.result.passed for o in evaluation.outcomes if o.blocking)

    def _level(self, row: _Row) -> Decimal | None:
        """The row's trigger level: its playbook's level expression over the row's setup levels
        on daily bars (`trigger.daily_level`, the opening range unavailable, HAT-BT-025)."""
        return trigger.daily_level(self.config.playbook(row.playbook), _levels(row))

    def _fill(self, row: _Row, bar: Bar, day: date, level: Decimal) -> Decimal | str | None:
        """The entry fill of the row's `level` under the run's entry fill rule, None when the
        level did not break, or the skip reason when there is no tick to penetrate by
        (HAT-BT-020). A gap over the level fills at 09:20, not at the open
        (`fill_0920.gap_over_fill`)."""
        rule = self.spec.conventions.entry_fill_rule
        # The tick matters only to a penetration from below; read it only then.
        tick = None
        if rule == PENETRATION and bar.open <= level < bar.high:
            tick = self.view.tick(str(row.instrument_id), day)
            if tick is None:
                return SKIP_BAD_INPUT  # no tick to penetrate by (nor to plan a stop on)
        if bar.open > level:
            # a gap over: never the 09:15 open, the estimated price at 09:20 (`fill_0920`), at
            # most the day's high
            return fill_0920.gap_over_fill(bar.open, bar.high)
        return entry_fill(bar.open, bar.high, level, tick, rule)

    def _close_fill(
        self, playbook: Playbook, instrument_id: str, bar: Bar, day: date, level: Decimal
    ) -> Decimal | str | None:
        """A close entry (`entry_timing: daily_close`): the day's close when it is above the
        level, None when not. With a breakout volume test (`volume_vs_avg`) the day's volume,
        known at the close, must be at least the multiple of the mean of the sessions before
        (`breakout_volume_ok`): the skip reason when it is short or its volumes are missing.
        The official close stands in for the price live would act on before the 15:15 cut-off
        (`daily_close_entry_not_live`)."""
        if bar.close <= level:
            return None
        params = playbook.level_trigger
        if params is None or params.volume_vs_avg is None:
            return bar.close
        days = params.volume_avg_days
        volumes = self.view.volumes_through(instrument_id, day, days + 1)
        ok = breakout_volume_ok(volumes, days, params.volume_vs_avg)
        if ok is None:
            return SKIP_NO_VOLUME
        return bar.close if ok else SKIP_LOW_VOLUME

    def _locked(self, instrument_id: str, price: Decimal, high: Decimal, day: date) -> bool | None:
        """Whether the entry at `price` is at the upper circuit (`upper_circuit_locked`)."""
        prev_close = self.view.prev_close(instrument_id, day)
        return upper_circuit_locked(price, high, prev_close, self.view.tick(instrument_id, day))

    def _lower_locked(self, instrument_id: str, day: date, bar: Bar) -> bool:
        """Whether the instrument sat at its lower circuit all of `day` (`lower_circuit_locked`);
        an unknown state (no previous close or tick) is not locked."""
        if not bar.open == bar.high == bar.low == bar.close:
            return False  # the common case, without reading the tick
        ohlc = (bar.open, bar.high, bar.low, bar.close)
        prev_close = self.view.prev_close(instrument_id, day)
        return lower_circuit_locked(ohlc, prev_close, self.view.tick(instrument_id, day)) is True

    def _close_locked(self, instrument_id: str, day: date, bar: Bar) -> bool:
        """Whether the instrument closed `day` locked at its lower circuit (`close_lower_locked`);
        an unknown state is not locked."""
        if bar.close != bar.low:
            return False  # the common case, without reading the tick
        prev_close = self.view.prev_close(instrument_id, day)
        tick = self.view.tick(instrument_id, day)
        return close_lower_locked(bar.close, bar.low, prev_close, tick) is True

    def _entry_filters(
        self,
        playbook: Playbook,
        row: _Row,
        daily: trigger.DailyInputs,
        entry: tuple[Decimal, Decimal, Decimal, bool | None],
        at: datetime,
    ) -> tuple[str | None, tuple[dict[str, object], ...]]:
        """(the type of the first failing entry filter or None, each filter's outcome as live
        records it on the signal) for the playbook's entry filters at the trigger's instant `at`,
        evaluated as live evaluates them (HAT-BT-025) on the context live builds from the same
        nightly inputs and setup levels (`trigger.daily_context`, `entry_filter_context`), without
        the bar's intraday values: `entry` is (price, stop, level, locked)."""
        price, stop, level, locked = entry
        ctx = trigger.entry_filter_context(
            trigger.daily_context(at, daily, _levels(row)),
            entry=price,
            stop=stop,
            level=level,
            locked=locked,
        )
        failed, outcomes = trigger.evaluate_entry_filters(
            playbook, ctx, configured_pct=self.chase[playbook.name], strategy_key=self.config.bot
        )
        return failed, tuple({"stage": "entry_filter", **o.to_json()} for o in outcomes)

    def _entry_lows(
        self,
        playbook: Playbook,
        instrument_id: str,
        bar: Bar,
        level: Decimal,
        price: Decimal,
        days: tuple[date, date],
    ) -> tuple[Decimal, Decimal | None] | str:
        """(the entry day's low before the break, its low after the entry or None), or the skip
        reason when a low-of-day stop cannot know the low before the break (HAT-BT-020, PR 60):
        the daily estimate, the session having no 5-minute bars here."""
        _, prev = days
        adr20 = self.view.feature(prev, instrument_id, "adr20")
        low_of_day = isinstance(playbook.stop, StopLowOfDay)
        gapped = price == bar.open
        if low_of_day:
            if gapped:
                # A gap over enters at the open: its stop needs the opening range's low.
                return SKIP_GAP_NO_INTRADAY
            if adr20 is None or adr20 < 0:
                return SKIP_BAD_INPUT  # the whole day's low would be read before the break
        # The break is at the entry price (level + 1 tick under penetration); a gap over at the
        # open breaks on the first bar either way.
        broke = level if gapped else price
        before, after = pre_break_low(bar.open, bar.low, broke, adr20)
        if gapped and not low_of_day:
            # A gap over enters at the open and then sees the whole day's way down.
            return before, bar.low
        return before, after

    def _enter(
        self,
        row: _Row,
        bar: Bar,
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
            uuid.UUID(h.instrument_id): self._mark_price(h.instrument_id, row.trade_date, last[1])
            for h in self.held.values()
            if (last := self._last_close(h.instrument_id)) is not None
        }
        open_count, open_risk = self._book()
        # what selling the sleeve would credit now: the free cash an entry may size on
        credit = _ZERO if self.overlay is None else self.overlay.credit_for(row.trade_date, at)
        if self.funder is not None:  # an overlap run: what selling the rotation would net
            credit += self.funder.credit(
                row.trade_date, lambda i: self._funding_quote(i, row.trade_date, at)
            )
        features = self.signal_features.at(
            instrument_id,
            row.trade_date,
            prev,
            entry=price,
            stop=plan.stop,
            score=Decimal(row.score),
            pivot=None if row.pivot is None else Decimal(row.pivot),
            criteria_results=row.criteria_results,
        )
        record = self._ep_of(row)
        features = {**features, **({} if record is None else record.columns)}
        request = entries.EntryRequest(
            bot_id=self.bot_id,
            playbook=row.playbook,
            instrument_id=row.instrument_id,
            trade_date=row.trade_date,
            triggered_at=at,
            context=self._context(row, plan, prev, credit),
            last_prices=held_prices,
            skip_reason=plan.skip,
            criteria=plan.filters,
        )
        try:
            # a backtest signal has no evidence of its own: its label is low, `backtest`
            outcome = entries.open_entry(
                self.session,
                request,
                # a 5-minute entry is live's LIMIT at its price: never filled above it
                fills=self.spec.fills.within(
                    self._free_cash() + credit, plan.price if plan.basis == BASIS_5M else None
                ),
                confidence=BACKTEST_CONFIDENCE,
            )
        except positions.TradeError as refused:
            if refused.code != entries.NOT_IN_UNIVERSE:
                raise
            # Scanned on the night's members, the stock left the universe by its entry day:
            # no entry (HAT-PORT-023) and, as live, no signal, so the setup's window stays open.
            self._skip(entries.NOT_IN_UNIVERSE)
            if record is not None:
                record.failed(entries.NOT_IN_UNIVERSE, ep_log.FILLED)
            return
        signal = self.session.get(HatSignal, outcome.signal_id)
        if signal is None or outcome.duplicate:
            return
        reason = signal.risk_decision.get("reason")
        if self.funder is not None and outcome.position_id is not None:
            self._fund(row.trade_date, at, outcome.position_id)
        if self.overlay is not None:
            if outcome.position_id is not None and self._free_cash() < 0:
                # the entry took more than the cash: the sleeve sells for the rest as it fills
                self.overlay.fund(row.trade_date, at, -self._free_cash())
            elif outcome.position_id is None:
                found = refusal_reason(reason, self.overlay.held_units())
                if found == INSUFFICIENT_CREDIT and reason != found:
                    self.overlay.refused_for_credit()
                reason = found
        self.signal_rows.append(
            SignalRow(
                trade_date=row.trade_date,
                playbook=self.leg.prefix + row.playbook,
                instrument_id=instrument_id,
                state=signal.state,
                reason=reason,
                trigger_price=Decimal(signal.trigger_price),
                stop_price=Decimal(signal.stop_price),
                qty=signal.qty,
                strategy_version_id=signal.strategy_version_id,
                risk_policy_version_id=signal.risk_policy_version_id,
                allocation=self.allocations.get(row.trade_date),
                entry_price=price,
                scan_date=prev,
                ladder_level=self.ladder.level,
                open_positions=open_count,
                open_risk=open_risk,
                equity=self._equity_now()[0],
                regime_on=self.gate.get(row.trade_date),
                breadth_pct=self.view.breadth_on(prev),
                features=features,
            )
        )
        if record is not None:
            record.signal_state = signal.state
            if outcome.position_id is None:
                record.failed(signal.risk_decision.get("reason") or signal.state, ep_log.FILLED)
            else:
                record.passed(ep_log.FILLED)
        if outcome.position_id is None:
            return
        year = self.entry_bars.setdefault(row.trade_date.year, {})
        year[plan.basis] = year.get(plan.basis, 0) + 1
        position = self.session.get(HatPosition, outcome.position_id)
        if position is None:  # pragma: no cover - open_entry returned the id it wrote
            return
        day = row.trade_date
        self.held[position.id] = _Held(
            instrument_id, day, price, price, row.playbook, leg=self.legs.index(self.leg)
        )
        if plan.at_close:
            return  # entered at the close: the day's path is behind it
        high = bar.high if plan.high is None else plan.high
        if dip is not None and dip < price:
            # From the entry down to the low: a stop it reaches fills at min(entry, stop).
            self._down_leg(position, Bar(price, high, dip, bar.close), at + timedelta(seconds=1))
        self._up_leg(position, price, high, day, at + timedelta(seconds=2))

    def _fund(self, day: date, at: datetime, position_id: uuid.UUID) -> None:
        """An overlap run's setup entry that took more than the free cash: the rotation sells
        for the rest as it fills (`RotationBook.fund`), and the cash it covered is the
        position's funding, as a share of its entry cost (`_funded_open`). Each sale is counted
        by its price's source; one at a stale 09:20 price is an estimate the run reports."""
        if self.funder is None or (shortfall := -self._free_cash()) <= 0:
            return
        sales = len(self.funder.fill_rows)
        raised = self.funder.fund(
            day, shortfall, lambda i: self._funding_quote(i, day, at), self._symbol(day)
        )
        for fill in self.funder.fill_rows[sales:]:
            source = self._funding_sources[(fill.instrument_id, day)]
            self.funding_fills[source] = self.funding_fills.get(source, 0) + 1
            if source == _FUNDING_0920:
                self.estimated.add(OVERLAP_FUNDING_ESTIMATED)
        position = self.session.get(HatPosition, position_id)
        if position is not None and raised > 0 and position.qty_open > 0:
            cost = position.qty_open * Decimal(position.avg_entry)
            self.funded[position_id] = min(Decimal(1), min(shortfall, raised) / cost)

    # --- the close, delistings, the period end ------------------------------------------------

    def _close_view(self, position: HatPosition, bar: Bar, day: date) -> exits.ExitView:
        """The position's view at the day's close, the stand-in for live's pre-15:15 price
        (`exits.view_from_bars`, as live builds it)."""
        held = self.held[position.id]
        rules = self.legs[held.leg].config.exits_for(held.playbook)
        before = self.view.bars_through(
            held.instrument_id, day, exits.history_sessions(rules), inclusive=False
        )
        since = (
            self.view.bars_since(held.instrument_id, held.entry_day, day)
            if exits.reads_peak(rules)
            else None
        )
        return exits.view_from_bars(
            bar.close,
            rules,
            self.index[day] - self.index[held.entry_day],
            [b.close for b in before],
            [b.low for b in before],
            [b.high for b in before],
            None if since is None else [b.high for b in since],
            None if since is None else [b.close for b in since],
        )

    def _close_phase(self, day: date) -> None:
        for position in self._open_positions():
            held = self.held[position.id]
            bar = self.view.bar(held.instrument_id, day)
            if bar is None:
                continue
            held.low = min(held.low, bar.close)
            if self._lower_locked(held.instrument_id, day, bar) or self._close_locked(
                held.instrument_id, day, bar
            ):
                # Nothing sells at a locked lower band, a close rule's sale included, nor at a
                # close locked there since some time in the day; a position entered today (a gap
                # over at the band) counts the day here.
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
        self.closed = day

    def _last_close(self, instrument_id: str) -> tuple[date, Decimal] | None:
        """The instrument's last session with a bar in the period, up to the last close phase,
        and its close; None before one."""
        if self.closed is None:
            return None
        found = self.view.last_close(instrument_id, self.closed)
        return found if found is not None and found[0] >= self.period[0] else None

    def _ambiguous_dip(self, position: HatPosition, bar: Bar, day: date) -> bool:
        """Whether the position's day is ambiguous (`raised_stop_dip`: the way up raised its
        stop, the low reaches the raised stop, the close is above it), counting the day; True
        when the run's rule is `pessimistic`, so the raised stop fills at its price."""
        raised = self.held[position.id].raised
        if raised is None or raised[0] != day:
            return False
        if not raised_stop_dip(bar.low, bar.close, raised[1], Decimal(position.current_stop)):
            return False
        self.ambiguous_days.add(day)
        return self.spec.conventions.ambiguous == AMBIGUOUS_PESSIMISTIC

    def _close(self, position: HatPosition, price: Decimal, reason: str, at: datetime) -> None:
        held = self.held[position.id]
        shadow.close_position(
            self.session,
            position.id,
            price,
            reason,
            at,
            high=held.high,
            low=held.low,
            fills=AccountFills(self.spec.fills, self.dp_sold, held.instrument_id),
        )

    def _demergers(self, day: date) -> None:
        """A position held into a demerger whose record date was announced before this session
        closes at this, the last session before the ex-date, at its close (CA8,
        `MarketData.demerger_exits`): exit reason `demerger`, the run's fills. A session with no
        bar, or one locked at its lower band, sells nothing, and the position holds through as
        before."""
        for position in self._open_positions():
            held = self.held[position.id]
            if (held.instrument_id, day) not in self.data.demerger_exits:
                continue
            bar = self.view.bar(held.instrument_id, day)
            if bar is None or self._lower_locked(held.instrument_id, day, bar):
                continue
            self._close(position, bar.close, DEMERGER, _at(day, _AT_END))
        for book in self.books.values():
            for name in sorted(book.holdings):
                if (name, day) not in self.data.demerger_exits:
                    continue
                bar = self.view.bar(name, day)
                if bar is not None and not self._lower_locked(name, day, bar):
                    book.close_out(name, day, bar.close, DEMERGER, self._symbol(day))

    def _delistings(self, day: date) -> None:
        """An instrument's last traded session closes its open position at that close
        (HAT-BT-012): a delisted stock's loss stays in the results. Which session is the last
        is known from the registry and the later bars, not on the day (no point-in-time rule
        exists in the data), so each such exit is counted `HINDSIGHT_DELISTED_EXIT`."""
        for position in self._open_positions():
            held = self.held[position.id]
            if self.data.last_traded.get(held.instrument_id) != day:
                continue
            bar = self.view.bar(held.instrument_id, day)
            if bar is not None:
                price: Decimal | None = bar.close
            else:
                last = self._last_close(held.instrument_id)
                price = last[1] if last is not None else None
            if price is not None:
                self.view.count_hindsight(HINDSIGHT_DELISTED_EXIT)
                self._close(position, price, DELISTED, _at(day, _AT_END))
        for book in self.books.values():
            for name in sorted(book.holdings):
                if self.data.last_traded.get(name) != day:
                    continue
                bar = self.view.bar(name, day)
                last = self._last_close(name)
                price = bar.close if bar is not None else None if last is None else last[1]
                if price is not None:
                    self.view.count_hindsight(HINDSIGHT_DELISTED_EXIT)
                    book.close_out(name, day, price, DELISTED, self._symbol(day))

    def _finish(self, last: date) -> None:
        for position in self._open_positions():
            held = self.held[position.id]
            price = self._mark_price(held.instrument_id, last, Decimal(position.avg_entry))
            held.low = min(held.low, price)  # a suspension mark below the traded lows is the MAE
            self._close(position, price, END_OF_PERIOD, _at(last, _AT_END, 30))
        for book in self.books.values():
            book.finish(last, self._rotation_mark(last))
        self.session.flush()
        if self.overlay is not None:
            # full liquidation: the sleeve sells at the close and the open year's tax is paid
            self._tax_fills(last)
            self.overlay.finish(last, self._free_cash())
            self.overlay.final_row(last, self._marked(last)[0])
        if self.equity:
            equity = self._marked(last)[0]
            self.equity[-1] = (last, equity)
            # every position closed at the period end: the last session's book is cash
            self.sessions[-1] = replace(
                self.sessions[-1],
                equity=equity,
                cash=equity,
                position_value=_ZERO,
                open_positions=0,
                open_risk=_ZERO,
            )
            if self.invested:
                self.invested[-1] = _ZERO  # and holds no position: `invested_share` agrees
            self._log_book(self.sessions[-1])

    def _sync_ladder(self) -> None:
        """Step each strategy's exposure ladder for each of its trades closed since the last
        step, in close order: the backtest counts its trades as real ones."""
        for leg in self.legs:
            rows = self.session.execute(
                select(HatTradeResult.position_id, HatTradeResult.r_multiple, HatPosition.closed_at)
                .join(HatPosition, HatPosition.id == HatTradeResult.position_id)
                .where(HatPosition.bot_id == leg.bot_id)
            ).all()
            fresh = sorted(
                (r for r in rows if r[0] not in self.seen_results),
                key=lambda r: (as_utc(r[2]), self.held[r[0]].instrument_id),
            )
            for position_id, r_multiple, _ in fresh:
                self.seen_results.add(position_id)
                leg.ladder = apply_outcome(leg.ladder, is_win(Decimal(r_multiple)), self.policy)

    def _cash(self) -> Decimal:
        """The account's cash: the start, the dividends and every fill's flow, and what the
        overlay moved (its sales and buys, its tax), pending credits included."""
        cash = self.spec.initial_equity + self.dividend_cash
        for side, value, fees in exact_flows(self.session):
            cash += value if side == "sell" else -value
            cash -= fees
        for book in self.books.values():
            cash += book.flow
        return cash if self.overlay is None else cash + self.overlay.cash

    def _free_cash(self) -> Decimal:
        """The cash an entry or a buy may spend: the cash less the overlay's sale proceeds not
        credited until the next session."""
        return self._cash() if self.overlay is None else self._cash() - self.overlay.pending

    def _tax_fills(self, day: date) -> None:
        """The day's strategy fills into the overlay's tax book: a buy's outlay, a sale's net."""
        if self.overlay is None:
            return
        rotated = [
            (f.side, f.instrument_id, f.qty, f.price, f.fees)
            for book in self.books.values()
            for f in book.fills_on(day)
        ]
        for side, instrument_id, qty, price, fees in [*fills_on(self.session, day), *rotated]:
            amount = qty * price + fees if side == "buy" else qty * price - fees
            self.overlay.trade(side, instrument_id, qty, amount, day)

    def _overlay_close(self, day: date, prev: date | None) -> None:
        """The overlay's close: the day's fills taxed, then its distribution, tax, exposure
        check and the next open's plan, on the account's mark."""
        if self.overlay is None:
            return
        self._tax_fills(day)
        equity, held_value = self._marked(day)
        at = self.index[day]
        next_day = self.days[at + 1] if at + 1 < len(self.days) else None
        self.overlay.close_session(
            day, prev, next_day, equity=equity, positions=held_value, free_cash=self._free_cash()
        )

    def _mark_price(self, instrument_id: str, day: date, fallback: Decimal) -> Decimal:
        """The instrument's mark on `day`: its last close, marked down one band per session
        after `suspension_sessions` sessions without a bar (`suspended_mark`, PR 64); `fallback`
        before its first close. A session skipped near a jump (`MarketData.skipped`) is not one
        without a bar."""
        found = self._last_close(instrument_id)
        if found is None:
            return fallback
        seen, last = found
        sessions = (
            self.index[day] - self.index[seen] - self.view.skipped_after(instrument_id, seen, day)
        )
        after = self.spec.conventions.suspension_sessions
        if sessions <= after:
            return last
        tick = self.view.tick(instrument_id, day) or _FINEST_TICK
        return suspended_mark(last, sessions, after, SUSPENSION_BAND_PCT, tick)

    def _held_lines(self, day: date) -> list[Held]:
        """The open positions and rotation holdings at their marks on `day`."""
        lines = []
        for position in self._open_positions():
            held = self.held[position.id]
            price = self._mark_price(held.instrument_id, day, Decimal(position.avg_entry))
            lines.append(Held(held.instrument_id, position.qty_open, price))
        mark = self._rotation_mark(day)
        for book in self.books.values():
            lines += [
                Held(h.instrument_id, h.qty, mark(h.instrument_id, h.avg))
                for h in book.holdings.values()
            ]
        return lines

    def _marked(self, day: date) -> tuple[Decimal, Decimal]:
        """(the account's mark on `day`, the marked value of its open positions)."""
        held_value = sum((line.qty * line.mark for line in self._held_lines(day)), _ZERO)
        sleeve = _ZERO if self.overlay is None else self.overlay.value(day)
        return self._cash() + held_value + sleeve, held_value

    def _log_book(self, row: SessionRow) -> None:
        """The session's book for the ledger reconciliation (`reconcile`), replacing the
        session's earlier one (the period end's closes rewrite the last session)."""
        if self.books_log and self.books_log[-1].day == row.day:
            dividends = self.books_log.pop().dividends
        else:
            dividends = _ZERO
        self.books_log.append(
            SessionBook(
                day=row.day,
                equity=row.equity,
                cash=row.cash,
                position_value=row.position_value,
                held=tuple(self._held_lines(row.day)),
                sleeve_value=_ZERO if self.overlay is None else self.overlay.value(row.day),
                dividends=dividends + self.day_dividends,
            )
        )
        self.day_dividends = _ZERO

    def _book(self) -> tuple[int, Decimal]:
        """(the open positions, their open risk: (entry - stop) x shares, none below 0)."""
        positions = self._open_positions()
        risk = sum(
            (
                max(Decimal(p.avg_entry) - Decimal(p.current_stop), _ZERO) * p.qty_open
                for p in positions
            ),
            _ZERO,
        )
        return len(positions), risk

    def _mark(self, day: date) -> None:
        equity, held_value = self._marked(day)
        self.equity.append((day, equity))
        self.invested.append(held_value / equity if equity > 0 else _ZERO)
        count, risk = self._book()
        count += sum(len(book.holdings) for book in self.books.values())  # no stop: no risk
        if self.overlay is not None:  # the book's held value is the positions and the sleeve
            held_value += self.overlay.value(day)
        prev = self.view.prev(day)
        self.sessions.append(
            SessionRow(
                day=day,
                equity=equity,
                cash=equity - held_value,
                position_value=held_value,
                open_positions=count,
                open_risk=risk,
                # the run's own strategy's (the first leg's) ladder and gate
                ladder_level=self.legs[0].ladder.level,
                regime_on=self.legs[0].gate.get(day),
                breadth_pct=None if prev is None else self.view.breadth_on(prev),
            )
        )
        self._log_book(self.sessions[-1])

    def _path(self, instrument_id: str, day: date, sessions: int) -> list[Bar | None]:
        """The instrument's tradeable bar (None without one) of each of the `sessions` store
        sessions after `day`, as far as the run's data goes."""
        at = self.index[day]
        return [self.view.bar(instrument_id, d) for d in self.days[at + 1 : at + 1 + sessions]]

    def _forward(self, signal: SignalRow) -> SignalRow:
        """A refused signal with the R its entry would have reached 20 and 60 sessions on
        (`standard.fwd_r`): a session without a bar keeps the last close and tests no low."""
        if signal.qty > 0 or signal.entry_price is None:
            return signal
        path: list[tuple[Decimal | None, Decimal | None]] = []
        last: Decimal | None = None
        for bar in self._path(signal.instrument_id, signal.trade_date, 60):
            if bar is not None:
                last = bar.close
            path.append((None if bar is None else bar.low, last))
        entry, stop = signal.entry_price, signal.stop_price
        return replace(
            signal, fwd_r_20d=fwd_r(entry, stop, path, 20), fwd_r_60d=fwd_r(entry, stop, path, 60)
        )

    def _mfe_date(self, instrument_id: str, entry: date, exit_: date) -> date | None:
        """The first session from `entry` to `exit_` with the trade's highest high."""
        best: tuple[Decimal, date] | None = None
        for day in self.days[self.index[entry] : self.index[exit_] + 1]:
            bar = self.view.bar(instrument_id, day)
            if bar is not None and (best is None or bar.high > best[0]):
                best = (bar.high, day)
        return None if best is None else best[1]

    # --- the result --------------------------------------------------------------------------

    def _funnel(self) -> dict[str, Any]:
        """The run's funnel (`folder`): the regime-off nights (the run's own strategy's), and
        per playbook (each strategy's, by its prefix) the stock-days that passed each scan gate
        then the watchlist rows that passed each of the day's."""
        by_playbook: dict[str, Any] = {}
        for leg in self.legs:
            for name, counts in leg.funnel.items():
                by_playbook[leg.prefix + name] = {
                    **{g: c for g, c in counts.items() if g not in TRIGGER_GATES},
                    **{g: counts.get(g, 0) for g in TRIGGER_GATES},
                }
            for playbook in leg.config.playbooks:
                if playbook.preopen:
                    records = [r for r in leg.ep_records.values() if r.playbook == playbook.name]
                    by_playbook[leg.prefix + playbook.name] = ep_log.funnel(
                        records, ep_log.ep_gates(playbook)
                    )
        return {"regime_off_nights": self.legs[0].regime_off_nights, "by_playbook": by_playbook}

    def _rows_of(self, leg: _Leg) -> tuple[tuple[WatchRow, ...], tuple[TradeRow, ...]]:
        """The strategy's watchlist and trade rows, its playbooks named with its prefix."""
        watched = ledger_watch_rows(self.session, leg.bot_id)
        trades = tuple(
            replace(
                t,
                exit_sma=exit_sma(leg.config.exits_for(t.playbook), t.rules_fired),
                target_price=planned_target(
                    leg.config.exits_for(t.playbook), t.entry_price, t.initial_stop
                ),
            )
            for t in ledger_trade_rows(self.session, leg.bot_id, watched)
        )
        if not leg.prefix:
            return watched, trades
        return (
            tuple(replace(w, playbook=leg.prefix + w.playbook) for w in watched),
            tuple(replace(t, playbook=leg.prefix + t.playbook) for t in trades),
        )

    def _all_signals(self) -> list[SignalRow]:
        """The ledger's signals and the rotation entries, by trade date (stable)."""
        found = [*self.signal_rows, *(s for book in self.books.values() for s in book.signals)]
        return sorted(found, key=lambda s: s.trade_date) if self.books else found

    def _all_fills(self) -> tuple[FillRow, ...]:
        """The ledger's fills and the rotation books', by day (stable)."""
        found = fill_rows(self.session)
        if not self.books:
            return found
        rotated = [f for book in self.books.values() for f in book.fill_rows]
        return tuple(sorted([*found, *rotated], key=lambda f: f.day))

    def _result(self) -> BacktestResult:
        rows = [self._rows_of(leg) for leg in self.legs]
        watch_rows = tuple(w for watched, _ in rows for w in watched)
        traded = tuple(t for _, trades in rows for t in trades)
        traded += tuple(t for book in self.books.values() for t in book.trades)
        if len(self.legs) > 1 or self.books:
            watch_rows = tuple(
                sorted(
                    watch_rows, key=lambda w: (w.trade_date, -w.score, w.playbook, w.instrument_id)
                )
            )
            traded = tuple(
                sorted(
                    traded, key=lambda t: (t.exit_date, t.entry_date, t.instrument_id, t.playbook)
                )
            )
        funnel = self._funnel()  # before the EP records take their strategy's prefix
        for leg in self.legs:
            for record in leg.ep_records.values() if leg.prefix else ():
                record.playbook = leg.prefix + record.playbook
        records = [r for leg in self.legs for r in leg.ep_records.values()]
        trade_rows = tuple(
            replace(
                t,
                qty_real=self.view.real_shares(t.instrument_id, t.entry_date, t.qty),
                mfe_date=self._mfe_date(t.instrument_id, t.entry_date, t.exit_date),
            )
            for t in flag_auction_risk(traded, auction_risk_days(self.view.days))
        )
        equity = tuple(self.equity)
        skips = dict(sorted(self.skips.items()))
        invested = tuple(self.invested)
        metrics = compute_metrics(
            trade_rows,
            equity,
            self.spec,
            invested,
            signals=self.signal_rows,
            skips=skips,
            ambiguous_days=len(self.ambiguous_days),
            lower_circuit_days=len(self.lower_circuit_days),
            estimated=self.estimated,
            entry_bars=self.entry_bars,
        )
        metrics["auction_risk_exits"] = auction_risk_summary(trade_rows)
        metrics["dividend_income"] = str(self.dividend_cash + self.rotation_dividends)
        metrics["dividend_credits"] = self.dividend_credits
        if self.books:
            metrics["rotation_fills_0920"] = self.rotation_fills.get(fill_0920.REAL, 0)
            metrics["rotation_fills_estimated"] = self.rotation_fills.get(fill_0920.ESTIMATED, 0)
        if self.funder is not None:  # an overlap run's funding sales, by their price's source
            metrics["rotation_funding_fills"] = {
                k: self.funding_fills.get(k, 0)
                for k in (_FUNDING_BAR, _FUNDING_CLOSE, _FUNDING_AT_0920, _FUNDING_0920)
            }
        overlay: dict[str, Any] = {}
        if self.overlay is not None and self.spec.overlay is not None:
            overlay = {
                "config": self.spec.overlay.record(),
                "data": self.overlay.data.record(),
                **self.overlay.summary(),
                # the sleeve's sales and buys and the tax, all of it cash after the last session
                "net_flow": str(self.overlay.cash),
                "benchmarks": benchmarks(
                    self.spec.overlay,
                    self.overlay.data,
                    self.spec.fills.schedule,
                    self.spec.initial_equity,
                    self.period,
                ),
            }
        fills = self._all_fills()
        ledger_report = reconcile(
            self.spec.initial_equity,
            self.books_log,
            fills,
            trade_rows,
            _LedgerMarket(self.view, self.spec, self.overlay),
            NO_OVERLAY if self.overlay is None else _journal(self.overlay),
        )
        result = BacktestResult(
            trades=trade_rows,
            equity=equity,
            watchlists=watch_rows,
            signals=tuple(self._forward(s) for s in self._all_signals()),
            fills=fills,
            skips=skips,
            metrics=metrics,
            invested=invested,
            allocations=dict(self.legs[0].allocations),
            sessions=tuple(self.sessions),
            funnel=funnel,
            ep_candidates=tuple(r.row() for r in sorted(records, key=lambda r: r.row_key)),
            overlay=overlay,
            overlay_rows=() if self.overlay is None else tuple(r.csv() for r in self.overlay.rows),
            rebalances=tuple(row for book in self.books.values() for row in book.rows),
            ledger=ledger_report,
            hindsight=dict(sorted(self.view.hindsight_reads.items())),
            decisions=tuple(self.decisions),
        )
        self.session.close()
        return result


@dataclass(frozen=True)
class AccountFills:
    """The run's fills for one instrument's strategy sales, sharing the account's DP set with
    the rotation books: the depository charges one debit per scrip per sell day per demat,
    whichever book sold it (`shadow.FillModel`)."""

    fills: BacktestFills
    sold: set[tuple[date, str]]
    instrument_id: str

    def fill(
        self, side: str, qty: int, price: Decimal, day: date, /, *, sold_today: bool = False
    ) -> tuple[Decimal, Decimal]:
        key = (day, self.instrument_id)
        found = self.fills.fill(side, qty, price, day, sold_today=sold_today or key in self.sold)
        if side == "sell":
            self.sold.add(key)
        return found


def _journal(overlay: Overlay) -> OverlayJournal:
    """The sleeve's fills, tax payments and distributed units, for `reconcile`."""
    fills = tuple(
        FillRow(day, asset, "overlay", side, qty, price, fees)
        for day, asset, side, qty, price, fees, _ in overlay.journal
    )
    estimated = frozenset(n for n, row in enumerate(overlay.journal) if row[-1])
    return OverlayJournal(fills, estimated, tuple(overlay.taxes), tuple(overlay.distributions))


class _LedgerMarket:
    """`reconcile.Market` over the run's data, read after the period (the clock released): the
    stocks' bars, last traded closes, ticks and dividends from the view, the ETFs' from the
    overlay's data, the cost schedule and slippage from the run's spec."""

    def __init__(self, view: MarketView, spec: RunSpec, overlay: Overlay | None) -> None:
        self.view = view
        self.schedule = spec.fills.schedule
        self.bps = spec.fills.slippage_bps
        self.suspension_sessions = spec.conventions.suspension_sessions
        self.etf = {} if overlay is None else overlay.data.daily
        self.etf_days = {a: sorted(bars) for a, bars in self.etf.items()}
        self.etf_bps = {} if overlay is None else dict(overlay.config.slippage_bps)

    def model(self, day: date) -> CostModel:
        return self.schedule.on(day)

    def slippage_bps(self, instrument_id: str) -> Decimal:
        return self.etf_bps.get(instrument_id, self.bps)

    def bar(self, instrument_id: str, day: date) -> tuple[Decimal, Decimal, Decimal] | None:
        if instrument_id in self.etf:
            etf = self.etf[instrument_id].get(day)
            return None if etf is None else (etf.low, etf.high, etf.close)
        found = self.view.bar(instrument_id, day)
        return None if found is None else (found.low, found.high, found.close)

    def last_close(self, instrument_id: str, day: date) -> tuple[date, Decimal] | None:
        if instrument_id in self.etf:
            days = self.etf_days[instrument_id]
            n = bisect_right(days, day)
            return None if n == 0 else (days[n - 1], self.etf[instrument_id][days[n - 1]].close)
        return self.view.last_close(instrument_id, day)

    def stale_sessions(self, instrument_id: str, seen: date, day: date) -> int:
        index = self.view.index
        return index[day] - index[seen] - self.view.skipped_after(instrument_id, seen, day)

    def tick(self, instrument_id: str, day: date) -> Decimal:
        return self.view.tick(instrument_id, day) or _FINEST_TICK

    def dividend(self, instrument_id: str, day: date) -> Decimal:
        return self.view.dividends_on(instrument_id, day)


def _etf_data(etf: EtfData | None) -> EtfData:
    if etf is None:
        raise BacktestError("backtest_overlay_no_etf_data")
    return etf


def run_backtest(spec: RunSpec, data: MarketData, etf: EtfData | None = None) -> BacktestResult:
    """Run `spec` over `data` (and, with an overlay, the ETF data `etf`) and return its trades,
    equity curve and metrics."""
    if spec.start > spec.end:
        raise BacktestError("backtest_bad_period")
    if not spec.initial_equity.is_finite() or spec.initial_equity <= 0:
        raise BacktestError("backtest_bad_equity")
    try:
        return _Run(spec, data, etf).run()
    except OverlayError as exc:
        raise BacktestError(exc.code) from None

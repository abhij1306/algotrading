"""The backtest day loop (HAT-BT-010..024, HAT-DEC-T12): one engine for backtest and live.

The loop has no strategy, risk or exit logic of its own. Each simulated trade date d runs the
code live trading runs, in the same order:

1. the nightly scan of d's previous session, published as d's watchlist
   (`watchlist.scan_and_publish`, which calls `scan.scan_bot`);
2. open positions: the nightly exit rules at d's open, then the live rules along the day's path
   (`shadow.evaluate_position`, which calls `exits.evaluate`);
3. entries: each watchlist row whose level the day breaks becomes a trigger, the stop comes from
   `trigger.entry_stop`, and `entries.open_entry` decides it with `evaluate_signal` (caps, max
   chase, sizing) and fills it with the shadow engine's virtual fills;
4. the 15:05-15:10 close rules at the day's official close, then the session-end expiry of the
   watchlist
   (`shadow.expire_watchlist`).

Those functions work on a database session, so the loop keeps a private ledger (`ledger`): an
in-memory SQLite database holding the run's one bot, its pinned strategy version, its universe
(point in time) and the virtual signals, orders, fills, positions and trade results the engine
writes. It never touches the HAT database and nothing in it reaches a broker.

What the backtest adds is only what live trading gets from the market: prices. Daily bars cannot
show where in a day a level broke, so a day is read as the path open -> low -> high -> close (the
adverse move first, `app.hat.backtest.fills`): an entry breaks its level on the way up, a stop is
hit on the way down, a profit rule fires at the first tick price on the way up at which
`exits.evaluate` acts, and the close rules see the official close. Fills go through the cost
model (`app.hat.costs`): slippage against the trade, then statutory charges.

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
measured in shadow: among them, the trigger is the break of the level alone
(`volume_pace` and the opening range need intraday bars, HAT-DEC-I4), the pre-open playbooks
(`Playbook.preopen`, the episodic pivot) are not backtested (the scan publishes no row of
theirs), close-based exits decide on and fill at the daily close (the auction price), which
stands in for the price live acts on before the 15:15 cut-off (owner 2026-10-25), kill switches
are not simulated (the exposure ladder is), a sleeve's health multiplier is 1 (its regime
multiplier too, unless the run applies the policy's), and the run is one sleeve, so the
account-level caps never bind across sleeves.

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

The command line runs a sleeve's strategy version over the Parquet store and records the run
(`app.hat.backtest.runs`): `python -m app.hat.backtest --bot <id> --from YYYY-MM-DD --to
YYYY-MM-DD`.
"""

from __future__ import annotations

import uuid
from bisect import bisect_right
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy import select

from ..data.identity import series_as_of
from ..engine import entries, exits, lifecycle, positions, shadow, trigger, watchlist
from ..engine.confidence import BACKTEST_CONFIDENCE
from ..engine.portfolio import regime as regime_v1
from ..engine.portfolio.allocator import LIVE, Allocation, SleeveInput, allocate, sleeve_key
from ..engine.portfolio.health import Health
from ..engine.regime_gate import regime_allows_longs
from ..engine.scan import ScanInputs
from ..engine.stops import SKIP_BAD_INPUT, StopPlan, history_inputs
from ..engine.strategy.config import Playbook, StopLowOfDay
from ..models import (
    HatPosition,
    HatSignal,
    HatTradeResult,
    HatWatchlistEntry,
)
from ..money import dividend_credit
from ..risk.decision import SignalContext
from ..risk.exposure import LadderState, apply_outcome, is_win
from ..risk.kill import KillState
from ..risk.sessions import SessionCalendar, SessionDay
from ..time import IST, as_utc
from .auction_days import auction_risk_days
from .data import Bar, MarketData, MarketView, industry_on
from .fillrules import (
    breakout_volume_ok,
    first_trigger,
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
from .ledger import exact_flows, fill_rows, ledger, set_series
from .ledger import trade_rows as ledger_trade_rows
from .ledger import watch_rows as ledger_watch_rows
from .report import (
    DELISTED,
    DEMERGER,
    END_OF_PERIOD,
    BacktestResult,
    SignalRow,
    auction_risk_summary,
    compute_metrics,
    flag_auction_risk,
)
from .slots import order_slots, trigger_rank
from .spec import REGIME_MULT_ONE, BacktestError, RunSpec, sleeve_weight

__all__ = ["run_backtest"]

SKIP_NO_LEVEL = "no_trigger_level"
SKIP_FACTOR_PENDING = "factor_pending"  # CA5: the day's adjusted history is not point in time
SKIP_GAP_NO_INTRADAY = "gap_over_no_intraday"
SKIP_DEMERGER_PENDING = "demerger_pending"  # the demerger exit session (`data/demerger_notice.py`)
SKIP_LOW_VOLUME = "breakout_volume_low"  # a close above the level on too little volume
SKIP_NO_VOLUME = "breakout_volume_missing"  # the volumes the breakout volume test reads
# The sleeve's id in its allocations: the ledger's bot id is new each run, a result's is fixed.
_SANDBOX_SLEEVE = uuid.UUID(int=2)
_ZERO = Decimal(0)


# The extra acts a day's way up allows: a step trail acts once per step the rise reaches.
_STEP_TRAIL_MOVES = 100
_FINEST_TICK = Decimal("0.01")  # the NSE tick below ₹250 (`data.ticks`), the finest slab
# The instants of a simulated day (IST): a later step of the day has a later instant.
_AT_NIGHT = time(16, 0)
_AT_OPEN = time(9, 15)
_AT_LOW = time(9, 20)
_AT_ENTRY = time(9, 30)
_AT_DIP = time(9, 45)  # the entry day's low after the entry
_AT_HIGH = time(10, 0)
# The close phase at live's times (`exits.close` 15:05-15:10, before the 15:15 cut-off), deciding
# and filling on the daily close, which stands in for the price live acts on then (owner
# 2026-10-25, `close_rules_decided_on_official_close`).
_AT_FALL = time(15, 5)
_AT_CLOSE = time(15, 10)
# A close entry (`entry_timing: daily_close`): after the close phase, before the 15:15 cut-off.
_AT_CLOSE_ENTRY = time(15, 11)
_AT_END = time(15, 14)


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
    local = datetime.combine(day, moment, tzinfo=IST)
    return as_utc(local.replace(second=offset % 60, minute=moment.minute + offset // 60))


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


def _levels(row: HatWatchlistEntry) -> dict[str, Decimal | None]:
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
    # The day the way up last raised the stop, and the stop before it (PR 64).
    raised: tuple[date, Decimal] | None = None


class _Sleeve:
    """The run's one sleeve, allocated each night by the live allocator (HAT-BT-014): Live,
    health 1, base weight the run's weight (`sleeve_weight`), at the regime multiplier 1 or, under
    `policy`, the live regime engine's, its last multiplier carried for the smoothing."""

    def __init__(self, spec: RunSpec, data: MarketData, days: tuple[date, ...]) -> None:
        self.spec = spec
        self.days = days
        self.index = {d: n for n, d in enumerate(days)}
        self.breadth = data.breadth
        self.key = sleeve_key(spec.config.bot)
        self.policy = replace(spec.policy.policy, base_weight={self.key: sleeve_weight(spec)})
        self.regime_closes = dict(data.index_closes.get(regime_v1.INDEX_NAME, ()))
        self.vix = dict(data.index_closes.get(regime_v1.VIX_NAME, ()))
        # the index days, as the regime job reads the last `HISTORY_DAYS` of them
        self.index_days = sorted({*self.regime_closes, *self.vix})
        self.confirmation: regime_v1.Confirmation | None = None
        self.regime_mult: Decimal | None = None  # the last allocation's, which smoothing reads
        self.nights = 0  # the sessions the regime has been read for

    def allocate(self, night: date, equity: Decimal) -> Allocation | None:
        """The sleeve's allocation of `night` over `equity`, the account's equity at the night's
        close; None when the regime inputs of the night are missing, as the live allocation then
        fails. Under `policy` the regime engine first reads every earlier session of the run's
        store (the warm-up of the 3-day rule and the smoothing), each allocated over the initial
        equity."""
        if self.spec.conventions.regime_mult == REGIME_MULT_ONE:
            return self._allocate(Decimal(1), equity)
        allocation = None
        while self.nights <= self.index[night]:
            day = self.days[self.nights]
            self.nights += 1
            allocation = self._regime_night(
                day, equity if day == night else self.spec.initial_equity
            )
        return allocation

    def _regime_night(self, day: date, equity: Decimal) -> Allocation | None:
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
        table = self.spec.regime_table
        return self._allocate(regime_v1.multiplier_for(self.confirmation.confirmed, table), equity)

    def _allocate(self, table_value: Decimal, equity: Decimal) -> Allocation:
        (allocation,) = allocate(
            [
                SleeveInput(
                    bot_id=_SANDBOX_SLEEVE,
                    sleeve=self.key,
                    cluster=self.key,
                    lifecycle_state=LIVE,
                    regime_table_value=table_value,
                    health=Health(Decimal(1), probation=False),
                    previous_regime_mult=self.regime_mult,
                )
            ],
            self.policy,
            equity,
        )
        self.regime_mult = allocation.regime_mult
        return allocation


class _Run:
    """One run of the day loop over `MarketData` (see the module docstring)."""

    def __init__(self, spec: RunSpec, data: MarketData) -> None:
        self.spec = spec
        self.data = data
        self.config = spec.config
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
        # Each playbook's configured max chase, as the trigger reads it (HAT-CRIT-122).
        self.chase = {
            b.name: trigger.last_configured_chase([self.config], b.name)
            for b in self.config.playbooks
        }
        self.view = MarketView(data, self.config.regime.index)
        self.days, self.index = self.view.days, self.view.index
        self.period = tuple(d for d in self.days if spec.start <= d <= spec.end)
        if not self.period:
            raise BacktestError("backtest_empty_period")
        self.session, self.bot_id = ledger(spec, data)
        self.calendar = SessionCalendar(
            {
                d: SessionDay(
                    True, _at(d, _AT_OPEN), as_utc(datetime.combine(d, time(15, 30), IST))
                )
                for d in self.period
            }
        )
        self.ladder = LadderState()
        self.held: dict[uuid.UUID, _Held] = {}
        self.seen_results: set[uuid.UUID] = set()
        self.closed: date | None = None  # the last session whose close phase has run
        self.ambiguous_days: set[date] = set()
        self.lower_circuit_days: set[date] = set()
        self.dividend_cash = _ZERO  # the dividends the held positions earned (CA3)
        self.dividend_credits = 0
        self.equity: list[tuple[date, Decimal]] = []
        self.skips: dict[str, int] = {}
        self.gate: dict[date, bool] = {}
        self.signal_rows: list[SignalRow] = []
        self.invested: list[Decimal] = []
        # The sleeve's nightly allocation (HAT-BT-014), by the trade date it sizes.
        self.sleeve = _Sleeve(spec, data, self.days)
        self.allocations: dict[date, Allocation | None] = {}

    def run(self) -> BacktestResult:
        for day in self.period:
            prev = self.view.prev(day)
            if prev is not None:
                self._publish(prev, day)
                self.allocations[day] = self.sleeve.allocate(prev, self._equity_now()[0])
            self._morning(day, prev)
            self._sync_ladder()
            if prev is not None:
                self._entries(day, prev)
            self._close_phase(day)
            if prev is not None:
                self._entries(day, prev, at_close=True)
            self._demergers(day)
            self._delistings(day)
            self._sync_ladder()
            shadow.expire_watchlist(self.session, day)
            self._mark(day)
        self._finish(self.period[-1])
        return self._result()

    def _publish(self, prev: date, day: date) -> None:
        series = series_as_of(self.data.series, prev)
        set_series(self.session, series)
        inputs = ScanInputs(
            as_of=prev,
            members=(),  # replaced by the bot's universe on `prev`
            bars=self.view.window(prev),
            regime_closes=self.view.closes_through(prev),
            features=self.view.features.get(prev),
            series=series,
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
        shadow.evaluate_position(
            self.session,
            position.id,
            view,
            phase,
            at,
            high=held.high,
            low=held.low,
            fills=self.spec.fills,
        )

    def _down_leg(self, position: HatPosition, bar: Bar, day: date, moment: time = _AT_LOW) -> None:
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
        tick = self.view.tick(held.instrument_id, day) or _FINEST_TICK
        for step in range(len(self.config.exits) + 2 + _STEP_TRAIL_MOVES):
            if position.closed_at is not None:
                return
            rules, state = positions.position_state(self.session, position)
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
            self.config.regime, self.view.closes_through(prev)
        )
        for position in self._open_positions():
            held = self.held[position.id]
            # Held into the ex-date: the shares earn the dividend, a session or not.
            per_share = self.view.dividends_on(held.instrument_id, day)
            if per_share and position.qty_open > 0:
                self.dividend_cash += dividend_credit(position.qty_open, per_share)
                self.dividend_credits += 1
            bar = self.view.bar(held.instrument_id, day)
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

    def _daily(
        self, playbook: Playbook, instrument_id: str, day: date, prev: date
    ) -> trigger.DailyInputs:
        """The nightly inputs of an entry on `day` as live reads them (`DailyData.daily`): the
        previous close, the day's tick, ADV(20) and ADTR(20) as of `prev`, and the ATR and swing
        lows of the playbook's own stop rule over the sessions through `prev`."""
        atr, swing = history_inputs(playbook.stop, _History(self.view, instrument_id, prev))
        return trigger.DailyInputs(
            prev_close=self.view.prev_close(instrument_id, day),
            adv20=self.view.feature(prev, instrument_id, "adv20"),
            adtr20=self.view.feature(prev, instrument_id, "adtr20"),
            tick=self.view.tick(instrument_id, day),
            atr=atr,
            swing_lows=tuple(swing),
        )

    def _stop(
        self,
        playbook: Playbook,
        row: HatWatchlistEntry,
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

    def _context(self, row: HatWatchlistEntry, plan: _EntryPlan, prev: date) -> SignalContext:
        """The entry's context as live builds it (`daily.entries`): sized from the night's
        allocation, the book being the run's one sleeve; without one, the allocation inputs are
        missing and the entry is rejected for it."""
        # The free cash net of the entry's slippage and charges: the cash the shares it can
        # afford take at the entry price, so no size the cash term allows overdraws the account
        # (a broker refuses that order); with no cash left the entry is a cash rejection.
        cash = self.spec.fills.affordable(self._cash(), plan.price, row.trade_date) * plan.price
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
            total_equity=capital,
            policy_versions=[self.policy_version],
            ladder=self.ladder,
            gate_on=self.gate.get(row.trade_date, False),
            feed_stale=False,
            quality_failed=False,
            kill=KillState(),
            calendar=self.calendar,
        )

    def _slots(self, day: date, *, at_close: bool) -> list[HatWatchlistEntry]:
        """The day's watchlist rows of the intraday-break playbooks, or with `at_close` of the
        daily-close ones, in the order their triggers take the open slots (HAT-BT-026); close
        entries all trigger at the close, so they keep the score order under `time`."""
        rows = [
            row
            for row in self._watchlist(day)
            if self.config.playbook(row.playbook).at_close == at_close
        ]

        def rank(row: HatWatchlistEntry) -> tuple[int, Decimal]:
            if at_close:
                return 0, _ZERO
            bar = self.view.bar(str(row.instrument_id), day)
            return trigger_rank(
                None if bar is None else bar.open,
                self._level(row),
                self.spec.conventions.entry_fill_rule,
            )

        return order_slots(rows, self.spec.conventions.slot_order, day, rank)

    def _entries(self, day: date, prev: date, *, at_close: bool = False) -> None:
        """The day's entries: the intraday breaks on the way up, or with `at_close` the close
        entries of the `daily_close` playbooks, after the close phase."""
        for n, row in enumerate(self._slots(day, at_close=at_close)):
            instrument_id = str(row.instrument_id)
            if instrument_id in self.view.pending_on(day):
                self._skip(SKIP_FACTOR_PENDING)
                continue
            if (instrument_id, day) in self.data.demerger_exits:
                self._skip(SKIP_DEMERGER_PENDING)  # sold at this close (Q5)
                continue
            bar = self.view.bar(instrument_id, day)
            if bar is None:
                continue  # no session today: the level cannot break
            level = self._level(row)
            if level is None:
                self._skip(SKIP_NO_LEVEL)
                continue
            playbook = self.config.playbook(row.playbook)
            if at_close:
                price = self._close_fill(playbook, instrument_id, bar, day, level)
            else:
                price = self._fill(row, bar, day, level)
            if isinstance(price, str):
                self._skip(price)
                continue
            if price is None:
                continue  # the level did not break: the row expires at the session end
            # A close entry comes after the whole day's path: its low is the day's low.
            lows = (
                (bar.low, None)
                if at_close
                else self._entry_lows(playbook, instrument_id, bar, level, price, (day, prev))
            )
            if isinstance(lows, str):
                self._skip(lows)
                continue
            before, dip = lows
            daily = self._daily(playbook, instrument_id, day, prev)
            planned = self._stop(playbook, row, daily, price, before)
            stop = planned.stop
            if stop is None:
                self._skip(planned.skip or SKIP_BAD_INPUT)
                continue
            at = _at(day, _AT_CLOSE_ENTRY if at_close else _AT_ENTRY, n)
            locked = self._locked(instrument_id, price, bar.high, day)
            failed, filters = self._entry_filters(
                playbook, row, daily, (price, stop, level, locked), at
            )
            plan = _EntryPlan(price, stop, dip, locked, failed, level, filters, at_close)
            self._enter(row, bar, plan, prev, at)

    def _level(self, row: HatWatchlistEntry) -> Decimal | None:
        """The row's trigger level: its playbook's level expression over the row's setup levels
        on daily bars (`trigger.daily_level`, the opening range unavailable, HAT-BT-025)."""
        return trigger.daily_level(self.config.playbook(row.playbook), _levels(row))

    def _fill(
        self, row: HatWatchlistEntry, bar: Bar, day: date, level: Decimal
    ) -> Decimal | str | None:
        """The entry fill of the row's `level` under the run's entry fill rule, None when the
        level did not break, or the skip reason when there is no tick to penetrate by
        (HAT-BT-020)."""
        rule = self.spec.conventions.entry_fill_rule
        # The tick matters only to a penetration from below; read it only then.
        tick = None
        if rule == PENETRATION and bar.open <= level < bar.high:
            tick = self.view.tick(str(row.instrument_id), day)
            if tick is None:
                return SKIP_BAD_INPUT  # no tick to penetrate by (nor to plan a stop on)
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

    def _entry_filters(
        self,
        playbook: Playbook,
        row: HatWatchlistEntry,
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
        reason when a low-of-day stop cannot know the low before the break (HAT-BT-020, PR 60)."""
        day, prev = days
        intraday = self.data.intraday.get((instrument_id, day), ())
        adr20 = self.view.feature(prev, instrument_id, "adr20")
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
        request = entries.EntryRequest(
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
        # a backtest signal has no evidence of its own: its label is low, `backtest`
        outcome = entries.open_entry(
            self.session,
            request,
            fills=self.spec.fills.within(self._cash()),
            confidence=BACKTEST_CONFIDENCE,
        )
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
                allocation=self.allocations.get(row.trade_date),
            )
        )
        if outcome.position_id is None:
            return
        position = self.session.get(HatPosition, outcome.position_id)
        if position is None:  # pragma: no cover - open_entry returned the id it wrote
            return
        day = row.trade_date
        self.held[position.id] = _Held(instrument_id, day, price, price)
        if plan.at_close:
            return  # entered at the close: the day's path is behind it
        if dip is not None and dip < price:
            # From the entry down to the low: a stop it reaches fills at min(entry, stop).
            self._down_leg(position, Bar(price, bar.high, dip, bar.close), day, _AT_DIP)
        self._up_leg(position, price, bar.high, day)

    # --- the close, delistings, the period end ------------------------------------------------

    def _close_view(self, position: HatPosition, bar: Bar, day: date) -> exits.ExitView:
        """The position's view at the day's close, the stand-in for live's pre-15:15 price
        (`exits.view_from_bars`, as live builds it)."""
        held = self.held[position.id]
        rules = self.config.exits
        before = self.view.bars_through(
            held.instrument_id, day, exits.history_sessions(rules), inclusive=False
        )
        since = (
            [b.high for b in self.view.bars_since(held.instrument_id, held.entry_day, day)]
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
            since,
        )

    def _close_phase(self, day: date) -> None:
        for position in self._open_positions():
            held = self.held[position.id]
            bar = self.view.bar(held.instrument_id, day)
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
            fills=self.spec.fills,
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

    def _delistings(self, day: date) -> None:
        """An instrument's last traded session closes its open position at that close
        (HAT-BT-012): a delisted stock's loss stays in the results."""
        for position in self._open_positions():
            held = self.held[position.id]
            if self.data.last_traded.get(held.instrument_id) != day:
                continue
            bar = self.view.bar(held.instrument_id, day)
            last = self._last_close(held.instrument_id)
            price = bar.close if bar is not None else (last[1] if last is not None else None)
            if price is not None:
                self._close(position, price, DELISTED, _at(day, _AT_END))

    def _finish(self, last: date) -> None:
        for position in self._open_positions():
            held = self.held[position.id]
            price = self._mark_price(held.instrument_id, last, Decimal(position.avg_entry))
            held.low = min(held.low, price)  # a suspension mark below the traded lows is the MAE
            self._close(position, price, END_OF_PERIOD, _at(last, _AT_END, 30))
        self.session.flush()
        if self.equity:
            self.equity[-1] = (last, self._marked(last)[0])

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
        cash = self.spec.initial_equity + self.dividend_cash
        for side, value, fees in exact_flows(self.session):
            cash += value if side == "sell" else -value
            cash -= fees
        return cash

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

    def _marked(self, day: date) -> tuple[Decimal, Decimal]:
        """(the account's mark on `day`, the marked value of its open positions)."""
        held_value = _ZERO
        for position in self._open_positions():
            held = self.held[position.id]
            price = self._mark_price(held.instrument_id, day, Decimal(position.avg_entry))
            held_value += position.qty_open * price
        return self._cash() + held_value, held_value

    def _mark(self, day: date) -> None:
        equity, held_value = self._marked(day)
        self.equity.append((day, equity))
        self.invested.append(held_value / equity if equity > 0 else _ZERO)

    # --- the result --------------------------------------------------------------------------

    def _result(self) -> BacktestResult:
        watch_rows = ledger_watch_rows(self.session, self.bot_id)
        trade_rows = tuple(
            replace(t, qty_real=self.view.real_shares(t.instrument_id, t.entry_date, t.qty))
            for t in flag_auction_risk(
                ledger_trade_rows(self.session, self.bot_id, watch_rows),
                auction_risk_days(self.view.days),
            )
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
        )
        metrics["auction_risk_exits"] = auction_risk_summary(trade_rows)
        metrics["dividend_income"] = str(self.dividend_cash)
        metrics["dividend_credits"] = self.dividend_credits
        result = BacktestResult(
            trades=trade_rows,
            equity=equity,
            watchlists=watch_rows,
            signals=tuple(self.signal_rows),
            fills=fill_rows(self.session),
            skips=skips,
            metrics=metrics,
            invested=invested,
            allocations=dict(self.allocations),
        )
        self.session.close()
        return result


def run_backtest(spec: RunSpec, data: MarketData) -> BacktestResult:
    """Run `spec` over `data` and return its trades, equity curve and metrics."""
    if spec.start > spec.end:
        raise BacktestError("backtest_bad_period")
    if not spec.initial_equity.is_finite() or spec.initial_equity <= 0:
        raise BacktestError("backtest_bad_equity")
    return _Run(spec, data).run()

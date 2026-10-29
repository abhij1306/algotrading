"""What a backtest run is (HAT-BT-010, HAT-BT-070): its spec, its conventions and the fixed facts
every run records. A leaf of the backtest package: the loop, the report and the evidence read
it, and it imports none of them.

A run is one strategy version under one risk-policy version over a period, with a fill model
(slippage and cost model, `costs`) and its conventions (`RunConventions`, PR 62, O5): the entry
fill rule, the slot order, the ambiguous-day rule, the bar-less sessions before the suspension
markdown, the sleeve weight, the regime rule, the entry bars and the volume pace rule. A run
flagged for promotion (`for_promotion`, the only kind `publish_version` accepts and the sleeve's
evidence reads) is refused under conventions live does not trade by. Every run records its
conventions and hashes them into its `config_digest`, so two runs under different conventions
never read as comparable.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from ..costs import BacktestFills
from ..engine.portfolio import regime as regime_v1
from ..engine.portfolio.allocator import sleeve_key
from ..engine.strategy.config import Playbook, StrategyConfig
from ..errors import HatError
from ..risk.policy import PolicyError, PolicyVersion, parse_policy
from .fills import (
    AMBIGUOUS_OPTIMISTIC,
    AMBIGUOUS_RULES,
    ENTRY_FILL_RULES,
    FILL_PATH,
    GAP_THROUGH_RULE,
    PENETRATION,
    SUSPENSION_BAND_PCT,
    SUSPENSION_SESSIONS,
    TOUCH,
    TRIGGER_PROXY,
)
from .overlay import OverlayConfig
from .overrides import Overrides
from .slots import SLOT_ORDER_RS, SLOT_ORDER_TIME, is_slot_order

__all__ = [
    "ANALYST_BEAT_NOT_EVALUATED",
    "CATALYST_NOT_EVALUATED",
    "ENTRY_BARS_5M",
    "ENTRY_BARS_DAILY",
    "ENTRY_BARS_RULES",
    "ENTRY_DAY_LOW",
    "ENTRY_DAY_LOW_ESTIMATED",
    "EP_TRIGGER_ESTIMATED",
    "ESTIMATE_GAPS",
    "GAP_OVER_AT_OPEN",
    "GAP_PLAYBOOKS",
    "INITIAL_EQUITY",
    "KNOWN_GAPS",
    "LEVEL_BREAK_ESTIMATED",
    "LOWER_CIRCUIT_RULE",
    "REGIME_MULT_ONE",
    "REGIME_MULT_POLICY",
    "REGIME_MULT_RULES",
    "RESEARCH_SLEEVE_WEIGHT",
    "TOUCH_GAP",
    "VOLUME_PACE_ESTIMATED",
    "VOLUME_PACE_FULL_DAY",
    "VOLUME_PACE_LIVE",
    "VOLUME_PACE_RULES",
    "BacktestError",
    "CoSleeve",
    "RunConventions",
    "RunSpec",
    "known_gaps",
    "membership_key",
    "sleeve_weight",
    "with_cooldown",
]

INITIAL_EQUITY = Decimal(1_000_000)
# The weight a research run sizes a sleeve at when the policy gives it none (`sleeve_weight`):
# the seed policy's weight of a sleeve it trades.
RESEARCH_SLEEVE_WEIGHT = Decimal("0.5")
# What the loop does not model, by fixed code (HAT-BT-070); measured in shadow. Every run records
# these; `ESTIMATE_GAPS` only when it approximated them.
KNOWN_GAPS = (
    "close_exits_at_official_close",
    # the close rules decide and fill on the daily close: the auction price stands in for the
    # price live acts on before the 15:15 cut-off, a small, consistent bias (owner 2026-10-25)
    "close_rules_decided_on_official_close",
    # that bias is not symmetric: on an F&O expiry or an index-rebalance close the auction close
    # can sit far further from the 15:05 price than usual; those exits are flagged per trade
    # (`auction_risk`) and counted (`auction_risk_exits`) so they can be discounted
    "close_bias_large_on_expiry_and_rebalance_days",
    "kill_switches_not_simulated",
    "upper_circuit_inferred_from_bar",  # O=H=C at an NSE band (lower: O=H=L=C, PR 64)
    "multipliers_fixed_at_one",  # health is 1; regime too unless `regime_mult` is `policy`
    # no regime table is fitted to each style: S1-S3 name the `momentum` profile
    # (`registry.Strategy.regime`), and the EMA pullback (S4) and RSI(2) mean-reversion (S5)
    # profiles have no stored row, so they size under the most conservative one (today momentum's)
    "regime_table_shared_momentum",
    # the run is one sleeve: its capital is the account's equity and only its positions count,
    # so the account-level caps (global open risk, the cluster cap, the position and entry
    # slots) never bind across sleeves as they do live
    "single_sleeve_caps",
    # `entry_timing: daily_close` enters at the daily close (the auction price standing in for
    # the price before the 15:15 cut-off) and live does not trade it (`GAP_PLAYBOOKS`)
    "daily_close_entry_not_live",
)
# A session's entries follow the live trigger on its stored 5-minute bars (`intraday`); without
# them (no bars stored, or a `daily` run) they are estimated from the daily bar, and the run
# records what it estimated, by fixed code:
# an intraday break taken as the daily high's break of the setup level, without the opening
# range or the close confirmation (a playbook with an enabled `volume_pace` is one whose trigger
# live decides on more than the level: `GAP_PLAYBOOKS`)
LEVEL_BREAK_ESTIMATED = "trigger_is_level_break_only"
# an enabled `volume_pace` judged on the full day's volume against vs_avg50 x the mean of the 50
# sessions through the previous one: a look-ahead, as the day's volume is unknown at the trigger
# (the daily estimate always; a 5-minute session too under `volume_pace: full_day`)
VOLUME_PACE_ESTIMATED = "volume_pace_on_full_day_volume"
ENTRY_DAY_LOW_ESTIMATED = "entry_day_low_estimated"  # the pre-break low is the adr20 estimate
GAP_OVER_AT_OPEN = "gap_over_fills_at_open"  # live fills at the first tick after the open
# a pre-open playbook's trigger (opening-range break, early volume) estimated from the daily
# bar with the constants calibrated on the 5-minute years (`intraday.EP_ESTIMATE`)
EP_TRIGGER_ESTIMATED = "episodic_pivot_trigger_estimated"
# `catalyst_attached` has no announcements to read before the store's (none stored): never
# blocking in a run, the gap and the early volume stand in for it; not recorded for a playbook
# whose `earnings_catalyst` judges the catalyst on the stored results filings
CATALYST_NOT_EVALUATED = "catalyst_not_evaluated"
# `earnings_catalyst` judges reported growth only: no analyst estimates are stored, so a beat of
# expectations is not modelled (plan B1)
ANALYST_BEAT_NOT_EVALUATED = "analyst_beat_not_evaluated"
ESTIMATE_GAPS = (
    LEVEL_BREAK_ESTIMATED,
    VOLUME_PACE_ESTIMATED,
    ENTRY_DAY_LOW_ESTIMATED,
    GAP_OVER_AT_OPEN,
    EP_TRIGGER_ESTIMATED,
    CATALYST_NOT_EVALUATED,
    ANALYST_BEAT_NOT_EVALUATED,
)
# How a run decides a session's entries (`RunConventions.entry_bars`).
ENTRY_BARS_5M = "bars_5m"  # the default: the live trigger on the stored 5-minute bars
ENTRY_BARS_DAILY = "daily"  # the variant: the daily estimate everywhere, to compare the models
ENTRY_BARS_RULES = (ENTRY_BARS_5M, ENTRY_BARS_DAILY)
TRIGGER_5M = "bars_5m_live_trigger_else_daily_bar_level_break"
# What decided an entry (`entry_bars_by_year`): the session's 5-minute bars, the daily estimate,
# or the daily close of a `daily_close` playbook.
BASIS_5M = "bars_5m"
BASIS_DAILY = "daily"
BASIS_CLOSE = "close"
# How a run judges an enabled `volume_pace` (`RunConventions.volume_pace`): `live`, the default,
# replays live's pace on a session's 5-minute bars and reads the full day's volume only on the
# daily estimate; `full_day` reads the full day's volume on every session (the S1 book spec's
# owner decision 7), the 5-minute replay then deciding the trigger without the pace.
VOLUME_PACE_LIVE = "live"
VOLUME_PACE_FULL_DAY = "full_day"
VOLUME_PACE_RULES = (VOLUME_PACE_LIVE, VOLUME_PACE_FULL_DAY)


def _preopen(playbook: Playbook) -> bool:
    return playbook.preopen


VOLUME_PACE = "volume_pace"


def _volume_pace(playbook: Playbook) -> bool:
    return any(i.enabled and i.type == VOLUME_PACE for i in playbook.trigger)


def _daily_close(playbook: Playbook) -> bool:
    return playbook.at_close


# The playbooks a known gap names, by what they use rather than by name: the lifecycle gate
# refuses a version that enables one of them on this evidence (PR 69, HAT-PORT-061).
# `episodic_pivot_trigger_estimated` names every pre-open playbook (`Playbook.preopen`), whatever
# it is called, and `episodic_pivot_not_backtested` too (no run records it now; a stored run made
# before the episodic pivot was backtested holds it); `trigger_is_level_break_only` and
# `volume_pace_on_full_day_volume` every one with an enabled `volume_pace` trigger (a run made
# before the first named any playbook names them too), and
# `daily_close_entry_not_live` every one whose level trigger enters at the daily close (a timing
# no earlier version could hold). A gap not listed here names no playbook: it holds for all of
# them alike, a measured approximation of the loop, and blocks none; `catalyst_not_evaluated`
# among them, so a pre-open entry decided on 5-minute bars whose only gap is the catalyst is
# evidence.
GAP_PLAYBOOKS: dict[str, Callable[[Playbook], bool]] = {
    EP_TRIGGER_ESTIMATED: _preopen,
    "episodic_pivot_not_backtested": _preopen,
    LEVEL_BREAK_ESTIMATED: _volume_pace,
    VOLUME_PACE_ESTIMATED: _volume_pace,
    "daily_close_entry_not_live": _daily_close,
}
# Reported, besides `KNOWN_GAPS`, by a run of more than one strategy (`RunSpec.co_sleeves`): its
# window is chosen for the portfolio, so it may hold a member strategy's own in-sample dates (S2
# chose its config on 2022-2024, inside a combined hold-out from 2022): such a hold-out is not
# independent of that strategy's selection.
CO_SLEEVE_GAPS = ("co_sleeve_window_overlaps_member_in_sample",)
# Reported by a run with the ETF overlay (`RunSpec.overlay`): today's tax rules on every year, the
# year's tax paid on its last session, a sleeve fill without its 5-minute ETF bar priced from the
# previous close (counted `sleeve_price_estimated`, `overlay.sleeve_price`), LIQUIDBEES' return
# from the 1D Rate Index less its expense ratio where its dividends are not known (the ranges in
# `overlay.liquidbees_tr_proxy`), ETF trades on the stock cost model (equity delivery STT on every
# ETF, LIQUIDBEES included).
OVERLAY_GAPS = (
    "tax_rules_current_for_all_years",
    "tax_paid_at_fy_end",
    "sleeve_price_estimated_from_prev_close",
    "liquidbees_tr_proxy",
    "etf_costs_on_stock_cost_model",
)
# Reported, besides `KNOWN_GAPS`, by a run under the `touch` entry rule (PR 62).
TOUCH_GAP = "entry_fills_on_touch"  # a high at the level fills; live needs a trade above it
# A stop on a session pinned at its lower band waits for the next open with a bar not pinned.
LOWER_CIRCUIT_RULE = "carry_to_next_unlocked_open"
MAX_SUSPENSION_SESSIONS = 250  # about a year of sessions
# The regime multiplier a run sizes with (PR 68, HAT-BT-014): 1, or the live regime engine's.
REGIME_MULT_ONE = "1"
REGIME_MULT_POLICY = "policy"
REGIME_MULT_RULES = (REGIME_MULT_ONE, REGIME_MULT_POLICY)
# How the loop reads the entry day's low before and after the break (HAT-BT-020, PR 60).
ENTRY_DAY_LOW = "bars_5m_else_max_low_min_open_level_less_adr20"


class BacktestError(HatError):
    """A run was refused, or failed (`backtest_error`); `code` is a fixed word and carries no
    data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RunConventions:
    """The rules a run chooses besides its slippage and cost model (PR 62, O5): the entry fill
    rule (HAT-BT-020), the slot order (HAT-BT-026, PR 63), the ambiguous-day rule (HAT-BT-021)
    and the bar-less sessions before the suspension markdown (PR 64), the sleeve weight (None:
    the policy's base weight), the regime rule the entries are sized under (HAT-BT-014, PR 68),
    the start capital and the config and policy overrides (`overrides`). Refused at construction
    (`BacktestError`): an unknown entry fill rule, slot order, ambiguous-day rule or regime rule,
    a bar-less session count that is not a whole number in 0..`MAX_SUSPENSION_SESSIONS`, a
    sleeve weight that is not a Decimal in (0, 1], and a start capital that is not a positive
    Decimal."""

    entry_fill_rule: str = PENETRATION  # `touch` is the variant
    slot_order: str = SLOT_ORDER_TIME  # `score` and `random:K` are variants
    ambiguous: str = AMBIGUOUS_OPTIMISTIC  # `pessimistic` fills a raised stop's dip
    suspension_sessions: int = SUSPENSION_SESSIONS
    sleeve_weight: Decimal | None = None
    regime_mult: str = REGIME_MULT_ONE  # `policy` applies the live regime engine
    start_capital: Decimal = INITIAL_EQUITY
    entry_bars: str = ENTRY_BARS_5M  # `daily` decides every entry on the daily estimate
    volume_pace: str = VOLUME_PACE_LIVE  # `full_day` reads the day's volume on 5-minute days too
    # the run's config and policy overrides (`overrides`), already applied to its spec
    overrides: Overrides = field(default_factory=Overrides, hash=False)

    def __post_init__(self) -> None:
        if self.entry_fill_rule not in ENTRY_FILL_RULES:
            raise BacktestError("backtest_bad_fill_rule")
        if not is_slot_order(self.slot_order):
            raise BacktestError("backtest_bad_slot_order")
        if self.ambiguous not in AMBIGUOUS_RULES:
            raise BacktestError("backtest_bad_ambiguous_rule")
        after = self.suspension_sessions
        if (
            isinstance(after, bool)
            or not isinstance(after, int)
            or not 0 <= after <= MAX_SUSPENSION_SESSIONS
        ):
            raise BacktestError("backtest_bad_suspension")
        weight = self.sleeve_weight
        if weight is not None and not (
            isinstance(weight, Decimal) and weight.is_finite() and 0 < weight <= 1
        ):
            raise BacktestError("backtest_bad_sleeve_weight")
        if self.regime_mult not in REGIME_MULT_RULES:
            raise BacktestError("backtest_bad_regime_mult")
        capital = self.start_capital
        if not (isinstance(capital, Decimal) and capital.is_finite() and capital > 0):
            raise BacktestError("backtest_bad_start_capital")
        if not isinstance(self.overrides, Overrides):
            raise BacktestError("backtest_bad_override")
        if self.entry_bars not in ENTRY_BARS_RULES:
            raise BacktestError("backtest_bad_entry_bars")
        if self.volume_pace not in VOLUME_PACE_RULES:
            raise BacktestError("backtest_bad_volume_pace")

    def check_promotion(self, slippage_bps: Decimal) -> None:
        """Refuse these conventions for a run flagged for promotion: perfect fills (a slippage
        of 0), the optimistic `touch` rule, a slot order other than the estimated trigger time
        or the RS rank (`rs`, the S1 book's sizing rule), a suspension markdown later than
        `SUSPENSION_SESSIONS`, a weight other than the policy's base weight (PR 68) or a regime
        rule other than `policy` (PR 69)."""
        if slippage_bps == 0:
            raise BacktestError("backtest_promotion_without_slippage")
        if self.entry_fill_rule != PENETRATION:
            raise BacktestError("backtest_promotion_on_touch")
        if self.slot_order not in (SLOT_ORDER_TIME, SLOT_ORDER_RS):
            raise BacktestError("backtest_promotion_off_time_order")
        if self.suspension_sessions > SUSPENSION_SESSIONS:
            raise BacktestError("backtest_promotion_late_suspension")
        if self.sleeve_weight is not None:
            raise BacktestError("backtest_promotion_off_base_weight")
        if self.regime_mult != REGIME_MULT_POLICY:  # live sizes under the regime engine (PR 69)
            raise BacktestError("backtest_promotion_regime_variant")
        if not self.overrides.empty:  # the evidence is of the version's own config and policy
            raise BacktestError("backtest_promotion_with_overrides")
        if self.entry_bars != ENTRY_BARS_5M:  # live triggers on 5-minute bars
            raise BacktestError("backtest_promotion_daily_only")

    def record(
        self,
        fills: BacktestFills,
        window: tuple[date, date],
        weight: Decimal,
        table: Mapping[str, Decimal],
    ) -> dict[str, Any]:
        """The conventions a run used, as recorded in its metrics and hashed into its
        `config_digest`, with the run's fills, its resolved sleeve weight and the regime table it
        sized under, and its overrides when it has any (a run without keeps the digest it had
        before overrides existed). The keys and their order are the stored format: a change makes
        new digests differ from every stored run's."""
        return {
            "fill_path": FILL_PATH,
            "entry_fill_rule": self.entry_fill_rule,
            "gap_through_rule": GAP_THROUGH_RULE,
            "trigger_proxy": TRIGGER_5M if self.entry_bars == ENTRY_BARS_5M else TRIGGER_PROXY,
            # normalised, so 20 and 20.0 bps record (and hash) alike
            "slippage_bps": format(abs(fills.slippage_bps).normalize(), "f"),  # -0 is 0
            # the version and the digest of each dated file in force in the run's window
            "cost_model": fills.schedule.to_json(*window),
            "slot_order": self.slot_order,
            "ambiguous": self.ambiguous,
            "lower_circuit": LOWER_CIRCUIT_RULE,
            "suspension_markdown": {
                "after_sessions": self.suspension_sessions,
                "band_pct": str(SUSPENSION_BAND_PCT),
            },
            # the sizing; the regime table only when it is applied
            "sleeve_weight": format(weight.normalize(), "f"),
            "regime_mult": self.regime_mult,
            "health_mult": "1",
            **(
                {"regime_table": {k: str(table[k]) for k in regime_v1.STATES}}
                if self.regime_mult == REGIME_MULT_POLICY
                else {}
            ),
            # only when it is not the default, so a default run keeps the digest it had before
            **(
                {}
                if self.start_capital == INITIAL_EQUITY
                else {"start_capital": format(self.start_capital.normalize(), "f")}
            ),
            **({} if self.overrides.empty else {"overrides": self.overrides.record()}),
            **({} if self.volume_pace == VOLUME_PACE_LIVE else {"volume_pace": self.volume_pace}),
        }


@dataclass(frozen=True)
class CoSleeve:
    """A strategy run beside the run's own on the same account (a portfolio run): its config
    (overrides applied), version, weight of the account and the regime table it sizes under.
    Its trades share the cash, the policy's account caps and the market with the run's."""

    config: StrategyConfig
    strategy_version_id: uuid.UUID
    weight: Decimal
    regime_table: Mapping[str, Decimal] = field(
        default_factory=lambda: dict(regime_v1.DEFAULT_MULTIPLIERS), hash=False
    )
    overrides: Overrides = field(default_factory=Overrides, hash=False)  # those `config` took

    def record(self) -> dict[str, Any]:
        """As the conventions record it (the overrides only when set, as the run's own)."""
        return {
            "strategy_version_id": str(self.strategy_version_id),
            "bot": self.config.bot,
            "weight": format(self.weight.normalize(), "f"),
            "regime_table": {k: str(self.regime_table[k]) for k in regime_v1.STATES},
            **({} if self.overrides.empty else {"overrides": self.overrides.record()}),
        }


@dataclass(frozen=True)
class RunSpec:
    """One run: a strategy version, one risk-policy version, a period and a fill model; in a
    portfolio run also the strategies beside it on the same account (`co_sleeves`) and the ETF
    overlay (`overlay`). A run without either is the run it always was, digest included."""

    config: StrategyConfig
    strategy_version_id: uuid.UUID
    policy: PolicyVersion
    start: date
    end: date
    fills: BacktestFills
    initial_equity: Decimal = INITIAL_EQUITY
    for_promotion: bool = False  # the run may evidence a version (`publish_version`)
    conventions: RunConventions = field(default_factory=RunConventions)
    # the regime multiplier table the `policy` rule sizes under: the stored one live allocates
    # with, read when the run starts (C23 E3, `regime_service.stored_regime_table`)
    regime_table: Mapping[str, Decimal] = field(
        default_factory=lambda: dict(regime_v1.DEFAULT_MULTIPLIERS), hash=False
    )
    co_sleeves: tuple[CoSleeve, ...] = ()
    overlay: OverlayConfig | None = None

    def __post_init__(self) -> None:
        """Refuse a run flagged for promotion under conventions it may not use, a sleeve weight
        the policy cannot give (`sleeve_weight`) and a regime table without an exact multiplier
        for every state: a spec that exists is a valid run."""
        if self.for_promotion:
            self.conventions.check_promotion(self.fills.slippage_bps)
        weight = sleeve_weight(self)
        if self.co_sleeves:
            if self.for_promotion:  # evidence is of one sleeve
                raise BacktestError("backtest_promotion_portfolio")
            bots = [self.config.bot, *(c.config.bot for c in self.co_sleeves)]
            if len(set(bots)) != len(bots):
                raise BacktestError("backtest_portfolio_duplicate_strategy")
            if any(c.config.regime.index != self.config.regime.index for c in self.co_sleeves):
                raise BacktestError("backtest_sweep_mixed_index")
            weights = [weight, *(c.weight for c in self.co_sleeves)]
            cap = self.policy.policy.cluster_cap
            if not all(0 < w <= cap for w in weights) or sum(weights) > 1:
                raise BacktestError("backtest_bad_sleeve_weight")
        if self.overlay is not None and self.for_promotion:
            raise BacktestError("backtest_promotion_portfolio")
        if set(self.regime_table) != set(regime_v1.STATES) or not all(
            isinstance(v, Decimal) and v.is_finite() for v in self.regime_table.values()
        ):
            raise BacktestError("backtest_bad_regime_table")

    def recorded_conventions(self) -> dict[str, Any]:
        """The conventions block recorded in the run's metrics, which `config_digest` hashes;
        the co-sleeves and the overlay only when the run has them."""
        out = self.conventions.record(
            self.fills, (self.start, self.end), sleeve_weight(self), self.regime_table
        )
        if self.co_sleeves:
            out["co_sleeves"] = [c.record() for c in self.co_sleeves]
        if self.overlay is not None:
            out["overlay"] = self.overlay.record()
        return out


def sleeve_weight(spec: RunSpec) -> Decimal:
    """The run's sleeve weight (HAT-BT-014): its conventions' `sleeve_weight`, else the
    policy's base weight of the strategy's sleeve. When the policy has none or gives it 0 (a
    policy may: a sleeve still in research), a research run sizes at `RESEARCH_SLEEVE_WEIGHT`
    (the cluster cap, if lower), recorded in its conventions like an explicit weight, and a run
    flagged for promotion is refused `backtest_no_base_weight`: live would give the sleeve
    nothing. Above the policy's cluster cap, which the allocator would silently cut it to, the
    weight is refused `backtest_weight_above_cluster_cap`."""
    policy = spec.policy.policy
    weight = spec.conventions.sleeve_weight
    if weight is None:
        weight = policy.base_weight.get(sleeve_key(spec.config.bot))
        if weight is None or weight <= 0:
            if spec.for_promotion:
                raise BacktestError("backtest_no_base_weight")
            weight = min(RESEARCH_SLEEVE_WEIGHT, policy.cluster_cap)
    if weight > policy.cluster_cap:
        raise BacktestError("backtest_weight_above_cluster_cap")
    return weight


def known_gaps(spec: RunSpec, estimated: Iterable[str] | None = None) -> list[str]:
    """The run's known gaps (HAT-BT-070): `KNOWN_GAPS`, the `ESTIMATE_GAPS` it `estimated` (all
    of them when not known yet: a run that did not finish may have any), and the touch fill
    under `touch`."""
    found = set(ESTIMATE_GAPS if estimated is None else estimated)
    return [
        # a portfolio run's account caps bind across its sleeves, as live
        *(g for g in KNOWN_GAPS if not (spec.co_sleeves and g == "single_sleeve_caps")),
        *(CO_SLEEVE_GAPS if spec.co_sleeves else ()),
        *(OVERLAY_GAPS if spec.overlay is not None else ()),
        *(code for code in ESTIMATE_GAPS if code in found),
        *([TOUCH_GAP] if spec.conventions.entry_fill_rule == TOUCH else []),
    ]


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


def membership_key(definition: Mapping[str, Any]) -> str | None:
    """What fixes a universe version's backtest member set: its index and the owner's includes
    and excludes (the constituents list it was published from does not, as the run reads the
    index point in time), as a digest a run records and its evidence must match. None for a
    version naming no index."""
    index = definition.get("index")
    if not index:
        return None
    text = json.dumps(
        {
            "index": index,
            "include": sorted(definition.get("include", ())),
            "exclude": sorted(definition.get("exclude", ())),
        },
        sort_keys=True,
    )
    return hashlib.sha256(text.encode()).hexdigest()

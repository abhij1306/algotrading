"""The strategy config model: nine stages, playbooks, criterion instances.

A config is data: criterion instances for universe, setup, trigger and entry filters, one regime
rule, one stop rule per playbook and an exit stack. It holds no risk, size or capital number
(HAT-BOT-072): those live in the risk policy, and `parse_config` rejects any such key at any
depth with the reason `risk_key` before anything else is read.

`parse_config` accepts the YAML shape of the seeds (single playbook inline, `stage` and `order`
implied by position) and the canonical shape `StrategyConfig.to_json` writes; parsing the
canonical form gives the same config, so `config_hash` of a stored config is stable.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from ...confighash import config_hash as _hash
from ...data.index_history import INDEX_NAMES, INDIA_VIX
from ...errors import HatError
from ...risk.policy import POLICY_FIELDS, SEED_POLICY
from ..criteria import PREOPEN, REGISTRY, UnknownCriterionError, get_type
from ..criteria.base import STAGES, dec
from ..criteria.live import (
    DAILY_CLOSE,
    INTRADAY_BREAK,
    LEVEL_OPENING_RANGE,
    GapUpParams,
    PriceBreaksLevelParams,
    level_names,
)

PRICE_BREAKS_LEVEL = "price_breaks_level"
GAP_UP = "gap_up"

DEFAULT_THRESHOLD = 0.6
DEFAULT_PLAYBOOK = "default"
# The setup criteria that publish the pivot `close_vs_pivot` reads (HAT-CRIT-123).
PIVOT_SETUPS = ("vcp", "book_vcp")
MAX_INSTANCES = 40
RISK_KEY = re.compile(
    r"risk|capital|allocation|p_max|position|qty|quantity|leverage|drawdown|stopout"
    r"|cluster_cap|adv_cap|exposure|kill|multiplier|base_weight|equity|notional"
    r"|sizing|(?:^|_)size|stop_price|entry_price",
    re.IGNORECASE,
)


def _policy_key_names() -> frozenset[str]:
    """Every key the risk policy document uses at any depth, bar the per-strategy names under
    `p_max` and `base_weight` and the bare bounds `lo` and `hi`: a config naming one of them is
    trying to set a number that only the policy may hold."""
    names = set(POLICY_FIELDS)

    def walk(node: object) -> None:
        if isinstance(node, Mapping):
            for key, value in node.items():
                if key not in ("p_max", "base_weight"):
                    names.add(str(key))
                    walk(value)

    walk(SEED_POLICY)
    return frozenset(names - {"lo", "hi"})


POLICY_KEYS = _policy_key_names()
NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class ConfigError(HatError):
    """A strategy config was refused. `problems` lists `(path, code)` pairs; neither carries
    submitted values."""

    def __init__(self, problems: Sequence[tuple[str, str]]) -> None:
        super().__init__("invalid strategy config")
        self.problems = tuple(problems)

    @property
    def codes(self) -> set[str]:
        return {code for _, code in self.problems}


class Model(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False, frozen=True)


# --- criterion instances ----------------------------------------------------------------------


class Instance(Model):
    """HAT-CRIT-020: the seven fields. `weight` counts only for `mode = score`."""

    type: str
    stage: Literal["universe", "setup", "trigger", "entry_filter"]
    mode: Literal["hard", "score"] = "hard"
    weight: float = Field(default=1.0, ge=0, le=1000)
    params: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    order: int = Field(ge=0, le=10_000)

    @model_validator(mode="after")
    def _check(self) -> Instance:
        try:
            ctype = get_type(self.type)
        except UnknownCriterionError:
            raise ValueError("unknown_type") from None
        if self.stage != ctype.stage and self.stage not in ctype.extra_stages:
            raise ValueError("wrong_stage")
        if self.mode == "score" and self.stage != "setup":
            raise ValueError("score_outside_setup")
        if self.mode == "score" and self.weight <= 0:
            raise ValueError("score_needs_weight")
        # Normalise: every parameter explicit, so the stored config and its hash do not depend
        # on which defaults the submitter left out.
        normal = ctype.params.model_validate(self.params).model_dump(mode="json")
        object.__setattr__(self, "params", normal)
        return self


def _sorted_unique(instances: list[Instance]) -> list[Instance]:
    orders = [i.order for i in instances]
    if len(set(orders)) != len(orders):
        raise ValueError("duplicate_order")
    return sorted(instances, key=lambda i: i.order)


class SetupStage(Model):
    threshold: float = Field(default=DEFAULT_THRESHOLD, ge=0, le=1)
    criteria: list[Instance] = Field(default_factory=list, max_length=MAX_INSTANCES)

    @model_validator(mode="after")
    def _check(self) -> SetupStage:
        object.__setattr__(self, "criteria", _sorted_unique(self.criteria))
        # HAT-CRIT-123: `close_vs_pivot` is a gate, so hard only, and reads the pivot of an
        # enabled `vcp` or `book_vcp` evaluated before it; without one it could never pass, and
        # the playbook would silently stop trading.
        pivot_seen = False
        for instance in self.criteria:
            if instance.type == "close_vs_pivot" and instance.mode != "hard":
                raise ValueError("close_vs_pivot_not_hard")
            if instance.enabled and instance.type in PIVOT_SETUPS:
                pivot_seen = True
            if instance.enabled and instance.type == "close_vs_pivot" and not pivot_seen:
                raise ValueError("close_vs_pivot_needs_vcp")
        return self


# --- regime, stop and exit rules --------------------------------------------------------------


class _Rule(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class CloseAboveSma(_Rule):
    """The close above its `sma`-session SMA and, unless `rising_days` is 0 (no slope test), the
    SMA above its value `rising_days` sessions earlier."""

    rule: Literal["close_above_sma_and_rising"]
    sma: int = Field(default=50, ge=2, le=400)
    rising_days: int = Field(default=10, ge=0, le=252)


class FastAboveSlow(_Rule):
    rule: Literal["sma_fast_above_slow_both_rising"]
    fast: int = Field(default=10, ge=2, le=400)
    slow: int = Field(default=20, ge=2, le=400)
    # Both SMAs above their value 3 sessions earlier: owner default;
    # one session let a single down tick of the slow SMA flip the gate for a night.
    rising_days: int = Field(default=3, ge=1, le=252)

    @model_validator(mode="after")
    def _check(self) -> FastAboveSlow:
        if self.fast >= self.slow:
            raise ValueError("fast_not_below_slow")
        return self


class BreadthAbove(_Rule):
    """The market breadth (`breadth_daily.breadth_pct`, the share of the ranked universe above
    its SMA50), averaged over the last `smooth_days` sessions, at least `min_pct`; once on, off
    only when it falls below `off_below_pct` (left out: `min_pct`, no hysteresis). Reads no
    index: the regime's `index` still names the market the backtest runs on."""

    rule: Literal["breadth_above"]
    min_pct: float = Field(default=50.0, ge=0, le=100)
    off_below_pct: float | None = Field(default=None, ge=0, le=100, exclude_if=lambda v: v is None)
    smooth_days: int = Field(default=1, ge=1, le=60)

    @model_validator(mode="after")
    def _check(self) -> BreadthAbove:
        if self.off_below_pct is not None and self.off_below_pct > self.min_pct:
            raise ValueError("off_below_above_min_pct")
        return self


# The closed set of strategy regime rules, one model each; `regime_gate.RULES` maps each to its
# function, as `stops` maps each stop rule to its planner.
RegimeRuleModel = CloseAboveSma | FastAboveSlow | BreadthAbove
RegimeRule = Annotated[RegimeRuleModel, Field(discriminator="rule")]
_REGIME_RULE: TypeAdapter[RegimeRuleModel] = TypeAdapter(RegimeRule)
# The indices a regime may read: those the store ingests, less India VIX (not a price index).
REGIME_INDICES = tuple(name for name in INDEX_NAMES if name != INDIA_VIX)


class Regime(Model):
    """HAT-BOT-053: one index the store holds and one rule with parameters (`RegimeRule`)."""

    index: str
    rule: str
    params: dict[str, Any]

    @model_validator(mode="after")
    def _check(self) -> Regime:
        if self.index not in REGIME_INDICES:
            raise ValueError("unknown_regime_index")
        model = self.parsed()
        object.__setattr__(self, "params", model.model_dump(exclude={"rule"}))
        return self

    def parsed(self) -> RegimeRuleModel:
        """The rule as its model (`RegimeRule`), its parameters validated."""
        return _REGIME_RULE.validate_python({**self.params, "rule": self.rule})


class StopBelowFinalContractionLow(Model):
    """`min_pct` (0 = off) skips a stop nearer the entry than that percentage
    (`stop_too_narrow`, HAT-EXEC-050)."""

    rule: Literal["below_final_contraction_low"]
    buffer_pct: float = Field(default=0.5, ge=0, le=50)
    max_pct: float = Field(default=8.0, gt=0, le=100)
    min_pct: float = Field(default=0.0, ge=0, lt=100)

    @model_validator(mode="after")
    def _check(self) -> StopBelowFinalContractionLow:
        if self.min_pct >= self.max_pct:
            raise ValueError("min_pct_not_below_max_pct")
        return self


# An optional stop-width cap on the rules without one of their own: the stop is skipped
# (`stop_too_wide`) when it is more than `max_pct` percent below the entry. Left out of the
# canonical form when unset, so the stored configs' hashes do not change.
WidthCap = Field(default=None, gt=0, le=100, exclude_if=lambda v: v is None)


class StopLowOfDay(Model):
    rule: Literal["low_of_day"]
    buffer_ticks: int = Field(default=1, ge=0, le=100)
    max_pct: float | None = WidthCap


class StopPctBelowEntry(Model):
    rule: Literal["pct_below_entry"]
    pct: float = Field(gt=0, lt=100)


class StopAtrMultiple(Model):
    rule: Literal["atr_multiple"]
    days: int = Field(ge=1, le=252)
    multiple: float = Field(gt=0, le=100)
    max_pct: float | None = WidthCap


class StopSwingLow(Model):
    rule: Literal["swing_low"]
    lookback_days: int = Field(ge=1, le=252)
    max_pct: float | None = WidthCap


StopRule = Annotated[
    StopBelowFinalContractionLow
    | StopLowOfDay
    | StopPctBelowEntry
    | StopAtrMultiple
    | StopSwingLow,
    Field(discriminator="rule"),
]
Fraction = Field(default=1.0, gt=0, le=1)
# A trail's start: the rule fires only once the position's best price since entry (`ExitView.peak`)
# has reached +`start_r` R; unset, it trails from the entry. Left out of the canonical form when
# unset, so the stored configs' hashes do not change.
TrailStart = Field(default=None, gt=0, le=100, exclude_if=lambda v: v is None)


class ExitHardStop(Model):
    rule: Literal["hard_stop"]
    enabled: bool = True


class ExitBreakeven(Model):
    rule: Literal["move_stop_to_breakeven"]
    at_r: float | None = Field(default=None, gt=0, le=100)
    after: Literal["partial"] | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> ExitBreakeven:
        if (self.at_r is None) == (self.after is None):
            raise ValueError("breakeven_needs_one_trigger")
        return self


class ExitStepTrail(Model):
    """A live-phase stop ratchet in R (initial risk): once the price is +k x `step_r` R above the
    entry (k = 1, 2, ...), the stop is the initial stop plus k x `move_r` R, raised only (step 1.5,
    move 1: -1R -> 0R at +1.5R -> +1R at +3R). `move_r` is at most `step_r`, so the stop stays at
    least 1R under the price that set it. It never sells and never counts as fired: it acts each
    time it would raise the stop."""

    rule: Literal["step_trail"]
    step_r: float = Field(gt=0, le=100)
    move_r: float = Field(gt=0, le=100)
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> ExitStepTrail:
        if self.move_r > self.step_r:
            raise ValueError("move_r_above_step_r")
        return self


class ExitPartialAtR(Model):
    rule: Literal["partial_at_r"]
    at_r: float = Field(gt=0, le=100)
    sell_fraction: float = Fraction
    enabled: bool = True


class ExitPartialAfterDays(Model):
    rule: Literal["partial_after_days"]
    days: int = Field(ge=1, le=252)
    sell_fraction: float = Fraction
    only_if_profit: bool = False
    enabled: bool = True


class ExitVsSma(Model):
    """A close-phase exit against the `sma`-session SMA, evaluated on the live price in the
    session's close window (decisions.md "Square-off before the closing auction"); the window is
    the session's, never a rule's."""

    sma: int = Field(ge=2, le=400)
    sell_fraction: float = Fraction
    enabled: bool = True


class ExitCloseBelowSma(ExitVsSma):
    """`grace_sessions` N: no exit in the first N sessions after the entry session (the stops
    still act); unset, the rule acts from the entry. Left out of the canonical form when unset."""

    rule: Literal["close_below_sma"]
    start_r: float | None = TrailStart
    grace_sessions: int | None = Field(default=None, ge=1, le=252, exclude_if=lambda v: v is None)
    # `above_stop`: the rule acts only while the SMA is above the position's current stop (QM:
    # trail "once they surpass your initial stop"); until then only the stop applies. Left out of
    # the canonical form when off.
    above_stop: bool = Field(default=False, exclude_if=lambda v: not v)


class ExitCloseAboveSma(ExitVsSma):
    """The mean-reversion target: the price back above a short SMA (Connors' 5-day line)."""

    rule: Literal["close_above_sma"]


class ExitCloseBelowEma(Model):
    """The close-phase exit under the `ema`-session EMA (`criteria.pullback.ema`, seeded over
    `EMA_WARMUP` x `ema` closes), the live price standing in for the session's close, as
    `close_below_sma` reads its SMA."""

    rule: Literal["close_below_ema"]
    ema: int = Field(ge=2, le=60)
    sell_fraction: float = Fraction
    start_r: float | None = TrailStart
    enabled: bool = True


class ExitTrailingSwingLow(Model):
    rule: Literal["trailing_swing_low"]
    lookback_days: int = Field(ge=1, le=252)
    sell_fraction: float = Fraction
    start_r: float | None = TrailStart
    enabled: bool = True


class ExitTrailingAtr(Model):
    """A close-phase exit under the best price since entry less `multiple` x ATR(`days`) (a
    chandelier trail)."""

    rule: Literal["trailing_atr"]
    days: int = Field(ge=1, le=252)
    multiple: float = Field(gt=0, le=100)
    sell_fraction: float = Fraction
    start_r: float | None = TrailStart
    enabled: bool = True


class ExitPctGainTarget(Model):
    rule: Literal["pct_gain_target"]
    pct: float = Field(gt=0, le=10_000)
    sell_fraction: float = Fraction
    enabled: bool = True


class ExitTimeStop(Model):
    rule: Literal["time_stop"]
    days: int = Field(ge=1, le=252)
    min_r: float = Field(default=0.0, ge=0, le=100)
    enabled: bool = True


class ExitEarlyFailure(Model):
    """A close-phase full exit of a position that has not reached +`min_r` R (its best price since
    entry, `ExitView.peak`) after `days` sessions held."""

    rule: Literal["early_failure"]
    days: int = Field(ge=1, le=252)
    min_r: float = Field(default=0.5, gt=0, le=100)
    enabled: bool = True


class ExitMaxHold(Model):
    """A close-phase full exit after `days` sessions held, whatever the price."""

    rule: Literal["max_hold"]
    days: int = Field(ge=1, le=1000)
    enabled: bool = True


class ExitRegime(Model):
    rule: Literal["regime_exit"]
    sell_fraction: float = Fraction
    enabled: bool = False


ExitRuleModel = (
    ExitHardStop
    | ExitBreakeven
    | ExitStepTrail
    | ExitPartialAtR
    | ExitPartialAfterDays
    | ExitCloseBelowSma
    | ExitCloseAboveSma
    | ExitCloseBelowEma
    | ExitTrailingSwingLow
    | ExitTrailingAtr
    | ExitPctGainTarget
    | ExitTimeStop
    | ExitEarlyFailure
    | ExitMaxHold
    | ExitRegime
)
ExitRule = Annotated[
    ExitRuleModel,
    Field(discriminator="rule"),
]


# --- playbooks and the config -----------------------------------------------------------------


def _one_hard_stop(rules: Sequence[ExitRuleModel]) -> None:
    """Every exit stack has exactly one enabled hard stop: the stop fires only through this rule
    in a backtest and in shadow, while live the broker stop always rests; without it the evidence
    is of another strategy."""
    if sum(isinstance(e, ExitHardStop) and e.enabled for e in rules) != 1:
        raise ValueError("one_enabled_hard_stop")


class Playbook(Model):
    """HAT-BOT-050: a named setup, trigger, entry filters and one stop rule. No regime rule or
    sizing value: those are the bot's. `universe` and `exits`, when set, replace the bot's
    universe stage and exit stack for this playbook (`StrategyConfig.universe_for`,
    `exits_for`); left out of the canonical form when unset, so stored configs keep their hash."""

    name: str
    setup: SetupStage
    trigger: list[Instance] = Field(default_factory=list, max_length=MAX_INSTANCES)
    entry_filters: list[Instance] = Field(default_factory=list, max_length=MAX_INSTANCES)
    stop: StopRule
    universe: list[Instance] | None = Field(
        default=None, max_length=MAX_INSTANCES, exclude_if=lambda v: v is None
    )
    exits: list[ExitRule] | None = Field(
        default=None, min_length=1, max_length=20, exclude_if=lambda v: v is None
    )

    @model_validator(mode="after")
    def _check(self) -> Playbook:
        if not NAME.match(self.name):
            raise ValueError("bad_name")
        if self.universe is not None:
            if any(i.stage != "universe" for i in self.universe):
                raise ValueError("wrong_stage")
            object.__setattr__(self, "universe", _sorted_unique(self.universe))
        if self.exits is not None:
            _one_hard_stop(self.exits)
        object.__setattr__(self, "trigger", _sorted_unique(self.trigger))
        object.__setattr__(self, "entry_filters", _sorted_unique(self.entry_filters))
        # A pre-open playbook's candidates come from the gap scan, which has no setup levels:
        # only the opening range can be its trigger level, else it could never trigger.
        if self.preopen and any(
            i.type == PRICE_BREAKS_LEVEL and i.params.get("level") != LEVEL_OPENING_RANGE
            for i in self.trigger
        ):
            raise ValueError("preopen_level_not_opening_range")
        # Every level a trigger names is one its setup exposes or the opening range (HAT-BOT-071),
        # else the playbook could never trigger.
        exposed = {LEVEL_OPENING_RANGE, *self.setup_levels}
        breaks = [i for i in self.trigger if i.type == PRICE_BREAKS_LEVEL and i.enabled]
        for instance in breaks:
            if not set(level_names(instance.params["level"])) <= exposed:
                raise ValueError("level_not_exposed")
        # One trigger level (HAT-BOT-071): live, the scan and the backtest all read this one
        # instance's expression (`level_expression`), so two could not disagree.
        if len(breaks) > 1:
            raise ValueError("one_trigger_level")
        return self

    @property
    def setup_levels(self) -> tuple[str, ...]:
        """The levels the playbook's enabled setup criteria expose (`CriterionType.levels`), in
        criterion order: the scan stores these with each watchlist row."""
        found: dict[str, None] = {}
        for instance in self.setup.criteria:
            if instance.enabled:
                found.update(dict.fromkeys(get_type(instance.type).levels))
        return tuple(found)

    @property
    def level_trigger(self) -> PriceBreaksLevelParams | None:
        """The parameters of the playbook's enabled `price_breaks_level` trigger, None without
        one (`one_trigger_level`: at most one)."""
        for instance in self.trigger:
            if instance.type == PRICE_BREAKS_LEVEL and instance.enabled:
                return PriceBreaksLevelParams.model_validate(instance.params)
        return None

    @property
    def entry_timing(self) -> str:
        """When the level trigger takes the entry (`PriceBreaksLevelParams.entry_timing`):
        `intraday_break` without one."""
        found = self.level_trigger
        return INTRADAY_BREAK if found is None else found.entry_timing

    @property
    def at_close(self) -> bool:
        """The level trigger enters at the daily close (`entry_timing: daily_close`)."""
        return self.entry_timing == DAILY_CLOSE

    @property
    def level_expression(self) -> str | None:
        """The level of the playbook's enabled `price_breaks_level` trigger, None without one."""
        for instance in self.trigger:
            if instance.type == PRICE_BREAKS_LEVEL and instance.enabled:
                return str(instance.params["level"])
        return None

    @property
    def gap_min_pct(self) -> Decimal | None:
        """The least `min_pct` of the playbook's enabled `gap_up` triggers, None without one."""
        found = [
            dec(GapUpParams.model_validate(i.params).min_pct)
            for i in self.trigger
            if i.type == GAP_UP and i.enabled
        ]
        return min(found, default=None)

    @property
    def preopen(self) -> bool:
        """A pre-open playbook (HAT-BOT-052): one of its criteria, enabled or not, needs the
        pre-open data (`data_needs`), so its candidates come from the gap scan at the open, never
        from the nightly watchlist, and the daily-bar backtest cannot have its trades."""
        instances = (*self.setup.criteria, *self.trigger, *self.entry_filters)
        return any(PREOPEN in get_type(i.type).data_needs for i in instances)


class StrategyConfig(Model):
    bot: str
    market: Literal["NSE"] = "NSE"
    universe: list[Instance] = Field(max_length=MAX_INSTANCES)
    regime: Regime
    playbooks: list[Playbook] = Field(min_length=1, max_length=8)
    exits: list[ExitRule] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def _check(self) -> StrategyConfig:
        if not NAME.match(self.bot):
            raise ValueError("bad_name")
        object.__setattr__(self, "universe", _sorted_unique(self.universe))
        names = [p.name for p in self.playbooks]
        if len(set(names)) != len(names):
            raise ValueError("duplicate_playbook")
        _one_hard_stop(self.exits)
        return self

    def universe_for(self, playbook: str) -> list[Instance]:
        """The universe stage of `playbook`: its own when set, else the bot's."""
        own = self.playbook(playbook).universe
        return self.universe if own is None else own

    def exits_for(self, playbook: str) -> list[ExitRule]:
        """The exit stack of `playbook`: its own when set, else the bot's (the bot's for a name
        the config does not have, such as a stored signal's playbook since renamed)."""
        found = next((p for p in self.playbooks if p.name == playbook), None)
        return self.exits if found is None or found.exits is None else found.exits

    def to_json(self) -> dict[str, Any]:
        """The canonical stored form: plain JSON data, every default explicit."""
        return self.model_dump(mode="json")

    def hash(self) -> str:
        return _hash(self.to_json())

    def playbook(self, name: str) -> Playbook:
        for playbook in self.playbooks:
            if playbook.name == name:
                return playbook
        raise ConfigError([(name, "unknown_playbook")])


# --- parsing ----------------------------------------------------------------------------------

_SECTION_STAGE = {"universe": "universe", "trigger": "trigger", "entry_filters": "entry_filter"}


def risk_key_paths(value: object, path: str = "") -> list[str]:
    """Paths of every mapping key, at any depth, that names a risk, size or capital parameter."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            where = f"{path}.{key}" if path else str(key)
            if isinstance(key, str) and (key in POLICY_KEYS or RISK_KEY.search(key)):
                found.append(where)
            found.extend(risk_key_paths(item, where))
    elif isinstance(value, list | tuple):
        for index, item in enumerate(value):
            found.extend(risk_key_paths(item, f"{path}[{index}]"))
    return found


def _instances(raw: object, stage: str) -> object:
    """Fill `stage` and `order` from the section and the position."""
    if not isinstance(raw, list):
        return raw
    out: list[object] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            out.append(item)
            continue
        filled = dict(item)
        filled.setdefault("stage", stage)
        filled.setdefault("order", index)
        out.append(filled)
    return out


def _playbook(raw: Mapping[str, Any]) -> dict[str, Any]:
    out = dict(raw)
    setup = out.get("setup")
    if isinstance(setup, Mapping):
        setup = dict(setup)
        setup["criteria"] = _instances(setup.get("criteria", []), "setup")
        out["setup"] = setup
    for key in ("trigger", "entry_filters"):
        out[key] = _instances(out.get(key, []), _SECTION_STAGE[key])
    if out.get("universe") is not None:
        out["universe"] = _instances(out["universe"], "universe")
    return out


def normalise_raw(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The seeds' YAML shape (or the canonical one) as the canonical shape's input. `version`
    is not part of a config (the database numbers versions)."""
    out = {k: v for k, v in raw.items() if k != "version"}
    out["universe"] = _instances(out.get("universe", []), "universe")
    if "playbooks" in out:
        playbooks = out["playbooks"]
        out["playbooks"] = (
            [_playbook(p) if isinstance(p, Mapping) else p for p in playbooks]
            if isinstance(playbooks, list)
            else playbooks
        )
    else:
        inline = {k: out.pop(k) for k in ("setup", "trigger", "entry_filters", "stop") if k in out}
        out["playbooks"] = [_playbook({"name": DEFAULT_PLAYBOOK, **inline})]
    return out


def _problems(error: ValidationError) -> list[tuple[str, str]]:
    problems = []
    for item in error.errors():
        where = ".".join(str(part) for part in item["loc"])
        code = (
            str(item["msg"]).removeprefix("Value error, ")
            if item["type"] == "value_error"
            else (item["type"])
        )
        problems.append((where, code))
    return problems


def parse_config(raw: Mapping[str, Any]) -> StrategyConfig:
    """Validate and normalise a submitted config, or raise `ConfigError` with field-level
    problems and no submitted value. The risk-key scan runs first."""
    if not isinstance(raw, Mapping):
        raise ConfigError([("", "not_a_mapping")])
    risky = risk_key_paths(raw)
    if risky:
        raise ConfigError([(path, "risk_key") for path in risky])
    try:
        return StrategyConfig.model_validate(normalise_raw(raw))
    except ValidationError as error:
        raise ConfigError(_problems(error)) from None


__all__ = [
    "DEFAULT_PLAYBOOK",
    "DEFAULT_THRESHOLD",
    "REGISTRY",
    "STAGES",
    "ConfigError",
    "Instance",
    "Playbook",
    "SetupStage",
    "StrategyConfig",
    "parse_config",
    "risk_key_paths",
]

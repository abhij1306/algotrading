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
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from ...confighash import config_hash as _hash
from ...errors import HatError
from ...risk.policy import POLICY_FIELDS, SEED_POLICY
from ..criteria import REGISTRY, UnknownCriterionError, get_type
from ..criteria.base import STAGES

DEFAULT_THRESHOLD = 0.6
DEFAULT_PLAYBOOK = "default"
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
CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


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
        # enabled `vcp` evaluated before it; without one it could never pass, and the playbook
        # would silently stop trading.
        pivot_seen = False
        for instance in self.criteria:
            if instance.type == "close_vs_pivot" and instance.mode != "hard":
                raise ValueError("close_vs_pivot_not_hard")
            if instance.enabled and instance.type == "vcp":
                pivot_seen = True
            if instance.enabled and instance.type == "close_vs_pivot" and not pivot_seen:
                raise ValueError("close_vs_pivot_needs_vcp")
        return self


# --- regime, stop and exit rules --------------------------------------------------------------

REGIME_RULES = ("close_above_sma_and_rising", "sma_fast_above_slow_both_rising")


class Regime(Model):
    """HAT-BOT-053: one index and one rule with parameters."""

    index: Literal["NIFTY 500", "NIFTY SMALLCAP 250"]
    rule: Literal["close_above_sma_and_rising", "sma_fast_above_slow_both_rising"]
    params: dict[str, Any]

    @model_validator(mode="after")
    def _check(self) -> Regime:
        model: type[BaseModel] = (
            CloseAboveSma if self.rule == "close_above_sma_and_rising" else FastAboveSlow
        )
        object.__setattr__(self, "params", model.model_validate(self.params).model_dump())
        return self


class _Rule(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


class CloseAboveSma(_Rule):
    sma: int = Field(default=50, ge=2, le=400)
    rising_days: int = Field(default=10, ge=1, le=252)


class FastAboveSlow(_Rule):
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


class StopLowOfDay(Model):
    rule: Literal["low_of_day"]
    buffer_ticks: int = Field(default=1, ge=0, le=100)


class StopPctBelowEntry(Model):
    rule: Literal["pct_below_entry"]
    pct: float = Field(gt=0, lt=100)


class StopAtrMultiple(Model):
    rule: Literal["atr_multiple"]
    days: int = Field(ge=1, le=252)
    multiple: float = Field(gt=0, le=100)


class StopSwingLow(Model):
    rule: Literal["swing_low"]
    lookback_days: int = Field(ge=1, le=252)


StopRule = Annotated[
    StopBelowFinalContractionLow
    | StopLowOfDay
    | StopPctBelowEntry
    | StopAtrMultiple
    | StopSwingLow,
    Field(discriminator="rule"),
]
Fraction = Field(default=1.0, gt=0, le=1)


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


class ExitCloseBelowSma(Model):
    rule: Literal["close_below_sma"]
    sma: int = Field(ge=2, le=400)
    sell_fraction: float = Fraction
    when: str = "15:25"
    enabled: bool = True

    @model_validator(mode="after")
    def _check(self) -> ExitCloseBelowSma:
        if not CLOCK.match(self.when):
            raise ValueError("bad_clock_time")
        return self


class ExitTrailingSwingLow(Model):
    rule: Literal["trailing_swing_low"]
    lookback_days: int = Field(ge=1, le=252)
    sell_fraction: float = Fraction
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


class ExitRegime(Model):
    rule: Literal["regime_exit"]
    sell_fraction: float = Fraction
    enabled: bool = False


ExitRule = Annotated[
    ExitHardStop
    | ExitBreakeven
    | ExitPartialAtR
    | ExitPartialAfterDays
    | ExitCloseBelowSma
    | ExitTrailingSwingLow
    | ExitPctGainTarget
    | ExitTimeStop
    | ExitRegime,
    Field(discriminator="rule"),
]


# --- playbooks and the config -----------------------------------------------------------------


class Playbook(Model):
    """HAT-BOT-050: a named setup, trigger, entry filters and one stop rule. No regime rule,
    sizing value or exit stack: those are the bot's."""

    name: str
    setup: SetupStage
    trigger: list[Instance] = Field(default_factory=list, max_length=MAX_INSTANCES)
    entry_filters: list[Instance] = Field(default_factory=list, max_length=MAX_INSTANCES)
    stop: StopRule

    @model_validator(mode="after")
    def _check(self) -> Playbook:
        if not NAME.match(self.name):
            raise ValueError("bad_name")
        object.__setattr__(self, "trigger", _sorted_unique(self.trigger))
        object.__setattr__(self, "entry_filters", _sorted_unique(self.entry_filters))
        return self


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
        return self

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

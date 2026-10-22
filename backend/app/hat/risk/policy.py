"""Risk-policy governance rules that are pure functions (06 §1, 03-schema §8).

Imports nothing from the database, broker or models, so the rule is the same in the app, in
the backtest and in the property tests.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from ..errors import HatError
from ..time import as_utc

# Risk increases take effect no sooner than this after they are requested (assumption A in
# HAT-SCH-084). The Postgres CHECK states the same span in hours (168), so a server time zone
# with daylight saving cannot change it.
COOLING_OFF = timedelta(days=7)
DIRECTIONS = ("initial", "increase", "decrease")


class CoolingOffError(HatError):
    """A risk increase was scheduled before the cooling-off period ended."""


def cooling_off_satisfied(direction: str, requested_at: datetime, effective_from: datetime) -> bool:
    """True when a risk-policy version of `direction` may take effect at `effective_from`.

    Only an `increase` waits: `initial` and `decrease` are never delayed.
    """
    if direction != "increase":
        return True
    return as_utc(effective_from) - as_utc(requested_at) >= COOLING_OFF


def require_cooling_off(direction: str, requested_at: datetime, effective_from: datetime) -> None:
    """Raise `CoolingOffError` unless `cooling_off_satisfied`."""
    if not cooling_off_satisfied(direction, requested_at, effective_from):
        raise CoolingOffError("a risk increase must take effect at least 7 days after request")


# --- The policy record (06 §1, HAT-RISK-010, HAT-RISK-011) -----------------------------------

LEVELS = ("L1", "L2", "L3")
STRATEGY_KEYS = ("stage2-contraction-breakout", "momentum-breakout-ep")
SLEEVES = ("S1", "S2")


class PolicyError(HatError):
    """A policy document is malformed: a missing or unknown field, a float, a value out of range."""


def to_decimal(value: object, name: str = "value") -> Decimal:
    """`value` as a finite Decimal. A float, a bool, None or a non-finite number is refused:
    money and ratios are never float (HAT-RISK-020, HAT-RISK-102)."""
    if isinstance(value, bool) or isinstance(value, float) or value is None:
        raise PolicyError(f"{name}: not an exact number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise PolicyError(f"{name}: not a number") from exc
    if not result.is_finite():
        raise PolicyError(f"{name}: not finite")
    return result


def _count(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PolicyError(f"{name}: not a positive whole number")
    return value


def _ratio(value: object, name: str, *, zero_ok: bool = False) -> Decimal:
    result = to_decimal(value, name)
    if result < 0 or result > 1 or (result == 0 and not zero_ok):
        raise PolicyError(f"{name}: outside {'[0' if zero_ok else '(0'}, 1]")
    return result


@dataclass(frozen=True)
class LadderLevel:
    max_positions: int
    risk_mult: Decimal


@dataclass(frozen=True)
class RiskPolicy:
    """Every risk number of the account, as typed values (06 §1). Money is Decimal, counts int."""

    risk_pct: Decimal
    p_max: Mapping[str, Decimal]
    adv_cap_pct: Decimal
    global_open_risk_pct: Decimal
    max_open_positions: int
    max_positions_per_industry: int
    max_entries_per_day: int
    max_stopouts_per_day: int
    ladder: Mapping[str, LadderLevel]
    up_after_wins: int
    down_after_losses: int
    account_drawdown_pct: Decimal
    bot_drawdown_pct: Decimal
    base_weight: Mapping[str, Decimal]
    cluster_cap: Decimal
    multiplier_lo: Decimal
    multiplier_hi: Decimal
    health_half_dd: Decimal
    health_zero_dd: Decimal
    health_full_mult: Decimal
    health_half_mult: Decimal
    # The owner has set `base_weight` and `cluster_cap` as real numbers (06 §1, 06:47): until
    # then no bot runs confirm or auto (HAT-GOV-085). Not a risk number: it sizes nothing.
    weights_confirmed: bool = False

    def to_json(self) -> dict[str, Any]:
        """The JSON document stored in `hat_risk_policy_version.policy` (numbers as strings)."""
        return {
            "risk_pct": str(self.risk_pct),
            "p_max": {k: str(v) for k, v in self.p_max.items()},
            "adv_cap_pct": str(self.adv_cap_pct),
            "global_open_risk_pct": str(self.global_open_risk_pct),
            "max_open_positions": self.max_open_positions,
            "max_positions_per_industry": self.max_positions_per_industry,
            "max_entries_per_day": self.max_entries_per_day,
            "max_stopouts_per_day": self.max_stopouts_per_day,
            "exposure_ladder": {
                **{
                    level: {"max_positions": row.max_positions, "risk_mult": str(row.risk_mult)}
                    for level, row in self.ladder.items()
                },
                "up_after_wins": self.up_after_wins,
                "down_after_losses": self.down_after_losses,
            },
            "kill": {
                "account_drawdown_pct": str(self.account_drawdown_pct),
                "bot_drawdown_pct": str(self.bot_drawdown_pct),
            },
            "base_weight": {k: str(v) for k, v in self.base_weight.items()},
            "cluster_cap": str(self.cluster_cap),
            "multiplier_bounds": {"lo": str(self.multiplier_lo), "hi": str(self.multiplier_hi)},
            "health_bands": {
                "half_dd": str(self.health_half_dd),
                "zero_dd": str(self.health_zero_dd),
                "full_mult": str(self.health_full_mult),
                "half_mult": str(self.health_half_mult),
            },
            "weights_confirmed": self.weights_confirmed,
        }


# The top-level fields of the policy document: exactly the 06 §1 table (CONF
# risk.policy_schema_fields). Nothing else may appear, and none may be missing.
POLICY_FIELDS = (
    "risk_pct",
    "p_max",
    "adv_cap_pct",
    "global_open_risk_pct",
    "max_open_positions",
    "max_positions_per_industry",
    "max_entries_per_day",
    "max_stopouts_per_day",
    "exposure_ladder",
    "kill",
    "base_weight",
    "cluster_cap",
    "multiplier_bounds",
    "health_bands",
    "weights_confirmed",
)
# Fields a stored document may lack, with the value a missing one reads as. A document written
# before the field existed reads as unconfirmed, which keeps every bot in shadow (fail closed).
OPTIONAL_FIELDS: Mapping[str, object] = {"weights_confirmed": False}

# The initial seed (version 1, direction "initial"): the 06 §1 seed column.
SEED_POLICY: dict[str, Any] = {
    "risk_pct": "0.0025",
    "p_max": {"stage2-contraction-breakout": "0.20", "momentum-breakout-ep": "0.25"},
    "adv_cap_pct": "0.01",
    "global_open_risk_pct": "0.03",
    "max_open_positions": 6,
    "max_positions_per_industry": 2,
    "max_entries_per_day": 3,
    "max_stopouts_per_day": 3,
    "exposure_ladder": {
        "L1": {"max_positions": 2, "risk_mult": "0.5"},
        "L2": {"max_positions": 4, "risk_mult": "1.0"},
        "L3": {"max_positions": 6, "risk_mult": "1.0"},
        "up_after_wins": 2,
        "down_after_losses": 2,
    },
    "kill": {"account_drawdown_pct": "0.15", "bot_drawdown_pct": "0.10"},
    "base_weight": {"S1": "0.50", "S2": "0.50"},
    "cluster_cap": "1.00",
    "multiplier_bounds": {"lo": "0.25", "hi": "1.0"},
    "health_bands": {"half_dd": "0.08", "zero_dd": "0.12", "full_mult": "1.0", "half_mult": "0.5"},
    "weights_confirmed": False,
}


def _exact_keys(doc: object, keys: Sequence[str], name: str) -> Mapping[str, Any]:
    if not isinstance(doc, Mapping) or set(doc) != set(keys):
        raise PolicyError(f"{name}: fields must be exactly {sorted(keys)}")
    return doc


def parse_policy(doc: object) -> RiskPolicy:
    """The typed policy of a stored document, or `PolicyError`. Strict: an unknown or missing
    field, a float, or a number outside its range is refused, so a malformed or half-written
    document can never size an order."""
    if isinstance(doc, Mapping):
        doc = {**OPTIONAL_FIELDS, **doc}
    top = _exact_keys(doc, POLICY_FIELDS, "policy")
    confirmed = top["weights_confirmed"]
    if not isinstance(confirmed, bool):
        raise PolicyError("weights_confirmed: must be true or false")
    p_max_doc = top["p_max"]
    if not isinstance(p_max_doc, Mapping) or not p_max_doc:
        raise PolicyError("p_max: needs one entry per strategy")
    ladder_doc = top["exposure_ladder"]
    ladder_keys = (*LEVELS, "up_after_wins", "down_after_losses")
    ladder_doc = _exact_keys(ladder_doc, ladder_keys, "exposure_ladder")
    ladder = {}
    for level in LEVELS:
        row = _exact_keys(ladder_doc[level], ("max_positions", "risk_mult"), level)
        ladder[level] = LadderLevel(
            _count(row["max_positions"], f"{level}.max_positions"),
            _ratio(row["risk_mult"], f"{level}.risk_mult"),
        )
    kill = _exact_keys(top["kill"], ("account_drawdown_pct", "bot_drawdown_pct"), "kill")
    weights = top["base_weight"]
    if not isinstance(weights, Mapping) or not weights:
        raise PolicyError("base_weight: needs one entry per sleeve")
    bounds = _exact_keys(top["multiplier_bounds"], ("lo", "hi"), "multiplier_bounds")
    bands = _exact_keys(
        top["health_bands"], ("half_dd", "zero_dd", "full_mult", "half_mult"), "health_bands"
    )
    policy = RiskPolicy(
        risk_pct=_ratio(top["risk_pct"], "risk_pct"),
        p_max={str(k): _ratio(v, f"p_max.{k}") for k, v in p_max_doc.items()},
        adv_cap_pct=_ratio(top["adv_cap_pct"], "adv_cap_pct"),
        global_open_risk_pct=_ratio(top["global_open_risk_pct"], "global_open_risk_pct"),
        max_open_positions=_count(top["max_open_positions"], "max_open_positions"),
        max_positions_per_industry=_count(
            top["max_positions_per_industry"], "max_positions_per_industry"
        ),
        max_entries_per_day=_count(top["max_entries_per_day"], "max_entries_per_day"),
        max_stopouts_per_day=_count(top["max_stopouts_per_day"], "max_stopouts_per_day"),
        ladder=ladder,
        up_after_wins=_count(ladder_doc["up_after_wins"], "up_after_wins"),
        down_after_losses=_count(ladder_doc["down_after_losses"], "down_after_losses"),
        account_drawdown_pct=_ratio(kill["account_drawdown_pct"], "kill.account_drawdown_pct"),
        bot_drawdown_pct=_ratio(kill["bot_drawdown_pct"], "kill.bot_drawdown_pct"),
        base_weight={
            str(k): _ratio(v, f"base_weight.{k}", zero_ok=True) for k, v in weights.items()
        },
        cluster_cap=_ratio(top["cluster_cap"], "cluster_cap"),
        multiplier_lo=_ratio(bounds["lo"], "multiplier_bounds.lo"),
        multiplier_hi=to_decimal(bounds["hi"], "multiplier_bounds.hi"),
        health_half_dd=_ratio(bands["half_dd"], "health_bands.half_dd"),
        health_zero_dd=_ratio(bands["zero_dd"], "health_bands.zero_dd"),
        health_full_mult=_ratio(bands["full_mult"], "health_bands.full_mult"),
        health_half_mult=_ratio(bands["half_mult"], "health_bands.half_mult", zero_ok=True),
        weights_confirmed=confirmed,
    )
    if policy.multiplier_hi < policy.multiplier_lo or policy.multiplier_hi <= 0:
        raise PolicyError("multiplier_bounds: hi must be positive and not below lo")
    if policy.health_zero_dd < policy.health_half_dd:
        raise PolicyError("health_bands: zero_dd must not be below half_dd")
    return policy


def seed_policy() -> RiskPolicy:
    return parse_policy(SEED_POLICY)


# --- Which version is in force (HAT-RISK-010) ------------------------------------------------


@dataclass(frozen=True)
class PolicyVersion:
    """One stored risk-policy version, as the effective-version rule reads it."""

    version_id: UUID
    version: int
    effective_from: datetime
    superseded_at: datetime | None
    policy: RiskPolicy


def effective_version(versions: Iterable[PolicyVersion], at: datetime) -> PolicyVersion | None:
    """The version in force at `at`: the latest `effective_from` at or before `at` among those not
    superseded by then; None when there is none. A version whose `effective_from` is later (a
    pending increase still in its cooling-off) never applies, so it cannot size a trade early."""
    moment = as_utc(at)
    candidates = [
        v
        for v in versions
        if as_utc(v.effective_from) <= moment
        and (v.superseded_at is None or as_utc(v.superseded_at) > moment)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda v: (as_utc(v.effective_from), v.version))


# --- Increase or decrease (HAT-GOV-090, HAT-GOV-093) -----------------------------------------


def _permissive_fields(policy: RiskPolicy) -> dict[str, tuple[Decimal, bool]]:
    """Every number of the policy with the direction in which it is more permissive:
    (value, True when a higher value allows more risk)."""
    fields: dict[str, tuple[Decimal, bool]] = {
        "risk_pct": (policy.risk_pct, True),
        "adv_cap_pct": (policy.adv_cap_pct, True),
        "global_open_risk_pct": (policy.global_open_risk_pct, True),
        "max_open_positions": (Decimal(policy.max_open_positions), True),
        "max_positions_per_industry": (Decimal(policy.max_positions_per_industry), True),
        "max_entries_per_day": (Decimal(policy.max_entries_per_day), True),
        "max_stopouts_per_day": (Decimal(policy.max_stopouts_per_day), True),
        "up_after_wins": (Decimal(policy.up_after_wins), False),
        "down_after_losses": (Decimal(policy.down_after_losses), True),
        "kill.account_drawdown_pct": (policy.account_drawdown_pct, True),
        "kill.bot_drawdown_pct": (policy.bot_drawdown_pct, True),
        "cluster_cap": (policy.cluster_cap, True),
        "multiplier_bounds.lo": (policy.multiplier_lo, True),
        "multiplier_bounds.hi": (policy.multiplier_hi, True),
        "health_bands.half_dd": (policy.health_half_dd, True),
        "health_bands.zero_dd": (policy.health_zero_dd, True),
        "health_bands.full_mult": (policy.health_full_mult, True),
        "health_bands.half_mult": (policy.health_half_mult, True),
    }
    for key, value in policy.p_max.items():
        fields[f"p_max.{key}"] = (value, True)
    for key, value in policy.base_weight.items():
        fields[f"base_weight.{key}"] = (value, True)
    for level, row in policy.ladder.items():
        fields[f"ladder.{level}.max_positions"] = (Decimal(row.max_positions), True)
        fields[f"ladder.{level}.risk_mult"] = (row.risk_mult, True)
    return fields


def more_permissive_fields(current: RiskPolicy, proposed: RiskPolicy) -> list[str]:
    """The fields on which `proposed` allows more risk than `current`. A field that exists only
    in `proposed` (a new strategy or sleeve) counts as more permissive: it adds capacity."""
    now = _permissive_fields(current)
    new = _permissive_fields(proposed)
    found = []
    for name, (value, higher_is_more) in new.items():
        if name not in now:
            found.append(name)
            continue
        old_value = now[name][0]
        if (value > old_value) if higher_is_more else (value < old_value):
            found.append(name)
    return sorted(found)


def classify_direction(current: RiskPolicy, proposed: RiskPolicy) -> str:
    """`decrease` when no field of `proposed` is more permissive than `current` (equal counts as
    a decrease), else `increase`. An increase is never filed as a decrease."""
    return "increase" if more_permissive_fields(current, proposed) else "decrease"


# --- The evidence gate for an increase (HAT-GOV-091) -----------------------------------------

MIN_LIVE_TRADES = 50
Z_90 = Decimal("1.6448536269514722")  # two-sided 90% normal quantile


@dataclass(frozen=True)
class TradeOutcome:
    """A closed trade as the evidence gate reads it."""

    r_multiple: Decimal
    is_virtual: bool


def build_gate_evidence(
    trades: Sequence[TradeOutcome], backtest_expectancy_r: Decimal
) -> dict[str, Any]:
    """The `gate_evidence` document: the count of live (non-virtual) closed trades, their mean R
    and 90% confidence interval (normal approximation), and the discounted backtest expectancy.
    A virtual (shadow) trade never counts."""
    live = [t.r_multiple for t in trades if not t.is_virtual]
    count = len(live)
    evidence: dict[str, Any] = {
        "live_trades": count,
        "backtest_expectancy_r": str(to_decimal(backtest_expectancy_r, "backtest_expectancy_r")),
    }
    if count >= 2:
        mean = sum(live, Decimal(0)) / count
        variance = sum(((x - mean) ** 2 for x in live), Decimal(0)) / (count - 1)
        half = Z_90 * (variance / count).sqrt()
        evidence.update(
            expectancy_r=str(mean), ci90_low=str(mean - half), ci90_high=str(mean + half)
        )
    return evidence


def increase_gate_ok(evidence: Mapping[str, Any] | None) -> bool:
    """True when an increase carries the evidence the gate wants: at least 50 live closed trades,
    live expectancy at or above 0 R, and its 90% interval containing the discounted backtest
    expectancy (OWNER-PENDING default). Missing or malformed evidence is a no."""
    if not isinstance(evidence, Mapping):
        return False
    try:
        count = evidence["live_trades"]
        if isinstance(count, bool) or not isinstance(count, int) or count < MIN_LIVE_TRADES:
            return False
        expectancy = to_decimal(evidence["expectancy_r"], "expectancy_r")
        low = to_decimal(evidence["ci90_low"], "ci90_low")
        high = to_decimal(evidence["ci90_high"], "ci90_high")
        backtest = to_decimal(evidence["backtest_expectancy_r"], "backtest_expectancy_r")
    except (KeyError, PolicyError):
        return False
    return expectancy >= 0 and low <= backtest <= high


class GateError(HatError):
    """A risk increase was requested without the evidence the gate needs."""


def require_increase_gate(direction: str, evidence: Mapping[str, Any] | None) -> None:
    """Raise `GateError` for an `increase` whose evidence does not pass `increase_gate_ok`."""
    if direction == "increase" and not increase_gate_ok(evidence):
        raise GateError(
            "a risk increase needs at least 50 live trades with expectancy in tolerance"
        )

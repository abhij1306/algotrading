"""The backtest fill and cost model (HAT-BT-022, HAT-BT-023, HAT-BT-024): exact and pure.

A simulated fill is moved against the trade by the run's slippage, then charged the statutory
and exchange costs of a delivery (CNC) trade. Every rate lives in a versioned cost-model file
(`cost_models/<version>.yaml`); nothing here holds a rate. Each charge is computed on the traded
value, capped where the file says so, and rounded half up to the paisa; GST is charged on the sum
of the components the file marks taxable. All of it is Decimal.

`BacktestFills` is the fill model the shadow engine's virtual fills accept
(`app.hat.engine.trades.FillModel`), so the backtest fills through the same code as shadow
trading, with slippage and charges where shadow has neither.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from importlib import resources
from typing import Any, Literal

import yaml

from ..confighash import config_hash
from ..errors import HatError
from ..money import exact, to_money

__all__ = [
    "DEFAULT_COST_MODEL",
    "DEFAULT_SLIPPAGE_BPS",
    "MAX_SLIPPAGE_BPS",
    "BacktestFills",
    "Charges",
    "Component",
    "CostError",
    "CostModel",
    "charges",
    "load_cost_model",
    "parse_cost_model",
    "slipped",
]

Side = Literal["buy", "sell"]
SIDES: tuple[Side, ...] = ("buy", "sell")
DEFAULT_COST_MODEL = "cnc-delivery-1"
MAX_SLIPPAGE_BPS = Decimal(10_000)  # a slippage must be below 100%
# The slippage a run uses unless it names one (O5, PR 62): pessimistic, never perfect fills.
DEFAULT_SLIPPAGE_BPS = Decimal(20)
PRICE_PLACES = Decimal("0.0001")  # hat_fill.price is numeric(18, 4)
_BPS = Decimal(10_000)
_ZERO = Decimal(0)


class CostError(HatError):
    """A cost model or slippage was refused; `code` is a fixed word and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Component:
    """One charge: `rate` of the traded value plus `per_order`, at most `cap` (None: no cap),
    on the `sides` named, and part of the GST base when `taxable`."""

    name: str
    sides: frozenset[str]
    rate: Decimal
    taxable: bool
    per_order: Decimal = _ZERO
    cap: Decimal | None = None


@dataclass(frozen=True)
class CostModel:
    version: str
    gst_rate: Decimal
    components: tuple[Component, ...]
    digest: str  # sha256 of the canonical document, recorded with every run

    def to_json(self) -> dict[str, Any]:
        return {"version": self.version, "digest": self.digest}


@dataclass(frozen=True)
class Charges:
    """The charges of one fill: each component, the GST, and their total."""

    by_component: Mapping[str, Decimal]
    gst: Decimal
    total: Decimal


def _rate(value: object, name: str) -> Decimal:
    # a float would carry binary rounding into money, so only text or an int is a rate
    number = exact(value) if isinstance(value, str | int) else None
    if number is None or number < 0:
        raise CostError(f"cost_bad_{name}")
    return number


def _component(raw: object) -> Component:
    if not isinstance(raw, Mapping):
        raise CostError("cost_bad_component")
    allowed = {"name", "sides", "rate", "per_order", "cap", "taxable"}
    if not set(raw) <= allowed or not {"name", "sides", "rate", "taxable"} <= set(raw):
        raise CostError("cost_bad_component")
    name, sides, taxable = raw["name"], raw["sides"], raw["taxable"]
    if not isinstance(name, str) or not name or not isinstance(taxable, bool):
        raise CostError("cost_bad_component")
    if not isinstance(sides, Sequence) or isinstance(sides, str) or not set(sides) <= set(SIDES):
        raise CostError("cost_bad_sides")
    cap = raw.get("cap")
    return Component(
        name=name,
        sides=frozenset(sides),
        rate=_rate(raw["rate"], "rate"),
        taxable=taxable,
        per_order=_rate(raw.get("per_order", "0"), "per_order"),
        cap=None if cap is None else _rate(cap, "cap"),
    )


def parse_cost_model(document: Mapping[str, Any]) -> CostModel:
    """A validated cost model, or `CostError`. Rates are strings or integers, never floats."""
    if not isinstance(document, Mapping) or set(document) != {"version", "gst_rate", "components"}:
        raise CostError("cost_bad_document")
    version = document["version"]
    if not isinstance(version, str) or not version:
        raise CostError("cost_bad_version")
    raw = document["components"]
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise CostError("cost_bad_component")
    components = tuple(_component(c) for c in raw)
    if len({c.name for c in components}) != len(components):
        raise CostError("cost_duplicate_component")
    return CostModel(
        version=version,
        gst_rate=_rate(document["gst_rate"], "gst_rate"),
        components=components,
        digest=config_hash(dict(document)),
    )


def load_cost_model(version: str = DEFAULT_COST_MODEL) -> CostModel:
    """The packaged cost model `cost_models/<version>.yaml`; its `version` must match the name."""
    if not version.replace("-", "").isalnum():
        raise CostError("cost_unknown_version")
    path = resources.files(__package__).joinpath("cost_models", f"{version}.yaml")
    if not path.is_file():
        raise CostError("cost_unknown_version")
    model = parse_cost_model(yaml.safe_load(path.read_text(encoding="utf-8")))
    if model.version != version:
        raise CostError("cost_version_mismatch")
    return model


def charges(model: CostModel, side: str, qty: int, price: Decimal) -> Charges:
    """The charges of one fill of `qty` shares at `price` on `side` (HAT-BT-023)."""
    if side not in SIDES or isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
        raise CostError("cost_bad_fill")
    if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
        raise CostError("cost_bad_fill")
    value = price * qty
    found: dict[str, Decimal] = {}
    taxable = _ZERO
    for component in model.components:
        if side not in component.sides:
            continue
        amount = component.rate * value + component.per_order
        if component.cap is not None:
            amount = min(amount, component.cap)
        found[component.name] = to_money(amount)
        if component.taxable:
            taxable += found[component.name]
    gst = to_money(taxable * model.gst_rate)
    return Charges(found, gst, sum(found.values(), _ZERO) + gst)


def check_slippage(bps: Decimal) -> Decimal:
    """`bps` when 0 <= bps < 10000, else `CostError` (checked when a run starts)."""
    if not isinstance(bps, Decimal) or not bps.is_finite() or not _ZERO <= bps < MAX_SLIPPAGE_BPS:
        raise CostError("cost_bad_slippage")
    return bps


def slipped(price: Decimal, side: str, bps: Decimal) -> Decimal:
    """The fill price after slippage, moved against the trade (HAT-BT-022): a buy at
    p x (1 + s/10000) rounded up to the stored 4 places, a sell at p x (1 - s/10000) rounded
    down, so rounding never favours the trade. A sell never fills below one price unit (0.0001),
    which only a slippage of nearly 100% reaches."""
    check_slippage(bps)
    if side == "buy":
        return (price * (_BPS + bps) / _BPS).quantize(PRICE_PLACES, rounding=ROUND_CEILING)
    if side == "sell":
        sold = (price * (_BPS - bps) / _BPS).quantize(PRICE_PLACES, rounding=ROUND_FLOOR)
        return max(sold, PRICE_PLACES)
    raise CostError("cost_bad_fill")


@dataclass(frozen=True)
class BacktestFills:
    """The backtest's fill model: slippage, then the cost model's charges on the slipped fill."""

    model: CostModel
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        check_slippage(self.slippage_bps)

    def fill(self, side: str, qty: int, price: Decimal) -> tuple[Decimal, Decimal]:
        """(fill price, fees) of a fill of `qty` at the reference `price`."""
        filled = slipped(price, side, self.slippage_bps)
        return filled, charges(self.model, side, qty, filled).total

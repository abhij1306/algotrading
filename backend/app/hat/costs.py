"""The fill and cost model of the backtest and shadow trading (HAT-BT-022..024, QA4): exact and
pure.

A simulated fill is moved against the trade by the run's slippage, then charged the statutory
and exchange costs of a delivery (CNC) trade in force on the fill's trade date. Every rate lives
in a dated cost-model file (`cost_models/<version>/<effective_from>.yaml`), and a version's files
form its schedule; nothing here holds a rate. Each charge is computed on the traded value, capped
where the file says so, and rounded half up to the paisa; GST is charged on the sum of the
components the file marks taxable. A sell also pays the file's flat depository (DP) charge, GST
included, once per instrument per sell day. All of it is Decimal.

`BacktestFills` is the fill model the shadow engine's virtual fills accept
(`app.hat.engine.shadow.FillModel`): shadow trading fills at the default slippage and cost model
(`engine.shadow.SHADOW_FILLS`), a backtest run at its own, through the same code.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from importlib import resources
from typing import Any, Literal

import yaml

from .confighash import config_hash
from .errors import HatError
from .money import exact, to_money

__all__ = [
    "DEFAULT_COST_MODEL",
    "DEFAULT_SLIPPAGE_BPS",
    "MAX_SLIPPAGE_BPS",
    "BacktestFills",
    "CashBoundFills",
    "Charges",
    "Component",
    "CostError",
    "CostModel",
    "CostSchedule",
    "charges",
    "load_cost_schedule",
    "parse_cost_model",
    "slipped",
]

Side = Literal["buy", "sell"]
SIDES: tuple[Side, ...] = ("buy", "sell")
DEFAULT_COST_MODEL = "cnc-delivery-2"
MAX_SLIPPAGE_BPS = Decimal(10_000)  # a slippage must be below 100%
# The slippage a run uses unless it names one (O5, PR 62): pessimistic, never perfect fills.
DEFAULT_SLIPPAGE_BPS = Decimal(20)
PRICE_PLACES = Decimal("0.0001")  # hat_fill.price is numeric(18, 4)
_BPS = Decimal(10_000)
_ZERO = Decimal(0)
DP = "dp"  # the depository charge's name in `Charges.by_component`


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
    """The charges in force from `effective_from` until the next file of its schedule."""

    version: str
    effective_from: date
    gst_rate: Decimal
    dp_per_sell_day: Decimal  # flat, GST included
    components: tuple[Component, ...]
    digest: str  # sha256 of the canonical document, recorded with every run that used it


@dataclass(frozen=True)
class CostSchedule:
    """A cost-model version's dated files, oldest first: the file in force on a day is the
    latest one effective on or before it."""

    version: str
    models: tuple[CostModel, ...]

    def __post_init__(self) -> None:
        days = [m.effective_from for m in self.models]
        if not days or days != sorted(set(days)):
            raise CostError("cost_bad_schedule")
        if any(m.version != self.version for m in self.models):
            raise CostError("cost_version_mismatch")

    def on(self, day: date) -> CostModel:
        """The file in force on `day`; `CostError` before the first one."""
        found = None
        for model in self.models:
            if model.effective_from > day:
                break
            found = model
        if found is None:
            raise CostError("cost_no_schedule")
        return found

    def used(self, start: date, end: date) -> tuple[CostModel, ...]:
        """The files in force on some day from `start` to `end`: those a fill in it can use."""
        first = self.on(start)
        return tuple(m for m in self.models if first.effective_from <= m.effective_from <= end)

    def to_json(self, start: date, end: date) -> dict[str, Any]:
        """The version and the digest of each file in force from `start` to `end`."""
        return {
            "version": self.version,
            "schedules": [
                {"effective_from": m.effective_from.isoformat(), "digest": m.digest}
                for m in self.used(start, end)
            ],
        }


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
    """A validated cost model, or `CostError`. Rates are strings or integers, never floats;
    `effective_from` is an ISO date string."""
    keys = {"version", "effective_from", "gst_rate", "dp_per_sell_day", "components"}
    if not isinstance(document, Mapping) or set(document) != keys:
        raise CostError("cost_bad_document")
    version = document["version"]
    if not isinstance(version, str) or not version:
        raise CostError("cost_bad_version")
    try:
        effective_from = date.fromisoformat(document["effective_from"])
    except (TypeError, ValueError):
        raise CostError("cost_bad_effective_from") from None
    raw = document["components"]
    if not isinstance(raw, Sequence) or isinstance(raw, str):
        raise CostError("cost_bad_component")
    components = tuple(_component(c) for c in raw)
    if len({c.name for c in components}) != len(components):
        raise CostError("cost_duplicate_component")
    if DP in {c.name for c in components}:
        raise CostError("cost_duplicate_component")
    return CostModel(
        version=version,
        effective_from=effective_from,
        gst_rate=_rate(document["gst_rate"], "gst_rate"),
        dp_per_sell_day=_rate(document["dp_per_sell_day"], "dp_per_sell_day"),
        components=components,
        digest=config_hash(dict(document)),
    )


def load_cost_schedule(version: str = DEFAULT_COST_MODEL) -> CostSchedule:
    """The packaged schedule `cost_models/<version>/`: every file in it, each named for its
    `effective_from` and naming `version`."""
    if not version.replace("-", "").isalnum():
        raise CostError("cost_unknown_version")
    folder = resources.files(__package__).joinpath("cost_models", version)
    if not folder.is_dir():
        raise CostError("cost_unknown_version")
    models = []
    for path in sorted(folder.iterdir(), key=lambda p: p.name):
        if not path.name.endswith(".yaml"):
            continue
        model = parse_cost_model(yaml.safe_load(path.read_text(encoding="utf-8")))
        if path.name != f"{model.effective_from.isoformat()}.yaml":
            raise CostError("cost_bad_schedule")
        models.append(model)
    return CostSchedule(version, tuple(models))


def charges(model: CostModel, side: str, qty: int, price: Decimal, *, dp: bool = False) -> Charges:
    """The charges of one fill of `qty` shares at `price` on `side` (HAT-BT-023); with `dp`, a
    sell also pays the flat depository charge (the instrument's first sale of the day)."""
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
    if dp and side == "sell":
        found[DP] = to_money(model.dp_per_sell_day)
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
    """The backtest's fill model: slippage, then the charges in force on the fill's trade date
    on the slipped fill."""

    schedule: CostSchedule
    slippage_bps: Decimal

    def __post_init__(self) -> None:
        check_slippage(self.slippage_bps)

    def fill(
        self,
        side: str,
        qty: int,
        price: Decimal,
        day: date,
        /,
        *,
        sold_today: bool = False,
        limit: Decimal | None = None,
    ) -> tuple[Decimal, Decimal]:
        """(fill price, fees) of a fill of `qty` at the reference `price` on trade date `day`.
        A sell pays the DP charge unless the instrument was `sold_today` already (one DP debit
        per instrument per sell day, however many sales). A buy `limit` caps the slipped price:
        a LIMIT order never fills above it."""
        filled = slipped(price, side, self.slippage_bps)
        if limit is not None and side == "buy":
            filled = min(filled, limit)
        model = self.schedule.on(day)
        return filled, charges(model, side, qty, filled, dp=not sold_today).total

    def outlay(self, qty: int, price: Decimal, day: date) -> Decimal:
        """The cash a buy of `qty` at the reference `price` on `day` takes: its value after
        slippage plus its charges."""
        filled, fees = self.fill("buy", qty, price, day)
        return filled * qty + fees

    def affordable(self, cash: Decimal, price: Decimal, day: date) -> int:
        """The most shares a buy at the reference `price` can take whose outlay (slippage and
        charges included) fits in `cash`; 0 when not one does, or `cash` is not positive. The
        outlay grows with the quantity, so the largest one that fits is found by bisection."""
        if cash <= 0:
            return 0
        low, high = 0, int(cash // slipped(price, "buy", self.slippage_bps))
        while low < high:
            mid = (low + high + 1) // 2
            if self.outlay(mid, price, day) <= cash:
                low = mid
            else:
                high = mid - 1
        return low

    def within(self, cash: Decimal, limit: Decimal | None = None) -> CashBoundFills:
        """These fills, refusing a buy whose outlay exceeds `cash`, as the broker would; a buy
        under a LIMIT order fills at most at `limit`."""
        return CashBoundFills(self, cash, limit)


@dataclass(frozen=True)
class CashBoundFills:
    """A run's fills for one entry, with the account's free cash: a buy whose value plus
    charges exceeds `cash` is refused (`cost_cash_overdraw`), as the broker refuses it. Sizing
    reserves the outlay first (`BacktestFills.affordable`), so the refusal is a guard only."""

    fills: BacktestFills
    cash: Decimal
    limit: Decimal | None = None

    def fill(
        self, side: str, qty: int, price: Decimal, day: date, /, *, sold_today: bool = False
    ) -> tuple[Decimal, Decimal]:
        filled, fees = self.fills.fill(
            side, qty, price, day, sold_today=sold_today, limit=self.limit
        )
        if side == "buy" and filled * qty + fees > self.cash:
            raise CostError("cost_cash_overdraw")
        return filled, fees

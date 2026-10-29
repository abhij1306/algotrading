"""The idle-cash ETF overlay (docs/decisions.md, "Idle-cash ETF overlay"): a portfolio-level
sleeve of ETFs holding the cash the strategies do not use, switched by a fixed market-regime
rule, funding the strategies' entries and paying the run's income tax (`tax`). Off by default:
a run without an `OverlayConfig` is the run it always was.

The rules, each fixed before any result is seen:

- Regime (`band_states`): NIFTY 50 close against its 200-session SMA. RISK-ON after 5
  consecutive closes above SMA x 1.02, RISK-OFF after 5 below SMA x 0.98, else the previous
  state; RISK-OFF until the first switch. Decided on the close, acted on at the next open.
- Assets (`sleeve_asset`): `liquid` holds LIQUIDBEES, `nifty` NIFTYBEES, `regime` NIFTYBEES when
  on and LIQUIDBEES when off, `regime_gold` NIFTYBEES when on and when off GOLDBEES while
  GOLDBEES' own band rule is on, else LIQUIDBEES; `none` holds nothing (a run that only taxes).
- Sizing (`target_value`): equity = cash + sleeve + positions at mark; the target sleeve is
  equity - positions - 5% of equity, at least 0. The strategies' allocations read the whole
  equity, the sleeve included, so a run sizes its trades alike with and without the overlay.
  Each close compares the sleeve with its target and trades at the next open only when they
  differ by more than 2% of equity (`needs_rebalance`) or the asset switches. Open positions
  plus a NIFTYBEES sleeve never exceed equity (`exposure_ok`, checked every close).
- Prices (`sleeve_price`, `SleevePrices`): never the day's open, a thin pre-open auction print on
  these ETFs. The open's trades fill at the close of the session's 09:15 5-minute ETF bar, a
  funding sale at the close of the bar that ended as the entry filled; without that bar (before
  5-minute bars, from 2021-11, or a day missing it) at the previous close, NIFTYBEES' times
  NIFTY 50's open over its previous close, each such fill counted `sleeve_price_estimated`. A tax
  sale and the final liquidation fill at the day's close.
- Funding (`units_to_fund`): an entry the free cash cannot pay sells the sleeve for the
  shortfall when it fills; the sale credits `same_day_sale_credit_pct` of its proceeds
  that day (80% before 2024-10-07, 100% from then, `CreditSchedule`) and the rest at the next
  session. An entry the cash and the credit cannot pay is sized down by the risk check; one it
  refuses for cash while the sleeve held units counts `insufficient_same_day_credit`.
- Costs: every ETF trade pays the run's cost model at its own slippage (5 bps NIFTYBEES and
  GOLDBEES, 1 bp LIQUIDBEES a side, configurable and recorded).
- LIQUIDBEES' return is its daily dividend paid as units: each session credits the units its
  total-return series implies (`implied_distribution`), whole units, the fraction carried.
- Tax (`tax.TaxBook`): every fill of the run, strategy and sleeve, FIFO; stock dividends and
  LIQUIDBEES dividend units are slab income; a financial year's tax is paid from cash at its
  last session (31 March or the session before), the sleeve sold for what the cash lacks; the
  period's last session sells the sleeve and pays the open year's tax (full liquidation).
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Any

from ..costs import BacktestFills, CostSchedule
from ..money import exact
from ..time import IST
from . import tax as taxes

__all__ = [
    "ASSETS",
    "GOLDBEES",
    "LIQUIDBEES",
    "NIFTYBEES",
    "SLEEVES",
    "Band",
    "CreditSchedule",
    "EtfBar",
    "EtfData",
    "Overlay",
    "OverlayConfig",
    "SleevePrices",
    "band_states",
    "exposure_ok",
    "implied_distribution",
    "needs_rebalance",
    "parse_overlay",
    "replay_overlay",
    "refusal_reason",
    "sleeve_asset",
    "sleeve_price",
    "target_value",
    "units_to_fund",
]

NIFTYBEES = "NIFTYBEES"
GOLDBEES = "GOLDBEES"
LIQUIDBEES = "LIQUIDBEES"
ASSETS = (NIFTYBEES, GOLDBEES, LIQUIDBEES)
TAX_CLASS = {NIFTYBEES: taxes.EQUITY, GOLDBEES: taxes.GOLD, LIQUIDBEES: taxes.DEBT}
REGIME_INDEX = "NIFTY 50"
NONE, LIQUID, NIFTY, REGIME, REGIME_GOLD = "none", "liquid", "nifty", "regime", "regime_gold"
SLEEVES = (NONE, LIQUID, NIFTY, REGIME, REGIME_GOLD)
INSUFFICIENT_CREDIT = "insufficient_same_day_credit"
SLEEVE_PRICE_ESTIMATED = "sleeve_price_estimated"
FUNDING_SALES = "funding_sales"
# The cash refusals of the risk check an overlay run reads as the sleeve's credit falling short.
CASH_REFUSALS = frozenset({"size_zero_cash", "size_below_min"})
_ZERO = Decimal(0)
_ONE = Decimal(1)
_BAR = timedelta(minutes=5)
_TICK = Decimal("0.0001")
_OPEN = time(9, 15)  # the first 5-minute bar of continuous trading (IST)


class OverlayError(ValueError):
    """An overlay block the run cannot take; `code` is a fixed word."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the rules, pure ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Band:
    """A band rule on a close against its SMA: on after `confirm` consecutive closes above SMA x
    `upper`, off after as many below SMA x `lower`."""

    sma: int = 200
    upper: Decimal = Decimal("1.02")
    lower: Decimal = Decimal("0.98")
    confirm: int = 5

    def record(self) -> dict[str, Any]:
        return {
            "sma": self.sma,
            "upper": str(self.upper),
            "lower": str(self.lower),
            "confirm": self.confirm,
        }


def band_states(
    closes: Sequence[tuple[date, Decimal]], band: Band, initial: bool = False
) -> dict[date, bool]:
    """The state (True: on) after each close, in date order: `initial` (off) until a switch; a
    close before the SMA has `band.sma` closes resets both streaks and keeps the state."""
    out: dict[date, bool] = {}
    window: list[Decimal] = []
    total = _ZERO
    state, up, down = initial, 0, 0
    for day, close in sorted(closes):
        window.append(close)
        total += close
        if len(window) > band.sma:
            total -= window.pop(0)
        if len(window) < band.sma:
            up = down = 0
        else:
            sma = total / band.sma
            up = up + 1 if close > sma * band.upper else 0
            down = down + 1 if close < sma * band.lower else 0
            if not state and up >= band.confirm:
                state = True
            elif state and down >= band.confirm:
                state = False
        out[day] = state
    return out


def sleeve_asset(sleeve: str, risk_on: bool, gold_on: bool) -> str | None:
    """The asset the sleeve holds in a regime (the module docstring); None for `none`."""
    if sleeve == NONE:
        return None
    if sleeve == LIQUID:
        return LIQUIDBEES
    if sleeve == NIFTY or risk_on:
        return NIFTYBEES
    if sleeve == REGIME_GOLD and gold_on:
        return GOLDBEES
    return LIQUIDBEES


def target_value(equity: Decimal, positions: Decimal, buffer_pct: Decimal) -> Decimal:
    """The sleeve's target: equity less the open positions and the cash buffer, at least 0."""
    return max(equity - positions - equity * buffer_pct, _ZERO)


def needs_rebalance(
    actual: Decimal, target: Decimal, equity: Decimal, threshold_pct: Decimal
) -> bool:
    """Whether the sleeve differs from its target by more than `threshold_pct` of equity."""
    return abs(target - actual) > equity * threshold_pct


def exposure_ok(positions: Decimal, equity_sleeve: Decimal, equity: Decimal) -> bool:
    """Open strategy positions plus an equity (NIFTYBEES) sleeve within 100% of equity."""
    return positions + equity_sleeve <= equity


def units_to_fund(shortfall: Decimal, net_per_unit: Decimal, credit_pct: Decimal, held: int) -> int:
    """The units to sell so that their same-day credit (`credit_pct` of the proceeds net of
    charges, `net_per_unit` each) covers `shortfall`, at most `held`; 0 for no shortfall."""
    if shortfall <= 0 or held <= 0 or net_per_unit <= 0 or credit_pct <= 0:
        return 0
    need = (shortfall / (net_per_unit * credit_pct)).to_integral_value(ROUND_CEILING)
    return min(int(need), held)


def implied_distribution(
    prev_close: Decimal, close: Decimal, prev_tr: Decimal, tr: Decimal
) -> Decimal:
    """The cash per unit a total-return series implies a session paid out: the price the total
    return reached from the previous close, less the close; never below 0."""
    if prev_tr <= 0:
        return _ZERO
    return max(prev_close * tr / prev_tr - close, _ZERO)


def refusal_reason(reason: str | None, sleeve_held: bool) -> str | None:
    """A refused entry's reason in an overlay run: a cash refusal of the risk check while the
    sleeve held units (its same-day credit could not pay the entry) is
    `insufficient_same_day_credit`; any other reason is the risk check's."""
    if sleeve_held and reason in CASH_REFUSALS:
        return INSUFFICIENT_CREDIT
    return reason


@dataclass(frozen=True)
class CreditSchedule:
    """`same_day_sale_credit_pct`: the share of a sale's proceeds the broker credits on the sale
    day, from each effective date (sorted, the first in force from the start)."""

    steps: tuple[tuple[date, Decimal], ...] = (
        (date(2000, 1, 1), Decimal("0.8")),
        (date(2024, 10, 7), Decimal(1)),
    )

    def __post_init__(self) -> None:
        days = [d for d, _ in self.steps]
        if not days or days != sorted(set(days)) or not all(0 < p <= 1 for _, p in self.steps):
            raise OverlayError("overlay_bad_credit")

    def on(self, day: date) -> Decimal:
        found = self.steps[0][1]
        for start, pct in self.steps:
            if start <= day:
                found = pct
        return found

    def record(self) -> list[dict[str, str]]:
        return [{"from": d.isoformat(), "pct": str(p)} for d, p in self.steps]


# --- the block ---------------------------------------------------------------------------------

DEFAULT_SLIPPAGE = {NIFTYBEES: Decimal(5), GOLDBEES: Decimal(5), LIQUIDBEES: Decimal(1)}


@dataclass(frozen=True)
class OverlayConfig:
    """A run's overlay block (`parse_overlay`), recorded into its conventions and digest."""

    sleeve: str = REGIME
    band: Band = field(default_factory=Band)
    gold_band: Band = field(default_factory=Band)
    cash_buffer_pct: Decimal = Decimal("0.05")
    rebalance_pct: Decimal = Decimal("0.02")
    credit: CreditSchedule = field(default_factory=CreditSchedule)
    slippage_bps: Mapping[str, Decimal] = field(default_factory=lambda: dict(DEFAULT_SLIPPAGE))
    tax: bool = True
    slab_rate: Decimal = Decimal("0.30")

    def __post_init__(self) -> None:
        if self.sleeve not in SLEEVES:
            raise OverlayError("overlay_bad_sleeve")
        if set(self.slippage_bps) != set(ASSETS):
            raise OverlayError("overlay_bad_slippage")
        for pct in (self.cash_buffer_pct, self.rebalance_pct, self.slab_rate):
            if not (isinstance(pct, Decimal) and 0 <= pct <= 1):
                raise OverlayError("overlay_bad_pct")

    def record(self) -> dict[str, Any]:
        return {
            "sleeve": self.sleeve,
            "regime": {"index": REGIME_INDEX, **self.band.record()},
            "gold_regime": self.gold_band.record(),
            "cash_buffer_pct": str(self.cash_buffer_pct),
            "rebalance_pct": str(self.rebalance_pct),
            "same_day_sale_credit_pct": self.credit.record(),
            "slippage_bps": {k: str(self.slippage_bps[k]) for k in ASSETS},
            "tax": {"enabled": self.tax, "slab_rate": str(self.slab_rate), "cess": str(taxes.CESS)},
        }


OVERLAY_KEYS = frozenset(
    {
        "sleeve",
        "regime",
        "gold_regime",
        "cash_buffer_pct",
        "rebalance_pct",
        "same_day_sale_credit_pct",
        "slippage_bps",
        "tax",
    }
)
_BAND_KEYS = frozenset({"sma", "upper", "lower", "confirm"})


def _dec(value: object) -> Decimal:
    found = exact(value, floats=True)
    if found is None:
        raise OverlayError("overlay_bad_value")
    return found


def _band(raw: object) -> Band:
    if raw is None:
        return Band()
    if not isinstance(raw, Mapping) or not set(raw) <= _BAND_KEYS:
        raise OverlayError("overlay_bad_band")
    base = Band()
    sma, confirm = raw.get("sma", base.sma), raw.get("confirm", base.confirm)
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1 for v in (sma, confirm)):
        raise OverlayError("overlay_bad_band")
    return Band(
        int(sma), _dec(raw.get("upper", base.upper)), _dec(raw.get("lower", base.lower)), confirm
    )


def parse_overlay(raw: object) -> OverlayConfig:
    """An overlay block (a portfolio file's `overlay`) as its config: every key optional, the
    plan's values by default (`OverlayConfig`); `tax` is true, false or `{slab_rate: 0.3}`;
    `same_day_sale_credit_pct` a list of `{from: YYYY-MM-DD, pct: P}`; `slippage_bps` a mapping
    by ETF. `OverlayError` for any other shape."""
    if not isinstance(raw, Mapping) or not set(raw) <= OVERLAY_KEYS:
        raise OverlayError("overlay_bad_block")
    base = OverlayConfig()
    slippage = dict(DEFAULT_SLIPPAGE)
    given = raw.get("slippage_bps", {})
    if not isinstance(given, Mapping) or not set(given) <= set(ASSETS):
        raise OverlayError("overlay_bad_slippage")
    slippage.update({k: _dec(v) for k, v in given.items()})
    credit = base.credit
    if "same_day_sale_credit_pct" in raw:
        steps = raw["same_day_sale_credit_pct"]
        if not isinstance(steps, list) or not all(
            isinstance(s, Mapping) and set(s) == {"from", "pct"} for s in steps
        ):
            raise OverlayError("overlay_bad_credit")
        try:
            credit = CreditSchedule(
                tuple((date.fromisoformat(str(s["from"])), _dec(s["pct"])) for s in steps)
            )
        except ValueError:
            raise OverlayError("overlay_bad_credit") from None
    tax_raw = raw.get("tax", True)
    slab = base.slab_rate
    if isinstance(tax_raw, Mapping):
        if not set(tax_raw) <= {"slab_rate"}:
            raise OverlayError("overlay_bad_tax")
        slab = _dec(tax_raw.get("slab_rate", slab))
        tax_raw = True
    if not isinstance(tax_raw, bool):
        raise OverlayError("overlay_bad_tax")
    return OverlayConfig(
        sleeve=str(raw.get("sleeve", base.sleeve)),
        band=_band(raw.get("regime")),
        gold_band=_band(raw.get("gold_regime")),
        cash_buffer_pct=_dec(raw.get("cash_buffer_pct", base.cash_buffer_pct)),
        rebalance_pct=_dec(raw.get("rebalance_pct", base.rebalance_pct)),
        credit=credit,
        slippage_bps=slippage,
        tax=tax_raw,
        slab_rate=slab,
    )


def replay_overlay(record: object) -> OverlayConfig:
    """The config a run's recorded overlay (`OverlayConfig.record`, in its `config.json`) was:
    `parse_overlay` of the record less what it adds (the regime's index, the cess)."""
    if not isinstance(record, Mapping):
        raise OverlayError("overlay_bad_block")
    doc = dict(record)
    if isinstance(doc.get("regime"), Mapping):
        doc["regime"] = {k: v for k, v in doc["regime"].items() if k != "index"}
    tax = doc.get("tax")
    if isinstance(tax, Mapping):
        doc["tax"] = {"slab_rate": tax.get("slab_rate")} if tax.get("enabled") else False
    for key in ("regime", "gold_regime"):  # recorded as text, parsed as the file writes them
        band = doc.get(key)
        if isinstance(band, Mapping):
            doc[key] = {k: (int(v) if k in {"sma", "confirm"} else v) for k, v in band.items()}
    return parse_overlay(doc)


# --- the data ----------------------------------------------------------------------------------


@dataclass(frozen=True)
class EtfBar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


@dataclass(frozen=True)
class EtfData:
    """What the overlay reads (`etf_data.load`): each ETF's daily bars (split-adjusted) and
    5-minute bars by start instant (IST), LIQUIDBEES' total-return index with the date ranges a
    proxy stands in for, and the NIFTY 50 closes."""

    daily: Mapping[str, Mapping[date, EtfBar]]
    bars_5m: Mapping[str, Mapping[date, Sequence[tuple[datetime, EtfBar]]]]
    liquid_tr: Mapping[date, Decimal]
    proxy_ranges: Sequence[tuple[date, date]]
    regime_closes: Sequence[tuple[date, Decimal]]
    # the reference total-return indices (NIFTY 50 TRI, NIFTY 500 TRI), pre-tax only
    references: Mapping[str, Sequence[tuple[date, Decimal]]] = field(default_factory=dict)
    # NIFTY 50's opens, for NIFTYBEES' estimated open price (`sleeve_price`)
    regime_opens: Mapping[date, Decimal] = field(default_factory=dict)

    def record(self) -> dict[str, Any]:
        return {
            "liquidbees_tr_proxy": [[a.isoformat(), b.isoformat()] for a, b in self.proxy_ranges]
        }


def sleeve_price(
    bars: Sequence[tuple[datetime, EtfBar]],
    at: datetime | None,
    prev_close: Decimal | None,
    index_move: Decimal | None,
    close: Decimal,
) -> tuple[Decimal, bool]:
    """A sleeve fill's price on a session and whether it is estimated: the close of the
    session's 09:15 5-minute bar of `bars` (the open's trades, `at` None) or of the bar that
    ended in the five minutes up to `at` (a funding sale: an entry fills at its trigger bar's
    final instant, just after that bar's end), so never a price after `at`; without that bar,
    estimated, `prev_close` times `index_move` (NIFTY 50's open over its previous close,
    NIFTYBEES only; else 1), or the session's `close` without a previous close. Never the day's
    open (the module docstring)."""
    if at is None:
        found = next((bar for start, bar in bars if start.astimezone(IST).time() == _OPEN), None)
    else:
        found = next((bar for start, bar in bars if start + _BAR <= at < start + 2 * _BAR), None)
    if found is not None:
        return found.close, False
    if prev_close is None:
        return close, True
    return (prev_close * (index_move or _ONE)).quantize(_TICK), True


class SleevePrices:
    """`sleeve_price` over the ETF data: each fill's bars, previous close and index move."""

    def __init__(self, data: EtfData) -> None:
        self.data = data
        self.days = {a: sorted(data.daily.get(a, {})) for a in ASSETS}
        self.index_closes = dict(data.regime_closes)

    def prev_close(self, asset: str, day: date) -> tuple[date, Decimal] | None:
        """The asset's last daily close before `day`, with its date."""
        days = self.days.get(asset, [])
        n = bisect_left(days, day)
        return None if n == 0 else (days[n - 1], self.data.daily[asset][days[n - 1]].close)

    def at(self, asset: str, day: date, at: datetime | None = None) -> tuple[Decimal, bool] | None:
        """The asset's fill price on `day` (`sleeve_price`); None without a daily bar that day."""
        bar = self.data.daily.get(asset, {}).get(day)
        if bar is None:
            return None
        before = self.prev_close(asset, day)
        move = None
        if before is not None and asset == NIFTYBEES:
            index_open, index_close = (
                self.data.regime_opens.get(day),
                self.index_closes.get(before[0]),
            )
            move = index_open / index_close if index_open and index_close else None
        bars = self.data.bars_5m.get(asset, {}).get(day, ())
        return sleeve_price(bars, at, None if before is None else before[1], move, bar.close)


# --- the sleeve, day by day --------------------------------------------------------------------


@dataclass
class _Plan:
    """The next open's trades: the asset the sleeve should hold and its target value."""

    asset: str | None
    target: Decimal
    reason: str


@dataclass
class OverlayRow:
    """One session of the overlay (overlay.csv)."""

    day: date
    risk_on: bool
    asset: str | None
    sleeve_value: Decimal
    target: Decimal
    cash: Decimal
    positions: Decimal
    equity: Decimal
    tax_paid: Decimal

    def csv(self) -> list[str]:
        return [
            self.day.isoformat(),
            "on" if self.risk_on else "off",
            self.asset or "",
            str(self.sleeve_value),
            str(self.target),
            str(self.cash),
            str(self.positions),
            str(self.equity),
            str(self.tax_paid),
            str(self.equity + self.tax_paid),
        ]


OVERLAY_CSV_HEADER = (
    "date",
    "regime",
    "asset",
    "sleeve_value",
    "target",
    "cash",
    "positions",
    "equity",
    "tax_paid_to_date",
    "equity_pre_tax",
)


class Overlay:
    """The sleeve's state over one run: its units, the cash it moved into or out of the
    account (`cash`), the sale proceeds credited only from the next session (`pending`), the
    plan for the next open, the tax book and what it logged. The loop calls `open_session`
    before the day's exits, `credit_for` and `fund` around each entry, `close_session` after the
    day's entries and `finish` on the period's last session."""

    def __init__(self, config: OverlayConfig, data: EtfData, schedule: CostSchedule) -> None:
        self.config = config
        self.data = data
        self.fills = {a: BacktestFills(schedule, config.slippage_bps[a]) for a in ASSETS}
        self.prices = SleevePrices(data)
        self.regime = band_states(data.regime_closes, config.band)
        gold = [(d, b.close) for d, b in data.daily.get(GOLDBEES, {}).items()]
        self.gold = band_states(gold, config.gold_band)
        self.units: dict[str, int] = dict.fromkeys(ASSETS, 0)
        self.residue: Decimal = _ZERO  # LIQUIDBEES distribution short of a whole unit
        self.cash = _ZERO
        self.pending = _ZERO
        self.tax_due = _ZERO
        self.tax_paid = _ZERO
        self.plan: _Plan | None = None
        self.book = taxes.TaxBook(config.slab_rate) if config.tax else None
        self.rows: list[OverlayRow] = []
        self.events: list[dict[str, Any]] = []
        # the entries refused for credit, the fills at an estimated price, the funding sales
        self.counts: dict[str, int] = {
            INSUFFICIENT_CREDIT: 0,
            SLEEVE_PRICE_ESTIMATED: 0,
            FUNDING_SALES: 0,
        }
        self.by_year: dict[int, dict[str, Decimal]] = {}
        self.years_tax: list[taxes.YearTax] = []

    # --- prices and value --------------------------------------------------------------------

    def _bar(self, asset: str, day: date) -> EtfBar | None:
        return self.data.daily.get(asset, {}).get(day)

    def _close(self, asset: str, day: date) -> Decimal | None:
        """The asset's last close on or before `day`."""
        bars = self.data.daily.get(asset, {})
        bar = bars.get(day)
        if bar is not None:
            return bar.close
        before = [d for d in bars if d <= day]
        return bars[max(before)].close if before else None

    def value(self, day: date) -> Decimal:
        """The sleeve's units at their last close on or before `day`."""
        total = _ZERO
        for asset, units in self.units.items():
            if units:
                total += units * (self._close(asset, day) or _ZERO)
        return total

    def held_asset(self) -> str | None:
        found = [a for a, u in self.units.items() if u]
        return found[0] if len(found) == 1 else (None if not found else found[0])

    def _year(self, day: date) -> dict[str, Decimal]:
        return self.by_year.setdefault(
            day.year, {"costs": _ZERO, "turnover": _ZERO, "switches": _ZERO, "rebalances": _ZERO}
        )

    # --- trades ------------------------------------------------------------------------------

    def _sell(
        self,
        asset: str,
        qty: int,
        price: Decimal,
        day: date,
        credit: Decimal,
        estimated: bool = False,
    ) -> Decimal:
        """Sell `qty` units at `price`; the proceeds net of charges, `credit` of them credited now
        and the rest pending to the next session. Returns the credited amount."""
        if qty <= 0:
            return _ZERO
        self.counts[SLEEVE_PRICE_ESTIMATED] += estimated
        filled, fees = self.fills[asset].fill("sell", qty, price, day)
        proceeds = filled * qty - fees
        self.units[asset] -= qty
        now = proceeds * credit
        self.cash += proceeds
        self.pending += proceeds - now
        year = self._year(day)
        year["costs"] += fees + (price - filled) * qty
        year["turnover"] += filled * qty
        if self.book is not None:
            self.book.sell(asset, qty, proceeds, day)
        return now

    def _buy(self, asset: str, budget: Decimal, price: Decimal, day: date, estimated: bool) -> None:
        """Buy as many units at `price` as `budget` pays for (slippage and charges included)."""
        qty = self.fills[asset].affordable(budget, price, day)
        if qty <= 0:
            return
        self.counts[SLEEVE_PRICE_ESTIMATED] += estimated
        filled, fees = self.fills[asset].fill("buy", qty, price, day)
        outlay = filled * qty + fees
        self.units[asset] += qty
        self.cash -= outlay
        year = self._year(day)
        year["costs"] += fees + (filled - price) * qty
        year["turnover"] += filled * qty
        if self.book is not None:
            self.book.buy(asset, TAX_CLASS[asset], qty, outlay, day)

    def _proceeds(self, asset: str, qty: int, price: Decimal, day: date) -> Decimal:
        """A sale's proceeds net of slippage and charges."""
        filled, fees = self.fills[asset].fill("sell", qty, price, day)
        return filled * qty - fees

    def _units_for(
        self, asset: str, need: Decimal, price: Decimal, day: date, credit: Decimal
    ) -> int:
        """The fewest units whose sale credits `need` now (`units_to_fund` on the mean net
        price of the whole holding, then up one unit at a time past the flat charges), at most
        the units held."""
        held = self.units[asset]
        if need <= 0 or not held:
            return 0
        qty = units_to_fund(need, self._proceeds(asset, held, price, day) / held, credit, held)
        while qty < held and self._proceeds(asset, qty, price, day) * credit < need:
            qty += 1
        return qty

    # --- the session -------------------------------------------------------------------------

    def open_session(self, day: date, free_cash: Decimal) -> None:
        """Release the last session's pending credit, then trade the plan at `day`'s open (at
        `SleevePrices`' price): sell what the target asset is not (and any excess), then buy up
        to the target with what the free cash allows (`free_cash` the account's, pending
        excluded)."""
        free_cash += self.pending
        self.pending = _ZERO
        plan, self.plan = self.plan, None
        if plan is None:
            return
        for asset in ASSETS:
            units = self.units[asset]
            found = self.prices.at(asset, day) if units and asset != plan.asset else None
            if found is not None:
                free_cash += self._sell(asset, units, found[0], day, _ONE, found[1])
        asset = plan.asset
        found = None if asset is None else self.prices.at(asset, day)
        if asset is None or found is None:
            return
        price, estimated = found
        held = self.units[asset] * price
        if held > plan.target:
            qty = int(((held - plan.target) / price).to_integral_value(ROUND_FLOOR))
            free_cash += self._sell(asset, min(qty, self.units[asset]), price, day, _ONE, estimated)
        elif plan.target > held:
            self._buy(asset, min(plan.target - held, max(free_cash, _ZERO)), price, day, estimated)
        self.events.append(
            {
                "date": day.isoformat(),
                "event": plan.reason,
                "asset": asset,
                "target": str(plan.target),
                "price": str(price),
                "estimated": estimated,
            }
        )

    def credit_for(self, day: date, at: datetime) -> Decimal:
        """What selling the whole sleeve at `at` would credit today (net of charges)."""
        total = _ZERO
        pct = self.config.credit.on(day)
        for asset, units in self.units.items():
            found = self.prices.at(asset, day, at) if units else None
            if found is not None:
                total += self._proceeds(asset, units, found[0], day) * pct
        return total

    def fund(self, day: date, at: datetime, shortfall: Decimal) -> None:
        """Sell enough of the sleeve at `at` that today's credit covers `shortfall`."""
        pct = self.config.credit.on(day)
        for asset in ASSETS:
            if shortfall <= 0:
                return
            found = self.prices.at(asset, day, at) if self.units[asset] else None
            if found is None:
                continue
            price, estimated = found
            qty = self._units_for(asset, shortfall, price, day, pct)
            if qty:
                shortfall -= self._sell(asset, qty, price, day, pct, estimated)
                self.counts[FUNDING_SALES] += 1

    def refused_for_credit(self) -> None:
        self.counts[INSUFFICIENT_CREDIT] += 1

    def held_units(self) -> bool:
        return any(self.units.values())

    def _distribute(self, day: date, prev: date | None) -> None:
        """LIQUIDBEES' distribution of `day` as whole units (the fraction carried), slab income."""
        units = self.units[LIQUIDBEES]
        if not units or prev is None:
            return
        tr, prev_tr = self.data.liquid_tr.get(day), self.data.liquid_tr.get(prev)
        bar, before = self._bar(LIQUIDBEES, day), self._close(LIQUIDBEES, prev)
        if tr is None or prev_tr is None or bar is None or before is None:
            return
        self.residue += units * implied_distribution(before, bar.close, prev_tr, tr)
        whole = int((self.residue / bar.close).to_integral_value(ROUND_FLOOR))
        if whole:
            value = whole * bar.close
            self.residue -= value
            self.units[LIQUIDBEES] += whole
            if self.book is not None:
                self.book.dividend_units(LIQUIDBEES, whole, value, day)

    def trade(self, side: str, instrument: str, qty: int, amount: Decimal, day: date) -> None:
        """A strategy fill into the tax book: a buy's outlay, a sale's net proceeds."""
        if self.book is None:
            return
        if side == "buy":
            self.book.buy(instrument, taxes.EQUITY, qty, amount, day)
        else:
            self.book.sell(instrument, qty, amount, day)

    def dividend(self, day: date, cash: Decimal) -> None:
        """A stock dividend credited in cash: slab income of its year."""
        if self.book is not None and cash:
            self.book.income(day, cash)

    def _pay_tax(self, day: date, free_cash: Decimal) -> None:
        """Pay what tax is due from the free cash, selling the sleeve at the close for the rest
        (credited in full: the payment is the account's own); what still lacks waits."""
        if self.tax_due <= 0:
            return
        if free_cash < self.tax_due:
            for asset in ASSETS:
                price = self._close(asset, day)
                if not self.units[asset] or price is None or free_cash >= self.tax_due:
                    continue
                qty = self._units_for(asset, self.tax_due - free_cash, price, day, _ONE)
                free_cash += self._sell(asset, qty, price, day, _ONE)
        paid = min(self.tax_due, max(free_cash, _ZERO))
        self.cash -= paid
        self.tax_due -= paid
        self.tax_paid += paid

    def _settle(self, fy: int) -> None:
        if self.book is None:
            return
        found = self.book.settle(fy)
        self.years_tax.append(found)
        self.tax_due += found.tax

    def close_session(
        self,
        day: date,
        prev: date | None,
        next_day: date | None,
        *,
        equity: Decimal,
        positions: Decimal,
        free_cash: Decimal,
    ) -> None:
        """After the day's trades: LIQUIDBEES' distribution, the year's tax on its last
        session, the exposure check, and the next open's plan. `equity` is the account's mark
        before the distribution and tax, `free_cash` its cash less what is pending."""
        value, cash = self.value(day), self.cash
        self._distribute(day, prev)
        if next_day is None or taxes.fy_of(next_day) != taxes.fy_of(day):
            self._settle(taxes.fy_of(day))
        self._pay_tax(day, free_cash)
        # the distribution adds units, the tax takes cash and the sale that paid it its charges
        equity_after = equity + (self.value(day) - value) + (self.cash - cash)
        risk_on = self.regime.get(day, False)
        asset = sleeve_asset(self.config.sleeve, risk_on, self.gold.get(day, False))
        sleeve = self.value(day)
        nifty = self.units[NIFTYBEES] * (self._close(NIFTYBEES, day) or _ZERO)
        if not exposure_ok(positions, nifty, equity_after):
            raise OverlayError("overlay_exposure_cap")
        target = (
            _ZERO
            if asset is None
            else target_value(equity_after, positions, self.config.cash_buffer_pct)
        )
        held = self.held_asset()
        switch = asset != held and sleeve > 0
        if next_day is not None and asset is not None:
            if switch:
                self.plan = _Plan(asset, target, "switch")
                self._year(next_day)["switches"] += 1
            elif needs_rebalance(sleeve, target, equity_after, self.config.rebalance_pct):
                self.plan = _Plan(asset, target, "rebalance")
                self._year(next_day)["rebalances"] += 1
        self._row(day, risk_on, asset, target, positions, equity_after)

    def _row(
        self,
        day: date,
        risk_on: bool,
        asset: str | None,
        target: Decimal,
        positions: Decimal,
        equity: Decimal,
    ) -> None:
        sleeve = self.value(day)
        self.rows.append(
            OverlayRow(
                day,
                risk_on,
                asset,
                sleeve,
                target,
                equity - positions - sleeve,
                positions,
                equity,
                self.tax_paid,
            )
        )

    def first_plan(self, day: date, capital: Decimal) -> _Plan | None:
        """The buy at `day`'s open of a run that holds the sleeve from its first session: the
        asset of the states on the last close before it."""
        before = [d for d in self.regime if d < day]
        gold = [d for d in self.gold if d < day]
        asset = sleeve_asset(
            self.config.sleeve,
            self.regime[max(before)] if before else False,
            self.gold[max(gold)] if gold else False,
        )
        if asset is None:
            return None
        return _Plan(asset, target_value(capital, _ZERO, self.config.cash_buffer_pct), "start")

    def final_row(self, day: date, equity: Decimal) -> None:
        """The period's last session, after `finish`: all of it cash."""
        self._row(day, self.regime.get(day, False), None, _ZERO, _ZERO, equity)

    def finish(self, day: date, free_cash: Decimal) -> None:
        """The period's last session: sell the sleeve at the close and pay the open year's tax
        (and any tax still due)."""
        for asset in ASSETS:
            price = self._close(asset, day)
            if self.units[asset] and price is not None:
                free_cash += self._sell(asset, self.units[asset], price, day, _ONE)
        free_cash += self.pending
        self.pending = _ZERO
        if self.book is not None and taxes.fy_of(day) not in self.book.settled:
            self._settle(taxes.fy_of(day))
        self._pay_tax(day, free_cash)

    def summary(self) -> dict[str, Any]:
        """run.json's `overlay` block (besides the config the conventions record)."""
        return {
            "counts": dict(self.counts),
            "tax_paid": str(self.tax_paid),
            "tax_unpaid": str(self.tax_due),
            "tax_by_fy": [
                {
                    "fy": f"{y.fy}-{(y.fy + 1) % 100:02d}",
                    "tax": str(y.tax),
                    "income": str(y.income),
                    "exemption_used": str(y.exemption_used),
                    "taxable": {k: str(v) for k, v in y.taxable.items()},
                    "carried_st": str(y.carried_st),
                    "carried_lt": str(y.carried_lt),
                }
                for y in self.years_tax
            ],
            "by_year": {
                str(y): {k: str(v) for k, v in row.items()}
                for y, row in sorted(self.by_year.items())
            },
            "events": self.events,
            **self.data.record(),
        }


# --- the benchmarks (plan 1.6 and Phase 2's B1, B2) --------------------------------------------


def alone(
    config: OverlayConfig,
    data: EtfData,
    schedule: CostSchedule,
    capital: Decimal,
    days: Sequence[date],
) -> dict[str, str]:
    """The overlay with no strategy over `days` from `capital` (every session's account is the
    sleeve and its cash): the final equity after tax, the tax paid and the equity before it
    (after tax + tax paid), the sleeve liquidated on the last session."""
    sleeve = Overlay(config, data, schedule)
    sleeve.plan = sleeve.first_plan(days[0], capital)
    for n, day in enumerate(days):
        prev = days[n - 1] if n else None
        sleeve.open_session(day, capital + sleeve.cash - sleeve.pending)
        if n == len(days) - 1:
            sleeve.finish(day, capital + sleeve.cash - sleeve.pending)
        else:
            sleeve.close_session(
                day,
                prev,
                days[n + 1],
                equity=capital + sleeve.cash + sleeve.value(day),
                positions=_ZERO,
                free_cash=capital + sleeve.cash - sleeve.pending,
            )
    final = capital + sleeve.cash
    return {
        "final_equity_after_tax": str(final),
        "tax_paid": str(sleeve.tax_paid),
        "final_equity_pre_tax": str(final + sleeve.tax_paid),
    }


def benchmarks(
    config: OverlayConfig,
    data: EtfData,
    schedule: CostSchedule,
    capital: Decimal,
    days: Sequence[date],
) -> dict[str, Any]:
    """run.json's `overlay.benchmarks`: buy-and-hold NIFTYBEES (B1: all of `capital` at the
    first open, held, sold on the last session; pre-tax and after tax), the regime sleeve alone
    (B2: the run's switching on all of the capital, no cash buffer; the `regime` rule when the run's
    sleeve has none), and the reference TRIs' growth of `capital` (pre-tax)."""
    if not days:
        return {}
    hold = OverlayConfig(
        sleeve=NIFTY,
        cash_buffer_pct=_ZERO,
        rebalance_pct=_ONE,  # bought once, never rebalanced
        slippage_bps=config.slippage_bps,
        tax=config.tax,
        slab_rate=config.slab_rate,
    )
    switching = OverlayConfig(
        sleeve=config.sleeve if config.sleeve in (REGIME, REGIME_GOLD) else REGIME,
        band=config.band,
        gold_band=config.gold_band,
        cash_buffer_pct=_ZERO,
        rebalance_pct=config.rebalance_pct,
        credit=config.credit,
        slippage_bps=config.slippage_bps,
        tax=config.tax,
        slab_rate=config.slab_rate,
    )
    out: dict[str, Any] = {
        "buy_hold_niftybees": alone(hold, data, schedule, capital, days),
        "sleeve_alone": {
            "sleeve": switching.sleeve,
            **alone(switching, data, schedule, capital, days),
        },
    }
    for name, series in sorted(data.references.items()):
        closes = dict(series)
        first, last = closes.get(days[0]), closes.get(days[-1])
        if first and last:
            out[name] = {"final_equity_pre_tax": str(capital * last / first)}
    return out

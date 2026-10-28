"""Income tax on a backtest's trades (the idle-cash ETF overlay's tax rule, docs/decisions.md):
FIFO lots per instrument, the gains of each sale classified by the instrument's tax class and
its holding period, set off and carried forward per financial year, and the year's tax with
cess. A leaf: plain data in, Decimal out; the overlay (`overlay`) and the loop feed it.

The rules are today's, applied to every year of a run (no historical rate is modelled; a known
gap of the overlay):

- `EQUITY` (strategy stocks, NIFTYBEES): a lot held up to 12 months is short term at 20%; longer,
  long term at 12.5% on the year's long-term equity gains above the exemption (₹1.25 lakh a
  financial year, shared by every long-term equity gain of that year).
- `GOLD` (GOLDBEES): short term at the slab rate; long term (over 12 months) at 12.5%, no
  exemption.
- `DEBT` (LIQUIDBEES): every gain on a sale at the slab rate, whatever the holding period; units
  received as a dividend are income at the slab rate in the year received, their value then
  their cost.
- Losses: a short-term loss sets off any gain of its year, a long-term loss only long-term
  gains; what is left is carried forward up to 8 years (oldest first) under the same rule. The
  set-off takes the highest-taxed gains first (the slab rate ranked against 20% and 12.5%), and
  at equal rates the gains without the exemption first. Dividend income is not a capital gain:
  no loss sets it off.
- 4% cess on all of it. The year's tax is paid on 31 March (the overlay's simplification).

A holding is long term when it is sold after the same calendar day 12 months on (held more
than 12 months). Each lot's cost includes its buy charges, and a sale's proceeds are net of its
sell charges.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

__all__ = [
    "CARRY_YEARS",
    "CESS",
    "DEBT",
    "EQUITY",
    "GOLD",
    "LTCG_EXEMPTION",
    "LTCG_RATE",
    "STCG_EQUITY_RATE",
    "TAX_CLASSES",
    "Lot",
    "TaxBook",
    "TaxError",
    "YearTax",
    "fy_end",
    "fy_of",
    "long_term",
]

EQUITY = "equity"
GOLD = "gold"
DEBT = "debt"
TAX_CLASSES = (EQUITY, GOLD, DEBT)
STCG_EQUITY_RATE = Decimal("0.20")
LTCG_RATE = Decimal("0.125")
LTCG_EXEMPTION = Decimal(125_000)
CESS = Decimal("0.04")
CARRY_YEARS = 8
_ZERO = Decimal(0)
_PAISA = Decimal("0.01")

# The gain buckets of a year, by how they are taxed: short-term at the slab rate (GOLD short
# term, every DEBT sale), short-term equity at 20%, long-term without the exemption (GOLD) and
# long-term equity (the exemption applies). Dividend units are `income`, set off by no loss.
_ST_SLAB = "st_slab"
_ST_EQUITY = "st_equity"
_LT_OTHER = "lt_other"
_LT_EQUITY = "lt_equity"


class TaxError(ValueError):
    """A sale of more units than are held, or an unknown tax class."""


def fy_of(day: date) -> int:
    """The financial year `day` falls in, named by the calendar year it starts (April to
    March): 2024-03-31 is in 2023, 2024-04-01 in 2024."""
    return day.year if day.month >= 4 else day.year - 1


def fy_end(fy: int) -> date:
    """31 March closing financial year `fy`."""
    return date(fy + 1, 3, 31)


def _months_on(day: date, months: int) -> date:
    """The same calendar day `months` on, the month's last day when it has no such day."""
    total = day.month - 1 + months
    year, month = day.year + total // 12, total % 12 + 1
    for last in (31, 30, 29, 28):
        try:
            return date(year, month, min(day.day, last))
        except ValueError:
            continue
    raise AssertionError("unreachable")  # pragma: no cover - every month has a 28th


def long_term(acquired: date, sold: date) -> bool:
    """Held more than 12 months: sold after the same day 12 months on."""
    return sold > _months_on(acquired, 12)


@dataclass
class Lot:
    """Units of one buy (or one dividend credit), their cost per unit including the buy's
    charges, and the day acquired."""

    qty: int
    unit_cost: Decimal
    acquired: date


@dataclass
class _Year:
    gains: dict[str, Decimal] = field(
        default_factory=lambda: dict.fromkeys((_ST_SLAB, _ST_EQUITY, _LT_OTHER, _LT_EQUITY), _ZERO)
    )
    st_loss: Decimal = _ZERO  # positive amounts
    lt_loss: Decimal = _ZERO
    income: Decimal = _ZERO  # dividend units at their value on receipt


@dataclass(frozen=True)
class YearTax:
    """One financial year's tax: the gains after set-off by bucket, the dividend income, the
    exemption used, the tax with cess, and the losses carried out of it (short, long)."""

    fy: int
    taxable: dict[str, Decimal]
    income: Decimal
    exemption_used: Decimal
    tax: Decimal
    carried_st: Decimal
    carried_lt: Decimal


def _bucket(tax_class: str, is_long: bool) -> str:
    if tax_class == EQUITY:
        return _LT_EQUITY if is_long else _ST_EQUITY
    if tax_class == GOLD:
        return _LT_OTHER if is_long else _ST_SLAB
    if tax_class == DEBT:
        return _ST_SLAB
    raise TaxError("unknown tax class")


def _set_off(gains: dict[str, Decimal], loss: Decimal, order: tuple[str, ...]) -> Decimal:
    """Set `loss` off the `gains` buckets in `order`, in place; the loss left over."""
    for bucket in order:
        used = min(gains[bucket], loss)
        gains[bucket] -= used
        loss -= used
    return loss


# A loss sets off the highest-taxed gains first, so the set-off takes the least tax: by rate (the
# slab rate against 20% short-term equity, 12.5% long term), at equal rates the gains the
# exemption does not cover first (Python's sort is stable).
_LT_ORDER = (_LT_OTHER, _LT_EQUITY)


def _st_order(slab_rate: Decimal) -> tuple[str, ...]:
    rates = {
        _ST_SLAB: slab_rate,
        _ST_EQUITY: STCG_EQUITY_RATE,
        _LT_OTHER: LTCG_RATE,
        _LT_EQUITY: LTCG_RATE,
    }
    return tuple(sorted(rates, key=lambda bucket: -rates[bucket]))


class TaxBook:
    """The FIFO lots of every instrument and the gains, losses and income of each financial
    year, settled one year at a time (`settle`) with the losses carried forward."""

    def __init__(self, slab_rate: Decimal) -> None:
        if not (isinstance(slab_rate, Decimal) and 0 <= slab_rate <= 1):
            raise TaxError("slab rate outside [0, 1]")
        self.slab_rate = slab_rate
        self.lots: dict[str, deque[Lot]] = {}
        self.classes: dict[str, str] = {}
        self.years: dict[int, _Year] = {}
        # unused losses by the year they arose: (fy, short-term, long-term)
        self.carried: list[tuple[int, Decimal, Decimal]] = []
        self.settled: dict[int, YearTax] = {}

    def _year(self, day: date) -> _Year:
        return self.years.setdefault(fy_of(day), _Year())

    def held(self, instrument: str) -> int:
        return sum(lot.qty for lot in self.lots.get(instrument, ()))

    def buy(self, instrument: str, tax_class: str, qty: int, cost: Decimal, day: date) -> None:
        """A buy of `qty` units costing `cost` in all (value plus charges)."""
        if tax_class not in TAX_CLASSES:
            raise TaxError("unknown tax class")
        if qty <= 0:
            return
        self.classes[instrument] = tax_class
        self.lots.setdefault(instrument, deque()).append(Lot(qty, cost / qty, day))

    def income(self, day: date, amount: Decimal) -> None:
        """Income at the slab rate in `day`'s year that no loss sets off (a cash dividend)."""
        self._year(day).income += amount

    def dividend_units(self, instrument: str, qty: int, value: Decimal, day: date) -> None:
        """Units received as a dividend (LIQUIDBEES): income at the slab rate in `day`'s year,
        and a lot whose cost is their `value` on receipt."""
        if qty <= 0:
            return
        self._year(day).income += value
        self.buy(instrument, self.classes.get(instrument, DEBT), qty, value, day)

    def sell(self, instrument: str, qty: int, proceeds: Decimal, day: date) -> Decimal:
        """A sale of `qty` units for `proceeds` in all (net of charges), FIFO; its gain (a loss
        below 0), booked to `day`'s year by each lot's holding period."""
        lots = self.lots.get(instrument)
        if qty <= 0:
            return _ZERO
        if lots is None or self.held(instrument) < qty:
            raise TaxError("sale of more units than held")
        tax_class = self.classes[instrument]
        year = self._year(day)
        per_unit = proceeds / qty
        left, total = qty, _ZERO
        while left:
            lot = lots[0]
            take = min(lot.qty, left)
            gain = (per_unit - lot.unit_cost) * take
            total += gain
            is_long = long_term(lot.acquired, day)
            if gain >= 0:
                year.gains[_bucket(tax_class, is_long)] += gain
            elif is_long:
                year.lt_loss -= gain
            else:
                year.st_loss -= gain
            lot.qty -= take
            left -= take
            if not lot.qty:
                lots.popleft()
        return total

    def settle(self, fy: int) -> YearTax:
        """Year `fy`'s tax (once; a second call returns the first answer): its own losses set
        off first, then the losses carried from the 8 years before (oldest first), the
        exemption on what is left of the long-term equity gains, the rates and cess; the losses
        left over are carried forward and those older than 8 years lapse."""
        if fy in self.settled:
            return self.settled[fy]
        year = self.years.get(fy, _Year())
        gains = dict(year.gains)
        st_order = _st_order(self.slab_rate)
        st_left = _set_off(gains, year.st_loss, st_order)
        lt_left = _set_off(gains, year.lt_loss, _LT_ORDER)
        carried: list[tuple[int, Decimal, Decimal]] = []
        for origin, st, lt in sorted(self.carried):
            if fy - origin > CARRY_YEARS:
                continue  # lapsed
            st = _set_off(gains, st, st_order)
            lt = _set_off(gains, lt, _LT_ORDER)
            if st or lt:
                carried.append((origin, st, lt))
        if st_left or lt_left:
            carried.append((fy, st_left, lt_left))
        self.carried = carried
        exemption = min(gains[_LT_EQUITY], LTCG_EXEMPTION)
        slab = self.slab_rate
        base = (
            (gains[_ST_SLAB] + year.income) * slab
            + gains[_ST_EQUITY] * STCG_EQUITY_RATE
            + (gains[_LT_OTHER] + gains[_LT_EQUITY] - exemption) * LTCG_RATE
        )
        tax = (base * (1 + CESS)).quantize(_PAISA, rounding=ROUND_HALF_UP)
        found = YearTax(
            fy=fy,
            taxable=gains,
            income=year.income,
            exemption_used=exemption,
            tax=tax,
            carried_st=sum((c[1] for c in carried), _ZERO),
            carried_lt=sum((c[2] for c in carried), _ZERO),
        )
        self.settled[fy] = found
        return found

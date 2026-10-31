"""A rotation strategy's book in the day loop (`strategy.config.Rotation`, `engine.rotation`).

A rotation leg does not go through the ledger: it has no watchlist, trigger, stop or risk check,
so it takes no slot, open risk or per-trade risk of the account's caps, and its orders never
reach `evaluate_signal`. The loop holds its book here and shares the account with it: the book's
cash flows are the account's (`flow`), its holdings are marked with the strategies' positions,
its fills go into the overlay's tax book, and its buys draw on the overlay's same-day credit.

Each session, at 09:20 (never at the 09:15 open, owner 2026-10-30; the price is
`engine/fill_0920.py`'s: the 09:15 bar's close where the name's five-minute session may be read,
else the open moved by the median to 09:20): the dividends its holdings earned that ex-date, then
any sale still pending fills where it can, then, when the session before closed a rebalance
period (`rotation.is_rebalance`), the rebalance decided on that close (`plan`): every member
passing the config's universe stage that night is scored on its total-return closes, the target
list is selected with the hold buffer, and the order diff to an equal share of the sleeve's
capital (its weight of the account's equity at that close), sized on the prices at 09:20, is
traded then (`rebalance`): sales and trims first, then buys and top-ups, scaled down together
when the cash and the overlay's credit cannot pay them all. A name without a bar that day does
not trade, nor does a sale pinned at the lower band (its 09:15 bar, else the whole day) or a buy
at the upper band (its 09:15 bar's close, else the open): a sale waits for the next session it
can fill at (`_Holding.exit`), a buy is not made. A factor-pending name (CA5) is not scored or
bought that night; a holding in it is kept. At the close the loop sells a holding at the close of
its last session before a dated demerger (CA8) or its last traded session (HAT-BT-012), and at
the period's end at its mark (`finish`).

A trade is one holding from its first buy to its last sale; top-ups and trims change its size.
Its entry price is its first buy's fill. It has no stop: its `initial_stop` is 0, so its R is its
net P&L over all the shares it bought at that price (its return on cost, when it was never
topped up or trimmed), its MFE and MAE its best and worst prices over that entry price less 1,
and it carries the rank and score at entry and at exit.
`rows` is the run's `rebalances.csv`: one row per order planned, traded or not, and one per
target held unchanged.

In an overlap run (`spec.RunConventions.overlap`) the book also funds the setup sleeves beside
it: a setup entry may size on what selling every holding would net at its instant (`credit`),
and when it takes more than the free cash the book sells for the shortfall (`fund`), the weakest
holding first (`funding_order`: unranked on the last rebalance's close, then the lowest rank),
the fewest shares that cover it (`shares_to_fund`), each sale a `sell` or `trim` row with the
reason `rotation_funding` (`FUNDING`), its exit reason too when it closes the trade. The next
rebalance sizes the book on `overlap_capital`, so it does not buy back what the setups still hold
at their mark.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_FLOOR, Decimal
from functools import partial
from typing import Any, Protocol, TypeVar

import numpy as np
import polars as pl

from ..costs import BacktestFills
from ..engine import rotation
from ..engine.positions import trade_r
from ..engine.rotation import Plan
from ..engine.strategy.config import Rotation, StrategyConfig
from ..money import dividend_credit, require_exact
from .fillrules import close_lower_locked
from .report import END_OF_PERIOD, FillRow, LegRow, SignalRow, TradeRow

__all__ = [
    "FUNDING",
    "PLAYBOOK",
    "Plan",
    "Quote",
    "RotationBook",
    "TrCloses",
    "bar_ending_at",
    "funding_order",
    "funding_sale",
    "overlap_capital",
    "shares_to_fund",
]

PLAYBOOK = rotation.PLAYBOOK  # a rotation trade's playbook in the result files
HOLD = "hold"
CASH = "cash"  # a buy the cash cut down or out
FUNDING = "rotation_funding"  # a sale paying for a setup entry (an overlap run)
_ZERO = Decimal(0)
_PRICE = Decimal("0.0001")
_SCORE = Decimal("0.000001")
_FIVE = timedelta(minutes=5)
B = TypeVar("B", bound="_Started")


class _Started(Protocol):
    @property
    def start(self) -> datetime: ...


# (the price a name can trade at now, else None and why not: `no_bar`, `lower_circuit`, ...)
Quote = tuple[Decimal | None, str | None]


class TrCloses:
    """Every instrument's total-return closes (the price basis times its dividend factor, the
    series `MarketView.bars_through` reads, on one fixed scale) by session index: the ratio of
    two of them is the total return between their sessions."""

    def __init__(self, bars: pl.DataFrame, days: Sequence[date]) -> None:
        div = pl.col("div_factor") if "div_factor" in bars.columns else pl.lit(1.0)
        index = pl.DataFrame({"date": list(days), "n": list(range(len(days)))})
        frame = (
            bars.select("instrument_id", "date", tr=pl.col("close") * div)
            .filter(pl.col("tr") > 0)
            .join(index, on="date", how="inner")
            .sort("instrument_id", "n")
        )
        self.series: dict[str, tuple[np.ndarray, np.ndarray]] = {
            str(key[0]): (part["n"].to_numpy(), part["tr"].cast(pl.Float64).to_numpy())
            for key, part in frame.partition_by("instrument_id", as_dict=True).items()
        }

    def window(self, names: Sequence[str], end: int, rows: int) -> np.ndarray:
        """The closes of `names` (columns) over the `rows` sessions ending at index `end`, NaN
        where a name has no bar."""
        out = np.full((rows, len(names)), np.nan)
        first = end - rows + 1
        empty = (np.empty(0, dtype=np.int64), np.empty(0))
        for column, name in enumerate(names):
            sessions, closes = self.series.get(name, empty)
            lo, hi = np.searchsorted(sessions, [first, end + 1])
            out[sessions[lo:hi] - first, column] = closes[lo:hi]
        return out


@dataclass
class _Holding:
    """One open rotation trade: its shares now, its buys and sales so far, its dividends, the
    extremes since its first buy (MFE and MAE), the rank and score it entered on and its sales
    (`legs`); `exit` the reason, rank and score of a sale still pending."""

    instrument_id: str
    entry_date: date
    entry_rank: int | None
    entry_score: Decimal | None
    high: Decimal
    low: Decimal
    qty: int = 0
    first: Decimal = _ZERO  # the first buy's fill price: the trade's entry price
    bought: int = 0
    cost: Decimal = _ZERO
    proceeds: Decimal = _ZERO
    fees: Decimal = _ZERO
    dividends: Decimal = _ZERO
    legs: list[LegRow] = field(default_factory=list)
    exit: tuple[str, int | None, Decimal | None] | None = None

    @property
    def avg(self) -> Decimal:
        """The mean price its shares were bought at, to the store's 4 places."""
        return (self.cost / self.bought).quantize(_PRICE) if self.bought else _ZERO


@dataclass(frozen=True)
class Market:
    """What a rebalance reads of the day from the loop: the price at 09:20 of each name with a
    bar (`opens`, which sizes the orders), whether a name can be sold or bought at it now
    (`sell`, `buy`), the cash the account can spend now (`cash`: free cash and the overlay's
    credit), each name's symbol, and a new trade's signal row (`entry`)."""

    opens: Mapping[str, Decimal]
    sell: Callable[[str], Quote]
    buy: Callable[[str], Quote]
    cash: Callable[[], Decimal]
    symbol: Callable[[str], str]
    entry: Callable[[str, int, Decimal, int | None, Decimal | None], SignalRow]


def funding_order(held: Iterable[str], ranks: Mapping[str, int]) -> list[str]:
    """The order an overlap run sells `held` names in to fund a setup entry, the weakest first:
    those without a rank on the last rebalance's close (`ranks`), then the lowest rank (the
    largest number) first; ties by name."""
    return sorted(held, key=lambda name: (name in ranks, -ranks.get(name, 0), name))


def bar_ending_at(bars: Iterable[B], when: datetime) -> B | None:
    """The five-minute bar of `bars` (each with a `start`) that ended in the five minutes up to
    `when`: the last a sale at `when` may be priced from, never one ending after it."""
    return next((b for b in bars if b.start + _FIVE <= when < b.start + 2 * _FIVE), None)


def funding_sale(
    close: Decimal, low: Decimal, prev_close: Decimal | None, tick: Decimal | None
) -> Quote:
    """A funding sale priced at the close of the bar it reads (a five-minute bar, or the day's
    at a close entry): blocked `lower_circuit` when that bar closed locked at the lower band
    (`close_lower_locked`), as a rotation sale at 09:20 is; an unknown state is not locked."""
    if close_lower_locked(close, low, prev_close, tick) is True:
        return None, "lower_circuit"
    return close, None


def shares_to_fund(need: Decimal, held: int, proceeds: Callable[[int], Decimal]) -> int:
    """The fewest of `held` shares whose net `proceeds` (shares -> cash after charges, rising
    with the shares) cover `need`; all of them when even those do not, 0 for no need."""
    if need <= 0 or held <= 0:
        return 0
    if proceeds(held) < need:
        return held
    lo, hi = 1, held
    while lo < hi:
        mid = (lo + hi) // 2
        if proceeds(mid) >= need:
            hi = mid
        else:
            lo = mid + 1
    return lo


def overlap_capital(
    weight: Decimal, equity: Decimal, funded: Iterable[tuple[Decimal, int, Decimal]]
) -> Decimal:
    """A rotation's capital at an overlap run's rebalance: `weight` of `equity` less the setup
    positions its sales funded at their mark, each funded position's (share of its entry cost
    the sales covered, shares still open, mark) counting that share of its marked value, so a
    loser's funding is not left idle and a winner's does not over-size the book; never below
    0."""
    held = sum((share * left * mark for share, left, mark in funded), _ZERO)
    return max(_ZERO, weight * equity - held)


class RotationBook:
    """One rotation leg's holdings, cash flow, fills, trades and rebalance rows (module
    docstring)."""

    def __init__(
        self,
        config: StrategyConfig,
        strategy_version_id: uuid.UUID,
        risk_policy_version_id: uuid.UUID,
        fills: BacktestFills,
        prefix: str = "",
        sold: set[tuple[date, str]] | None = None,
    ) -> None:
        if config.rotation is None:
            raise ValueError("not_a_rotation")
        self.config = config
        self.rules: Rotation = config.rotation
        self.version = strategy_version_id
        self.policy_version = risk_policy_version_id
        self.fills = fills
        self.playbook = prefix + PLAYBOOK
        self.holdings: dict[str, _Holding] = {}
        self.flow = _ZERO  # the account's cash the book has taken (-) or given back (+)
        self.fill_rows: list[FillRow] = []
        self.trades: list[TradeRow] = []
        self.signals: list[SignalRow] = []
        self.rows: list[dict[str, Any]] = []
        self.ranks: dict[str, int] = {}  # the last rebalance's ranking, which `fund` reads
        # one DP charge per name per sell day: the account's set, shared with its other books
        self._sold: set[tuple[date, str]] = set() if sold is None else sold

    # --- what the loop reads ---------------------------------------------------------------

    def value(self, mark: Callable[[str, Decimal], Decimal]) -> Decimal:
        """The holdings' value at `mark` (name, fallback price) -> price."""
        return sum((h.qty * mark(h.instrument_id, h.avg) for h in self.holdings.values()), _ZERO)

    def fills_on(self, day: date) -> list[FillRow]:
        return [f for f in self.fill_rows if f.day == day]

    # --- the decision ----------------------------------------------------------------------

    def plan(
        self,
        night: date,
        passers: Sequence[str],
        closes: np.ndarray,
        *,
        pending: frozenset[str],
        gate_on: bool,
        can_buy: Callable[[str], bool],
    ) -> Plan:
        """The rebalance decided on `night`'s close: `passers` (the members passing the
        universe stage; `closes` their total-return window, a column each) less the
        factor-pending names, scored and ranked, and the target list (`rotation.select`), a
        held factor-pending name kept without a rank. Under the market filter with the gate off
        the target is empty."""
        return rotation.plan(
            night, passers, closes, self.rules, self.holdings,
            pending=pending, gate_on=gate_on, can_buy=can_buy,
        )  # fmt: skip

    # --- the session -----------------------------------------------------------------------

    def dividends(self, per_share: Callable[[str], Decimal]) -> Decimal:
        """Credit each holding's dividends going ex today (`per_share`); the total."""
        total = _ZERO
        for holding in self.holdings.values():
            cash = dividend_credit(holding.qty, per_share(holding.instrument_id))
            if cash:
                holding.dividends += cash
                self.flow += cash
                total += cash
        return total

    def see(self, bar: Callable[[str], tuple[Decimal, Decimal] | None]) -> None:
        """Widen each holding's extremes with the day's (high, low) (for MFE and MAE)."""
        for name, holding in self.holdings.items():
            found = bar(name)
            if found is not None:
                holding.high = max(holding.high, found[0])
                holding.low = min(holding.low, found[1])

    def sell_pending(
        self, day: date, sell: Callable[[str], Quote], symbol: Callable[[str], str]
    ) -> None:
        """Sell each holding whose sale is pending at `day`'s 09:20, where it can now."""
        for name in sorted(self.holdings):
            holding = self.holdings[name]
            if holding.exit is None:
                continue
            price, _ = sell(name)
            if price is None:
                continue
            reason, rank, score = holding.exit
            qty = holding.qty
            self._sell(holding, qty, price, day, reason, rank, score)
            self._row(day, None, name, symbol(name), "sell", reason, rank, score, qty, price)

    def rebalance(self, day: date, plan: Plan, capital: Decimal, market: Market) -> None:
        """Trade `plan` at `day`'s 09:20 on `capital`, the sleeve's (module docstring)."""
        rank = {name: n for n, (name, _) in enumerate(plan.ranking, start=1)}
        self.ranks = rank
        score = {
            name: require_exact(value, "score", floats=True).quantize(_SCORE)
            for name, value in plan.ranking
        }
        orders = rotation.orders(
            plan, {name: h.qty for name, h in self.holdings.items()}, market.opens, capital,
            self.rules,
        )  # fmt: skip
        each = (capital / self.rules.top_n).quantize(Decimal("0.01"))
        for name in plan.target:
            if name in self.holdings:
                self.holdings[name].exit = None  # back in the target: a pending sale is off

        def row(name: str, action: str, why: str | None, qty: int, price: Decimal | None) -> None:
            self._row(
                day, plan.night, name, market.symbol(name), action, why,
                rank.get(name), score.get(name), qty, price, each,
            )  # fmt: skip

        buys: list[rotation.Order] = []
        for order in orders:
            name = order.instrument_id
            if order.action == rotation.SELL:
                why = rotation.sale_reason(plan, name)
                holding = self.holdings[name]
                price, blocked = market.sell(name)
                if price is None:  # waits for the next session it can sell at
                    holding.exit = (why, rank.get(name), score.get(name))
                    row(name, "sell", blocked, 0, None)
                    continue
                self._sell(holding, order.qty, price, day, why, rank.get(name), score.get(name))
                row(name, "sell", why, order.qty, price)
            elif order.action == rotation.TRIM:
                price, blocked = market.sell(name)
                if price is None:
                    row(name, "trim", blocked, 0, None)
                    continue
                self._sell(self.holdings[name], order.qty, price, day, rotation.TRIMMED, None, None)
                row(name, "trim", None, order.qty, price)
            else:
                buys.append(order)
        traded = {o.instrument_id for o in orders}
        for name in plan.target:
            if name in self.holdings and name not in traded:
                row(name, HOLD, None, 0, market.opens.get(name))
        self._buy_all(day, buys, market, rank, score, row)

    def _buy_all(
        self,
        day: date,
        buys: Sequence[rotation.Order],
        market: Market,
        rank: Mapping[str, int],
        score: Mapping[str, Decimal],
        row: Callable[[str, str, str | None, int, Decimal | None], None],
    ) -> None:
        """The buys and top-ups: each scaled down by the share of their outlay the cash covers
        when it cannot pay them all (whole shares, rounded down), then cut to what the cash
        left can pay. A name that cannot be bought at 09:20 is skipped."""
        priced: list[tuple[rotation.Order, Decimal]] = []
        for order in buys:
            price, blocked = market.buy(order.instrument_id)
            if price is None:
                row(order.instrument_id, order.action, blocked, 0, None)
            else:
                priced.append((order, price))
        total = sum((self.fills.outlay(o.qty, p, day) for o, p in priced), _ZERO)
        share = rotation.buy_share(total, market.cash())
        for order, price in priced:
            name = order.instrument_id
            qty = int((order.qty * share).to_integral_value(ROUND_FLOOR))
            qty = min(qty, self.fills.affordable(market.cash(), price, day))
            if qty <= 0:
                row(name, order.action, CASH, 0, price)
                continue
            holding = self.holdings.get(name)
            if holding is None:
                holding = _Holding(name, day, rank.get(name), score.get(name), price, price)
                self.holdings[name] = holding
                self.signals.append(market.entry(name, qty, price, rank.get(name), score.get(name)))
            self._buy(holding, qty, price, day)
            row(name, order.action, None if qty == order.qty else CASH, qty, price)

    def credit(self, day: date, quote: Callable[[str], Quote]) -> Decimal:
        """What selling every holding at `quote` would net now (an overlap run's setup entry
        sizes on it); a name `quote` blocks counts nothing."""
        total = _ZERO
        for name, holding in self.holdings.items():
            price, _ = quote(name)
            if price is not None:
                total += self._proceeds(name, holding.qty, price=price, day=day)
        return total

    def fund(
        self,
        day: date,
        shortfall: Decimal,
        quote: Callable[[str], Quote],
        symbol: Callable[[str], str],
    ) -> Decimal:
        """Sell holdings at `quote` until their net proceeds cover `shortfall`, a setup entry's
        in an overlap run (module docstring); what they netted."""
        raised = _ZERO
        for name in funding_order(self.holdings, self.ranks):
            if raised >= shortfall:
                break
            price, _ = quote(name)
            if price is None:
                continue
            holding = self.holdings[name]
            held = holding.qty
            qty = shares_to_fund(
                shortfall - raised, held, partial(self._proceeds, name, price=price, day=day)
            )
            before = self.flow
            self._sell(holding, qty, price, day, FUNDING, None, None)
            raised += self.flow - before
            action = "sell" if qty == held else "trim"
            self._row(
                day, None, name, symbol(name), action, FUNDING, self.ranks.get(name), None, qty,
                price,
            )  # fmt: skip
        return raised

    def close_out(
        self, name: str, day: date, price: Decimal, reason: str, symbol: Callable[[str], str]
    ) -> None:
        """Sell the whole holding in `name` at `price` (a close) for `reason`."""
        holding = self.holdings.get(name)
        if holding is None:
            return
        qty = holding.qty
        self._sell(holding, qty, price, day, reason, None, None)
        self._row(day, None, name, symbol(name), "sell", reason, None, None, qty, price)

    def finish(self, day: date, mark: Callable[[str, Decimal], Decimal]) -> None:
        """Close every holding at its mark on the period's last session (`END_OF_PERIOD`)."""
        for name in sorted(self.holdings):
            holding = self.holdings[name]
            price = mark(name, holding.avg)
            holding.low = min(holding.low, price)
            self._sell(holding, holding.qty, price, day, END_OF_PERIOD, None, None)

    # --- fills and trades ------------------------------------------------------------------

    def _proceeds(self, name: str, qty: int, *, price: Decimal, day: date) -> Decimal:
        """What selling `qty` of `name` at `price` would net, as `_sell` fills it."""
        sold_today = (day, name) in self._sold
        filled, fees = self.fills.fill("sell", qty, price, day, sold_today=sold_today)
        return filled * qty - fees

    def _buy(self, holding: _Holding, qty: int, price: Decimal, day: date) -> None:
        filled, fees = self.fills.fill("buy", qty, price, day)
        if not holding.bought:
            holding.first = filled
        holding.qty += qty
        holding.bought += qty
        holding.cost += filled * qty
        holding.fees += fees
        self.flow -= filled * qty + fees
        self.fill_rows.append(
            FillRow(day, holding.instrument_id, "entry", "buy", qty, filled, fees)
        )

    def _sell(
        self,
        holding: _Holding,
        qty: int,
        price: Decimal,
        day: date,
        reason: str,
        rank: int | None,
        score: Decimal | None,
    ) -> None:
        key = (day, holding.instrument_id)
        filled, fees = self.fills.fill("sell", qty, price, day, sold_today=key in self._sold)
        self._sold.add(key)
        holding.qty -= qty
        holding.proceeds += filled * qty
        holding.fees += fees
        self.flow += filled * qty - fees
        holding.legs.append(LegRow(day, qty, filled, fees, reason))
        purpose = "exit" if holding.qty == 0 else "trim"
        self.fill_rows.append(
            FillRow(day, holding.instrument_id, purpose, "sell", qty, filled, fees)
        )
        if holding.qty == 0:
            self._close(holding, day, reason, rank, score)

    def _close(
        self, holding: _Holding, day: date, reason: str, rank: int | None, score: Decimal | None
    ) -> None:
        del self.holdings[holding.instrument_id]
        entry = holding.first
        pnl = holding.proceeds - holding.cost - holding.fees + holding.dividends
        self.trades.append(
            TradeRow(
                entry_date=holding.entry_date,
                exit_date=day,
                playbook=self.playbook,
                instrument_id=holding.instrument_id,
                qty=holding.bought,
                entry_price=entry,
                initial_stop=_ZERO,
                exit_reason=reason,
                realized_pnl=pnl,
                fees=holding.fees,
                r_multiple=trade_r(pnl, entry, _ZERO, holding.bought),
                mfe_r=_share(max(holding.high, entry) - entry, entry),
                mae_r=_share(min(holding.low, entry) - entry, entry),
                holding_days=(day - holding.entry_date).days,
                strategy_version_id=self.version,
                risk_policy_version_id=self.policy_version,
                score=holding.entry_score,
                dividends=holding.dividends,
                exit_price=holding.proceeds / holding.bought,
                final_stop=_ZERO,
                legs=tuple(holding.legs),
                entry_rank=holding.entry_rank,
                exit_rank=rank,
                exit_score=score,
            )
        )

    def _row(
        self,
        day: date,
        night: date | None,
        name: str,
        symbol: str,
        action: str,
        reason: str | None,
        rank: int | None,
        score: Decimal | None,
        qty: int,
        price: Decimal | None,
        target_value: Decimal | None = None,
    ) -> None:
        self.rows.append(
            {
                "date": day,
                "decision_date": night,
                "strategy": self.config.bot,
                "instrument_id": name,
                "symbol": symbol,
                "action": action,
                "reason": reason,
                "rank": rank,
                "score": score,
                "qty": qty,
                "price": price,
                "value": None if price is None else price * qty,
                "target_value": target_value,
            }
        )


def _share(move: Decimal, entry: Decimal) -> Decimal:
    """`move` over `entry`: a trade without a stop has its excursions as shares of its cost."""
    return (move / entry).quantize(_PRICE) if entry > 0 else _ZERO

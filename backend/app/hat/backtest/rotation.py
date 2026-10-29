"""A rotation strategy's book in the day loop (`strategy.config.Rotation`, `engine.rotation`).

A rotation leg does not go through the ledger: it has no watchlist, trigger, stop or risk check,
so it takes no slot, open risk or per-trade risk of the account's caps, and its orders never
reach `evaluate_signal`. The loop holds its book here and shares the account with it: the book's
cash flows are the account's (`flow`), its holdings are marked with the strategies' positions,
its fills go into the overlay's tax book, and its buys draw on the overlay's same-day credit.

Each session, at the open: the dividends its holdings earned that ex-date, then any sale still
pending fills at the open where it can, then, when the session before closed a rebalance period
(`rotation.is_rebalance`), the rebalance decided on that close (`plan`): every member passing the
config's universe stage that night is scored on its total-return closes, the target list is
selected with the hold buffer, and the order diff to an equal share of the sleeve's capital (its
weight of the account's equity at that close) is traded at the day's open (`rebalance`): sales and
trims first, then buys and top-ups, scaled down together when the cash and the overlay's credit
cannot pay them all. A name without a bar that day does not trade, nor does a sale pinned at the
lower band all day or a buy at the upper band at the open: a sale waits for the next open it can
fill at (`_Holding.exit`), a buy is not made. A factor-pending name (CA5) is not scored or bought
that night; a holding in it is kept. At the close the loop sells a holding at the close of its
last session before a dated demerger (CA8) or its last traded session (HAT-BT-012), and at the
period's end at its mark (`finish`).

A trade is one holding from its first buy to its last sale; top-ups and trims change its size.
Its entry price is its first buy's fill. It has no stop: its `initial_stop` is 0, so its R is its
net P&L over all the shares it bought at that price (its return on cost, when it was never
topped up or trimmed), its MFE and MAE its best and worst prices over that entry price less 1,
and it carries the rank and score at entry and at exit.
`rows` is the run's `rebalances.csv`: one row per order planned, traded or not, and one per
target held unchanged.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_FLOOR, Decimal
from typing import Any

import numpy as np
import polars as pl

from ..costs import BacktestFills
from ..engine import rotation
from ..engine.positions import trade_r
from ..engine.strategy.config import Rotation, StrategyConfig
from ..money import dividend_credit, require_exact
from .report import END_OF_PERIOD, FillRow, LegRow, SignalRow, TradeRow

__all__ = [
    "PLAYBOOK",
    "Plan",
    "Quote",
    "RotationBook",
    "TrCloses",
]

PLAYBOOK = "rotation"  # a rotation trade's playbook in the result files
OUT_OF_BUFFER = "rotation_out"  # ranked below the buffer
NOT_ELIGIBLE = "rotation_not_eligible"  # failed the universe stage, or not scored
MARKET_FILTER = "rotation_market_filter"  # the regime gate off on the rebalance close
TRIMMED = "rotation_trim"
HOLD = "hold"
CASH = "cash"  # a buy the cash cut down or out
_ZERO = Decimal(0)
_PRICE = Decimal("0.0001")
_SCORE = Decimal("0.000001")

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
class Plan:
    """A rebalance decided on `night`: the eligible names best first with their scores, the
    target list, and whether the market filter emptied it."""

    night: date
    ranking: tuple[tuple[str, float], ...]
    target: tuple[str, ...]
    filtered: bool = False


@dataclass(frozen=True)
class Market:
    """What a rebalance reads of the day from the loop: the open of each name with a bar
    (`opens`, which sizes the orders), whether a name can be sold or bought at it now
    (`sell`, `buy`), the cash the account can spend now (`cash`: free cash and the overlay's
    credit), each name's symbol, and a new trade's signal row (`entry`)."""

    opens: Mapping[str, Decimal]
    sell: Callable[[str], Quote]
    buy: Callable[[str], Quote]
    cash: Callable[[], Decimal]
    symbol: Callable[[str], str]
    entry: Callable[[str, int, Decimal, int | None, Decimal | None], SignalRow]


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
        columns = [n for n, name in enumerate(passers) if name not in pending]
        names = [passers[n] for n in columns]
        values = rotation.scores(closes[:, columns], self.rules) if names else np.empty(0)
        ranking = tuple(rotation.ranked(names, values))
        if self.rules.market_filter and not gate_on:
            return Plan(night, ranking, (), filtered=True)
        target = rotation.select(
            [name for name, _ in ranking],
            self.holdings,
            self.rules.top_n,
            self.rules.buffer_rank,
            can_buy,
            kept=[h for h in self.holdings if h in pending],
        )
        return Plan(night, ranking, tuple(target))

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
        """Sell each holding whose sale is pending at `day`'s open, where it can now."""
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
        """Trade `plan` at `day`'s open on `capital`, the sleeve's (module docstring)."""
        rank = {name: n for n, (name, _) in enumerate(plan.ranking, start=1)}
        score = {
            name: require_exact(value, "score", floats=True).quantize(_SCORE)
            for name, value in plan.ranking
        }
        orders = rotation.order_diff(
            plan.target,
            {name: h.qty for name, h in self.holdings.items()},
            market.opens,
            capital,
            self.rules.top_n,
            require_exact(self.rules.min_change_pct, "min_change_pct", floats=True),
            reweight=self.rules.reweight,
        )
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
                why = (
                    MARKET_FILTER
                    if plan.filtered
                    else OUT_OF_BUFFER
                    if name in rank
                    else NOT_ELIGIBLE
                )
                holding = self.holdings[name]
                price, blocked = market.sell(name)
                if price is None:  # waits for the next open it can sell at
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
                self._sell(self.holdings[name], order.qty, price, day, TRIMMED, None, None)
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
        left can pay. A name that cannot be bought at the open is skipped."""
        priced: list[tuple[rotation.Order, Decimal]] = []
        for order in buys:
            price, blocked = market.buy(order.instrument_id)
            if price is None:
                row(order.instrument_id, order.action, blocked, 0, None)
            else:
                priced.append((order, price))
        total = sum((self.fills.outlay(o.qty, p, day) for o, p in priced), _ZERO)
        available = market.cash()
        share = min(Decimal(1), available / total) if total > 0 and available > 0 else _ZERO
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

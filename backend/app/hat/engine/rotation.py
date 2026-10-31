"""The rotation strategy's rules (`strategy.config.Rotation`), as pure functions of plain data so
the backtest and a live sleeve read the same ones: the rebalance calendar, the cross-sectional
score, the selection with its hold buffer and the order diff to equal target weights.

A rebalance is decided on the close of its session (`is_rebalance`) and traded at the next
session's open. The score reads each name's total-return closes over a window ending on the
decision session, forward-filled over at most `FFILL_SESSIONS` sessions without a bar
(`scores`); `riskadj` is a cross-sectional z-score (`zscores`) over the names scored that night,
so a name's score depends on the others'. `select` keeps a holding while it ranks within the
buffer and fills the rest best first; `order_diff` turns the target list into sells, trims, buys
and top-ups at whole shares, skipping a change under the threshold.

`ranking`, `targets` and `plan` compose them as both books decide: the backtest's
(`backtest/rotation.py`) on the decision close, and live's (`engine/rotation_live.py`), which
ranks on that close and selects at the next open, where it knows what it can buy. `orders`,
`sale_reason` and `buy_share` are the rebalance's own rules, shared the same way, and
`lot_sales` splits a sale over a live holding's lots (a top-up is a lot of its own).

Two optional rules, off unless the config sets them: `max_per_industry` caps the targets per
industry in `select` (an unknown industry is not capped by default, `unknown_industry: free`: in
the point-in-time store it marks mostly names that later left the index, so one capped bucket,
`bucket`, tilts the selection towards the survivors; either choice is a bias, `decisions.md`),
and `scale_down` (`scale_down_fires`) trims the book to a fraction mid-period
(`scale_down_trims`) or sizes a rebalance on that fraction of the capital (`capital_share`).

Five more, off unless set (`strategy.config.Rotation`): the relative liquidity floor
(`median_traded_values`, `liquidity_tiers`: the names ranked are the hold tier's, a name below
the entry tier is kept but never bought), the skip of `riskadj`'s last sessions
(`riskadj_parts`), inverse-beta weights (`betas`, `target_weights`, read by `order_diff`), the
staggered rebalance (`tranche_due`, `sessions_since_close`) and the refusal of an order at or
within one tick of its price band (`near_limit`).

Two more, off unless set: `industry_relative` ranks each name on its score less the median score
of its industry among the names scored that night (`industry_relative`, in `ranking`; a name
whose industry is unknown keeps its raw score, which sits on another scale, so it is a bias of
its own, `decisions.md`), and `wide_book` widens one rebalance's `top_n` and `buffer_rank` while
its breadth checks fire on the decision close (`wide_fires`, `widened`).

Every rotation order trades at 09:20, never at the open: its price is `engine/fill_0920.py`'s.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import TypeVar

import numpy as np
import polars as pl

from ..money import require_exact
from ..risk.decision import ROTATION_PLAYBOOK
from .regime_gate import RULES
from .strategy.config import (
    InverseBeta,
    Liquidity,
    RegimeRule,
    Rotation,
    ScaleDown,
    Tranches,
    WideBook,
    wide_buffer_rank,
)
from .trigger import DailyInputs, lower_circuit_price, upper_circuit_price

__all__ = [
    "BUY",
    "MARKET_FILTER",
    "NOT_ELIGIBLE",
    "OUT_OF_BUFFER",
    "PLAYBOOK",
    "SCALED_DOWN",
    "SELL",
    "TOP_UP",
    "TRIM",
    "TRIMMED",
    "NEAR_LOWER_LIMIT",
    "NEAR_UPPER_LIMIT",
    "UNKNOWN_INDUSTRY",
    "Order",
    "Plan",
    "betas",
    "betas_by_name",
    "book_equity",
    "buy_share",
    "capital_share",
    "checks_fire",
    "industry_relative",
    "is_rebalance",
    "liquidity_tiers",
    "lot_sales",
    "median_traded_values",
    "near_limit",
    "order_diff",
    "orders",
    "plan",
    "ranked",
    "ranking",
    "riskadj_parts",
    "sale_reason",
    "scale_down_fires",
    "scale_down_trims",
    "scores",
    "select",
    "selection_depth",
    "sessions_since_close",
    "target_weights",
    "targets",
    "tranche_due",
    "wide_fires",
    "widened",
    "window_rows",
    "zscores",
]

FFILL_SESSIONS = 10  # a close carries over at most this many sessions without a bar
SESSIONS_PER_YEAR = 252  # annualises the volatility
SELL, TRIM, BUY, TOP_UP = "sell", "trim", "buy", "top_up"
PLAYBOOK = ROTATION_PLAYBOOK  # a rotation trade's playbook: its signals live, its trades in a run
OUT_OF_BUFFER = "rotation_out"  # a sale: ranked below the buffer
NOT_ELIGIBLE = "rotation_not_eligible"  # a sale: failed the universe stage, or not scored
MARKET_FILTER = "rotation_market_filter"  # a sale: the regime gate off on the rebalance close
TRIMMED = "rotation_trim"
SCALED_DOWN = "rotation_scale_down"  # a trim: the scale-down fired on the close before
UNKNOWN_INDUSTRY = "unknown"  # a name without a classification in force (never capped)
NEAR_UPPER_LIMIT = "near_upper_limit"  # a buy refused at or within a tick of the upper band
NEAR_LOWER_LIMIT = "near_lower_limit"  # a sale held at or within a tick of the lower band
MIN_BETA_RETURNS = 20  # fewer paired daily returns than this: the name's beta is 1
NSE_BAND_PCTS = (Decimal(2), Decimal(5), Decimal(10), Decimal(20))  # a band unknown: candidates
BETA_PLACES = Decimal("0.0001")  # a beta's places (`inverse_beta`), as live records it
_HUNDRED = Decimal(100)
K = TypeVar("K")  # a lot's key


def is_rebalance(night: date, next_session: date, cadence: str) -> bool:
    """Whether `night`, the session before `next_session`, closes a rebalance period: the last
    session of its month (`month_end`), also the last of its first half (`fortnight`: on or
    before the 15th, the next after it), or the last of its ISO week (`weekly`)."""
    month_end = (night.year, night.month) != (next_session.year, next_session.month)
    if cadence == "month_end":
        return month_end
    if cadence == "fortnight":
        return month_end or night.day <= 15 < next_session.day
    if cadence == "weekly":
        return night.isocalendar()[:2] != next_session.isocalendar()[:2]
    raise ValueError("unknown_rebalance")


def window_rows(rotation: Rotation) -> int:
    """The rows of closes `scores` reads, ending on the decision session: the longest lookback
    and the rows a close may be carried over from before it."""
    return max(rotation.long_sessions, rotation.vol_sessions) + 1 + FFILL_SESSIONS


def _ffill(closes: np.ndarray, limit: int) -> np.ndarray:
    """`closes` (rows sessions, columns names; NaN without a bar) with each NaN replaced by the
    last close at most `limit` rows before it."""
    out = closes.copy()
    last = np.full(closes.shape[1], np.nan)
    gap = np.zeros(closes.shape[1], dtype=np.int64)
    for row in range(closes.shape[0]):
        seen = ~np.isnan(closes[row])
        last = np.where(seen, closes[row], last)
        gap = np.where(seen, 0, gap + 1)
        out[row] = np.where(seen, closes[row], np.where(gap <= limit, last, np.nan))
    return out


def _single(closes: np.ndarray, rotation: Rotation) -> np.ndarray:
    """The score of a one-ratio rule (`r6`, `r12_1`, `hi52`) per name, NaN where unscored."""
    filled, ok = _prepared(closes, rotation)
    last = filled[-1]
    long_back = filled[-1 - rotation.long_sessions]
    with np.errstate(divide="ignore", invalid="ignore"):
        if rotation.score == "r6":
            found = last / filled[-1 - rotation.short_sessions] - 1
        elif rotation.score == "r12_1":
            found = filled[-1 - rotation.skip_sessions] / long_back - 1
        else:  # hi52: the close over its high, to 1%, ties broken by the 12-1 return
            high = np.nanmax(_tail(filled, rotation.long_sessions), axis=0)
            r12_1 = filled[-1 - rotation.skip_sessions] / long_back - 1
            found = np.round(last / high, 2) + 1e-5 * np.clip(r12_1, -1, 10)
    return np.where(ok & np.isfinite(found), found, np.nan)


def riskadj_parts(closes: np.ndarray, rotation: Rotation) -> tuple[np.ndarray, np.ndarray]:
    """(short return / volatility, long return / volatility) per name, NaN where unscored: the
    returns over `short_sessions` and `long_sessions`, ending on the decision close (with
    `score_skip`, `skip_sessions` before it; the lookbacks still start counted from it), the
    volatility the annualised sample deviation of the last `vol_sessions` daily log returns,
    ending on the decision close either way."""
    filled, ok = _prepared(closes, rotation)
    with np.errstate(divide="ignore", invalid="ignore"):
        # `score_skip`: both returns end `skip_sessions` before the decision close (6-1, 12-1)
        last = filled[-1 - (rotation.skip_sessions if rotation.score_skip else 0)]
        short = last / filled[-1 - rotation.short_sessions] - 1
        long_ = last / filled[-1 - rotation.long_sessions] - 1
        returns = np.diff(np.log(_tail(filled, rotation.vol_sessions + 1)), axis=0)
        counts = np.sum(~np.isnan(returns), axis=0)
        vol = np.full(closes.shape[1], np.nan)
        enough = counts >= 2
        if enough.any():
            vol[enough] = np.nanstd(returns[:, enough], axis=0, ddof=1) * math.sqrt(
                SESSIONS_PER_YEAR
            )
        good = ok & np.isfinite(vol) & (vol > 0)
        return (
            np.where(good & np.isfinite(short), short / vol, np.nan),
            np.where(good & np.isfinite(long_), long_ / vol, np.nan),
        )


def _tail(rows: np.ndarray, count: int) -> np.ndarray:
    return rows[max(0, rows.shape[0] - count) :]


def _prepared(closes: np.ndarray, rotation: Rotation) -> tuple[np.ndarray, np.ndarray]:
    """(the closes forward-filled, which names have `min_bars` bars in the long window)."""
    if closes.shape[0] < window_rows(rotation):
        raise ValueError("rotation_window_short")
    bars = np.sum(~np.isnan(_tail(closes, rotation.long_sessions)), axis=0)
    return _ffill(closes, FFILL_SESSIONS), bars >= rotation.min_bars


def zscores(values: np.ndarray) -> np.ndarray:
    """Each value's distance from the mean of the finite values in sample deviations (NaN for
    a NaN value, or all NaN with fewer than two values or no spread)."""
    finite = values[np.isfinite(values)]
    if finite.size < 2:
        return np.full(values.shape, np.nan)
    spread = float(np.std(finite, ddof=1))
    if spread == 0:
        return np.full(values.shape, np.nan)
    return (values - float(np.mean(finite))) / spread


def scores(closes: np.ndarray, rotation: Rotation) -> np.ndarray:
    """The night's score of each column of `closes` (NaN: not ranked), the total-return closes
    of the last `window_rows` sessions (NaN without a bar), the decision session last: `riskadj`
    the mean of the z-scores of its two ratios across the names scored that night (the columns
    are the night's eligible names), the others their one ratio. A name needs `min_bars` bars
    among the last `long_sessions` sessions."""
    if rotation.score != "riskadj":
        return _single(closes, rotation)
    short, long_ = riskadj_parts(closes, rotation)
    return 0.5 * zscores(short) + 0.5 * zscores(long_)


def ranked(names: Sequence[str], values: np.ndarray) -> list[tuple[str, float]]:
    """The scored names, best first (ties by name), with their scores; a NaN is left out."""
    found = [(n, float(v)) for n, v in zip(names, values, strict=True) if math.isfinite(v)]
    return sorted(found, key=lambda pair: (-pair[1], pair[0]))


def select(
    ranking: Sequence[str],
    held: Collection[str],
    top_n: int,
    buffer_rank: int,
    can_buy: Callable[[str], bool],
    kept: Sequence[str] = (),
    *,
    max_per_industry: int | None = None,
    industry: Callable[[str], str] | None = None,
    bucket_unknown: bool = False,
) -> list[str]:
    """The target names: `kept` first (held names kept without a rank), then each holding
    ranked within `buffer_rank` (1 the best of `ranking`), best first, then the best names
    `ranking` holds that are not targets yet and `can_buy`, up to `top_n` in all. A holding
    outside the buffer, or not ranked, is not a target. With `max_per_industry` a holding or a
    new name whose `industry` already has that many targets is passed over for the next one (a
    held name passed over is sold); a kept name always stays and counts, and an unknown industry
    (`UNKNOWN_INDUSTRY`) is never capped unless `bucket_unknown` counts every unknown name as one
    industry."""
    target: list[str] = []
    counts: dict[str, int] = {}

    def room(name: str) -> bool:
        if max_per_industry is None or industry is None:
            return True
        key = _industry_key(industry(name), bucket_unknown)
        return key is None or counts.get(key, 0) < max_per_industry

    def take(name: str) -> None:
        target.append(name)
        if max_per_industry is not None and industry is not None:
            key = _industry_key(industry(name), bucket_unknown)
            if key is not None:
                counts[key] = counts.get(key, 0) + 1

    for name in list(kept)[:top_n]:
        take(name)
    rank = {name: n for n, name in enumerate(ranking, start=1)}
    stay = sorted((h for h in held if rank.get(h, buffer_rank + 1) <= buffer_rank), key=rank.get)
    for name in stay:
        if len(target) >= top_n:
            break
        if name not in target and room(name):
            take(name)
    for name in ranking:
        if len(target) >= top_n:
            break
        if name not in target and name not in held and room(name) and can_buy(name):
            take(name)
    return target


def _industry_key(found: str, bucket_unknown: bool) -> str | None:
    """The industry a name counts under the cap as, None when it is never capped (an unknown
    industry without `bucket_unknown`)."""
    return None if found == UNKNOWN_INDUSTRY and not bucket_unknown else found


def selection_depth(
    ranking: Sequence[str],
    rules: Rotation,
    industry: Callable[[str], str] | None = None,
    hold_only: Collection[str] = (),
) -> int:
    """How deep into `ranking` (best first) a rebalance's selection may reach, the names live
    subscribes and prices for the open: `top_n + buffer_rank` without an industry cap. With one,
    the rank by which `top_n + buffer_rank` names the cap could admit have been seen (a name
    beyond the cap's count in its industry is never selected, so it does not count; an unknown
    industry counts unless bucketed), or the whole ranking: the uncapped rule's slack measured in
    names the cap leaves selectable. A `hold_only` name (the liquidity floor's, never bought) is
    passed over as the cap passes one over, so the depth reaches past those too."""
    reach = rules.top_n + rules.buffer_rank
    cap = rules.max_per_industry
    capped = cap is not None and industry is not None
    if not capped and not hold_only:
        return reach
    bucket = rules.unknown_industry == "bucket"
    counts: dict[str, int] = {}
    admitted = 0
    for n, name in enumerate(ranking, start=1):
        if name in hold_only:
            continue
        key = _industry_key(industry(name), bucket) if capped and industry else None
        if key is None or counts.get(key, 0) < (cap or 0):
            admitted += 1
            if key is not None:
                counts[key] = counts.get(key, 0) + 1
        if admitted >= reach:
            return n
    return len(ranking)


@dataclass(frozen=True)
class Order:
    instrument_id: str
    action: str  # SELL, TRIM, BUY or TOP_UP
    qty: int


def _shares(value: Decimal, price: Decimal) -> int:
    return int((value / price).to_integral_value(ROUND_FLOOR))


def order_diff(
    target: Sequence[str],
    held: Mapping[str, int],
    prices: Mapping[str, Decimal],
    capital: Decimal,
    top_n: int,
    min_change_pct: Decimal,
    *,
    reweight: bool,
    weights: Mapping[str, Decimal] | None = None,
    no_buy: Collection[str] = (),
) -> list[Order]:
    """The orders that take `held` (shares by name) to `target` at `prices`: a sale of every
    holding not a target, then each target's change to `capital` / `top_n` of value (with
    `weights`, `capital` times its weight), in whole shares. A change of at most
    `min_change_pct` percent of that value is skipped; without `reweight` a held target is left
    as it is. Sales and trims come first, then buys and top-ups in target order. A target
    without a price gets no order, and one in `no_buy` (the liquidity floor's hold-only names)
    no buy or top-up: it may only be trimmed."""
    sells = [Order(name, SELL, qty) for name, qty in held.items() if qty > 0 and name not in target]
    equal = capital / top_n if top_n > 0 else Decimal(0)
    trims: list[Order] = []
    buys: list[Order] = []
    for name in target:
        each = equal if weights is None else capital * weights.get(name, Decimal(0))
        floor = each * min_change_pct / _HUNDRED
        price = prices.get(name)
        if price is None or price <= 0 or each <= 0:
            continue
        qty = held.get(name, 0)
        change = each - qty * price
        if qty == 0:
            if name not in no_buy and change > floor and (shares := _shares(change, price)) > 0:
                buys.append(Order(name, BUY, shares))
        elif reweight and -change > floor and (shares := _shares(-change, price)) > 0:
            trims.append(Order(name, TRIM, min(shares, qty)))
        elif (
            reweight
            and name not in no_buy
            and change > floor
            and (shares := _shares(change, price)) > 0
        ):
            buys.append(Order(name, TOP_UP, shares))
    return [*sells, *trims, *buys]


@dataclass(frozen=True)
class Plan:
    """A rebalance decided on `night`: the eligible names best first with their scores, the
    target list, whether the market filter emptied it, and the ranked names the liquidity
    floor never buys (`hold_only`)."""

    night: date
    ranking: tuple[tuple[str, float], ...]
    target: tuple[str, ...]
    filtered: bool = False
    hold_only: frozenset[str] = field(default_factory=frozenset)  # never bought nor topped up


def ranking(
    passers: Sequence[str],
    closes: np.ndarray,
    rules: Rotation,
    pending: Collection[str] = (),
    industry: Callable[[str], str] | None = None,
) -> tuple[tuple[str, float], ...]:
    """`passers` (the members passing the universe stage on the decision close; `closes` their
    total-return window, a column each) less the factor-pending names, scored and ranked; with
    the rules' `industry_relative` each score less its `industry`'s median that night
    (`industry_relative`)."""
    columns = [n for n, name in enumerate(passers) if name not in pending]
    names = [passers[n] for n in columns]
    values = scores(closes[:, columns], rules) if names else np.empty(0)
    if rules.industry_relative and names:
        if industry is None:
            raise ValueError("rotation_industry_unknown")  # the rule needs the classification
        values = industry_relative(names, values, industry)
    return tuple(ranked(names, values))


def industry_relative(
    names: Sequence[str], values: np.ndarray, industry: Callable[[str], str]
) -> np.ndarray:
    """Each name's score (`values`, NaN: not scored) less the median score of the scored names
    of its `industry` that night, itself included (a lone name of its industry scores 0); a name
    whose industry is unknown (`UNKNOWN_INDUSTRY`) keeps its raw score, and an unscored one stays
    NaN."""
    groups: dict[str, list[float]] = {}
    keys = [industry(name) for name in names]
    for key, value in zip(keys, values, strict=True):
        if key != UNKNOWN_INDUSTRY and math.isfinite(value):
            groups.setdefault(key, []).append(float(value))
    medians = {key: float(np.median(found)) for key, found in groups.items()}
    out = values.astype(np.float64, copy=True)
    for n, key in enumerate(keys):
        if key in medians and math.isfinite(out[n]):
            out[n] -= medians[key]
    return out


def targets(
    ranked_names: Sequence[tuple[str, float]],
    held: Collection[str],
    rules: Rotation,
    *,
    pending: Collection[str] = (),
    gate_on: bool,
    can_buy: Callable[[str], bool],
    industry: Callable[[str], str] | None = None,
    hold_only: Collection[str] = (),
) -> tuple[tuple[str, ...], bool]:
    """(the target list, whether the market filter emptied it): empty under the market filter
    with the gate off, else `select` with the hold buffer and the rules' industry cap (a name's
    `industry` on the trade date), a held factor-pending name kept without a rank. A name in
    `hold_only` (ranked in the liquidity floor's hold tier, below its entry tier) is never
    bought."""
    if rules.market_filter and not gate_on:
        return (), True
    if rules.max_per_industry is not None and industry is None:
        raise ValueError("rotation_industry_unknown")  # a capped rule needs the classification
    buyable = can_buy if not hold_only else (lambda n: n not in hold_only and can_buy(n))
    found = select(
        [name for name, _ in ranked_names],
        held,
        rules.top_n,
        rules.buffer_rank,
        buyable,
        kept=[h for h in held if h in pending],
        max_per_industry=rules.max_per_industry,
        industry=industry,
        bucket_unknown=rules.unknown_industry == "bucket",
    )
    return tuple(found), False


def plan(
    night: date,
    passers: Sequence[str],
    closes: np.ndarray,
    rules: Rotation,
    held: Collection[str],
    *,
    pending: Collection[str] = (),
    gate_on: bool,
    can_buy: Callable[[str], bool],
    industry: Callable[[str], str] | None = None,
    hold_only: Collection[str] = (),
) -> Plan:
    """The rebalance decided on `night`'s close (`ranking`, then `targets`); `passers` are
    already cut to the liquidity floor's hold tier when the rules have one, `hold_only` the
    names of it below the entry tier; `industry` (a name's on the trade date) feeds both the
    industry cap and `industry_relative`."""
    ranks = ranking(passers, closes, rules, pending, industry)
    target, filtered = targets(
        ranks, held, rules, pending=pending, gate_on=gate_on, can_buy=can_buy, industry=industry,
        hold_only=hold_only,
    )  # fmt: skip
    return Plan(night, ranks, target, filtered=filtered, hold_only=frozenset(hold_only))


def orders(
    chosen: Plan,
    held: Mapping[str, int],
    prices: Mapping[str, Decimal],
    capital: Decimal,
    rules: Rotation,
    weights: Mapping[str, Decimal] | None = None,
) -> list[Order]:
    """`order_diff` of `chosen`'s target under `rules`, on `capital`, the sleeve's; `weights`
    the targets' (`target_weights`) under `inverse_beta`, else equal; `chosen`'s hold-only
    names are never bought or topped up."""
    return order_diff(
        chosen.target,
        held,
        prices,
        capital,
        rules.top_n,
        require_exact(rules.min_change_pct, "min_change_pct", floats=True),
        reweight=rules.reweight,
        weights=weights,
        no_buy=chosen.hold_only,
    )


def checks_fire(
    checks: Sequence[RegimeRule],
    when: str,
    closes: Sequence[Decimal],
    breadth: Sequence[Decimal | None],
) -> bool:
    """Whether regime-rule `checks` fire on the last session of the series (`closes` of the
    strategy's regime index, `breadth`, both oldest first, as the regime gate reads them): one
    of them off (`when` `any_off`), or every one (`all_off`)."""
    on = [RULES[type(check)](closes, breadth, check) for check in checks]
    return not all(on) if when == "any_off" else not any(on)


def scale_down_fires(
    rule: ScaleDown, closes: Sequence[Decimal], breadth: Sequence[Decimal | None]
) -> bool:
    """Whether the scale-down fires on the last session of the series (`checks_fire`)."""
    return checks_fire(rule.checks, rule.when, closes, breadth)


def wide_fires(
    rule: WideBook, closes: Sequence[Decimal], breadth: Sequence[Decimal | None]
) -> bool:
    """Whether the wide book's checks fire on the rebalance close the series end on
    (`checks_fire`)."""
    return checks_fire(rule.checks, rule.when, closes, breadth)


def widened(rules: Rotation, wide: bool) -> Rotation:
    """The rules one rebalance decides and sizes under: with `wide_book` set and `wide` (its
    checks fired on the decision close) its `top_n` and the buffer raised in proportion
    (`strategy.config.wide_buffer_rank`), else `rules` themselves."""
    rule = rules.wide_book
    if rule is None or not wide:
        return rules
    buffer = wide_buffer_rank(rules.buffer_rank, rules.top_n, rule.top_n)
    return rules.model_copy(update={"top_n": rule.top_n, "buffer_rank": buffer})


def capital_share(rules: Rotation, fires: bool) -> Decimal:
    """The share of the sleeve's capital a rebalance sizes on: the scale-down's `keep_fraction`
    when it is set and fires on the rebalance close, else 1."""
    if rules.scale_down is None or not fires:
        return Decimal(1)
    return require_exact(rules.scale_down.keep_fraction, "keep_fraction", floats=True)


def scale_down_trims(held: Mapping[str, int], keep_fraction: Decimal) -> list[Order]:
    """The trims that take each holding (shares by name) to `keep_fraction` of its shares,
    rounded up (the shares sold rounded down), by name; a holding that would sell none has no
    order."""
    out: list[Order] = []
    for name in sorted(held):
        qty = held[name]
        sell = int((qty * (1 - keep_fraction)).to_integral_value(ROUND_FLOOR))
        if qty > 0 and sell > 0:
            out.append(Order(name, TRIM, min(sell, qty)))
    return out


def sale_reason(chosen: Plan, name: str) -> str:
    """Why a holding not in the target is sold: the market filter, out of the buffer (ranked),
    or not eligible (not ranked)."""
    if chosen.filtered:
        return MARKET_FILTER
    return OUT_OF_BUFFER if any(n == name for n, _ in chosen.ranking) else NOT_ELIGIBLE


def book_equity(
    at_cost: Decimal,
    holdings: Mapping[str, Sequence[tuple[int, Decimal]]],
    prices: Mapping[str, Decimal],
) -> Decimal:
    """The book's equity on its holdings' prices, the capital a rebalance sizes on (the
    backtest's: its cash plus its holdings marked): `at_cost`, the equity with every holding at
    its cost (cash plus the cost of the shares held), plus each lot's (shares, entry price) gain
    to its name's price; a name without a price stays at its cost."""
    gain = Decimal(0)
    for name, lots in holdings.items():
        price = prices.get(name)
        if price is not None:
            gain += sum((qty * (price - entry) for qty, entry in lots), Decimal(0))
    return at_cost + gain


def buy_share(outlay: Decimal, cash: Decimal) -> Decimal:
    """The share of each buy the cash pays when it cannot pay all of them (`outlay`, their cost
    with charges): 1 when it can, 0 without cash or outlay."""
    if outlay <= 0 or cash <= 0:
        return Decimal(0)
    return min(Decimal(1), cash / outlay)


def lot_sales(qty: int, lots: Sequence[tuple[K, int]]) -> list[tuple[K, int]]:
    """Split a sale of `qty` shares over a holding's `lots` (key, open shares), oldest first:
    the newest lot sells first, so a trim undoes the latest top-up; never more than a lot
    holds or `qty` in all."""
    out: list[tuple[K, int]] = []
    left = qty
    for key, open_qty in reversed(lots):
        if left <= 0:
            break
        take = min(left, open_qty)
        if take > 0:
            out.append((key, take))
            left -= take
    return out


# --- the optional rules (`Rotation.liquidity`, `inverse_beta`, `tranches`, `refuse_near_limit`) --


def median_traded_values(bars: pl.DataFrame, as_of: date, days: int) -> dict[str, float]:
    """Each instrument's median traded value over its last `days` bars through `as_of` (the
    reported traded value, else close x volume, as `min_turnover` reads it), for the instruments
    with a bar on `as_of` and a value on each of those `days` bars; `bars` the scan's frame
    (`instrument_id`, `date`, `close`, `volume`, optional `traded_value`)."""
    if bars.height == 0:
        return {}
    value = pl.col("close") * pl.col("volume")
    if "traded_value" in bars.columns:
        value = pl.coalesce(pl.col("traded_value"), value)
    frame = (
        bars.filter(pl.col("date") <= as_of)
        .select(pl.col("instrument_id").cast(pl.String), "date", value=value.cast(pl.Float64))
        .sort("instrument_id", "date")
        .group_by("instrument_id", maintain_order=True)
        .agg(pl.col("date").last(), pl.col("value").tail(days))
    )
    out: dict[str, float] = {}
    for name, last_day, values in frame.iter_rows():
        if last_day != as_of or len(values) < days or any(v is None for v in values):
            continue
        out[str(name)] = float(np.median(np.asarray(values, dtype=np.float64)))
    return out


def liquidity_tiers(
    values: Mapping[str, float], rule: Liquidity
) -> tuple[frozenset[str], frozenset[str]]:
    """(the entry tier, the hold tier) of the night's members by their median traded value
    (`values`, `median_traded_values`): the first `entry_pct` and `hold_pct` percent of them,
    most liquid first (ties by name), each count rounded up."""
    order = sorted(values, key=lambda name: (-values[name], name))
    count = len(order)

    def top(pct: float) -> frozenset[str]:
        share = require_exact(pct, "pct", floats=True) * count / _HUNDRED
        return frozenset(order[: int(share.to_integral_value(ROUND_CEILING))])

    return top(rule.entry_pct), top(rule.hold_pct)


def betas(closes: np.ndarray, bench: np.ndarray, rule: InverseBeta) -> np.ndarray:
    """Each column's beta to `bench` over the window (`closes` total-return closes, rows the
    `rule.sessions` + 1 sessions through the decision close, NaN without a bar; `bench` the
    NIFTY 500 TRI's closes on the same sessions): the covariance of their daily returns over the
    variance of the index's, on the sessions both have a return (a return needs a close that
    session and the one before), at least `rule.floor`; 1 (the market's) with fewer than
    `MIN_BETA_RETURNS` such returns or an index without variance."""
    out = np.ones(closes.shape[1])
    with np.errstate(divide="ignore", invalid="ignore"):
        market = bench[1:] / bench[:-1] - 1
        stock = closes[1:] / closes[:-1] - 1
    for column in range(closes.shape[1]):
        both = np.isfinite(stock[:, column]) & np.isfinite(market)
        if both.sum() < MIN_BETA_RETURNS:
            continue
        x, y = market[both], stock[both, column]
        variance = float(np.var(x, ddof=1))
        if variance > 0:
            out[column] = float(np.cov(x, y, ddof=1)[0, 1]) / variance
    return np.maximum(out, rule.floor)


def betas_by_name(names: Sequence[str], found: np.ndarray) -> dict[str, Decimal]:
    """`betas`' values by name, to `BETA_PLACES`: what the weights read, backtest and live."""
    return {
        name: require_exact(float(value), "beta", floats=True).quantize(BETA_PLACES)
        for name, value in zip(names, found, strict=True)
    }


def target_weights(
    target: Sequence[str], beta: Mapping[str, Decimal], top_n: int
) -> dict[str, Decimal]:
    """The targets' weights under `inverse_beta`: each 1 / its `beta` (a name without one has 1),
    scaled so they sum to len(`target`) / `top_n`, the share of the capital equal weights put
    into those targets (all of it with `top_n` of them)."""
    if not target or top_n <= 0:
        return {}
    inverse = {name: 1 / beta.get(name, Decimal(1)) for name in target}
    total = sum(inverse.values(), Decimal(0))
    scale = Decimal(len(target)) / top_n / total
    return {name: value * scale for name, value in inverse.items()}


def sessions_since_close(days: Sequence[date], cadence: str) -> list[int | None]:
    """For each session of `days` (consecutive sessions), how many sessions it is after the last
    rebalance close on or before it (0 on the close itself), None before the first one in
    `days` (the last session's own close cannot be judged without its next session: it counts
    on from the one before)."""
    out: list[int | None] = []
    since: int | None = None
    for n, night in enumerate(days):
        if n + 1 < len(days) and is_rebalance(night, days[n + 1], cadence):
            since = 0
        elif since is not None:
            since += 1
        out.append(since)
    return out


def tranche_due(since: int | None, rule: Tranches) -> int | None:
    """The tranche that decides on a close `since` sessions after its period's rebalance close
    (`sessions_since_close`): tranche k at k x `offset_sessions`, else None."""
    if since is None or since % rule.offset_sessions:
        return None
    k = since // rule.offset_sessions
    return k if k < rule.count else None


def near_limit(side: str, price: Decimal, daily: DailyInputs) -> bool:
    """Whether an order at `price` is at or within one tick of the band it trades against (a buy
    the upper band price, a sale the lower), read from what is known before the session:
    `daily`'s previous close, tick and the stored band in force on the trade date
    (`hat_price_band`, point in time; live's `DailyInputs`, the backtest's `MarketData.bands`),
    one rule for both books. With a band known the order is refused at or beyond one tick inside
    it; a known `band_pct` None (an F&O name, no band) never is. Without a stored band every NSE
    band (`NSE_BAND_PCTS`) is a candidate, and an order within one tick of any of their prices is
    refused (a price between two candidates is not). No previous close or tick: not refused."""
    prev, tick = daily.prev_close, daily.tick
    if prev is None or prev <= 0 or tick is None or tick <= 0:
        return False
    if daily.band_known and daily.band_pct is None:
        return False
    known = daily.band_known
    for pct in (daily.band_pct,) if known else NSE_BAND_PCTS:
        band = replace(daily, band_known=True, band_pct=pct)
        if side == "buy":
            limit = upper_circuit_price(band)
            if limit is not None and limit > prev:
                if price >= limit - tick if known else abs(price - limit) <= tick:
                    return True
        else:
            limit = lower_circuit_price(band)
            if limit is not None and limit < prev:
                if price <= limit + tick if known else abs(price - limit) <= tick:
                    return True
    return False

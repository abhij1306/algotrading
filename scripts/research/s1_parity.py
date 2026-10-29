"""S1 VCP parity (accuracy plan W2 B6): a separate, simple implementation of the S1 rules,
written from the strategy's resolved config and the documented rules (decisions.md, the
result standard), run on the market the engine reads, and compared with the engine's own run
of the same portfolio file: the nightly watchlist, the signals, the entries (date, price), the
exits (date, reason, price) and the trade list.

The independent side never calls the engine's scan, criteria, trigger, stop, sizing or exit
code. It reads the loaded market (`load_market`: the stored adjusted bars, features,
point-in-time membership, dividends, delistings) and shares only the cost schedule (`costs`:
slippage and charges, checked on their own in B3) and two data classifications (`isin`
ordinary equity, the NSE tick slabs' table). Every rule it applies:

- night `prev` for trade date d; the regime: NIFTY 500 close of `prev` above the mean of its
  last 50 closes; the universe: a NIFTY 500 member on d and on the session after d (the entry
  gate's conservative day), active, series EQ/BE as of `prev`, ordinary equity, a bar on `prev`;
  mean traded value of the 20 bars through `prev` at least Rs 10 cr, raw close at least Rs 50;
- the trend template on the dividend-adjusted (total return) series, bars since the last series
  break (a gap of more than 92 days): close > SMA150, close > SMA200, SMA150 > SMA200, SMA200
  above its value 21 bars back, SMA50 > SMA150, SMA50 > SMA200, close > SMA50, close at least
  30% above the 252-bar low, at most 25% below the 252-bar high;
- the VCP: the base runs from the first highest close of the last 120 bars; at least 15 bars;
  swing highs and lows strictly beyond the 3 bars either side; contractions from the base start
  to the next swing low, then from each next swing high to the swing low after it; the last 6;
  at least 2, each depth (high - low) / high at most 0.9 of the one before, the last at most
  10%; the volume of the last contraction at most the mean of the 50 bars before the base;
  score (1 + tight(max ratio, 0.9) + tight(final depth, 10) + tight(dry-up, 1)) / 4 at least
  0.6 with tight(v, L) = 0.5 + 0.5 (1 - v / L); the pivot is the last contraction's high,
  the close at or below it; the stop base is its low;
- a pivot is used once (an opened position's (instrument, pivot bar)); a held name is not
  scanned;
- entry on the daily bar: an open above the pivot fills at the open; else the first tick above
  the pivot when the high reaches it; stop = the base low less 0.5% floored to the tick, refused
  above 10% or not below the entry (no signal); a chase over 2% or a buy into an upper-circuit
  lock is a rejected signal; the day's entries in score order then by RS (rs_weighted of
  `prev`) highest first; size = min(risk 0.75% of the sleeve capital over the stop distance,
  p_max of capital, 1% of ADV20 shares, cash), refused below 2% of capital; at most 10 open, 4
  entries and 4 hard-stop exits a day, 10 per industry;
- the entry-day low before the break is estimated from ADR20 (the engine's documented
  estimate); a low at or below the stop after the break exits at the stop;
- exits: at the open when it is at or below the stop, at the stop when the low reaches it,
  at the stop when the close is at or below it, at the close when it is below SMA50 (49 prior
  closes and today's) or below the lowest low of the 20 prior bars; delisting, demerger and
  period-end closes; no sale on a day locked at the lower band.

    DB_NAME=hat_live PGOPTIONS="-c default_transaction_read_only=on" HAT_DATA_ROOT=<data> \\
        venv/bin/python scripts/research/s1_parity.py --portfolio P.yaml --from D --to D \\
        [--fresh] [--out NAME.json]

Prints the comparison and writes the differences under `reports/parity/` (gitignored).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import numpy as np  # noqa: E402
import parity_common as pc  # noqa: E402
import yaml  # noqa: E402

from app.hat.backtest.data import MarketData, symbol_on  # noqa: E402
from app.hat.backtest.report import BacktestResult  # noqa: E402
from app.hat.backtest.spec import RunSpec  # noqa: E402
from app.hat.data.isin import ordinary_equity  # noqa: E402
from app.hat.data.ticks import tick_size  # noqa: E402

E4 = Decimal("0.0001")
BREAK_DAYS = 92
CIRCUIT_BANDS = (2, 5, 10, 20)
SUSPENSION_SESSIONS = 5


def dec(x: float) -> Decimal:
    """A double as the engine stores a level: its shortest repr."""
    return Decimal(repr(float(x)))


def floor_tick(x: Decimal, tick: Decimal) -> Decimal:
    return (x / tick).to_integral_value(ROUND_FLOOR) * tick


def ceil_tick(x: Decimal, tick: Decimal) -> Decimal:
    return (x / tick).to_integral_value(ROUND_CEILING) * tick


def tight(v: float, limit: float) -> float:
    return 0.5 + 0.5 * min(1.0, max(0.0, 1.0 - v / limit))


# ---------------------------------------------------------------------------------------------
# the market as plain arrays


@dataclass
class Inst:
    days: list[date]
    o: np.ndarray  # price basis, doubles
    h: np.ndarray
    l: np.ndarray  # noqa: E741
    c: np.ndarray
    o4: list[Decimal]
    h4: list[Decimal]
    l4: list[Decimal]
    c4: list[Decimal]
    raw: np.ndarray
    vol: np.ndarray
    tv: np.ndarray
    div: np.ndarray
    seg: np.ndarray  # index of the first bar of each bar's series segment
    index: dict[date, int] = field(default_factory=dict)

    def at(self, day: date) -> int | None:
        return self.index.get(day)

    def last_before(self, day: date) -> int | None:
        n = bisect_left(self.days, day)
        return n - 1 if n else None

    def last_through(self, day: date) -> int | None:
        n = bisect_right(self.days, day)
        return n - 1 if n else None


def instruments(data: MarketData) -> dict[str, Inst]:
    out: dict[str, Inst] = {}
    for frame in data.bars.partition_by("instrument_id", maintain_order=True):
        iid = frame["instrument_id"][0]
        days = frame["date"].to_list()
        seg = np.zeros(len(days), dtype=int)
        for n in range(1, len(days)):
            seg[n] = n if (days[n] - days[n - 1]).days > BREAK_DAYS else seg[n - 1]

        def e4(col: str, frame: Any = frame) -> list[Decimal]:  # noqa: ANN401
            return [None if v is None else Decimal(int(v)) * E4 for v in frame[col].to_list()]

        def arr(col: str, frame: Any = frame) -> np.ndarray:  # noqa: ANN401
            return frame[col].cast(float).fill_null(np.nan).to_numpy()

        out[iid] = Inst(
            days,
            arr("open"),
            arr("high"),
            arr("low"),
            arr("close"),
            e4("open_e4"),
            e4("high_e4"),
            e4("low_e4"),
            e4("close_e4"),
            arr("raw_close"),
            arr("volume"),
            arr("traded_value"),
            arr("div_factor"),
            seg,
            {d: n for n, d in enumerate(days)},
        )
    return out


def features(data: MarketData) -> dict[tuple[str, date], dict[str, float | None]]:
    cols = [c for c in ("adr20", "adv20", "rs_weighted") if c in data.features.columns]
    out = {}
    for row in data.features.select(["instrument_id", "date", *cols]).iter_rows(named=True):
        out[(row["instrument_id"], row["date"])] = row
    return out


def members_on(data: MarketData) -> dict[str, list[tuple[date, date | None]]]:
    found: dict[str, list[tuple[date, date | None]]] = defaultdict(list)
    for m in data.members:
        found[str(m.instrument_id)].append((m.effective_from, m.effective_to))
    return found


def is_member(spans: Sequence[tuple[date, date | None]], day: date) -> bool:
    return any(f <= day and (t is None or day < t) for f, t in spans)


def series_on(points: Sequence[tuple[date, str]], day: date) -> str | None:
    found = None
    for first, series in points:
        if first <= day:
            found = series
    return found


def industry_on(data: MarketData, iid: str, day: date) -> str:
    found = data.industries.get(iid)
    if found is None or isinstance(found, str):
        return found or ""
    name = ""
    for first, industry in found:
        if first <= day:
            name = industry
    return name


# ---------------------------------------------------------------------------------------------
# the nightly scan


@dataclass(frozen=True)
class Setup:
    iid: str
    score: float
    pivot: Decimal
    base_low: Decimal
    pivot_date: date


def swing_points(h: np.ndarray, l: np.ndarray, w: int = 3) -> tuple[list[int], list[int]]:  # noqa: E741
    highs, lows = [], []
    for i in range(w, len(h) - w):
        around = list(range(i - w, i)) + list(range(i + 1, i + w + 1))
        if all(h[i] > h[j] for j in around):
            highs.append(i)
        if all(l[i] < l[j] for j in around):
            lows.append(i)
    return highs, lows


def vcp(
    c: np.ndarray,
    h: np.ndarray,
    l: np.ndarray,  # noqa: E741
    v: np.ndarray,
    before: np.ndarray,
    p: Mapping,
) -> tuple[float, int, int] | None:
    """(score, last contraction's first bar, its last bar) of the base ending at the last bar of
    `c`, None when it fails; `before` the volumes before the window, oldest first."""
    look = min(int(p["lookback_days"]), len(c))
    tail = c[-look:]
    start = len(c) - look + int(np.argmax(tail))
    base_h, base_l, base_v = h[start:], l[start:], v[start:]
    pre = np.concatenate([before, v[:start]])
    if len(pre) < 50 or np.isnan(pre[-50:]).any():
        return None
    if len(base_h) < 15 or np.isnan(base_h).any() or np.isnan(base_l).any():
        return None
    if np.isnan(base_v).any():
        return None
    highs, lows = swing_points(base_h, base_l)
    chain, a = [], 0
    while True:
        b = next((x for x in lows if x > a), None)
        if b is None:
            break
        chain.append((a, b))
        a = next((x for x in highs if x > b), None)
        if a is None:
            break
    lo_n, hi_n = p["contractions"]
    chain = chain[-hi_n:]
    if len(chain) < lo_n:
        return None
    depths = []
    for a, b in chain:
        top, bottom = max(base_h[a : b + 1]), min(base_l[a : b + 1])
        depths.append((dec(top) - dec(bottom)) / dec(top) * 100)
    ratios = [depths[i + 1] / depths[i] for i in range(len(depths) - 1) if depths[i] != 0]
    limit = Decimal(str(p["depth_ratio_max"]))
    if len(ratios) != len(depths) - 1 or any(r > limit for r in ratios):
        return None
    if depths[-1] > Decimal(str(p["final_depth_max_pct"])):
        return None
    a, b = chain[-1]
    base_mean = float(np.mean(pre[-50:]))
    if base_mean <= 0:
        return None
    dry = float(np.mean(base_v[a : b + 1])) / base_mean
    if dry > float(p["dryup_vs_avg50"]):
        return None
    max_ratio = float(max(ratios)) if ratios else 0.0
    score = (
        1.0
        + tight(max_ratio, float(limit))
        + tight(float(depths[-1]), float(p["final_depth_max_pct"]))
        + tight(dry, float(p["dryup_vs_avg50"]))
    ) / 4
    return score, start + a, start + b


def _sma(x: np.ndarray, n: int) -> float:
    return math.fsum(x[-n:]) / n


def template(c: np.ndarray, h: np.ndarray, l: np.ndarray) -> bool:  # noqa: E741
    if len(c) < 252 or len(c) < 221:
        return False
    close = c[-1]
    s50, s150, s200 = _sma(c, 50), _sma(c, 150), _sma(c, 200)
    s200_back = math.fsum(c[-221:-21]) / 200
    return (
        close > s150
        and close > s200
        and s150 > s200
        and s200 > s200_back
        and s50 > s150
        and s50 > s200
        and close > s50
        and close * 100 >= min(l[-252:]) * 130
        and close * 100 >= max(h[-252:]) * 75
    )


class Scanner:
    def __init__(self, data: MarketData, spec: RunSpec, inst: dict[str, Inst]) -> None:
        self.data, self.inst = data, inst
        pb = spec.config.playbooks[0]
        self.vcp_params = next(
            dict(c.params) for c in pb.setup.criteria if c.type == "vcp" and c.enabled
        )
        self.threshold = float(pb.setup.threshold)
        uni = {c.type: c.params for c in spec.config.universe if c.enabled}
        self.min_tv = float(uni["min_turnover"]["inr_cr"]) * 1e7
        self.tv_days = int(uni["min_turnover"]["days"])
        self.min_price = float(uni["min_price"]["inr"])
        self.series = set(uni["series_in"]["series"])
        self.index = [(d, float(c)) for d, c in data.index_closes[spec.config.regime.index]]
        self.index_days = [d for d, _ in self.index]
        self.regime_sma = int(spec.config.regime.params["sma"])

    def regime(self, prev: date) -> bool:
        n = bisect_right(self.index_days, prev)
        closes = [c for _, c in self.index[max(0, n - self.regime_sma) : n]]
        if len(closes) < self.regime_sma or self.index_days[n - 1] != prev:
            return False
        return closes[-1] * self.regime_sma > math.fsum(closes)

    def universe(self, s: Inst, i: int) -> bool:
        lo = max(s.seg[i], i - self.tv_days + 1)
        if i - lo + 1 < self.tv_days:
            return False
        tv = s.tv[lo : i + 1].copy()
        bad = np.isnan(tv)
        tv[bad] = s.c[lo : i + 1][bad] * s.div[lo : i + 1][bad] / s.div[i] * s.vol[lo : i + 1][bad]
        if np.isnan(tv).any():
            return False
        return math.fsum(tv) >= self.min_tv * self.tv_days and s.raw[i] >= self.min_price

    def setup(self, iid: str, s: Inst, i: int) -> Setup | None:
        lo = max(s.seg[i], i - 699)
        scale = s.div[lo : i + 1] / s.div[i]
        c = s.c[lo : i + 1] * s.div[lo : i + 1] / s.div[i]
        h = s.h[lo : i + 1] * s.div[lo : i + 1] / s.div[i]
        l = s.l[lo : i + 1] * s.div[lo : i + 1] / s.div[i]  # noqa: E741
        del scale
        if np.isnan(c[-252:]).any() or not template(c, h, l):
            return None
        v = s.vol[lo : i + 1]
        found = vcp(c, h, l, v, np.array([]), self.vcp_params)
        if found is None:
            return None
        score, a, b = found
        if score < self.threshold:
            return None
        pivot = dec(max(h[a : b + 1])).quantize(E4, ROUND_HALF_UP)
        base_low = dec(min(l[a : b + 1])).quantize(E4, ROUND_HALF_UP)
        top = max(h[a : b + 1])
        if dec(c[-1]) > dec(top):  # close_vs_pivot reads the unrounded pivot
            return None
        at = max(n for n in range(len(h)) if h[n] == top)
        return Setup(iid, score, pivot, base_low, s.days[lo + at])


# ---------------------------------------------------------------------------------------------
# the day loop


@dataclass
class Position:
    iid: str
    entry_day: date
    qty: int
    entry: Decimal  # the slipped fill
    ref: Decimal  # the reference entry price
    stop: Decimal
    pivot_date: date


@dataclass
class Out:
    watch: list[tuple] = field(default_factory=list)
    signals: list[tuple] = field(default_factory=list)
    trades: list[tuple] = field(default_factory=list)
    fills: list[tuple] = field(default_factory=list)
    skips: Counter = field(default_factory=Counter)
    equity: list[tuple] = field(default_factory=list)
    final: Decimal | None = None  # the cash after the period-end sales


class Book:
    def __init__(self, data: MarketData, spec: RunSpec) -> None:
        self.data, self.spec = data, spec
        self.inst = instruments(data)
        self.feat = features(data)
        self.members = members_on(data)
        self.scan = Scanner(data, spec, self.inst)
        policy = spec.policy.policy
        self.risk_pct = policy.risk_pct
        self.p_max = policy.p_max[spec.config.bot]
        self.adv_cap = policy.adv_cap_pct
        self.max_open = policy.max_open_positions
        self.max_industry = policy.max_positions_per_industry
        self.max_entries = policy.max_entries_per_day
        self.max_stopouts = policy.max_stopouts_per_day
        self.min_pos = policy.min_position_pct
        self.open_risk = policy.global_open_risk_pct
        self.weight = min(policy.base_weight["S1"], Decimal(1))
        pb = spec.config.playbooks[0]
        self.chase = Decimal(str(next(f.params["pct"] for f in pb.entry_filters if f.enabled)))
        self.stop_buffer = Decimal(str(pb.stop.buffer_pct))
        self.stop_max = Decimal(str(pb.stop.max_pct))
        exits = {e.rule: e for e in spec.config.exits if e.enabled}
        self.sma_n = int(exits["close_below_sma"].sma)
        self.swing_n = int(exits["trailing_swing_low"].lookback_days)
        self.fills = spec.fills
        self.days = [d for d in data.days if spec.start <= d <= spec.end]
        self.all_days = list(data.days)
        self.cash = spec.initial_equity
        self.positions: list[Position] = []
        self.used: set[tuple[str, date]] = set()
        self.equity = spec.initial_equity
        self.sold_today: set[tuple[date, str]] = set()
        self.out = Out()
        self.engine_signal: dict[tuple[str, date], dict[str, Decimal | None]] = {}

    # --- prices
    def tick(self, iid: str, day: date) -> Decimal | None:
        s = self.inst[iid]
        n = bisect_left(s.days, day)
        month = bisect_left(s.days[:n], day.replace(day=1))
        ref = month - 1 if month - 1 >= 0 else n - 1
        if ref < 0 or np.isnan(s.raw[ref]):
            return None
        t = tick_size(Decimal(repr(float(s.raw[ref]))).quantize(E4))
        last = s.last_through(day)
        if t is None or last is None:
            return None
        adj, raw = s.c4[last], Decimal(repr(float(s.raw[last]))).quantize(E4)
        return (t * adj / raw).quantize(E4, ROUND_HALF_EVEN)

    def bar(self, iid: str, day: date) -> int | None:
        s = self.inst.get(iid)
        i = None if s is None else s.at(day)
        if i is None or (iid, day) in self.data.skipped:
            return None
        if any(x is None or x <= 0 for x in (s.o4[i], s.h4[i], s.l4[i], s.c4[i])):
            return None
        return i

    def prev_close(self, iid: str, day: date) -> Decimal | None:
        s = self.inst[iid]
        n = s.last_before(day)
        return None if n is None else s.c4[n]

    def bands(self, iid: str, day: date, tick: Decimal) -> list[tuple[Decimal, Decimal]]:
        prev = self.prev_close(iid, day)
        if prev is None:
            return []
        return [
            (floor_tick(prev * (100 + b) / 100, tick), ceil_tick(prev * (100 - b) / 100, tick))
            for b in CIRCUIT_BANDS
        ]

    def lower_locked(self, iid: str, day: date, i: int, *, whole_day: bool) -> bool:
        s = self.inst[iid]
        tick = self.tick(iid, day)
        if tick is None:
            return False
        lows = [lo for _, lo in self.bands(iid, day, tick)]
        near = lambda x: any(abs(x - b) <= tick for b in lows)  # noqa: E731
        if whole_day:
            return s.o4[i] == s.h4[i] == s.l4[i] == s.c4[i] and near(s.c4[i])
        return s.c4[i] == s.l4[i] and near(s.c4[i])

    def upper_locked(self, iid: str, day: date, price: Decimal, i: int) -> bool | None:
        s = self.inst[iid]
        prev = self.prev_close(iid, day)
        tick = self.tick(iid, day)
        if prev is None or tick is None:
            return None
        if not (price == s.h4[i] and price > prev):
            return False
        return any(abs(price - up) <= tick for up, _ in self.bands(iid, day, tick))

    # --- money
    def sell(self, p: Position, day: date, price: Decimal, reason: str) -> None:
        filled, fees = self.fills.fill(
            "sell", p.qty, price, day, sold_today=(day, p.iid) in self.sold_today
        )
        self.sold_today.add((day, p.iid))
        self.cash += filled * p.qty - fees
        self.positions.remove(p)
        self.out.fills.append((day, p.iid, "sell", p.qty, filled, fees))
        self.out.trades.append((p.iid, p.entry_day, p.entry, p.stop, p.qty, day, reason, filled))
        if reason == "hard_stop":
            self.stopouts += 1

    def mark(self, day: date) -> Decimal:
        total = self.cash
        for p in self.positions:
            total += self.mark_price(p.iid, day) * p.qty
        return total

    def mark_price(self, iid: str, day: date) -> Decimal:
        s = self.inst[iid]
        if self.bar(iid, day) is not None:
            return s.c4[s.at(day)]
        n = s.last_through(day)
        while n is not None and n >= 0 and (iid, s.days[n]) in self.data.skipped:
            n -= 1
        last_day, last = s.days[n], s.c4[n]
        missing = sum(1 for d in self.all_days if last_day < d <= day)
        if missing <= SUSPENSION_SESSIONS:
            return last
        tick = self.tick(iid, day) or E4
        return floor_tick(last * Decimal("0.95") ** (missing - SUSPENSION_SESSIONS), tick)

    # --- the day
    def run(self) -> Out:
        for k, day in enumerate(self.days):
            prev = self._prev(day)
            self.stopouts = 0
            rows = [] if prev is None else self.publish(prev, day)
            capital = (self.weight * self.equity).quantize(E4, ROUND_FLOOR)
            self.morning(day)
            if prev is not None:
                self.entries(day, prev, rows, capital)
            self.close_phase(day)
            self.delistings(day)
            self.equity = self.mark(day).quantize(E4, ROUND_HALF_EVEN)
            self.out.equity.append((day, self.equity, self.cash))
            del k
        for p in sorted(self.positions, key=lambda p: (p.entry_day, p.iid)):
            self.sell(p, self.days[-1], self.mark_price(p.iid, self.days[-1]), "end_of_period")
        self.out.final = self.cash
        return self.out

    def _prev(self, day: date) -> date | None:
        n = bisect_left(self.all_days, day)
        return self.all_days[n - 1] if n else None

    def _next(self, day: date) -> date | None:
        n = bisect_right(self.all_days, day)
        return self.all_days[n] if n < len(self.all_days) else None

    def member(self, iid: str, day: date) -> bool:
        return is_member(self.members.get(iid, ()), day)

    def publish(self, prev: date, day: date) -> list[Setup]:
        if not self.scan.regime(prev):
            return []
        held = {p.iid for p in self.positions}
        rows = []
        for iid, s in self.inst.items():
            if iid in held or not self.member(iid, day):
                continue
            if series_on(self.data.series.get(iid, ()), prev) not in self.scan.series:
                continue
            if not ordinary_equity(self.data.isins.get(iid)):
                continue
            if (iid, prev) in self.data.pending:
                continue
            i = s.at(prev)
            if i is None or not self.scan.universe(s, i):
                continue
            found = self.scan.setup(iid, s, i)
            if found is None or (iid, found.pivot_date) in self.used:
                continue
            rows.append(found)
        rows.sort(key=lambda r: (-r.score, r.iid))
        for r in rows:
            self.out.watch.append((day, r.iid, round(r.score, 4), r.pivot))
        return rows

    def morning(self, day: date) -> None:
        for p in sorted(self.positions, key=lambda p: (p.entry_day, p.iid)):
            for ex, cash in self.data.dividends.get(p.iid, ()):
                if ex == day:  # credited to the paisa
                    self.cash += (cash * p.qty).quantize(Decimal("0.01"), ROUND_HALF_EVEN)
            i = self.bar(p.iid, day)
            if i is None or self.lower_locked(p.iid, day, i, whole_day=True):
                continue
            s = self.inst[p.iid]
            if s.o4[i] <= p.stop:
                self.sell(p, day, s.o4[i], "hard_stop")
            elif s.l4[i] <= p.stop:
                self.sell(p, day, min(s.o4[i], p.stop), "hard_stop")

    def entries(self, day: date, prev: date, rows: list[Setup], capital: Decimal) -> None:
        decided = []
        nxt = self._next(day)
        for r in rows:
            if (r.iid, day) in self.data.pending:
                self.out.skips["factor_pending"] += 1
                continue
            if (r.iid, day) in self.data.demerger_exits:
                self.out.skips["demerger_pending"] += 1
                continue
            i = self.bar(r.iid, day)
            if i is None:
                continue
            s = self.inst[r.iid]
            tick = self.tick(r.iid, day)
            o, h = s.o4[i], s.h4[i]
            gapped = o > r.pivot
            if gapped:
                price = o
            else:
                if tick is None:
                    if h > r.pivot:
                        self.out.skips["stop_input_missing"] += 1
                    continue
                above = floor_tick(r.pivot, tick) + tick
                if h < above:
                    continue
                price = above
            if tick is None:
                self.out.skips["stop_input_missing"] += 1
                continue
            stop = floor_tick(r.base_low * (100 - self.stop_buffer) / 100, tick)
            if not 0 < stop < price:
                self.out.skips["stop_not_below_entry"] += 1
                continue
            if (price - stop) * 100 > self.stop_max * price:
                self.out.skips["stop_too_wide"] += 1
                continue
            locked = self.upper_locked(r.iid, day, price, i)
            chase = locked is None or locked or price * 100 > r.pivot * (100 + self.chase)
            rs = (self.feat.get((r.iid, prev)) or {}).get("rs_weighted")
            decided.append((r, i, price, stop, gapped, chase, rs))
        decided.sort(key=lambda x: (-x[0].score, x[0].iid))
        decided.sort(key=lambda x: (x[6] is None, -(x[6] or 0)))
        entered = 0
        for r, i, price, stop, gapped, chase, _ in decided:
            if chase:
                self.signal(day, r, "Rejected", "max_chase", price, stop, 0)
                continue
            reason = None
            industry = industry_on(self.data, r.iid, day)
            if nxt is not None and not self.member(r.iid, nxt):
                self.out.skips["not_in_universe"] += 1
                continue
            if len(self.positions) >= self.max_open:
                reason = "max_positions"
            elif (
                sum(1 for p in self.positions if industry_on(self.data, p.iid, day) == industry)
                >= self.max_industry
            ):
                reason = "industry_cap"
            elif entered >= self.max_entries:
                reason = "entry_cap"
            elif self.stopouts >= self.max_stopouts:
                reason = "stopout_cap"
            adv = (self.feat.get((r.iid, prev)) or {}).get("adv20")
            if reason is None and adv is None:
                reason = "missing_input"
            qty = 0
            if reason is None:
                adv20 = dec(adv).quantize(E4)
                free = Decimal(self.fills.affordable(self.cash, price, day)) * price
                terms = [
                    (capital * self.risk_pct / (price - stop), "risk"),
                    (capital * self.p_max / price, "position"),
                    (adv20 * self.adv_cap, "liquidity"),
                    (free / price, "cash"),
                ]
                q, bind = min(terms, key=lambda t: t[0])
                qty = int(q.to_integral_value(ROUND_FLOOR))
                if qty < 1:
                    reason = f"size_zero_{bind}"
                elif qty * price < capital * self.min_pos:
                    reason = "size_below_min"
                else:
                    risk = sum(
                        (max(p.entry - p.stop, Decimal(0)) * p.qty for p in self.positions),
                        Decimal(0),
                    )
                    if risk + (price - stop) * qty > self.open_risk * capital:
                        reason = "global_open_risk"
            if reason is not None:
                self.signal(day, r, "Rejected", reason, price, stop, 0)
                continue
            filled, fees = self.fills.fill("buy", qty, price, day)
            self.cash -= filled * qty + fees
            p = Position(r.iid, day, qty, filled, price, stop, r.pivot_date)
            self.positions.append(p)
            self.used.add((r.iid, r.pivot_date))
            self.out.fills.append((day, r.iid, "buy", qty, filled, fees))
            self.signal(day, r, "Open", None, price, stop, qty)
            entered += 1
            # the entry-day low after the break (the engine's ADR20 estimate)
            s = self.inst[r.iid]
            low, o = s.l4[i], s.o4[i]
            adr = (self.feat.get((r.iid, prev)) or {}).get("adr20")
            if gapped:
                dip = low
            elif adr is None:
                dip = None
            else:
                before = max(low, min(o, price * (1 - dec(adr) / 100)))
                dip = low if low < before else None
            self.engine_signal[(r.iid, day)] = {"dip": dip}
            if dip is not None and dip <= stop:
                self.sell(p, day, stop, "hard_stop")

    def signal(self, day, r, state, reason, price, stop, qty) -> None:  # noqa: ANN001
        self.out.signals.append((day, r.iid, state, reason, price, stop, qty))

    def close_phase(self, day: date) -> None:
        for p in sorted(self.positions, key=lambda p: (p.entry_day, p.iid)):
            i = self.bar(p.iid, day)
            if i is None:
                continue
            if self.lower_locked(p.iid, day, i, whole_day=True) or self.lower_locked(
                p.iid, day, i, whole_day=False
            ):
                continue
            s = self.inst[p.iid]
            close = s.c4[i]
            if close <= p.stop:
                self.sell(p, day, p.stop, "hard_stop")
                continue
            lo = s.seg[i]
            tr = s.c * s.div
            if i - lo >= self.sma_n - 1:
                sma = math.fsum(tr[i - self.sma_n + 1 : i + 1]) / self.sma_n
                if tr[i] < sma:
                    self.sell(p, day, close, "close_below_sma")
                    continue
            if i - lo >= self.swing_n:
                swing = min(s.l[i - self.swing_n : i] * s.div[i - self.swing_n : i])
                if tr[i] < swing:
                    self.sell(p, day, close, "trailing_swing_low")

    def delistings(self, day: date) -> None:
        for p in sorted(self.positions, key=lambda p: (p.entry_day, p.iid)):
            if (p.iid, day) in self.data.demerger_exits:
                self.sell(p, day, self.mark_price(p.iid, day), "demerger")
            elif self.data.last_traded.get(p.iid) == day:
                self.sell(p, day, self.mark_price(p.iid, day), "delisted")


# ---------------------------------------------------------------------------------------------
# the exit replay check (research/vcp-leaks matched 90-92% of exit dates: why)

REPLAY_VARIANTS = {
    "engine_rules": {},
    "cap_basis_sma_swing": {"cap": True},  # SMA50 and swing low on split-adjusted closes
    "no_entry_day_stop": {"no_day0": True},  # no stop on the entry day (dip or close)
    "sells_on_lower_lock": {"no_lock": True},  # sells on a day locked at the lower band
    "vcp_leaks_replay": {"cap": True, "no_day0": True, "no_lock": True},
}


def replay_exit(book: Book, t: tuple, opts: Mapping[str, bool], end: date) -> tuple[date, str]:
    """The exit (day, rule) of one engine trade replayed from its entry under `opts`."""
    iid, entry_day, _, stop, *_ = t
    s = book.inst[iid]
    tr = s.c if opts.get("cap") else s.c * s.div
    lows = s.l if opts.get("cap") else s.l * s.div
    for day in (d for d in book.days if entry_day <= d <= end):
        i = book.bar(iid, day)
        if day == book.data.last_traded.get(iid) and i is None:
            return day, "delisted"
        if i is None:
            continue
        first = day == entry_day
        locked = not opts.get("no_lock") and book.lower_locked(iid, day, i, whole_day=True)
        if not first and not locked and s.l4[i] <= stop:
            return day, "hard_stop"
        if first and not opts.get("no_day0"):
            sig = book.engine_signal.get((iid, day))
            if sig is not None and sig.get("dip") is not None and sig["dip"] <= stop:
                return day, "hard_stop"
        if not locked and not (
            not opts.get("no_lock") and book.lower_locked(iid, day, i, whole_day=False)
        ):
            if s.c4[i] <= stop and not (first and opts.get("no_day0")):
                return day, "hard_stop"
            lo = s.seg[i]
            if i - lo >= book.sma_n - 1:
                if tr[i] < math.fsum(tr[i - book.sma_n + 1 : i + 1]) / book.sma_n:
                    return day, "close_below_sma"
            if i - lo >= book.swing_n and tr[i] < min(lows[i - book.swing_n : i]):
                return day, "trailing_swing_low"
        if (iid, day) in book.data.demerger_exits:
            return day, "demerger"
        if book.data.last_traded.get(iid) == day:
            return day, "delisted"
    return end, "end_of_period"


def replay_check(book: Book, engine_trades: list[tuple]) -> dict[str, Any]:
    out = {}
    for name, opts in REPLAY_VARIANTS.items():
        same = sum(1 for t in engine_trades if replay_exit(book, t, opts, book.days[-1])[0] == t[5])
        out[name] = {"same_exit_date": same, "trades": len(engine_trades)}
    return out


# ---------------------------------------------------------------------------------------------
# the comparison


def engine_rows(result: BacktestResult) -> dict[str, list[tuple]]:
    return {
        "watch": [
            (w.trade_date, w.instrument_id, round(float(w.score), 4), w.pivot)
            for w in result.watchlists
        ],
        "signals": [
            (s.trade_date, s.instrument_id, s.state, s.reason, s.entry_price, s.stop_price, s.qty)
            for s in result.signals
        ],
        "trades": [
            (
                t.instrument_id,
                t.entry_date,
                t.entry_price,
                t.initial_stop,
                t.qty,
                t.exit_date,
                t.exit_reason,
                t.legs[0].price if t.legs else None,
            )
            for t in result.trades
        ],
        "fills": [(f.day, f.instrument_id, f.side, f.qty, f.price, f.fees) for f in result.fills],
        "equity": [(d, e) for d, e in result.equity],
    }


def _norm(row: tuple) -> tuple:
    return tuple(v.normalize() if isinstance(v, Decimal) else v for v in row)


def compare(engine: Mapping[str, list[tuple]], ours: Out) -> dict[str, Any]:
    report: dict[str, Any] = {}
    # the engine's last mark is after the period-end sales: compared as the final equity
    named = (
        ("watch", engine["watch"], ours.watch),
        ("signals", engine["signals"], ours.signals),
        ("trades", engine["trades"], ours.trades),
        ("fills", engine["fills"], ours.fills),
        ("equity", engine["equity"][:-1], [(d, e) for d, e, _ in ours.equity[:-1]]),
    )
    for name, theirs, mine in named:
        a = [_norm(r) for r in theirs]
        b = [_norm(r) for r in mine]
        only_e, only_p = pc.multiset_diff(a, b)
        report[name] = {
            "engine": len(a),
            "parity": len(b),
            "only_engine": only_e,
            "only_parity": only_p,
        }
    report["final_equity"] = {"engine": str(engine["equity"][-1][1]), "parity": str(ours.final)}
    return report


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--portfolio", type=Path, required=True)
    parser.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    parser.add_argument("--fresh", action="store_true", help="rerun the engine")
    parser.add_argument("--out", default=None, help="a report file name under reports/parity")
    args = parser.parse_args(argv)
    text_ = args.portfolio.read_text(encoding="utf-8")
    spec, data, result = pc.engine_and_market(
        yaml.safe_load(text_), text_, args.start, args.end, fresh=args.fresh
    )
    book = Book(data, spec)
    ours = book.run()
    rows = engine_rows(result)
    report = compare(rows, ours)
    report["exit_replay"] = replay_check(book, rows["trades"])
    for name, part in report.items():
        if name in ("final_equity", "exit_replay"):
            print(f"{name}:", part)
            continue
        print(
            f"{name}: engine {part['engine']} parity {part['parity']} "
            f"only_engine {len(part['only_engine'])} only_parity {len(part['only_parity'])}"
        )
    print("parity skips:", dict(ours.skips), "engine skips:", dict(result.skips))
    if args.out:
        pc.REPORTS.mkdir(parents=True, exist_ok=True)

        def sym(row: tuple) -> list:
            out = [str(v) for v in row]
            for n, v in enumerate(row):
                if isinstance(v, str) and len(v) == 36:
                    day = next((x for x in row if isinstance(x, date)), args.end)
                    out[n] = symbol_on(data, v, day)
            return out

        dump = {
            k: {kk: [sym(r) for r in vv] if isinstance(vv, list) else vv for kk, vv in v.items()}
            for k, v in report.items()
        }
        (pc.REPORTS / args.out).write_text(json.dumps(dump, indent=1) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    del timedelta
    sys.exit(main())

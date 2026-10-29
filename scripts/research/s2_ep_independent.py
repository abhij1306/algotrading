"""An independent, simple implementation of the S2 episodic-pivot rules (accuracy plan W2 B6),
run by `s2_ep_parity.py parity` against the engine's result of the same portfolio and window.
It reads the same market (`load_market`: the stored bars, features, membership, 5-minute bars
and results filings) and the resolved config, and none of the engine's signal, trigger, stop,
risk or exit code: the rules below are transcribed from the playbook spec and the config.

- gap scan, each session D (scan night P, the session before): a NIFTY 500 member on D, a bar
  on D and a previous stored close, open >= previous close x (1 + 6%) exactly; the bot's
  universe on the bars through P (TR basis, since the series break, a bar on P): series EQ/BE
  as of P, the 20-row mean traded value through P at least Rs 10 cr (close x volume where null),
  the 20-row mean of 100 x (high / low - 1) at least 4%; else not a candidate. A candidate
  without a bar on P fails `setup`; with fewer than 64 closes before P it fails `neglect`
  (the cap is off); a factor-pending day, a demerger exit day: `not_open`;
- the trigger on 5-minute bars, when the session's bars fit (the full 09:15-15:25 grid, the
  first open within 5% of the daily open), scaled to the price basis by the day's adjusted /
  raw close: the opening range is the 09:15 bar; bars whose end + 2 s falls in [09:20, 15:05];
  the gates in the funnel order gap_up, earnings_catalyst (the newest filing public in
  (P 09:15, the bar's final instant], with a defined sales growth), early_volume (the day's
  volume through the bar >= 0.5 x ADV20 shares), price_breaks_level (high > ORH, close >= ORH);
  it fires on the first bar passing all; entry the ORH or the close, the larger, up to the tick;
  stop the lowest low through the trigger bar less a tick, down to the tick; filters adtr_filter
  (entry - stop <= 1.5 x ADTR20) and max_chase (entry <= ORH x 1.02, not at the upper band
  inferred from the day's high); a LIMIT no later bar traded down to does not fill;
- the daily estimate otherwise: fires when high >= open x 1.01 and the close is in the upper
  half of the range, with gap_up, early_volume on the whole day's volume and the filing public
  by D 09:35; entry min(high, open x 1.0248) up to the tick (none above the high), its level
  entry / 1.0058, the low before it max(low, open x 0.9598), the stop that low less a tick;
- the slots in trigger-time order; the risk checks and sizing of the S1 book with the exposure
  ladder (start L1; up after 2 straight wins, down after 2 straight losses; a win is a trade
  whose net P&L with dividends is positive), the max positions the lesser of the policy's and
  the level's; 5-minute entries fill at the LIMIT, daily ones with slippage;
- exits: the entry day's low after the trigger at or below the stop sells at the stop; then the
  S1 book's morning stops, the close at or below the stop, close below SMA20 once more than 2
  sessions are held, 60 sessions held; delisting, demerger and period-end closes.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from fractions import Fraction
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import s1_parity as s1
from s1_parity import E4, ceil_tick, dec, floor_tick

from app.hat.backtest.data import MarketData, symbol_on
from app.hat.backtest.report import BacktestResult
from app.hat.backtest.spec import RunSpec
from app.hat.data.isin import ordinary_equity

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = time(9, 15), time(15, 30)
BAR = timedelta(minutes=5)
LAG = timedelta(seconds=2)
FIRST_FINAL, LAST_FINAL = time(9, 20), time(15, 5)
OPEN_OFF_PCT = Decimal(5)
EST_FIRE, EST_ENTRY, EST_CHASE, EST_LOW = (
    Decimal(1),
    Decimal("2.48"),
    Decimal("0.58"),
    Decimal("4.02"),
)
EST_AT = time(9, 35)
ENTRY_AT = time(9, 30)
HISTORY = 64
GATES = ("gap_up", "earnings_catalyst", "early_volume", "price_breaks_level")
LEVELS = ("L1", "L2", "L3")
BARS_5M = "bars_5m"
SUPPORTED_EXITS = {"hard_stop", "close_below_sma", "max_hold"}


@dataclass
class Bar5:
    start: datetime
    o: Decimal
    h: Decimal
    l: Decimal  # noqa: E741
    c: Decimal
    v: int

    @property
    def final(self) -> datetime:
        return self.start + BAR + LAG


@dataclass
class Plan:
    iid: str
    basis: str
    price: Decimal
    stop: Decimal
    dip: Decimal | None
    skip: str | None
    seconds: int
    at: datetime | None


@dataclass
class Held:
    iid: str
    entry_day: date
    qty: int
    entry: Decimal
    ref: Decimal
    stop: Decimal
    basis: str
    cost: Decimal = Decimal(0)  # the buy's value and fees
    dividends: Decimal = Decimal(0)


@dataclass
class Record:
    basis: str | None = None
    failed_gate: str | None = None
    reason: str | None = None
    state: str | None = None


@dataclass
class EpOut(s1.Out):
    candidates: dict[tuple[date, str], Record] = field(default_factory=dict)


def at_ist(day: date, t: time) -> datetime:
    return datetime.combine(day, t, tzinfo=IST)


def growth_passes(event: Any, p: Mapping[str, Any]) -> bool | None:  # noqa: ANN401
    """The earnings rule on one filing; None when the sales growth it reads is undefined."""
    sales = None if event.sales_yoy is None else event.sales_yoy * 100
    if p.get("sales_yoy_pct") is not None:
        if sales is None:
            return None
        if sales < dec(p["sales_yoy_pct"]):
            return False
    eps = p.get("eps_yoy_pct")
    eps_ok = eps is None or (event.eps_yoy is not None and event.eps_yoy * 100 >= dec(eps))
    alone = p.get("sales_alone_pct")
    alone_ok = alone is not None and sales is not None and sales >= dec(alone)
    return eps_ok or bool(event.turnaround) or alone_ok


class EpBook(s1.Book):
    def __init__(self, data: MarketData, spec: RunSpec) -> None:  # noqa: PLR0915
        self.data, self.spec = data, spec
        self.inst = s1.instruments(data)
        cols = [c for c in ("adv20", "adtr20") if c in data.features.columns]
        self.feat = {
            (r["instrument_id"], r["date"]): r
            for r in data.features.select(["instrument_id", "date", *cols]).iter_rows(named=True)
        }
        self.members = s1.members_on(data)
        cfg = spec.config
        pb = next(p for p in cfg.playbooks if p.preopen)
        self.pb = pb
        trig = {i.type: i for i in pb.trigger if i.enabled}
        self.gap_min = dec(trig["gap_up"].params["min_pct"])
        self.early = dec(trig["early_volume"].params["vs_adv20"])
        self.earn_params = dict(trig["earnings_catalyst"].params)
        filters = {f.type: f.params for f in pb.entry_filters if f.enabled}
        self.adtr_max = dec(filters["adtr_filter"]["max_multiple"])
        self.chase = dec(filters["max_chase"]["pct"])
        self.buffer_ticks = int(pb.stop.buffer_ticks)
        uni = {c.type: c.params for c in cfg.universe if c.enabled}
        self.series_ok = set(uni["series_in"]["series"])
        self.tv_days = int(uni["min_turnover"]["days"])
        self.min_tv = dec(uni["min_turnover"]["inr_cr"]) * 10_000_000
        self.adr_days = int(uni["adr_min"]["days"])
        self.adr_min = dec(uni["adr_min"]["pct"])
        exits = {e.rule: e for e in cfg.exits if e.enabled}
        regime = cfg.regime
        supported = (
            set(exits) <= SUPPORTED_EXITS
            and regime.rule == "breadth_above"
            and int(regime.params.get("smooth_days", 1)) == 1
            and regime.params.get("off_below_pct") is None
            and spec.conventions.entry_bars in (BARS_5M, "daily")
        )
        if not supported:
            raise SystemExit(
                "s2_ep_independent covers the exits hard_stop, close_below_sma and max_hold, the "
                "breadth_above regime over one session and entry_bars bars_5m or daily only"
            )
        self.breadth_min = dec(regime.params.get("min_pct", 0))
        self.five_minute = spec.conventions.entry_bars == BARS_5M
        self.sma_n = int(exits["close_below_sma"].sma)
        self.grace = exits["close_below_sma"].grace_sessions
        self.max_hold = int(exits["max_hold"].days)
        policy = spec.policy.policy
        self.policy = policy
        self.risk_pct = policy.risk_pct
        self.p_max = policy.p_max[cfg.bot]
        self.adv_cap = policy.adv_cap_pct
        self.max_industry = policy.max_positions_per_industry
        self.max_entries = policy.max_entries_per_day
        self.max_stopouts = policy.max_stopouts_per_day
        self.min_pos = policy.min_position_pct
        self.open_risk = policy.global_open_risk_pct
        self.weight = min(policy.base_weight["S2"], Decimal(1))  # the sleeve's weight
        self.fills = spec.fills
        self.days = [d for d in data.days if spec.start <= d <= spec.end]
        self.all_days = list(data.days)
        self.day_no = {d: n for n, d in enumerate(self.all_days)}
        self.cash = spec.initial_equity
        self.positions: list[Held] = []  # type: ignore[assignment]
        self.equity = spec.initial_equity
        self.sold_today: set[tuple[date, str]] = set()
        self.out = EpOut()
        self.level, self.wins, self.losses = 0, 0, 0
        self.closed: list[tuple[tuple, str, bool]] = []  # (close order key, iid, win)
        self.results = {k: sorted(v, key=lambda r: r.public_at) for k, v in data.results.items()}

    # --- the ladder
    def max_open(self) -> int:
        return min(
            self.policy.max_open_positions, self.policy.ladder[LEVELS[self.level]].max_positions
        )

    def risk_mult(self) -> Decimal:
        return self.policy.ladder[LEVELS[self.level]].risk_mult

    def sync(self) -> None:
        for _, _, win in sorted(self.closed, key=lambda x: (x[0], x[1])):
            if win:
                self.wins, self.losses = self.wins + 1, 0
                if self.wins >= self.policy.up_after_wins and self.level < len(LEVELS) - 1:
                    self.level, self.wins, self.losses = self.level + 1, 0, 0
            else:
                self.losses, self.wins = self.losses + 1, 0
                if self.losses >= self.policy.down_after_losses and self.level > 0:
                    self.level, self.wins, self.losses = self.level - 1, 0, 0
        self.closed = []

    # --- money
    def sell(self, p: Held, day: date, price: Decimal, reason: str, order: tuple = ()) -> None:  # type: ignore[override]
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
        pnl = filled * p.qty - fees - p.cost + p.dividends
        self.closed.append((order or (9, day.isoformat()), p.iid, pnl > 0))

    # --- the day
    def run(self) -> EpOut:  # type: ignore[override]
        for day in self.days:
            prev = self._prev(day)
            self.stopouts = 0
            capital = (self.weight * self.equity).quantize(E4, ROUND_FLOOR)
            self.morning(day)
            self.sync()
            if prev is not None:
                found = self.scan(day, prev)
                self.ep_entries(day, prev, found, capital)
            self.close_phase(day)
            self.delistings(day)
            self.sync()
            self.equity = self.mark(day).quantize(E4, ROUND_HALF_EVEN)
            self.out.equity.append((day, self.equity, self.cash))
        for p in sorted(self.positions, key=lambda p: (p.entry_day, p.iid)):
            self.sell(p, self.days[-1], self.mark_price(p.iid, self.days[-1]), "end_of_period")
        self.out.final = self.cash
        return self.out

    def morning(self, day: date) -> None:
        for p in sorted(self.positions, key=lambda p: (p.entry_day, p.iid)):
            for ex, cash in self.data.dividends.get(p.iid, ()):
                if ex == day:
                    paid = (cash * p.qty).quantize(Decimal("0.01"), ROUND_HALF_EVEN)
                    self.cash += paid
                    p.dividends += paid
            i = self.bar(p.iid, day)
            if i is None or self.lower_locked(p.iid, day, i, whole_day=True):
                continue
            s = self.inst[p.iid]
            if s.o4[i] <= p.stop:
                self.sell(p, day, s.o4[i], "hard_stop", (0, p.iid))
            elif s.l4[i] <= p.stop:
                self.sell(p, day, min(s.o4[i], p.stop), "hard_stop", (0, p.iid))

    # --- the gap scan
    def universe(self, iid: str, prev: date) -> bool:
        if s1.series_on(self.data.series.get(iid, ()), prev) not in self.series_ok:
            return False
        s = self.inst[iid]
        i = s.at(prev)
        lo = s.seg[i]
        if i - lo + 1 < self.tv_days or i - lo + 1 < self.adr_days:
            return False
        tv = s.tv[i - self.tv_days + 1 : i + 1].copy()
        bad = np.isnan(tv)
        k = slice(i - self.tv_days + 1, i + 1)
        tv[bad] = (s.c[k] * s.div[k] / s.div[i] * s.vol[k])[bad]
        if np.isnan(tv).any() or sum(dec(x) for x in tv) < self.min_tv * self.tv_days:
            return False
        k = slice(i - self.adr_days + 1, i + 1)
        h, l = s.h[k] * s.div[k] / s.div[i], s.l[k] * s.div[k] / s.div[i]  # noqa: E741
        if np.isnan(h).any() or np.isnan(l).any() or (l <= 0).any():
            return False
        ranges = sum((100 * (dec(a) / dec(b) - 1) for a, b in zip(h, l, strict=True)), Decimal(0))
        return ranges >= self.adr_min * self.adr_days

    def scan(self, day: date, prev: date) -> list[str]:
        found = []
        for iid in sorted(self.inst):
            if not self.member(iid, day) or not ordinary_equity(self.data.isins.get(iid)):
                continue
            i = self.bar(iid, day)
            pc_ = self.prev_close(iid, day)
            if i is None or pc_ is None or pc_ <= 0:
                continue
            s = self.inst[iid]
            o = s.o4[i]
            if 100 * (Fraction(o) / Fraction(pc_) - 1) < Fraction(self.gap_min):
                continue
            j = s.at(prev)
            if j is None:
                self.out.candidates[(day, iid)] = Record(None, "neglect", "setup")
                continue
            if not self.universe(iid, prev):
                continue
            if j - s.seg[j] < HISTORY:
                self.out.candidates[(day, iid)] = Record(None, "neglect", "neglect")
                continue
            if (iid, day) in self.data.pending or (iid, day) in self.data.demerger_exits:
                self.out.candidates[(day, iid)] = Record(None, "earnings_catalyst", "not_open")
                continue
            self.out.candidates[(day, iid)] = Record()
            found.append(iid)
        return found

    # --- the trigger
    def filing(self, iid: str, since: datetime, at: datetime) -> Any:  # noqa: ANN401
        found = [r for r in self.results.get(iid, ()) if since < r.public_at <= at]
        return max(found, key=lambda r: (r.public_at, r.period_end), default=None)

    def earnings(self, iid: str, prev: date, at: datetime) -> tuple[bool, str]:
        event = self.filing(iid, at_ist(prev, OPEN), at)
        if event is None:
            return False, "no_results_filing"
        passed = growth_passes(event, self.earn_params)
        if passed is None:
            return False, "growth_undefined"
        return bool(passed), "growth_below_min"

    def session5(self, iid: str, day: date, i: int) -> list[Bar5] | None:
        s = self.inst[iid]
        raw = Decimal(repr(float(s.raw[i]))).quantize(E4)
        if not raw:
            return None
        scale = s.c4[i] / raw
        rows = self.bars5.get(iid)
        if not rows or scale <= 0:
            return None
        out = []
        for r in rows:

            def px(v: Any) -> Decimal:  # noqa: ANN401
                return (Decimal(str(v)) * scale).quantize(E4, ROUND_HALF_EVEN)

            vol = int((Decimal(str(r.volume)) / scale).to_integral_value(ROUND_HALF_EVEN))
            start = r.bar_start.astimezone(IST)
            out.append(Bar5(start, px(r.open), px(r.high), px(r.low), px(r.close), vol))
        out.sort(key=lambda b: b.start)
        opens, closes = at_ist(day, OPEN), at_ist(day, CLOSE)
        starts = {b.start for b in out}
        if any(not opens <= b.start < closes or (b.start - opens) % BAR for b in out):
            return None
        if len(starts) != len(out) or len(starts) != (closes - opens) // BAR:
            return None
        if abs(out[0].o / s.o4[i] - 1) * 100 > OPEN_OFF_PCT:
            return None
        return out

    def band_locked(self, iid: str, day: date, price: Decimal, high: Decimal) -> bool | None:
        """The 5-minute rule: the band is the narrowest whose upper price is within a tick of
        the day's high; at or above its upper price is locked."""
        prev, tick = self.prev_close(iid, day), self.tick(iid, day)
        if prev is None or tick is None or high <= prev:
            return False if prev is not None and tick is not None else None
        for band in s1.CIRCUIT_BANDS:
            up = floor_tick(prev * (100 + band) / 100, tick)
            if up > prev and abs(high - up) <= tick:
                return price >= up
        return False

    def filters(
        self,
        entry: Decimal,
        stop: Decimal,
        level: Decimal,
        locked: bool | None,
        adtr: Any,  # noqa: ANN401
    ) -> str | None:
        if adtr is None or stop >= entry or entry - stop > self.adtr_max * dec(adtr).quantize(E4):
            return "adtr_filter"
        if locked is None or locked or entry * 100 > level * (100 + self.chase):
            return "max_chase"
        return None

    def on_bars(self, iid: str, day: date, prev: date, i: int, bars: list[Bar5]) -> Plan | None:
        rec = self.out.candidates[(day, iid)]
        rec.basis = BARS_5M
        s = self.inst[iid]
        f = self.feat.get((iid, prev)) or {}
        adv = f.get("adv20")
        adv = None if adv is None else dec(adv).quantize(E4)
        orh = bars[0].h
        gap_ok = 100 * (Fraction(s.o4[i]) / Fraction(self.prev_close(iid, day)) - 1) >= Fraction(
            self.gap_min
        )
        cum = 0
        best: tuple[int, str | None, str | None] = (-1, None, None)
        fired = None
        for n, b in enumerate(bars):
            cum += b.v
            fin = b.final
            if not at_ist(day, FIRST_FINAL) <= fin <= at_ist(day, LAST_FINAL):
                continue
            earn_ok, earn_why = self.earnings(iid, prev, fin)
            checks = {
                "gap_up": (gap_ok, "gap_too_small"),
                "earnings_catalyst": (earn_ok, earn_why),
                "early_volume": (
                    adv is not None and cum >= self.early * adv,
                    "no_data" if adv is None else "early_volume_low",
                ),
                "price_breaks_level": (b.h > orh and b.c >= orh, "below_level"),
            }
            depth = 0
            for g in GATES:
                if not checks[g][0]:
                    break
                depth += 1
            if depth > best[0] or depth == best[0] == 0:
                gate = GATES[depth] if depth < len(GATES) else None
                best = (depth, gate, None if gate is None else checks[gate][1])
            if depth == len(GATES):
                fired = n
                break
        if fired is None:
            rec.failed_gate, rec.reason = best[1], best[2]
            if best[0] < 0:
                rec.failed_gate, rec.reason = GATES[0], None
            return None
        b = bars[fired]
        tick = self.tick(iid, day)
        if tick is None:
            rec.failed_gate, rec.reason = "adtr_filter", "stop_input_missing"
            return None
        entry = ceil_tick(max(orh, b.c), tick)
        low = min(x.l for x in bars[: fired + 1])
        stop = floor_tick(low - self.buffer_ticks * tick, tick)
        if not 0 < stop < entry:
            rec.failed_gate, rec.reason = "adtr_filter", "stop_not_below_entry"
            return None
        locked = self.band_locked(iid, day, entry, s.h4[i])
        skip = self.filters(entry, stop, orh, locked, f.get("adtr20"))
        if skip is not None:
            rec.failed_gate, rec.reason = skip, skip
        later = bars[fired + 1 :]
        traded = min((x.l for x in later), default=None)
        if skip is None and (traded is None or traded > entry):
            self.out.skips["limit_not_reached"] += 1
            return None
        lows = [x.l for x in later]
        if s.l4[i] < min(x.l for x in bars):
            lows.append(s.l4[i])
        dip = min(lows, default=None)
        local = b.final
        seconds = local.hour * 3600 + local.minute * 60 + local.second
        return Plan(iid, BARS_5M, entry, stop, dip, skip, seconds, b.final)

    def estimate(self, iid: str, day: date, prev: date, i: int) -> Plan | None:
        rec = self.out.candidates[(day, iid)]
        rec.basis = "daily"
        s = self.inst[iid]
        tick, prev_close = self.tick(iid, day), self.prev_close(iid, day)
        if tick is None or prev_close is None:
            self.out.skips["stop_input_missing"] += 1
            rec.basis, rec.failed_gate, rec.reason = None, "earnings_catalyst", "stop_input_missing"
            return None
        o, h, l, c = s.o4[i], s.h4[i], s.l4[i], s.c4[i]  # noqa: E741
        f = self.feat.get((iid, prev)) or {}
        adv = f.get("adv20")
        adv = None if adv is None else dec(adv).quantize(E4)
        vol = s.vol[i]
        vol = None if np.isnan(vol) else int(vol)
        earn_ok, earn_why = self.earnings(iid, prev, at_ist(day, EST_AT))
        fires = h * 100 >= o * (100 + EST_FIRE) and c * 2 >= h + l
        checks = {
            "gap_up": (
                100 * (Fraction(o) / Fraction(prev_close) - 1) >= Fraction(self.gap_min),
                "gap_too_small",
            ),
            "earnings_catalyst": (earn_ok, earn_why),
            "early_volume": (
                adv is not None and vol is not None and vol >= self.early * adv,
                "no_data" if adv is None or vol is None else "early_volume_low",
            ),
            "price_breaks_level": (fires, "estimate_not_fired"),
        }
        for g in GATES:
            if not checks[g][0]:
                rec.failed_gate, rec.reason = g, checks[g][1]
                return None
        price = ceil_tick(max(min(h, o * (100 + EST_ENTRY) / 100), o), tick)
        if price > h:
            rec.failed_gate, rec.reason = "adtr_filter", "limit_above_high"
            return None
        level = price * 100 / (100 + EST_CHASE)
        before = max(l, o * (100 - EST_LOW) / 100)
        stop = floor_tick(before - self.buffer_ticks * tick, tick)
        if not 0 < stop < price:
            rec.failed_gate, rec.reason = "adtr_filter", "stop_not_below_entry"
            return None
        locked = self.upper_locked(iid, day, price, i)
        skip = self.filters(price, stop, level, locked, f.get("adtr20"))
        if skip is not None:
            rec.failed_gate, rec.reason = skip, skip
        dip = l if l < before else None
        return Plan(
            iid, "daily", price, stop, dip, skip, EST_AT.hour * 3600 + EST_AT.minute * 60, None
        )

    # --- entries
    def ep_entries(self, day: date, prev: date, found: list[str], capital: Decimal) -> None:
        self.bars5 = {}
        store = self.data.intraday
        if self.five_minute and found and store is not None and store.path(day).is_file():
            for r in store.read(day, found):
                self.bars5.setdefault(r.instrument_id, []).append(r)
        plans = []
        for iid in found:
            i = self.bar(iid, day)
            bars = self.session5(iid, day, i)
            plan = (
                self.on_bars(iid, day, prev, i, bars) if bars else self.estimate(iid, day, prev, i)
            )
            if plan is not None:
                plans.append(plan)
        plans.sort(key=lambda p: p.iid)
        plans.sort(key=lambda p: p.seconds)
        breadth = self.data.breadth.get(prev)  # breadth_above over one session
        regime_on = breadth is not None and dec(breadth) >= self.breadth_min
        nxt = self._next(day)
        entered = 0
        for n, p in enumerate(plans):
            at = (
                p.at + timedelta(microseconds=n)
                if p.at is not None
                else at_ist(day, ENTRY_AT) + timedelta(microseconds=n)
            )
            rec = self.out.candidates[(day, p.iid)]
            if p.skip is not None:
                self.signal(day, p, "Rejected", p.skip, 0)
                rec.state = "Rejected"
                continue
            reason = None
            if not regime_on:
                reason = "regime_off"
            if reason is None and nxt is not None and not self.member(p.iid, nxt):
                self.out.skips["not_in_universe"] += 1
                continue
            industry = s1.industry_on(self.data, p.iid, day)
            if reason is not None:
                pass
            elif len(self.positions) >= self.max_open():
                reason = "max_positions"
            elif (
                sum(1 for q in self.positions if s1.industry_on(self.data, q.iid, day) == industry)
                >= self.max_industry
            ):
                reason = "industry_cap"
            elif entered >= self.max_entries:
                reason = "entry_cap"
            elif self.stopouts >= self.max_stopouts:
                reason = "stopout_cap"
            adv = (self.feat.get((p.iid, prev)) or {}).get("adv20")
            if reason is None and adv is None:
                reason = "missing_input"
            qty = 0
            price, stop = p.price, p.stop
            if reason is None:
                adv20 = dec(adv).quantize(E4)
                free = Decimal(self.fills.affordable(self.cash, price, day)) * price
                risk_cap = capital * self.risk_pct * self.risk_mult()
                terms = [
                    (risk_cap / (price - stop), "risk"),
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
                        (max(x.entry - x.stop, Decimal(0)) * x.qty for x in self.positions),
                        Decimal(0),
                    )
                    if risk + (price - stop) * qty > self.open_risk * capital:
                        reason = "global_open_risk"
            if reason is not None:
                self.signal(day, p, "Rejected", reason, 0)
                rec.state, rec.failed_gate, rec.reason = "Rejected", "filled", reason
                continue
            limit = price if p.basis == BARS_5M else None
            filled, fees = self.fills.fill("buy", qty, price, day, limit=limit)
            self.cash -= filled * qty + fees
            held = Held(p.iid, day, qty, filled, price, stop, p.basis, filled * qty + fees)
            self.positions.append(held)
            self.out.fills.append((day, p.iid, "buy", qty, filled, fees))
            self.signal(day, p, "Open", None, qty)
            rec.state = "Open"
            entered += 1
            if p.dip is not None and p.dip < price and p.dip <= stop:
                when = at + timedelta(seconds=1)
                self.sell(held, day, min(price, stop), "hard_stop", (1, when.isoformat()))

    def signal(self, day: date, p: Plan, state: str, reason: str | None, qty: int) -> None:  # type: ignore[override]
        self.out.signals.append((day, p.iid, state, reason, p.price, p.stop, qty))

    # --- the close
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
                self.sell(p, day, p.stop, "hard_stop", (2, p.iid))
                continue
            held = self.day_no[day] - self.day_no[p.entry_day]
            lo = s.seg[i]
            tr = s.c * s.div
            if i - lo >= self.sma_n - 1 and (self.grace is None or held > self.grace):
                sma = sum((dec(x) for x in tr[i - self.sma_n + 1 : i] / s.div[i]), Decimal(0))
                if close * self.sma_n < sma + close:
                    self.sell(p, day, close, "close_below_sma", (3, p.iid))
                    continue
            if held >= self.max_hold:
                self.sell(p, day, close, "max_hold", (3, p.iid))

    def delistings(self, day: date) -> None:
        for p in sorted(self.positions, key=lambda p: (p.entry_day, p.iid)):
            # a demerger exit sells at the close, not with no bar or locked at the lower band
            if (p.iid, day) not in self.data.demerger_exits:
                continue
            i = self.bar(p.iid, day)
            if i is not None and not self.lower_locked(p.iid, day, i, whole_day=True):
                self.sell(p, day, self.inst[p.iid].c4[i], "demerger", (4, p.iid))
        for p in sorted(self.positions, key=lambda p: (p.entry_day, p.iid)):
            if self.data.last_traded.get(p.iid) == day:
                self.sell(p, day, self.mark_price(p.iid, day), "delisted", (4, p.iid))


# ---------------------------------------------------------------------------------------------
# the comparison


def engine_candidates(result: BacktestResult) -> dict[tuple[date, str], Record]:
    out = {}
    for r in result.ep_candidates:
        day = r["trade_date"]
        day = day if isinstance(day, date) else date.fromisoformat(str(day))
        out[(day, str(r["instrument_id"]))] = Record(
            r.get("basis") or None,
            r.get("failed_gate") or None,
            r.get("reason") or None,
            r.get("signal_state") or None,
        )
    return out


def run_and_compare(spec: RunSpec, data: MarketData, result: BacktestResult) -> dict[str, Any]:
    book = EpBook(data, spec)
    ours = book.run()
    engine = s1.engine_rows(result)
    report = s1.compare(engine, ours)
    eng_c, our_c = engine_candidates(result), ours.candidates

    def sym(key: tuple[date, str]) -> str:
        return f"{key[0]} {symbol_on(data, key[1], key[0])}"

    only_e = sorted(set(eng_c) - set(our_c))
    only_p = sorted(set(our_c) - set(eng_c))
    both = sorted(set(eng_c) & set(our_c))
    differ = [
        (sym(k), vars(eng_c[k]), vars(our_c[k]))
        for k in both
        if (eng_c[k].basis, eng_c[k].failed_gate, eng_c[k].reason, eng_c[k].state)
        != (our_c[k].basis, our_c[k].failed_gate, our_c[k].reason, our_c[k].state)
    ]
    by_year = Counter(k[0].year for k in eng_c)
    report["candidates"] = {
        "engine": len(eng_c),
        "parity": len(our_c),
        "engine_by_year": dict(sorted(by_year.items())),
        "only_engine": [sym(k) for k in only_e],
        "only_parity": [sym(k) for k in only_p],
        "fields_differ": differ,
    }
    report["funnel_engine"] = {
        f"{g}/{r}": n
        for (g, r), n in Counter((r.failed_gate, r.reason) for r in eng_c.values()).items()
    }
    report["funnel_parity"] = {
        f"{g}/{r}": n
        for (g, r), n in Counter((r.failed_gate, r.reason) for r in our_c.values()).items()
    }
    report["parity_skips"] = dict(ours.skips)
    return report

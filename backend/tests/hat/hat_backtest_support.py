"""Synthetic markets and run specs for the backtest properties (09, PR 12).

A market is a few instruments with daily OHLC paths on weekday sessions, the features
`app.hat.data.features.compute_features` derives from them, a regime index and point-in-time
membership. The configs are deliberately permissive (a consolidation pivot, a percent stop) so a
short generated history makes trades; the seeds S1 and S2 run on the committed golden fixture.
"""

from __future__ import annotations

import copy
import csv
import io
import json
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import polars as pl
from hypothesis import strategies as st

from app.hat.backtest.costs import BacktestFills, load_cost_model, parse_cost_model
from app.hat.data.features import compute_features
from app.hat.engine.backtest import MarketData, RunSpec, run_backtest
from app.hat.engine.portfolio.universe import Interval, members_on
from app.hat.engine.scan import ScanInputs, WatchRow, scan_bot
from app.hat.engine.strategy.config import StrategyConfig, parse_config
from app.hat.engine.strategy.versions import seed_config
from app.hat.risk.policy import PolicyVersion, seed_policy

__all__ = [
    "BOT",
    "FIRST_DAY",
    "GOLDEN_SEEDS",
    "INDEX",
    "VERSION",
    "WARM_UP",
    "PricePath",
    "config",
    "fixture_market",
    "golden_document",
    "instrument",
    "market",
    "paths",
    "policy_version",
    "sessions",
    "scan_watch",
    "spec",
    "zero_fills",
]

D = Decimal
FIRST_DAY = date(2025, 1, 6)  # a Monday
INDEX = "NIFTY 500"
BOT = "stage2-contraction-breakout"  # a strategy key the seed policy has a p_max for
VERSION = uuid.UUID("00000000-0000-7000-8000-0000000000b1")
POLICY = uuid.UUID("00000000-0000-7000-8000-0000000000b2")
VOLUME = 1_000_000.0
ZERO = D(0)
# (open, high, low, close) per session, optionally with the session's volume
PricePath = list[tuple[float, ...]]

_EXITS: list[dict[str, Any]] = [
    {"rule": "hard_stop"},
    {"rule": "move_stop_to_breakeven", "at_r": 1},
    {"rule": "partial_at_r", "at_r": 2, "sell_fraction": 0.5},
    {"rule": "pct_gain_target", "pct": 20, "sell_fraction": 1.0},
    {"rule": "close_below_sma", "sma": 5, "sell_fraction": 1.0},
    {"rule": "time_stop", "days": 8, "min_r": 0.5},
]


def instrument(n: int) -> str:
    return str(uuid.UUID(int=0x1000 + n))


def sessions(count: int, start: date = FIRST_DAY) -> list[date]:
    days: list[date] = []
    day = start
    while len(days) < count:
        if day.weekday() < 5:
            days.append(day)
        day += timedelta(days=1)
    return days


def config(
    *,
    stop: Mapping[str, Any] | None = None,
    exits: Sequence[Mapping[str, Any]] | None = None,
    chase: float = 5.0,
    regime_sma: int = 3,
    adtr: float | None = None,
) -> StrategyConfig:
    """A one-playbook config whose pivot is a short consolidation's high; `adtr` adds an
    `adtr_filter` of that multiple before the max chase."""
    filters: list[dict[str, Any]] = [{"type": "max_chase", "params": {"pct": chase}}]
    if adtr is not None:
        filters.insert(0, {"type": "adtr_filter", "params": {"max_multiple": adtr}})
    raw: dict[str, Any] = {
        "bot": BOT,
        "universe": [{"type": "series_in", "params": {"series": ["EQ"]}}],
        "regime": {
            "index": INDEX,
            "rule": "close_above_sma_and_rising",
            "params": {"sma": regime_sma, "rising_days": 1},
        },
        "setup": {
            "threshold": 0.0,
            "criteria": [
                {
                    "type": "consolidation",
                    "mode": "score",
                    "weight": 1,
                    "params": {"days": [3, 5], "max_range_pct": 30, "higher_lows": False},
                }
            ],
        },
        "trigger": [{"type": "price_breaks_level", "params": {"level": "pivot"}}],
        "entry_filters": filters,
        "stop": dict(stop or {"rule": "pct_below_entry", "pct": 5}),
        "exits": copy.deepcopy(list(exits or _EXITS)),
    }
    return parse_config(raw)


def policy_version() -> PolicyVersion:
    return PolicyVersion(
        version_id=POLICY,
        version=1,
        effective_from=datetime(2020, 1, 1, tzinfo=UTC),
        superseded_at=None,
        policy=seed_policy(),
    )


FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_SEEDS = ("stage2-contraction-breakout", "momentum-breakout-ep")  # S1, S2
GOLDEN_SESSIONS = 100  # the replay runs the fixture's last 100 sessions

# adv20 (the liquidity cap's input) needs 20 sessions, so a run starts after them
WARM_UP = 21


def zero_fills() -> BacktestFills:
    """No slippage and no charges: fills at the proxy prices exactly."""
    model = parse_cost_model({"version": "zero", "gst_rate": "0", "components": []})
    return BacktestFills(model, ZERO)


def spec(
    cfg: StrategyConfig,
    days: Sequence[date],
    *,
    slippage: Decimal = ZERO,
    start: int = WARM_UP,
    fills: BacktestFills | None = None,
) -> RunSpec:
    return RunSpec(
        config=cfg,
        strategy_version_id=VERSION,
        policy=policy_version(),
        start=days[min(start, len(days) - 1)],
        end=days[-1],
        fills=fills or BacktestFills(load_cost_model(), slippage),
    )


def scan_watch(cfg: StrategyConfig, data: MarketData, as_of: date) -> tuple[WatchRow, ...]:
    """The live nightly scan of `as_of`, built directly from the market (not by the loop)."""
    closes = [c for d, c in data.index_closes[cfg.regime.index] if d <= as_of]
    features = data.features.filter(pl.col("date") == as_of)
    inputs = ScanInputs(
        as_of=as_of,
        members=sorted(str(i) for i in members_on(data.members, as_of)),
        bars=data.bars.filter(pl.col("date") <= as_of),
        regime_closes=closes,
        features=features,
        series=dict(data.series),
    )
    return scan_bot(cfg, inputs).rows


def _frame(paths_: Mapping[str, PricePath], days: Sequence[date]) -> pl.DataFrame:
    rows = []
    for instrument_id, path in sorted(paths_.items()):
        previous = None
        for day, bar in zip(days, path, strict=False):
            o, h, low, c = bar[:4]
            volume = bar[4] if len(bar) > 4 else VOLUME
            rows.append((instrument_id, day, o, h, low, c, previous, volume, c * volume))
            previous = c
    return pl.DataFrame(
        rows,
        schema={
            "instrument_id": pl.String,
            "date": pl.Date,
            "open": pl.Float64,
            "high": pl.Float64,
            "low": pl.Float64,
            "close": pl.Float64,
            "prev_close": pl.Float64,
            "volume": pl.Float64,
            "traded_value": pl.Float64,
        },
        orient="row",
    )


def market(
    paths_: Mapping[str, PricePath],
    days: Sequence[date],
    *,
    index: Sequence[float] | None = None,
    members: Sequence[Interval] | None = None,
    last_traded: Mapping[str, date] | None = None,
) -> MarketData:
    """The market of `paths_` (an instrument's path may be shorter than `days`: it stops
    trading). Every instrument is a member from the first session unless `members` says
    otherwise; the index rises steadily unless given."""
    bars = _frame(paths_, days)
    features, _ = compute_features(bars)
    closes = index if index is not None else [1000.0 + n for n in range(len(days))]
    return MarketData(
        days=tuple(days),
        bars=bars,
        features=features,
        index_closes={INDEX: [(d, D(repr(c))) for d, c in zip(days, closes, strict=False)]},
        members=tuple(
            members
            if members is not None
            else (Interval(uuid.UUID(i), days[0]) for i in sorted(paths_))
        ),
        industries={i: f"industry-{n}" for n, i in enumerate(sorted(paths_))},
        series=dict.fromkeys(paths_, "EQ"),
        last_traded=dict(last_traded or {}),
    )


@st.composite
def paths(draw: st.DrawFn, count: int, *, instruments: int = 3) -> dict[str, PricePath]:
    """Random-walk paths on a whole-paise grid: each session moves the close by up to 6% and
    draws an open, high and low around it."""
    found: dict[str, PricePath] = {}
    for n in range(draw(st.integers(1, instruments))):
        price = float(draw(st.integers(80, 200)))
        path: PricePath = []
        for _ in range(count):
            move = draw(st.integers(-60, 60)) / 1000
            gap = draw(st.integers(-20, 30)) / 1000
            close = round(max(5.0, price * (1 + move)), 2)
            open_ = round(max(5.0, price * (1 + gap)), 2)
            up = draw(st.integers(0, 30)) / 1000
            down = draw(st.integers(0, 30)) / 1000
            high = round(max(open_, close) * (1 + up), 2)
            low = round(min(open_, close) * (1 - down), 2)
            path.append((open_, high, low, close))
            price = close
        found[instrument(n)] = path
    return found


def fixture_market(text: str) -> MarketData:
    """The committed golden-replay market (`fixtures/backtest_market.csv`): daily bars of its
    instruments and the NIFTY 500 closes (rows whose instrument is the index name), every
    instrument a member throughout."""
    paths_: dict[str, PricePath] = {}
    closes: list[float] = []
    days: list[date] = []
    for row in csv.DictReader(io.StringIO(text)):
        if row["instrument_id"] == INDEX:
            days.append(date.fromisoformat(row["date"]))
            closes.append(float(row["close"]))
            continue
        bar = tuple(float(row[k]) for k in ("open", "high", "low", "close", "volume"))
        paths_.setdefault(str(uuid.UUID(row["instrument_id"])), []).append(bar)
    return market(paths_, days, index=closes)


def golden_document() -> dict[str, Any]:
    """S1 and S2 over the last sessions of the committed fixture market: each seed's metrics,
    trades (as text), and signal and watchlist counts (HAT-BT-051)."""
    data = fixture_market((FIXTURES / "backtest_market.csv").read_text(encoding="utf-8"))
    found: dict[str, Any] = {}
    for name in GOLDEN_SEEDS:
        cfg = seed_config(name)
        result = run_backtest(spec(cfg, data.days, start=len(data.days) - GOLDEN_SESSIONS), data)
        found[name] = {
            "metrics": result.metrics,
            "trades": [
                {k: str(v) for k, v in vars(t).items() if k not in ("strategy_version_id",)}
                for t in result.trades
            ],
            "signals": len(result.signals),
            "watchlist_rows": len(result.watchlists),
        }
    return json.loads(json.dumps(found, sort_keys=True))


if __name__ == "__main__":  # regenerate the golden file after an intended change
    golden = json.dumps(golden_document(), indent=1, sort_keys=True) + "\n"
    (FIXTURES / "backtest_golden.json").write_text(golden, encoding="utf-8")

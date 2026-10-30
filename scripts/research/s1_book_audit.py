"""The S1 book detector audit (Phase 4) of one in-sample run: its entries, seeded near-misses of
the base geometry (spec C), a chart per case, and where deeper first contractions drop out.

    DB_NAME=hat_live PGOPTIONS="-c default_transaction_read_only=on" HAT_DATA_ROOT=<data> \
        venv/bin/python scripts/research/s1_book_audit.py [--run-id ID] [--seed N]

It replays the run's nightly scan without its day loop: the run's `config.json` (as
`--rerun` reads it), the market loaded over the run's period only (`load_market`, in-sample),
each night's universe, eligible RS rank and contexts as `scan.scan_bot` builds them, and the
setup as `evaluate_playbook` evaluates it. The replayed funnel must equal the run's `funnel`
(checked, the script stops otherwise). On top of it:

- entries: `min(20, n)` of the run's trades, drawn with `--seed`, with the base the scan saw;
- near-misses: stock-days from `--near-from` to `--near-to` on regime-on nights, in the bot's
  universe, passing the trend template's eight checks and failing exactly one rule of C, each
  rule evaluated on its own (`book_diagnostic`); 10 drawn with the same seed, one per symbol;
- Depth_1: the stock-days passing C (after the template and the RS rank, the funnel's order),
  by first-contraction bucket, through each later gate in order and failing each on its own.

A near-miss's outcome reads at most 60 sessions after its signal; `--near-to` keeps them in the
run's period. Writes `reports/<variant>/audit/` at the repo root (gitignored): `cases.csv`,
`audit.md` (seed, case table, pool sizes, Depth_1 tables, chart paths) and `charts/*.png`.
Needs matplotlib, which is not a backend requirement. Reads the database only.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, MutableMapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

import matplotlib  # noqa: E402

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import polars as pl  # noqa: E402
from sqlalchemy import text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.database import db_session_scope  # noqa: E402
from app.hat.backtest.data import MarketView, load_market, symbol_on  # noqa: E402
from app.hat.backtest.ledger import ledger, set_series  # noqa: E402
from app.hat.backtest.run_files import read_config, read_run  # noqa: E402
from app.hat.backtest.runs import _replayed  # noqa: E402
from app.hat.configs.records import Bot  # noqa: E402
from app.hat.configs.unit import configs_of  # noqa: E402
from app.hat.data.features import eligible_rs  # noqa: E402
from app.hat.data.identity import series_as_of  # noqa: E402
from app.hat.data.store import DataStore  # noqa: E402
from app.hat.engine import watchlist  # noqa: E402
from app.hat.engine.criteria import (  # noqa: E402
    PIVOT,
    EvalContext,
    book,  # noqa: E402
    evaluate,
)
from app.hat.engine.criteria.book_diagnostic import (  # noqa: E402
    GEOMETRY_RULES,
    GeometryCheck,
    LaterCheck,
    geometry_check,
    later_check,
)
from app.hat.engine.regime_gate import regime_allows_longs  # noqa: E402
from app.hat.engine.scan import ScanInputs, _contexts, _member_bars, _stopped_gate  # noqa: E402
from app.hat.engine.strategy.config import Instance, Playbook, StrategyConfig  # noqa: E402
from app.hat.engine.strategy.scoring import evaluate_playbook  # noqa: E402
from app.hat.models import HatEvent  # noqa: E402
from app.hat.settings import operator_settings  # noqa: E402
from app.paths import PROJECT_ROOT  # noqa: E402

DEFAULT_RUN = "01a17d8a-f6df-7099-af99-7147629ba1ec"
DEFAULT_SEED = 20261027
IN_SAMPLE_END = date(2021, 12, 31)
ENTRY_SAMPLE = 20
NEAR_MISSES = 10
BREAK_SESSIONS = 20
OUTCOME_SESSIONS = 60
CHART_LEAD = 20
STOP_BELOW = Decimal("0.995")  # spec Exits: the initial stop 0.5% below the final low
VOLUME_BASELINE = 50
BUCKETS = ((10, 15), (15, 20), (20, 25), (25, 35))
# the funnel's gates from the template on (`scan.funnel_gates`), in order
LATER_GATES = (
    "time_compression",
    "dry_up",
    "relative_correction",
    "base_count",
    "close_vs_pivot",
)
GATE_RULES = {
    "time_compression": ("tc_ratio", "tc_min_sessions"),
    "dry_up": ("dryup_mean", "dryup_session"),
    "relative_correction": ("relative_correction",),
    "base_count": ("base_count",),
    "close_vs_pivot": ("close_vs_pivot",),
}
PASSED_TEMPLATE = frozenset({None, "rs_rank", "vcp_geometry", *LATER_GATES, "setup"}) | frozenset(
    book.gate_of(r) for _, reasons in book.GATES for r in reasons
)


@dataclass
class Base:
    """One stock-day's base as the audit reads it."""

    night: date
    instrument_id: str
    symbol: str
    geo: GeometryCheck
    later: LaterCheck | None
    dates: list[date]  # the history's dates (the indices of `geo` point into them)
    close: float  # the signal-night close, on the history's scale
    rs_rank: float | None
    base_number: int | None
    close_vs_pivot: bool | None
    extras: Mapping[str, object] = field(default_factory=dict)
    low: Sequence[float] = ()  # the history's lows

    @property
    def pivot(self) -> Decimal:
        return self.geo.contractions[-1].high

    @property
    def stop(self) -> Decimal:
        return self.geo.contractions[-1].low * STOP_BELOW

    @property
    def depth1(self) -> Decimal:
        return self.geo.contractions[0].depth_pct


@dataclass
class Collected:
    funnel: Counter[str] = field(default_factory=Counter)
    regime_off: int = 0
    pool: list[tuple[date, str, str, str]] = field(default_factory=list)  # night, id, sym, rule
    pool_by_rule: Counter[str] = field(default_factory=Counter)
    geometry_passed: list[Base] = field(default_factory=list)
    # RS-passing stock-days with an H0, a contraction and Depth_1 in C's band: rules they fail
    pre_geometry: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    wanted: dict[tuple[date, str], Base] = field(default_factory=dict)


# --- small pure pieces ----------------------------------------------------------------------


def bucket_of(depth: Decimal | float) -> str | None:
    value = float(depth)
    for low, high in BUCKETS:
        if low <= value < high or (high == BUCKETS[-1][1] and value == high):
            return f"{low}-{high}"
    return None


def swing_flags(geo: GeometryCheck, low: Sequence[float], dates: Sequence[date]) -> list[str]:
    """Where the contractions and the bars disagree: a contraction whose lowest low is not on
    its swing-low bar, a base low (H0 to the final swing low) outside every contraction, and a
    low after the final swing low below it (a final contraction not yet confirmed)."""
    flags = []
    for n, c in enumerate(geo.contractions, start=1):
        bar = min(range(c.start, c.end + 1), key=lambda i: (low[i], i))
        if low[bar] < low[c.end]:
            flags.append(f"C{n} low {low[bar]:.2f} on {dates[bar]}, swing low {dates[c.end]}")
    if not geo.contractions:
        return flags
    inside = {i for c in geo.contractions for i in range(c.start, c.end + 1)}
    floor = min(float(c.low) for c in geo.contractions)
    last = geo.contractions[-1].end
    outside = [i for i in range(geo.h0, last + 1) if i not in inside and low[i] < floor]
    if outside:
        bar = min(outside, key=lambda i: low[i])
        flags.append(f"base low {low[bar]:.2f} on {dates[bar]} outside every contraction")
    under = [i for i in range(last + 1, len(dates)) if low[i] < low[last]]
    if under:
        flags.append(f"final swing low undercut on {dates[under[0]]}")
    return flags


def pick_near_misses(
    pool: Sequence[tuple[date, str, str, str]], seed: int, count: int
) -> list[tuple[date, str, str, str]]:
    """`count` of `pool` drawn with `seed`, one per symbol (the pool sorted first, so the draw
    does not depend on the scan's order)."""
    order = sorted(pool)
    random.Random(seed).shuffle(order)  # noqa: S311  # NOSONAR - a seeded, reproducible draw
    out, seen = [], set()
    for item in order:
        if item[2] in seen:
            continue
        seen.add(item[2])
        out.append(item)
        if len(out) == count:
            break
    return sorted(out)


def outcome_of(
    pivot: float, stop: float, forward: pl.DataFrame
) -> tuple[date | None, float | None, float | None]:
    """(first close above the pivot within `BREAK_SESSIONS`, MFE R, MAE R over
    `OUTCOME_SESSIONS`) of a would-be entry at the pivot against `stop`, from the sessions after
    the signal (`forward`, oldest first)."""
    risk = pivot - stop
    broke = forward.head(BREAK_SESSIONS).filter(pl.col("close") > pivot)
    first = broke["date"][0] if broke.height else None
    window = forward.head(OUTCOME_SESSIONS)
    if not window.height or risk <= 0:
        return first, None, None
    high = float(window["high"].max())  # type: ignore[arg-type]
    low = float(window["low"].min())  # type: ignore[arg-type]
    return first, (high - pivot) / risk, (low - pivot) / risk


# --- the replayed scan ----------------------------------------------------------------------


def _template(playbook: Playbook) -> list[Instance]:
    return [i for i in playbook.setup.criteria if i.enabled and i.type in TEMPLATE_TYPES]


TEMPLATE_TYPES = frozenset({"sma_above", "sma_rising", "pct_from_52w_low", "pct_from_52w_high"})


def _params(playbook: Playbook, kind: str) -> dict[str, object]:
    found = [i for i in playbook.setup.criteria if i.enabled and i.type == kind]
    if len(found) != 1:
        raise SystemExit(f"expected one enabled {kind} in the playbook, found {len(found)}")
    return dict(found[0].params)


def base_number(
    ctx: EvalContext,
    series: book.Series,
    threshold: Sequence[float],
    h0: int,
    p: book.BookVcpParams,
) -> int:
    """G as `book_vcp` computes it (1 + earlier broken bases since the count start)."""
    end = len(series.dates) - 1
    evaluable, passes, below = book.template_passes(series.high, series.low, series.close)
    start, _ = book.count_start(evaluable, passes, below, end, p.stage2_fail_sessions)
    if start is None:
        return 1
    bases = book._bases_since(ctx, series, threshold, start, end, p)
    current = series.dates[h0]
    return 1 + book.earlier_bases(bases, series.close, series.high, start, end, current)


def _base(
    ctx: EvalContext,
    symbol: str,
    p: book.BookVcpParams,
    cvp: Mapping[str, object],
    rs: float | None,
    *,
    full: bool,
) -> Base | None:
    """The stock-day's base by `book_diagnostic` (None without an H0); with `full` the later
    gates too (D, E, F, G and close_vs_pivot, each on its own)."""
    series = book.series_of(ctx)
    if series is None:
        return None
    end = len(series.dates) - 1
    threshold = book.swing_thresholds(series.high, series.low, series.close, p)
    geo = geometry_check(series.high, series.low, threshold, end, p)
    if geo is None:
        return None
    base = Base(
        ctx.as_of,
        ctx.instrument_id,
        symbol,
        geo,
        None,
        series.dates,
        series.close[-1],
        rs,
        None,
        None,
    )
    base.low = series.low
    if not full or not geo.contractions:
        return base
    closes = book._benchmark_closes(ctx, series.dates[geo.h0])
    base.later = later_check(geo, series.low, series.volume, end, book.index_depth_pct(closes), p)
    base.base_number = base_number(ctx, series, threshold, geo.h0, p)
    pivot = float(base.pivot)
    base.close_vs_pivot = evaluate("close_vs_pivot", cvp, ctx.with_levels({PIVOT: pivot})).passed
    return base


def _night_inputs(
    view: MarketView,
    data_series: Mapping[str, Sequence[tuple[date, str]]],
    benchmark: Sequence[tuple[date, float]],
    memo: MutableMapping[object, object],
    session: Session,
    bot: Bot,
    night: date,
) -> ScanInputs:
    """`loop._publish`'s scan inputs for `night`, with the bot's universe as members."""
    series = series_as_of(data_series, night)
    set_series(session, series)
    days = [d for d, _ in benchmark]
    members = watchlist.bot_universe_members(session, bot, night)
    return ScanInputs(
        as_of=night,
        members={str(m) for m in members},
        bars=view.window(night),
        regime_closes=view.closes_through(night),
        breadth=view.breadth_through(night),
        features=view.features.get(night),
        series=series,
        benchmark=benchmark[: bisect_right(days, night)],
        memo=memo,
    )


def scan_night(
    config: StrategyConfig,
    inputs: ScanInputs,
    symbol: Callable[[str, date], str],
    near: tuple[date, date],
    wanted: set[tuple[date, str]],
    out: Collected,
) -> None:
    """One night: the funnel as the scan counts it, and the audit's stock-days."""
    playbook = config.playbooks[0]
    p = book.BookVcpParams.model_validate(_params(playbook, "book_vcp"))
    cvp = _params(playbook, "close_vs_pivot")
    per = _member_bars(inputs, sorted(inputs.members))
    contexts = _contexts(config, inputs, per)
    features = inputs.features
    if features is not None:
        features = features.filter(pl.col("instrument_id").cast(pl.String).is_in(inputs.members))
        features = eligible_rs(features, [i for i, (_, u) in contexts.items() if u.passed])
    for ctx, _ in contexts.values():
        ctx.universe = features
    for instrument_id in sorted(contexts):
        ctx, universe = contexts[instrument_id]
        evaluation = evaluate_playbook(
            config, playbook.name, ctx, regime_allows_longs=True, universe=universe
        )
        stopped = None if evaluation.passed else _stopped_gate(evaluation)
        _count(out.funnel, stopped)
        if stopped in ("universe", "trend_template"):
            continue
        setup = evaluation.stages[-1]
        rs = next((c.result.value for c in setup.outcomes if c.type == "rs_rank"), None)
        name = symbol(instrument_id, inputs.as_of)
        key = (inputs.as_of, instrument_id)
        in_near = near[0] <= inputs.as_of <= near[1]
        rs_passed = stopped != "rs_rank"
        geometry_passed = rs_passed and stopped != "vcp_geometry"
        if not (in_near or rs_passed or key in wanted):
            continue
        full = geometry_passed or key in wanted
        base = _base(ctx, name, p, cvp, rs, full=full)
        if base is None:
            continue
        failed = base.geo.failed
        if in_near and len(failed) == 1:
            out.pool.append((inputs.as_of, instrument_id, name, failed[0]))
            out.pool_by_rule[failed[0]] += 1
        if rs_passed and base.geo.contractions:
            band = bucket_of(base.depth1)
            if band is not None and base.geo.rules["first_depth_band"]:
                out.pre_geometry[band]["stock_days"] += 1
                if not failed:
                    out.pre_geometry[band]["passed"] += 1
                for rule in failed:
                    out.pre_geometry[band][rule] += 1
        if rs_passed and stopped == "vcp_geometry" and not failed:
            raise SystemExit(f"{name} {inputs.as_of}: book_vcp failed C, diagnostic passed it")
        if geometry_passed:
            if failed:
                raise SystemExit(f"{name} {inputs.as_of}: book_vcp passed C, diagnostic {failed}")
            book_result = next(c.result for c in setup.outcomes if c.type == "book_vcp")
            base.extras = dict(book_result.extras)
            if book_result.passed and base.extras.get("base_number") != base.base_number:
                raise SystemExit(f"{name} {inputs.as_of}: base number differs")
            out.geometry_passed.append(base)
        if key in wanted:
            out.wanted[key] = base


FUNNEL = (
    "evaluated",
    "universe",
    "trend_template",
    "rs_rank",
    "vcp_geometry",
    "time_compression",
    "dry_up",
    "relative_correction",
    "base_count",
    "close_vs_pivot",
    "setup",
)


def _count(funnel: Counter[str], stopped: str | None) -> None:
    for gate in FUNNEL:
        if gate == stopped:
            return
        funnel[gate] += 1


# --- trades, outcomes, charts ---------------------------------------------------------------


@dataclass(frozen=True)
class Trade:
    trade_id: int
    symbol: str
    instrument_id: str
    signal: date
    entry: date
    exit: date
    r: float
    exit_reason: str
    breakout_vol_ratio: float | None
    columns: Mapping[str, str]


def read_trades(path: Path) -> list[Trade]:
    legs: dict[int, list[dict[str, str]]] = defaultdict(list)
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh):
            legs[int(row["trade_id"])].append(row)
    out = []
    for trade_id, rows in sorted(legs.items()):
        rows.sort(key=lambda r: int(r["leg"]))
        last = rows[-1]
        reasons = " + ".join(f"{r['engine_exit_reason']}" for r in rows)
        ratio = last["breakout_vol_ratio"]
        out.append(
            Trade(
                trade_id,
                last["symbol"],
                last["instrument_id"],
                date.fromisoformat(last["signal_date"]),
                date.fromisoformat(last["entry_date"]),
                date.fromisoformat(last["exit_date"]),
                sum(float(r["r_net"]) for r in rows),
                reasons,
                float(ratio) if ratio else None,
                last,
            )
        )
    return out


def _frame(view: MarketView, instrument_id: str, through: date) -> pl.DataFrame:
    """The instrument's bars of the scan window ending `through`, on that night's scale."""
    frame = view.window(through).filter(pl.col("instrument_id").cast(pl.String) == instrument_id)
    return frame.sort("date")


def _session_after(view: MarketView, day: date, sessions: int) -> date:
    at = view.index[day] + sessions
    return view.days[min(at, len(view.days) - 1)]


def volume_ratio(view: MarketView, instrument_id: str, day: date) -> float | None:
    """The day's volume over the mean of its 50 sessions before (`signal_features`)."""
    volumes = view.volumes_through(instrument_id, day, VOLUME_BASELINE + 1)
    if len(volumes) <= VOLUME_BASELINE or any(np.isnan(v) for v in volumes):
        return None
    base = sum(volumes[:-1]) / VOLUME_BASELINE
    return None if base <= 0 else volumes[-1] / base


def chart(
    path: Path,
    title: str,
    frame: pl.DataFrame,
    base: Base,
    start: date,
    end: date,
    marks: Sequence[tuple[date, str]],
) -> None:
    """Price with SMA50/150/200, H0, the swings numbered by contraction, pivot and stop, and
    volume with its 50-day average, over `start..end`. `frame` is on its last bar's
    total-return scale; prices are drawn on the signal night's scale, the one the base,
    pivot and stop (and the case table) use, so a dividend after the signal moves nothing."""
    signal_close = frame.filter(pl.col("date") == base.night)["close"][0]
    scale = signal_close / base.close
    closes = frame["close"].to_numpy() / scale
    volume = frame["volume"].to_numpy().astype(float)
    smas = {n: pl.Series(closes).rolling_mean(n).to_numpy() for n in (50, 150, 200)}
    avg50 = pl.Series(volume).rolling_mean(VOLUME_BASELINE).to_numpy()
    dates = frame["date"].to_list()
    keep = [i for i, d in enumerate(dates) if start <= d <= end]
    lo_i, hi_i = keep[0], keep[-1] + 1
    x = np.arange(hi_i - lo_i)
    at = {d: i - lo_i for i, d in enumerate(dates)}
    high, low = frame["high"].to_numpy() / scale, frame["low"].to_numpy() / scale

    fig, (ax, axv) = plt.subplots(
        2, 1, figsize=(15, 8.5), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    for i in x:
        j = i + lo_i
        ax.vlines(i, low[j], high[j], color="#555555", linewidth=0.8)
        ax.hlines(closes[j], i, i + 0.4, color="#555555", linewidth=0.8)
    for n, colour in ((50, "#1f77b4"), (150, "#ff7f0e"), (200, "#d62728")):
        ax.plot(x, smas[n][lo_i:hi_i], color=colour, linewidth=1, label=f"SMA{n}")
    h0 = base.dates[base.geo.h0]
    if h0 in at:
        ax.annotate(
            "H0",
            (at[h0], high[at[h0] + lo_i]),
            textcoords="offset points",
            xytext=(-16, 26),
            ha="center",
            color="purple",
            fontweight="bold",
        )
    for n, c in enumerate(base.geo.contractions, start=1):
        hi_day, lo_day = base.dates[c.start], base.dates[c.end]
        if hi_day in at:
            ax.plot(at[hi_day], high[at[hi_day] + lo_i], "v", color="green", markersize=8)
            ax.annotate(
                f"H{n}",
                (at[hi_day], high[at[hi_day] + lo_i]),
                textcoords="offset points",
                xytext=(0, 14),
                ha="center",
                color="green",
                fontsize=9,
            )
        if lo_day in at:
            ax.plot(at[lo_day], low[at[lo_day] + lo_i], "^", color="red", markersize=8)
            ax.annotate(
                f"L{n} {float(c.depth_pct):.1f}%",
                (at[lo_day], low[at[lo_day] + lo_i]),
                textcoords="offset points",
                xytext=(0, -18),
                ha="center",
                color="red",
                fontsize=9,
            )
    trailing = (
        [s for s in base.geo.swings[1:] if s.high and s.index > base.geo.contractions[-1].end]
        if base.geo.contractions
        else []
    )
    for s in trailing:
        day = base.dates[s.index]
        if day in at:
            ax.plot(at[day], high[at[day] + lo_i], "v", color="grey", markersize=7)
    pivot, stop = float(base.pivot), float(base.stop)
    span = (at.get(h0, 0), len(x) - 1)
    ax.hlines(pivot, *span, color="green", linestyle="--", linewidth=1, label=f"pivot {pivot:.2f}")
    ax.hlines(stop, *span, color="red", linestyle="--", linewidth=1, label=f"stop {stop:.2f}")
    for k, (day, label) in enumerate(((base.night, "signal"), *marks)):
        if day in at:
            ax.axvline(at[day], color="black", linestyle=":", linewidth=0.8)
            ax.annotate(
                label,
                (at[day], ax.get_ylim()[1]),
                textcoords="offset points",
                xytext=(2, -12 - 11 * k),
                fontsize=8,
            )
    ax.set_title(title, fontsize=10)
    ax.legend(loc="upper left", fontsize=8)
    ax.grid(alpha=0.2)
    axv.bar(x, volume[lo_i:hi_i], color="#999999", width=0.8)
    axv.plot(x, avg50[lo_i:hi_i], color="#1f77b4", linewidth=1, label="avg50")
    axv.legend(loc="upper left", fontsize=8)
    ticks = list(range(0, len(x), max(1, len(x) // 12)))
    axv.set_xticks(ticks, [dates[t + lo_i].isoformat() for t in ticks], rotation=30, fontsize=8)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


# --- the run --------------------------------------------------------------------------------


def _fmt(value: object, places: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, (float, Decimal)):
        return f"{float(value):.{places}f}"
    return str(value)


def case_row(case: str, base: Base, outcome: Mapping[str, object]) -> dict[str, str]:
    later = base.later
    geo = base.geo
    weeks_footprint = (
        book.footprint(len(base.dates) - geo.h0, list(geo.contractions)) if geo.contractions else ""
    )
    return {
        "case": case,
        "symbol": base.symbol,
        "signal_date": base.night.isoformat(),
        "failed_rule": ",".join(geo.failed) or "-",
        "footprint": weeks_footprint,
        "depths_pct": "/".join(f"{float(d):.2f}" for d in geo.depths),
        "pivot": _fmt(base.pivot),
        "stop": _fmt(base.stop),
        "dryup_mean": _fmt(later.dryup_mean_ratio if later else None, 3),
        "dryup_min": _fmt(later.dryup_min_ratio if later else None, 3),
        "breakout_vol_ratio": _fmt(outcome.get("breakout_vol_ratio")),
        "base_number": _fmt(base.base_number),
        "tc_low_sessions": _fmt(later.tc_low_sessions if later else None),
        "tc_after_sessions": _fmt(later.tc_after_sessions if later else None),
        "rc_ratio": _fmt(later.rc_ratio if later else None),
        "idx_depth_pct": _fmt(later.idx_depth_pct if later else None),
        "rs_rank": _fmt(base.rs_rank, 1),
        "swing_flags": "; ".join(swing_flags(geo, base.low, base.dates)) or "-",
        "outcome": str(outcome["text"]),
        "chart": str(outcome["chart"]),
    }


def markdown(rows: Sequence[Mapping[str, str]], columns: Sequence[str]) -> str:
    lines = ["| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(r.get(c, "") for c in columns) + " |" for r in rows]
    return "\n".join(lines)


def depth_tables(bases: Iterable[Base]) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    """(cumulative: per bucket the stock-days passing C then each later gate in order;
    independent: per bucket how many fail each later rule on its own)."""
    cumulative: dict[str, Counter[str]] = defaultdict(Counter)
    alone: dict[str, Counter[str]] = defaultdict(Counter)
    idx_depths: dict[str, list[float]] = defaultdict(list)
    for base in bases:
        band = bucket_of(base.depth1) or "out"
        later = base.later
        assert later is not None
        rules = dict(later.rules)
        rules["base_count"] = base.base_number is not None and base.base_number <= 3
        rules["close_vs_pivot"] = bool(base.close_vs_pivot)
        cumulative[band]["vcp_geometry"] += 1
        alive = True
        for gate in LATER_GATES:
            alive = alive and all(rules[r] for r in GATE_RULES[gate])
            cumulative[band][gate] += alive
        alone[band]["vcp_geometry"] += 1
        if later.idx_depth_pct is not None:
            idx_depths[band].append(float(later.idx_depth_pct))
        for rule, ok in rules.items():
            alone[band][rule] += not ok
    bands = [f"{a}-{b}" for a, b in BUCKETS]
    total = Counter()
    for band in bands:
        total.update(cumulative[band])
    cum_rows = [
        {"Depth_1": band, **{g: str(cumulative[band][g]) for g in ("vcp_geometry", *LATER_GATES)}}
        for band in bands
    ]
    cum_rows.append(
        {"Depth_1": "all", **{g: str(total[g]) for g in ("vcp_geometry", *LATER_GATES)}}
    )
    rule_names = [r for g in LATER_GATES for r in GATE_RULES[g]]
    alone_rows = []
    for band in bands:
        n = alone[band]["vcp_geometry"]
        row = {"Depth_1": band, "vcp_geometry": str(n)}
        for rule in rule_names:
            k = alone[band][rule]
            row[rule] = f"{k} ({100 * k / n:.0f}%)" if n else "0"
        found = sorted(idx_depths[band])
        row["idx_depth_median_max"] = (
            f"{found[len(found) // 2]:.2f} / {found[-1]:.2f}" if found else ""
        )
        alone_rows.append(row)
    return cum_rows, alone_rows


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", default=DEFAULT_RUN)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--near-from", type=date.fromisoformat, default=date(2016, 1, 1))
    parser.add_argument("--near-to", type=date.fromisoformat, default=date(2021, 9, 30))
    parser.add_argument(
        "--nights-to", type=date.fromisoformat, default=None, help="dev: stop early"
    )
    args = parser.parse_args(argv)
    print(f"seed {args.seed}")
    settings = operator_settings()
    root = settings.data_root
    run = read_run(root, args.run_id)
    config_doc = read_config(root, args.run_id)
    if run is None or config_doc is None:
        raise SystemExit(f"no run {args.run_id} under {root}")
    target, spec = _replayed(config_doc)
    if spec.end > IN_SAMPLE_END:
        raise SystemExit(f"the run ends {spec.end}, after the in-sample period")
    variant = run["identity"].get("variant") or args.run_id
    out_dir = PROJECT_ROOT / "reports" / variant / "audit"
    (out_dir / "charts").mkdir(parents=True, exist_ok=True)
    trades = read_trades(root / "backtests" / "runs" / args.run_id / "trades.csv")
    n_sample = min(ENTRY_SAMPLE, len(trades))
    sampled_ids = sorted(
        random.Random(args.seed).sample(  # noqa: S311  # NOSONAR - a seeded, reproducible draw
            [t.trade_id for t in trades], n_sample
        )
    )
    sampled = [t for t in trades if t.trade_id in sampled_ids]
    print(f"entries: {len(trades)} trades, {n_sample} sampled: {sampled_ids}")
    started = time.monotonic()
    with db_session_scope() as session:
        bind = {"mapper": HatEvent}
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments=bind)
        store = DataStore(root, memory_limit=settings.duckdb_memory)
        data, snapshot = load_market(
            store,
            session,
            universe_version_id=target.universe_version_id,
            index=spec.config.regime.index,
            start=spec.start,
            end=spec.end,
        )
    if snapshot.get("content_digest") != run["data"]["data_snapshot_id"]:
        raise SystemExit("the store's data is not the run's snapshot")
    if max(data.days) > IN_SAMPLE_END or data.bars["date"].max() > IN_SAMPLE_END:  # type: ignore[operator]
        raise SystemExit("loaded data after the in-sample period")
    print(f"market loaded in {time.monotonic() - started:.0f}s, snapshot matches the run")
    config = spec.config
    view = MarketView(data, config.regime.index)
    ledger_session, bot_id = ledger(spec, data)
    bot = configs_of(ledger_session).get(Bot, bot_id)
    assert bot is not None
    benchmark = [(d, float(c)) for d, c in data.index_closes.get("NIFTY 500", ())]
    memo: dict[object, object] = {}

    def symbol(instrument_id: str, day: date) -> str:
        return symbol_on(data, instrument_id, day)

    wanted = {(t.signal, t.instrument_id) for t in sampled}
    collected = Collected()
    period = [d for d in view.days if spec.start <= d <= spec.end]
    last = args.nights_to or spec.end
    for n, day in enumerate(period):
        night = view.prev(day)
        if night is None or night > last:
            continue
        inputs = _night_inputs(view, data.series, benchmark, memo, ledger_session, bot, night)
        if not regime_allows_longs(config.regime, inputs.regime_closes, inputs.breadth):
            collected.regime_off += 1
            continue
        scan_night(config, inputs, symbol, (args.near_from, args.near_to), wanted, collected)
        if n % 100 == 0:
            print(f"  {night} {time.monotonic() - started:.0f}s funnel {dict(collected.funnel)}")
    # the replay is the run's scan: its funnel must match
    run_funnel = run["funnel"]["by_playbook"]["default"]
    mismatch = {
        g: (collected.funnel[g], run_funnel.get(g))
        for g in FUNNEL
        if collected.funnel[g] != run_funnel.get(g)
    }
    if run["funnel"]["regime_off_nights"] != collected.regime_off:
        mismatch["regime_off_nights"] = (collected.regime_off, run["funnel"]["regime_off_nights"])
    funnel_note = "matches run.json funnel" if not mismatch else f"MISMATCH {mismatch}"
    print(f"funnel {funnel_note}")
    if mismatch and args.nights_to is None:
        raise SystemExit("the replayed scan is not the run's")

    # entries
    rows: list[dict[str, str]] = []
    for n, t in enumerate(sampled, start=1):
        base = collected.wanted.get((t.signal, t.instrument_id))
        if base is None:
            if args.nights_to is not None:
                continue
            raise SystemExit(f"trade {t.trade_id}: no base on {t.signal}")
        case = f"E{n:02d}"
        frame = _frame(view, t.instrument_id, t.exit)
        png = out_dir / "charts" / f"{case}-{t.symbol}-{t.signal}.png"
        title = (
            f"{case} {t.symbol} signal {t.signal} entry {t.entry} exit {t.exit}: "
            f"R {t.r:+.2f} ({t.exit_reason})"
        )
        start = base.dates[max(0, base.geo.h0 - CHART_LEAD)]
        chart(png, title, frame, base, start, t.exit, ((t.entry, "entry"), (t.exit, "exit")))
        outcome = {
            "text": f"R {t.r:+.3f}, {t.exit_reason}",
            "chart": str(png.relative_to(PROJECT_ROOT)),
            "breakout_vol_ratio": t.breakout_vol_ratio,
        }
        row = case_row(case, base, outcome)
        row["trade_id"] = str(t.trade_id)
        checks = {
            "footprint": t.columns["footprint"] == base.extras.get("footprint"),
            "depths": t.columns["contraction_depths"]
            == "/".join(f"{float(d):.2f}" for d in base.geo.depths),
            "base_number": float(t.columns["base_number"]) == base.base_number,
        }
        row["matches_trades_csv"] = "yes" if all(checks.values()) else f"NO {checks}"
        rows.append(row)

    # near-misses
    picked = pick_near_misses(collected.pool, args.seed, NEAR_MISSES)
    print(f"near-miss pool {len(collected.pool)}: {dict(sorted(collected.pool_by_rule.items()))}")
    p = book.BookVcpParams.model_validate(_params(config.playbooks[0], "book_vcp"))
    cvp = _params(config.playbooks[0], "close_vs_pivot")
    for n, (night, instrument_id, name, rule) in enumerate(picked, start=1):
        case = f"N{n:02d}"
        inputs = _night_inputs(view, data.series, benchmark, memo, ledger_session, bot, night)
        per = _member_bars(inputs, [instrument_id])
        ctx, _ = _contexts(config, inputs, per)[instrument_id]
        features = eligible_rs(
            inputs.features.filter(pl.col("instrument_id").cast(pl.String).is_in(inputs.members)),  # type: ignore[union-attr]
            [
                i
                for i, (_, u) in _contexts(
                    config, inputs, _member_bars(inputs, sorted(inputs.members))
                ).items()
                if u.passed
            ],
        )
        ctx.universe = features
        rs = evaluate("rs_rank", _params(config.playbooks[0], "rs_rank"), ctx).value
        base = _base(ctx, name, p, cvp, rs, full=True)
        if base is None or base.geo.failed != (rule,):
            raise SystemExit(f"{case}: the replayed base differs from the pool")
        end_day = _session_after(view, night, OUTCOME_SESSIONS)
        if end_day > IN_SAMPLE_END:
            raise SystemExit(f"{case}: the outcome window leaves the in-sample period")
        frame = _frame(view, instrument_id, end_day)
        scale = frame.filter(pl.col("date") == night)["close"][0] / base.close
        forward = frame.filter(pl.col("date") > night)
        broke, mfe, mae = outcome_of(float(base.pivot) * scale, float(base.stop) * scale, forward)
        ratio = None if broke is None else volume_ratio(view, instrument_id, broke)
        text_out = (
            f"{'broke ' + broke.isoformat() if broke else 'no close above pivot'} in "
            f"{BREAK_SESSIONS}; {OUTCOME_SESSIONS}-session MFE {_fmt(mfe)}R MAE {_fmt(mae)}R"
        )
        png = out_dir / "charts" / f"{case}-{name}-{night}.png"
        title = f"{case} {name} {night} near-miss: fails {rule}; {text_out}"
        start = base.dates[max(0, base.geo.h0 - CHART_LEAD)]
        marks = ((broke, "close > pivot"),) if broke else ()
        last_day = (
            forward["date"][min(OUTCOME_SESSIONS, forward.height) - 1] if forward.height else night
        )
        chart(png, title, frame, base, start, last_day, marks)
        outcome = {
            "text": text_out,
            "chart": str(png.relative_to(PROJECT_ROOT)),
            "breakout_vol_ratio": ratio,
        }
        rows.append(case_row(case, base, outcome))

    cum_rows, alone_rows = depth_tables(collected.geometry_passed)
    pre_rows = []
    for a, b in BUCKETS:
        band = f"{a}-{b}"
        c = collected.pre_geometry[band]
        pre_rows.append(
            {
                "Depth_1": band,
                "stock_days": str(c["stock_days"]),
                "pass_C": str(c["passed"]),
                **{r: str(c[r]) for r in GEOMETRY_RULES if r != "first_depth_band"},
            }
        )

    columns = [
        "case",
        "symbol",
        "signal_date",
        "failed_rule",
        "footprint",
        "depths_pct",
        "pivot",
        "stop",
        "dryup_mean",
        "dryup_min",
        "breakout_vol_ratio",
        "base_number",
        "tc_low_sessions",
        "tc_after_sessions",
        "rc_ratio",
        "idx_depth_pct",
        "rs_rank",
        "swing_flags",
        "outcome",
        "chart",
    ]
    with (out_dir / "cases.csv").open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=[*columns, "trade_id", "matches_trades_csv"])
        writer.writeheader()
        writer.writerows(rows)
    entry_r = [t.r for t in trades]
    report = [
        f"# S1 book detector audit: {variant} (run {args.run_id})",
        "",
        f"seed {args.seed}; entries {n_sample} of {len(trades)} sampled: trade ids {sampled_ids}",
        f"entries mean R {_fmt(sum(entry_r) / len(entry_r) if entry_r else None, 4)} "
        f"(run.json expectancy {run['metrics'].get('expectancy_r')})",
        f"replayed funnel: {funnel_note}; regime-off nights {collected.regime_off}",
        f"near-miss window {args.near_from}..{args.near_to}; pool {len(collected.pool)}",
        "",
        "## Near-miss pool by failing rule",
        "",
        markdown(
            [{"rule": r, "stock_days": str(collected.pool_by_rule[r])} for r in GEOMETRY_RULES],
            ["rule", "stock_days"],
        ),
        "",
        "## Cases",
        "",
        markdown(rows, columns),
        "",
        "Entries checked against trades.csv (footprint, depths, base number): "
        + ", ".join(
            f"{r['case']} {r['matches_trades_csv']}" for r in rows if "matches_trades_csv" in r
        ),
        "",
        "## Depth_1 through the later gates (cumulative, funnel order)",
        "",
        markdown(cum_rows, ["Depth_1", "vcp_geometry", *LATER_GATES]),
        "",
        "## Depth_1: stock-days passing C that fail each later rule on its own",
        "",
        markdown(
            alone_rows,
            [
                "Depth_1",
                "vcp_geometry",
                *[r for g in LATER_GATES for r in GATE_RULES[g]],
                "idx_depth_median_max",
            ],
        ),
        "",
        "## Depth_1 before C: RS-passing stock-days whose Depth_1 is in C's band, rules failed",
        "",
        markdown(
            pre_rows,
            [
                "Depth_1",
                "stock_days",
                "pass_C",
                *[r for r in GEOMETRY_RULES if r != "first_depth_band"],
            ],
        ),
        "",
    ]
    (out_dir / "audit.md").write_text("\n".join(report))
    (out_dir / "summary.json").write_text(
        json.dumps(
            {
                "seed": args.seed,
                "run_id": args.run_id,
                "funnel": dict(collected.funnel),
                "regime_off": collected.regime_off,
                "pool_by_rule": dict(collected.pool_by_rule),
                "near_misses": [list(map(str, x)) for x in picked],
            },
            indent=1,
        )
    )
    print(f"wrote {out_dir.relative_to(PROJECT_ROOT)} in {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

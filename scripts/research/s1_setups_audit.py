"""The S1 setups chart audit (plan Phase 3) of one in-sample run of setups 2-5: its signals,
seeded near-misses at the funnel's two hardest cuts, and a daily chart per case.

    PGOPTIONS="-c default_transaction_read_only=on" HAT_DATA_ROOT=<data> \
        venv/bin/python scripts/research/s1_setups_audit.py --run-id ID [--seed N] --out DIR

It replays the run's nightly scan without its day loop, as the run published it: the run's
`config.json` (as `--rerun` reads it), the market over the run's period only, the run's ledger
(`ledger`, its bot and setup windows) and `watchlist.scan_and_publish` each night. Each row of a
trade date is decided as the daily estimate decides it (`loop._decide_daily`): the level broke
when the day's high is above the pivot, then `volume_pace` on the full day's volume
(`fillrules.breakout_volume_ok` over the 50 sessions before). A break that passes the volume
closes its setup's window for the next nights, as the run's entry or later skip does; one that
fails it does not (`loop.consumes`). The replay holds no positions, so it may list a row the run
dropped for a held instrument: the printed funnel and break counts beside the run's say how far
it is from the run.

- signals: the run's `signals.csv` rows, each matched to its replayed break; per playbook,
  `min(15, n)` drawn with `--seed`;
- near-misses, per playbook, one per symbol, drawn with the same seed: 3 level breaks refused by
  the 1.5x volume (pool a), and 2 stock-days that passed the detector's first gate (the cup, the
  thrust, the primary base) and failed the next one or the dry-up (pool b), each with its rule,
  value and threshold.

Each case's structure is rebuilt on the night's scan bars with the detectors' own pieces
(`markers`): H0 (T for a power play), C, the contractions, the tight area, the pivot and the
stop. Writes `cases.csv`, `audit.md` and `charts/*.png` to `--out`. Needs matplotlib (imported
by the chart only), which is not a backend requirement. Reads the database only.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from datetime import time as clock
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from app.hat.engine.criteria.book import (  # noqa: E402
    Contraction,
    Series,
    contractions_of,
    zigzag,
)
from app.hat.engine.criteria.setups import (  # noqa: E402
    BookCheatParams,
    BookCupHandleParams,
    BookPowerPlayParams,
    BookPrimaryBaseParams,
    Cup,
    Thrust,
    area,
    area_dryup,
    base_contractions,
    cup_of,
    handle_of,
    pause_of,
    thresholds,
    thrust_of,
)
from app.hat.engine.strategy.config import Playbook, StrategyConfig  # noqa: E402
from app.hat.engine.strategy.scoring import PlaybookEvaluation, stop_buffer_pct  # noqa: E402

if TYPE_CHECKING:
    from app.hat.backtest.data import MarketView
    from app.hat.engine.criteria.base import EvalContext

Params = BookCheatParams | BookCupHandleParams | BookPowerPlayParams | BookPrimaryBaseParams
T = TypeVar("T")

DEFAULT_SEED = 20261030
IN_SAMPLE_END = date(2021, 12, 31)
SIGNAL_SAMPLE = 15
VOLUME_MISSES = 3  # pool a: level breaks refused by the breakout volume
SHAPE_MISSES = 2  # pool b: past the detector's first gate, failed its next one or the dry-up
ADV_SESSIONS = 50
AT_NIGHT = clock(16, 0)  # the backtest loop's nightly scan instant, IST
CASE_PREFIX = {"cheat": "CT", "cup_handle": "CH", "power_play": "PP", "primary_base": "PB"}
LEAD = 20  # sessions charted before H0
AFTER = 10  # sessions charted after the break (or the night)

# Each detector: its criterion type, params model and the gates pool b takes (after its first).
DETECTORS: dict[str, tuple[type[Params], tuple[str, ...]]] = {
    "book_cheat": (BookCheatParams, ("pause", "dry_up")),
    "book_cup_handle": (BookCupHandleParams, ("handle", "dry_up")),
    "book_power_play": (BookPowerPlayParams, ("flag", "dry_up")),
    "book_primary_base": (BookPrimaryBaseParams, ("dry_up",)),
}


# --- the structure of a case (pure) ----------------------------------------------------------


@dataclass(frozen=True)
class Markers:
    """A setup's structure on one night's series, as bar indexes and levels: `h0` is H0 (T for
    a power play, the highest high since listing for a primary base), `c` the cup's low, `tight`
    the tight area (the pause, the handle, the final contraction) whose high is the pivot and
    whose low less the stop buffer is the stop. `note` says what the detector's pieces did not
    find."""

    h0: int | None = None
    c: int | None = None
    contractions: tuple[Contraction, ...] = ()
    tight: Contraction | None = None
    dryup: tuple[Decimal, Decimal] | None = None  # the tight area's (mean, lowest) vr
    note: str = ""

    @property
    def pivot(self) -> Decimal | None:
        return None if self.tight is None else self.tight.high

    def stop(self, buffer_pct: Decimal) -> Decimal | None:
        return None if self.tight is None else self.tight.low * (1 - buffer_pct / 100)


def _swings_from(series: Series, start: int, end: int) -> tuple[Contraction, ...]:
    swings = zigzag(series.high, series.low, thresholds(series), start, end)
    return tuple(contractions_of(series.high, series.low, swings))


def _with_dryup(series: Series, found: Markers) -> Markers:
    if found.tight is None:
        return found
    return replace(found, dryup=area_dryup(series.volume, found.tight))


def markers(detector: str, series: Series, params: Params) -> Markers:
    """The structure `detector` sees on the last bar of `series` with `params`, built with its
    own pieces (`cup_of`, `pause_of`, `handle_of`, `thrust_of`, `base_contractions`); a piece
    that finds nothing leaves its markers out and says so in `note`."""
    end = len(series.dates) - 1
    if detector in ("book_cheat", "book_cup_handle"):
        cup = cup_of(series.high, series.low, end, params)
        if not isinstance(cup, Cup):
            return Markers(note=f"no cup: {cup[0]}")
        found = Markers(h0=cup.h0, c=cup.c, contractions=_swings_from(series, cup.h0, end))
        if detector == "book_cheat":
            pause = pause_of(series.high, series.low, end, params)
            if pause is None:
                return replace(found, note="no pause")
            return _with_dryup(series, replace(found, tight=pause))
        handle = handle_of(series.high, series.low, cup, end, params)
        if isinstance(handle, Contraction):
            return _with_dryup(series, replace(found, tight=handle))
        right = series.high[cup.c + 1 : end + 1]
        if not right:
            return replace(found, note=f"handle: {handle[0]}")
        lip = cup.c + 1 + right.index(max(right))
        candidate = area(series.high, series.low, lip, end)  # the handle the rule judged
        return _with_dryup(series, replace(found, tight=candidate, note=f"handle: {handle[0]}"))
    if detector == "book_power_play":
        thrust = thrust_of(series.high, series.low, end, params)
        if not isinstance(thrust, Thrust):
            return Markers(note=f"no thrust: {thrust[0]}")
        threshold = thresholds(series)
        found = Markers(
            h0=thrust.top,
            contractions=base_contractions(series, threshold, thrust.top, end),
        )
        if not found.contractions:
            return replace(found, note="no contraction in the flag")
        return _with_dryup(series, replace(found, tight=found.contractions[-1]))
    if detector == "book_primary_base":
        h0 = series.high.index(max(series.high))
        if h0 >= end:
            return Markers(note="no base")
        found = Markers(h0=h0, contractions=base_contractions(series, thresholds(series), h0, end))
        if not found.contractions:
            return replace(found, note="no contraction in the base")
        return _with_dryup(series, replace(found, tight=found.contractions[-1]))
    raise ValueError(f"unknown detector {detector}")


# --- volume, sampling, labels (pure) ----------------------------------------------------------


def full_day_ratio(volumes: Sequence[float], days: int = ADV_SESSIONS) -> float | None:
    """The last of `volumes` over the mean of the `days` before it (the full-day vr the daily
    estimate's `volume_pace` reads); None without those `days + 1` finite volumes or a positive
    mean."""
    if len(volumes) != days + 1 or any(not math.isfinite(v) or v < 0 for v in volumes):
        return None
    mean = sum(volumes[:-1]) / days
    return None if mean <= 0 else volumes[-1] / mean


def adv(volumes: Sequence[float | None], days: int = ADV_SESSIONS) -> list[float | None]:
    """Each bar's ADV50: the mean volume of the `days` bars before it; None without them."""
    out: list[float | None] = []
    for i in range(len(volumes)):
        window = volumes[max(0, i - days) : i]
        ok = len(window) == days and all(v is not None and math.isfinite(v) for v in window)
        out.append(sum(v for v in window if v is not None) / days if ok else None)
    return out


def draw(
    items: Sequence[T], n: int, seed: int, key: Callable[[T], Hashable] | None = None
) -> list[T]:
    """`n` of `items` drawn with `seed` (all of them when there are no more), at most one per
    `key(item)` when a key is given, in the items' order."""
    order = list(range(len(items)))
    random.Random(seed).shuffle(order)  # noqa: S311  # NOSONAR - a seeded, reproducible draw
    picked: list[int] = []
    seen: set[Hashable] = set()
    for i in order:
        k = None if key is None else key(items[i])
        if k is not None and k in seen:
            continue
        picked.append(i)
        if k is not None:
            seen.add(k)
        if len(picked) == n:
            break
    return [items[i] for i in sorted(picked)]


def threshold_of(reason: str, params: Params) -> str:
    """The rule a near-miss failed, as its threshold in words."""
    p = params
    rules: dict[str, str] = {
        "no_pause": f"a 5-15 session range <= {getattr(p, 'pause_range_max_pct', '')}%",
        "low_too_recent": f">= {getattr(p, 'low_before_pause_sessions', '')} sessions C to pause",
        "pause_in_upper_third": f"pause high position <= {getattr(p, 'cheat_band', [0, ''])[1]}",
        "h0_not_in_uptrend": "the template's price checks on H0's bar",
        "handle_too_short": f">= {getattr(p, 'handle_sessions', [''])[0]} sessions",
        "handle_too_long": f"<= {getattr(p, 'handle_sessions', ['', ''])[-1]} sessions",
        "handle_above_h0": "handle high <= H0",
        "handle_too_low": f"handle low position >= {getattr(p, 'handle_low_min_fraction', '')}",
        "handle_too_deep": f"handle depth <= {getattr(p, 'handle_depth_max_pct', '')}%",
        "base_too_short": f">= {getattr(p, 'flag_sessions', [''])[0]} sessions from T",
        "base_too_long": f"<= {getattr(p, 'flag_sessions', ['', ''])[-1]} sessions from T",
        "flag_too_deep": f"flag depth <= {getattr(p, 'flag_depth_max_pct', '')}%",
        "too_few_contractions": f">= {getattr(p, 'min_contractions', 2)} contractions",
        "depth_ratio_too_wide": f"each contraction <= {getattr(p, 'depth_ratio_max', '')} x prior",
        "final_depth_too_deep": f"final contraction <= {getattr(p, 'final_depth_max_pct', '')}%",
        "no_volume_dryup": f"mean vr < {getattr(p, 'dryup_mean_max', '')}",
        "no_dryup_session": f"a session with vr < {getattr(p, 'dryup_session_max', '')}",
        "no_volume_data": "the tight area's volumes",
    }
    return rules.get(reason, reason)


def fmt(value: object, places: int = 2) -> str:
    if value is None:
        return ""
    if isinstance(value, (float, Decimal)):
        return f"{float(value):.{places}f}"
    return str(value)


def head(series: Series, n: int) -> Series:
    """The first `n` bars of `series`."""
    return Series(
        series.dates[:n], series.high[:n], series.low[:n], series.close[:n], series.volume[:n]
    )


def stitch(night: Series, later: Series) -> Series:
    """`night` (the scan bars a detector read) and the bars of `later` after its last date,
    their prices put on `night`'s scale by the two closes of that date (a later dividend or
    split rescales the whole window), so the markers' indexes and levels hold on the chart."""
    if not night.dates:
        return later
    last = night.dates[-1]
    at = bisect_left(later.dates, last)
    if at == len(later.dates) or later.dates[at] != last or later.close[at] <= 0:
        return night
    k = night.close[-1] / later.close[at]
    rest = slice(at + 1, None)
    return Series(
        night.dates + later.dates[rest],
        night.high + [v * k for v in later.high[rest]],
        night.low + [v * k for v in later.low[rest]],
        night.close + [v * k for v in later.close[rest]],
        night.volume + later.volume[rest],
    )


# --- the replay ------------------------------------------------------------------------------


@dataclass
class Break:
    """A watchlist row whose level broke on `day` (the daily estimate), its full-day vr and
    whether that passed the playbook's `volume_pace`."""

    day: date
    playbook: str
    instrument_id: str
    symbol: str
    level: Decimal
    found_on: date | None
    setup_type: str | None
    ratio: float | None
    passed: bool


@dataclass
class Miss:
    """A stock-day of pool b: the detector's gate it stopped at, its reason and value."""

    night: date
    playbook: str
    instrument_id: str
    symbol: str
    gate: str
    reason: str
    value: float | None


@dataclass
class Replay:
    funnel: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    regime_off: int = 0
    breaks: list[Break] = field(default_factory=list)
    misses: dict[str, list[Miss]] = field(default_factory=lambda: defaultdict(list))


def _detector_of(playbook: Playbook) -> str:
    return next(i.type for i in playbook.setup.criteria if i.type in DETECTORS and i.enabled)


def _params_of(playbook: Playbook) -> Params:
    instance = next(i for i in playbook.setup.criteria if i.type in DETECTORS and i.enabled)
    return DETECTORS[instance.type][0].model_validate(instance.params)


def replay(args: argparse.Namespace) -> tuple[Any, Any, Any, Replay]:  # pragma: no cover
    """The run's nightly scan and the daily estimate's breaks (module docstring)."""
    from sqlalchemy import select, text

    from app.database import db_session_scope
    from app.hat.backtest.data import MarketView, load_market, symbol_on
    from app.hat.backtest.fillrules import full_day_pace_skip
    from app.hat.backtest.ledger import ledger, set_series
    from app.hat.backtest.run_files import read_config, read_run
    from app.hat.backtest.runs import _replayed
    from app.hat.data.identity import series_as_of
    from app.hat.data.store import DataStore
    from app.hat.engine import lifecycle, scan, trigger, watchlist
    from app.hat.engine.scan import SESSIONS_AHEAD, ScanInputs, window_key
    from app.hat.models import HatEvent, HatWatchlistEntry
    from app.hat.settings import operator_settings
    from app.hat.time import IST, as_utc

    settings = operator_settings()
    root = settings.data_root
    run = read_run(root, args.run_id)
    config_doc = read_config(root, args.run_id)
    if run is None or config_doc is None:
        raise SystemExit(f"no run {args.run_id} under {root}")
    target, spec = _replayed(config_doc)
    if spec.end > IN_SAMPLE_END:
        raise SystemExit(f"the run ends {spec.end}, after the in-sample period")
    started = time.monotonic()
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
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
    ledger_session, bots = ledger(spec, data)
    bot_id = bots[0]
    benchmark = [(d, float(c)) for d, c in data.index_closes.get("NIFTY 500", ())]
    bench_days = [d for d, _ in benchmark]
    memo: dict[object, object] = {}
    consumed: set[Any] = set()
    out = Replay()
    pools = {p.name: DETECTORS[_detector_of(p)][1] for p in config.playbooks}
    original = scan.evaluate_playbook

    def recording(
        cfg: StrategyConfig,
        name: str,
        ctx: EvalContext,
        *,
        regime_allows_longs: bool,
        universe: Any = None,  # noqa: ANN401 - passed through
    ) -> PlaybookEvaluation:
        # pool b: the stock-days a detector stopped after its first gate
        evaluation = original(
            cfg, name, ctx, regime_allows_longs=regime_allows_longs, universe=universe
        )
        if not evaluation.passed:
            gate = scan._stopped_gate(evaluation)
            if gate in pools.get(name, ()):
                failed = evaluation.stages[-1].failed
                result = failed.result if failed is not None else None
                out.misses[name].append(
                    Miss(
                        ctx.as_of,
                        name,
                        ctx.instrument_id,
                        symbol_on(data, ctx.instrument_id, ctx.as_of),
                        gate,
                        "" if result is None else result.reason,
                        None if result is None else result.value,
                    )
                )
        return evaluation

    scan.evaluate_playbook = recording
    period = [d for d in view.days if spec.start <= d <= spec.end]
    try:
        for n, day in enumerate(period):
            night = view.prev(day)
            if night is None:
                continue
            view.open(day)
            series = series_as_of(data.series, night)
            set_series(ledger_session, series)
            inputs = ScanInputs(
                as_of=night,
                members=(),
                bars=view.window(night),
                regime_closes=view.closes_through(night),
                breadth=view.breadth_through(night),
                features=view.features_on(night),
                series=series,
                benchmark=benchmark[: bisect_right(bench_days, night)],
                memo=memo,
                triggered=frozenset(consumed),
                sessions_ahead=view.days[bisect_left(view.days, day) :][:SESSIONS_AHEAD],
            )
            now = as_utc(datetime.combine(night, AT_NIGHT, tzinfo=IST))  # as the loop's _publish
            published = watchlist.scan_and_publish(ledger_session, bot_id, inputs, day, now)
            if not published.gate_on and published.skipped is None:
                out.regime_off += 1
            for playbook, counts in published.funnel.items():
                out.funnel[playbook].update(counts)
            view.close(day)
            rows = ledger_session.scalars(
                select(HatWatchlistEntry).where(
                    HatWatchlistEntry.bot_id == bot_id,
                    HatWatchlistEntry.trade_date == day,
                    HatWatchlistEntry.status == lifecycle.WATCHLISTED,
                )
            ).all()
            for row in rows:
                instrument_id = str(row.instrument_id)
                bar = view.bar(instrument_id, day)
                results = row.criteria_results or {}
                book = config.playbook(row.playbook)
                level = trigger.daily_level(
                    book, trigger.watchlist_levels(results.get("levels", {}))
                )
                if bar is None or level is None or bar.high <= level:
                    continue
                volumes = view.volumes_through(instrument_id, day, ADV_SESSIONS + 1)
                ratio = full_day_ratio(volumes)
                multiple = trigger.volume_pace_multiple(book)
                passed = multiple is None or full_day_pace_skip(volumes, multiple) is None
                out.funnel[row.playbook]["trigger"] += 1
                if passed:
                    out.funnel[row.playbook]["breakout_volume"] += 1
                    key = window_key(row.playbook, instrument_id, results)
                    if key is not None:
                        consumed.add(key)
                out.breaks.append(
                    Break(
                        day,
                        row.playbook,
                        instrument_id,
                        symbol_on(data, instrument_id, day),
                        level,
                        scan.found_on(results),
                        row.setup_type,
                        ratio,
                        passed,
                    )
                )
            if n % 100 == 0:
                print(f"  {night} {time.monotonic() - started:.0f}s breaks {len(out.breaks)}")
    finally:
        scan.evaluate_playbook = original
    return run, config, view, out


def series_on(view: MarketView, instrument_id: str, night: date) -> Series:  # pragma: no cover
    """The instrument's scan bars of `night` (`MarketView.window`), as the detector read them."""
    import polars as pl

    view.close(night)
    frame = view.window(night).filter(pl.col("instrument_id").cast(pl.String) == instrument_id)
    frame = frame.sort("date")
    return Series(
        frame["date"].to_list(),
        [float(v) for v in frame["high"].to_list()],
        [float(v) for v in frame["low"].to_list()],
        [float(v) for v in frame["close"].to_list()],
        [None if v is None else float(v) for v in frame["volume"].to_list()],
    )


def chart(  # pragma: no cover - drawing only
    png: Path,
    title: str,
    series: Series,
    found: Markers,
    buffer_pct: Decimal,
    first: int,
    breakout: int | None,
) -> None:
    """A daily chart of `series` from bar `first`: high-low bars with the close, H0, C, the
    contractions, the tight area, the pivot and the stop; volume with ADV50 below, the breakout
    bar marked."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle

    xs = list(range(first, len(series.dates)))
    fig, (top, low) = plt.subplots(
        2, 1, figsize=(12, 7), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    top.vlines(xs, [series.low[i] for i in xs], [series.high[i] for i in xs], color="0.35", lw=1)
    top.plot(xs, [series.close[i] for i in xs], "_", color="0.1", ms=4)
    for k, c in enumerate(found.contractions, start=1):
        if c.end < first:
            continue
        top.plot([c.start, c.end], [float(c.high), float(c.low)], color="tab:blue", lw=1)
        top.annotate(
            f"c{k} {float(c.depth_pct):.0f}%",
            (c.end, float(c.low)),
            fontsize=7,
            color="tab:blue",
            va="top",
        )
    if found.h0 is not None:
        top.plot(found.h0, series.high[found.h0], "v", color="tab:green", ms=9)
        top.annotate("H0", (found.h0, series.high[found.h0]), fontsize=9, va="bottom")
    if found.c is not None:
        top.plot(found.c, series.low[found.c], "^", color="tab:red", ms=9)
        top.annotate("C", (found.c, series.low[found.c]), fontsize=9, va="top")
    tight = found.tight
    if tight is not None:
        top.add_patch(
            Rectangle(
                (tight.start - 0.5, float(tight.low)),
                tight.sessions,
                float(tight.high - tight.low),
                fill=False,
                ec="tab:orange",
                lw=1.5,
            )
        )
        right = len(series.dates) - 1
        top.hlines(float(tight.high), tight.start, right, colors="tab:green", linestyles="--")
        top.annotate(f"pivot {float(tight.high):.2f}", (right, float(tight.high)), fontsize=8)
        stop = found.stop(buffer_pct)
        if stop is not None:
            top.hlines(float(stop), tight.start, right, colors="tab:red", linestyles="--")
            top.annotate(f"stop {float(stop):.2f}", (right, float(stop)), fontsize=8)
    volume = [series.volume[i] or 0.0 for i in xs]
    colors = ["tab:purple" if i == breakout else "0.6" for i in xs]
    low.bar(xs, volume, color=colors, width=0.8)
    means = adv(series.volume)
    low.plot(xs, [means[i] for i in xs], color="tab:orange", lw=1, label="ADV50")
    low.legend(loc="upper left", fontsize=7)
    if breakout is not None:
        for ax in (top, low):
            ax.axvline(breakout, color="tab:purple", lw=0.8, alpha=0.6)
    ticks = xs[:: max(1, len(xs) // 10)]
    low.set_xticks(ticks, [series.dates[i].isoformat() for i in ticks], rotation=30, fontsize=7)
    top.set_title(title, fontsize=9)
    fig.tight_layout()
    fig.savefig(png, dpi=80)
    plt.close(fig)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - reads the store
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    print(f"seed {args.seed}")
    (args.out / "charts").mkdir(parents=True, exist_ok=True)
    run, config, view, found = replay(args)
    run_funnel = run["funnel"]["by_playbook"]
    lines = [
        f"# S1 setups chart audit, run `{args.run_id}`",
        "",
        f"Seed {args.seed}. Regime-off nights: replay {found.regime_off}, "
        f"run {run['funnel']['regime_off_nights']}.",
        "",
        "## Replayed funnel against the run's",
        "",
        "| playbook | gate | replay | run |",
        "|---|---|---|---|",
    ]
    for playbook, counts in run_funnel.items():
        for gate, count in counts.items():
            mine = found.funnel[playbook].get(gate)
            if mine is None:  # a gate after the break (the entry's stop, its chase)
                lines.append(f"| {playbook} | {gate} | not replayed | {count} |")
                continue
            flag = "" if mine == count else " (differs)"
            lines.append(f"| {playbook} | {gate} | {mine}{flag} | {count} |")
    signals_path = run_folder(args.run_id) / "signals.csv"
    with open(signals_path) as f:
        signals = list(csv.DictReader(f))
    by_key = {(b.day.isoformat(), b.symbol): b for b in found.breaks if b.passed}
    cases: list[dict[str, str]] = []
    for playbook in config.playbooks:
        name = playbook.name
        detector = _detector_of(playbook)
        params = _params_of(playbook)
        buffer = Decimal(str(stop_buffer_pct(playbook)))
        matched = [by_key.get((s["date"], s["symbol"])) for s in signals]
        mine = [b for b in matched if b is not None and b.playbook == name]
        chosen: list[tuple[str, Any]] = [
            ("signal", b) for b in draw(mine, SIGNAL_SAMPLE, args.seed)
        ]
        volume_pool = [b for b in found.breaks if b.playbook == name and not b.passed]
        chosen += [
            ("near-miss a", b)
            for b in draw(volume_pool, VOLUME_MISSES, args.seed, key=lambda b: b.symbol)
        ]
        shape_pool = found.misses.get(name, [])
        chosen += [
            ("near-miss b", m)
            for m in draw(shape_pool, SHAPE_MISSES, args.seed, key=lambda m: m.symbol)
        ]
        reasons = Counter(m.reason for m in shape_pool)
        lines += [
            "",
            f"## {name}: pools",
            "",
            f"signals matched {len(mine)}; volume-refused breaks {len(volume_pool)} "
            f"({len({b.symbol for b in volume_pool})} symbols); pool b {len(shape_pool)} "
            f"stock-days by reason {dict(reasons)}",
        ]
        for n, (kind, item) in enumerate(chosen, start=1):
            case = f"{CASE_PREFIX.get(name, name[:2].upper())}{n:02d}"
            if isinstance(item, Break):
                night = item.found_on or view.prev(item.day)
                day, ratio = item.day, item.ratio
                failed = "" if kind == "signal" else f"volume_pace: vr {fmt(ratio)} vs 1.5"
            else:
                night, day, ratio = item.night, None, None
                value = fmt(item.value) or "none"
                failed = f"{item.reason}: {value} vs {threshold_of(item.reason, params)}"
            last = view.days[min(len(view.days) - 1, view.index[day or night] + AFTER)]
            at_night = series_on(view, item.instrument_id, night)
            series = stitch(at_night, series_on(view, item.instrument_id, last))
            shape = markers(detector, at_night, params)
            if kind == "near-miss b" and item.reason in ("no_volume_dryup", "no_dryup_session"):
                mean, lowest = shape.dryup or (None, None)
                failed += f" (mean {fmt(mean)}, min {fmt(lowest)})"
            marks = (shape.h0, None if shape.tight is None else shape.tight.start)
            start = min([len(at_night.dates) - 60, *(i for i in marks if i is not None)])
            breakout = None if day is None else bisect_left(series.dates, day)
            png = args.out / "charts" / f"{case}-{item.symbol}-{(day or night).isoformat()}.png"
            title = (
                f"{case} {name} {item.symbol} {kind} night {night}"
                + ("" if day is None else f" break {day} vr {fmt(ratio)}")
                + (f" | {failed}" if failed else "")
                + (f" | {shape.note}" if shape.note else "")
            )
            chart(png, title, series, shape, buffer, max(0, start - LEAD), breakout)
            logged = getattr(item, "level", None)
            cases.append(
                {
                    "case": case,
                    "setup": name,
                    "setup_type": getattr(item, "setup_type", "") or "",
                    "symbol": item.symbol,
                    "night": night.isoformat(),
                    "break_day": "" if day is None else day.isoformat(),
                    "kind": kind,
                    "failed": failed,
                    "pivot_marked": fmt(shape.pivot),
                    "pivot_row": fmt(logged),
                    "stop_marked": fmt(shape.stop(buffer)),
                    "dryup_mean_min": ""
                    if shape.dryup is None
                    else f"{fmt(shape.dryup[0])}/{fmt(shape.dryup[1])}",
                    "contractions": "/".join(
                        f"{float(c.depth_pct):.1f}" for c in shape.contractions
                    ),
                    "note": shape.note,
                    "chart": png.name,
                }
            )
    unmatched = [
        f"{s['date']} {s['symbol']}" for s in signals if (s["date"], s["symbol"]) not in by_key
    ]
    lines += ["", f"signals.csv rows without a replayed break: {unmatched or 'none'}", ""]
    columns = list(cases[0]) if cases else []
    lines += ["## Cases", "", "| " + " | ".join(columns) + " |", "|" + "---|" * len(columns)]
    lines += ["| " + " | ".join(c[k] for k in columns) + " |" for c in cases]
    with open(args.out / "cases.csv", "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=columns)
        writer.writeheader()
        writer.writerows(cases)
    (args.out / "audit.md").write_text("\n".join(lines) + "\n")
    print(f"{len(cases)} cases, {args.out / 'audit.md'}")
    return 0


def run_folder(run_id: str) -> Path:  # pragma: no cover - reads the settings
    from app.hat.settings import operator_settings

    return operator_settings().data_root / "backtests" / "runs" / run_id


if __name__ == "__main__":
    raise SystemExit(main())

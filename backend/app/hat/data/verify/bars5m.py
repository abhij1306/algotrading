"""D4: the 5-minute history's refused days (`bars_5m_rejects`, rule `basis_mismatch`) traced
back to the Kotak raw files they came from (`--kotak`), and the stored days checked again.

The raw files are read here on their own, with their own time labelling: a day whose traded bars
include one labelled 15:30 and none 09:15 is labelled by bar end (each bar starts five minutes
before its label), any other by bar start (`end_labelled`); the importer's switch date is not
used. Each refused (instrument, day) is aggregated from those candles (the session's 5-minute
bars from 09:15 to 15:30 IST), and held against the raw daily bar with the import's envelope
(`bars_basis.check_day`, tolerance `history_envelope_tol`, never loosened here). The first class
that fits, in this order:

- `no_daily_now`: no raw daily bar for the day now (ours: the bar or the ISIN map);
- `stored_now`: the day is stored now (a later run let it in: the ledger row is stale);
- `no_source`: the raw file holds no candle that day (the source's gap);
- `screened_bucket`: they fit as they are, but the import screened a bad candle of the day
  (`ohlc`, `volume`) and the rest did not (the source's bad candle);
- `fits_now`: the raw candles fit as they are (ours: the import's conversion or aggregation);
- `fits_converted`: they fit once divided by the stored actions' factor after the day (ours:
  the import's decision that the source does not carry those actions);
- `basis_factor`: the high and the low are off the daily ones by more than `BASIS_STEP`, both
  the same way and by the same ratio within `BASIS_STEP` (ours: a factor the store lacks or a
  wrong ISIN map);
- `bad_source_exceeds`: a candle beyond the daily range (a bad source bar);
- `close_auction`: the missed extreme is the daily close (the closing auction made it);
- `open_auction_near`: the missed extreme is within `AUCTION_NEAR` of the daily open (the
  pre-open auction made it, just outside the open exception's tolerance);
- `source_short_extreme`: the candles miss a daily extreme inside the range (the source lacks
  the trades that made it).

Those classes fall under three causes (`CAUSE`): a bad or short source bar, our aggregation or
mapping, an auction effect. The summary adds the coverage of the history by year (stored days
over the raw daily bars of the instruments it covers), the bias of the refusals towards gap days
(an open more than `GAP_PCT` off the previous close), a seeded sample to check by hand with the
raw evidence, and the rule this evidence recommends. Stored days that fail the envelope now
(`bars_basis.misfit_days`) fail the check.

Stored bars bar by bar: a seeded sample of `COMPARE_SAMPLE` stored (instrument, day) pairs is
compared with the raw file's bars of that day (`compare_bars`): the alignment (start- or
end-labelled) and the day's scale (the stored history is the source divided by the factors it
carries, one number a day) are found from the bars themselves, and then every stored bar must be
a source bar at that scale (prices within `PRICE_TICK` of a tick, volume within one share or
`VOLUME_SLACK`), and every traded source bar in the session must be stored. A stored day with no
raw file, a bar that differs, or a bar on one side only fails (dated). The envelope check above
holds each scaled day to the raw daily bar, so the scale itself is checked there. The store's
bars came from Fyers' minute history inside its download windows (`minute_history/progress.json`)
and from the Kotak import outside them; the summary splits the compare by that writer
(`by_writer`): inside a Fyers window Kotak is a second vendor, not the bars' own source, and a
difference there still fails (the stored bars are not proven).
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from ...money import require_exact
from ...time import IST
from ..audit import FAIL, WARN, check_result, finding
from ..bars_basis import BASIS_MISMATCH, DailyRange, check_day, fyers_factor
from . import dated, seeded_sample
from .context import Context

BASIS_STEP = 0.03  # a factor's mark: both extremes off by 3% or more, the same way and ratio
AUCTION_NEAR = 0.01  # a missed extreme within 1% of the daily open: the auction's
GAP_PCT = 4.0
SAMPLE = 20
SAMPLE_SEED = 20261029
SESSION = (time(9, 15), time(15, 30))
COMPARE_SAMPLE = 3000
PRICE_TICK = 0.0105  # rupees at the source's scale: a 0.01 tick and its rounding
PRICE_SLACK = 0.0005  # relative: the scaled source rounded to the store's four places
VOLUME_SLACK = 0.001  # relative: the scaled volume rounded to whole shares
CAUSE = {
    "no_source": "source",
    "bad_source_exceeds": "source",
    "source_short_extreme": "source",
    "no_daily_now": "ours",
    "fits_now": "ours",
    "screened_bucket": "source",
    "fits_converted": "ours",
    "basis_factor": "ours",
    "close_auction": "auction",
    "open_auction_near": "auction",
    "stored_now": "stale_ledger",
}
_WIDTH = timedelta(minutes=5)


@dataclass(frozen=True)
class Candle:
    start: datetime  # IST
    open: float
    high: float
    low: float
    close: float


@dataclass(frozen=True)
class DayBars:
    """One day's session candles, aggregated."""

    open: float
    high: float
    low: float
    close: float
    candles: int
    high_at: str
    low_at: str


@dataclass(frozen=True)
class SourceBar:
    """One raw file row as the file labels it (IST wall clock)."""

    label: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int


def end_labelled(bars: Iterable[SourceBar]) -> bool:
    """Whether a day's rows label each bar by its end: a traded bar labelled 15:30 (the last
    bar's end) and none at 09:15 (the first bar's start)."""
    traded = {b.label.time() for b in bars if b.volume > 0}
    return time(15, 30) in traded and time(9, 15) not in traded


def candles_of(rows: Iterable[SourceBar]) -> list[Candle]:
    """The rows as candles by start: each day's labels moved back five minutes when the day is
    `end_labelled`."""
    by_day: dict[date, list[SourceBar]] = defaultdict(list)
    for row in rows:
        by_day[row.label.date()].append(row)
    out = []
    for group in by_day.values():
        shift = _WIDTH if end_labelled(group) else timedelta(0)
        out += [Candle(b.label - shift, b.open, b.high, b.low, b.close) for b in group]
    return out


@dataclass(frozen=True)
class BarCompare:
    """One stored day against its source bars."""

    shift: int  # minutes from a stored start to the source's label
    scale: float  # stored / source
    bars: int
    price: int  # bars with a price off the scaled source
    volume: int
    stored_only: int
    source_only: int
    rule: bool  # the alignment agrees with `end_labelled`


def _matches(stored: Sequence[float], source: SourceBar, scale: float) -> bool:
    return all(
        abs(mine - theirs * scale) <= max(PRICE_TICK * scale, PRICE_SLACK * abs(mine))
        for mine, theirs in zip(
            stored[:4], (source.open, source.high, source.low, source.close), strict=True
        )
    )


def _pairs(
    stored: Mapping[time, Sequence[float]],
    by_label: Mapping[time, SourceBar],
    day: date,
    shift: int,
) -> list[tuple[Sequence[float], SourceBar]]:
    move = timedelta(minutes=shift)
    found = ((v, by_label.get((datetime.combine(day, t) + move).time())) for t, v in stored.items())
    return [(v, b) for v, b in found if b is not None]


def _scale(pairs: Sequence[tuple[Sequence[float], SourceBar]]) -> float:
    ratios = sorted(v[3] / b.close for v, b in pairs if b.close)
    return ratios[len(ratios) // 2] if ratios else 1.0


def compare_bars(
    day: date, stored: Mapping[time, Sequence[float]], source: Sequence[SourceBar]
) -> BarCompare:
    """A day's stored bars (start time -> open, high, low, close, volume) against the source's
    rows of that day. The alignment (0 or 5 minutes from start to label) and the scale (the
    median of stored close over source close) are the ones under which the most bars match
    (ties: `end_labelled`); then each stored bar's prices and volume against the scaled source
    bar, and the bars on one side only (a source bar counts when it is traded and starts in the
    session)."""
    by_label = {b.label.time(): b for b in source if b.label.date() == day}
    rule = 5 if end_labelled(by_label.values()) else 0
    best = None
    for shift in (rule, 5 - rule):
        pairs = _pairs(stored, by_label, day, shift)
        scale = _scale(pairs)
        score = sum(_matches(v, b, scale) for v, b in pairs)
        if best is None or score > best[0]:
            best = (score, shift, scale, pairs)
    assert best is not None
    _, shift, scale, pairs = best
    price = sum(not _matches(v, b, scale) for v, b in pairs)
    volume = sum(
        abs(v[4] - b.volume / scale) > max(1.0, VOLUME_SLACK * abs(v[4])) for v, b in pairs
    )
    move = timedelta(minutes=shift)
    starts = {(datetime.combine(day, t) - move).time() for t, b in by_label.items() if b.volume > 0}
    source_only = sum(1 for t in starts if SESSION[0] <= t < SESSION[1] and t not in stored)
    return BarCompare(
        shift,
        scale,
        len(stored),
        price,
        volume,
        len(stored) - len(pairs),
        source_only,
        shift == rule,
    )


def session_days(candles: Iterable[Candle]) -> dict[date, DayBars]:
    """Each day's candles inside the session (`SESSION`, by start, IST), aggregated."""
    by_day: dict[date, list[Candle]] = defaultdict(list)
    for c in candles:
        if SESSION[0] <= c.start.time() < SESSION[1]:
            by_day[c.start.date()].append(c)
    out = {}
    for day, group in by_day.items():
        group.sort(key=lambda c: c.start)
        top = max(group, key=lambda c: c.high)
        bottom = min(group, key=lambda c: c.low)
        out[day] = DayBars(
            group[0].open,
            top.high,
            bottom.low,
            group[-1].close,
            len(group),
            f"{top.start:%H:%M}",
            f"{bottom.start:%H:%M}",
        )
    return out


def _fits(
    bars: DayBars, daily: DailyRange, tolerance: Decimal, factor: Decimal = Decimal(1)
) -> bool:
    from ..bars import Bar  # noqa: PLC0415

    def d(x: float) -> Decimal:
        return require_exact(x, "kotak_price", floats=True) / factor

    first = Bar(
        "x",
        datetime(2000, 1, 1, tzinfo=UTC),
        d(bars.open),
        d(bars.high),
        d(bars.low),
        d(bars.open),
        0,
    )
    last = Bar(
        "x",
        datetime(2000, 1, 1, tzinfo=UTC),
        d(bars.close),
        d(bars.high),
        d(bars.low),
        d(bars.close),
        0,
    )
    return check_day([first, last], daily, tolerance) is None


def classify(
    bars: DayBars | None,
    daily: DailyRange | None,
    stored_now: bool,
    factor: Decimal,
    tolerance: Decimal,
    screened: bool = False,
) -> str:
    """One refused day's class (see the module docstring), from the raw candles' aggregate, the
    raw daily bar, whether the day is stored now and the factor of the stored actions after it."""
    if daily is None:
        return "no_daily_now"
    if stored_now:
        return "stored_now"
    if bars is None:
        return "no_source"
    if _fits(bars, daily, tolerance):
        return "screened_bucket" if screened else "fits_now"
    if factor != 1 and _fits(bars, daily, tolerance, factor):
        return "fits_converted"
    tol = float(tolerance)
    high, low, d_open, d_close = (
        float(daily.high),
        float(daily.low),
        float(daily.open),
        float(daily.close),
    )
    up, down = bars.high / high - 1, bars.low / low - 1
    if abs(up) > BASIS_STEP and abs(down) > BASIS_STEP and up * down > 0:
        if abs((1 + up) / (1 + down) - 1) <= BASIS_STEP:
            return "basis_factor"
    if bars.high > high * (1 + tol) or bars.low < low * (1 - tol):
        return "bad_source_exceeds"
    missed = [
        x
        for x, short in ((high, bars.high < high * (1 - tol)), (low, bars.low > low * (1 + tol)))
        if short
    ]
    if any(abs(x - d_close) <= x * tol for x in missed):
        return "close_auction"
    if any(abs(x - d_open) <= x * AUCTION_NEAR for x in missed):
        return "open_auction_near"
    return "source_short_extreme"


def gap_day(open_: float, prev_close: float | None) -> bool:
    return bool(prev_close) and abs(open_ / prev_close - 1) * 100 > GAP_PCT  # type: ignore[operator]


def gap_bias(rows: Iterable[tuple[bool, int, int, int]]) -> dict[str, dict[str, float | int]]:
    """From (gap day, daily bars, stored, refused) counts: the days, and the shares stored and
    refused, of gap days and of the rest."""
    tally = {"gap": [0, 0, 0], "other": [0, 0, 0]}
    for gap, days, n_stored, n_refused in rows:
        t = tally["gap" if gap else "other"]
        t[0] += days
        t[1] += n_stored
        t[2] += n_refused
    return {
        k: {
            "days": d,
            "stored_pct": round(100 * s / d, 2) if d else 0.0,
            "refused_pct": round(100 * r / d, 2) if d else 0.0,
        }
        for k, (d, s, r) in tally.items()
    }


RECOMMENDATION = (
    "Keep the envelope at its tolerance: a refused day is a missing bar, never a wrong one. Fix "
    "the `ours` classes at their source (the missing factors and ISIN maps behind basis_factor, "
    "the import's carry decision behind fits_converted) and refetch those days; re-run the "
    "import over fits_now days; treat the auction classes like the open exception only with "
    "evidence that the auction price is the missed extreme (close_auction: the daily close is "
    "the extreme), never by widening the tolerance; leave the source classes refused and count "
    "them in coverage, which is biased against gap days (see `gap_bias`)."
)


# --- the real data ------------------------------------------------------------------------------


def source_rows(
    path: Path, days: Iterable[date] | None = None
) -> list[SourceBar]:  # pragma: no cover
    """A raw file's rows (`ts`, IST, as the file labels them), of `days` only when given."""
    import pyarrow as pa  # noqa: PLC0415
    import pyarrow.compute as pc  # noqa: PLC0415
    import pyarrow.parquet as pq  # noqa: PLC0415

    table = pq.read_table(path, columns=["ts", "open", "high", "low", "close", "volume"])
    table = table.set_column(0, "ts", pc.cast(table["ts"], pa.timestamp("us", "Asia/Kolkata")))
    wanted = None if days is None else set(days)
    out = []
    for ts, o, h, lo, c, v in zip(*(table[c].to_pylist() for c in table.column_names), strict=True):
        if None in (ts, o, h, lo, c):
            continue
        label = ts.astimezone(IST)
        if wanted is None or label.date() in wanted:
            out.append(SourceBar(label, float(o), float(h), float(lo), float(c), int(v or 0)))
    return out


def _candles(path: Path) -> list[Candle]:  # pragma: no cover - files
    return candles_of(source_rows(path))


_DAILY_SQL = """
SELECT CAST(instrument_id AS VARCHAR), date, open, high, low, close
FROM bars_daily_raw_latest WHERE CAST(instrument_id AS VARCHAR) = ? AND date BETWEEN ? AND ?
"""

_COVER_SQL = """
WITH d AS (
    SELECT CAST(instrument_id AS VARCHAR) AS instrument_id, date,
           CAST(open AS DOUBLE) AS open,
           lag(CAST(close AS DOUBLE)) OVER (PARTITION BY instrument_id ORDER BY date) AS prev_close
    FROM bars_daily_raw_latest
    WHERE date BETWEEN ? AND ?
      AND CAST(instrument_id AS VARCHAR) IN (SELECT instrument_id FROM covered)
)
SELECT year(d.date) AS year,
       abs(d.open / d.prev_close - 1) * 100 > ? AS gap,
       count(*) AS days,
       count(s.instrument_id) AS stored,
       count(r.instrument_id) AS refused
FROM d
LEFT JOIN stored s ON s.instrument_id = d.instrument_id AND s.day = d.date
LEFT JOIN refused r ON r.instrument_id = d.instrument_id AND r.day = d.date
WHERE d.date >= ? AND d.prev_close > 0
GROUP BY ALL ORDER BY 1, 2
"""

_STORED_SQL = """
SELECT DISTINCT instrument_id, CAST(timezone('Asia/Kolkata', bar_start) AS DATE) AS day
FROM read_parquet(?)
"""


def _stored_pairs(ctx: Context) -> set[tuple[str, date]]:  # pragma: no cover - real data
    from ..bars_store import DATASET, FILE_NAME  # noqa: PLC0415

    files = sorted((ctx.root / DATASET).glob(f"date=*/{FILE_NAME}"))
    pairs: set[tuple[str, date]] = set()
    with closing(ctx.store.connect()) as con:
        for at in range(0, len(files), 60):
            chunk = [str(f) for f in files[at : at + 60]]
            pairs.update((str(i), d) for i, d in con.execute(_STORED_SQL, [chunk]).fetchall())
    return pairs


def _refusals(
    ctx: Context,
) -> tuple[dict[tuple[str, date], str], set[tuple[str, date]], Counter[str]]:  # pragma: no cover
    """The ledger's day refusals for `basis_mismatch` (each once: runs append) with their symbol,
    the days the import screened a candle of, and the ledger's rows by rule."""
    from ..bars_store import BarStore  # noqa: PLC0415

    rules: Counter[str] = Counter()
    refused: dict[tuple[str, date], str] = {}
    screened: set[tuple[str, date]] = set()
    for r in BarStore(ctx.root).read_rejects():
        rules[r.rule] += 1
        if r.bar_start is not None and r.day is not None:
            screened.add((r.instrument_id, r.day))
        if r.rule == BASIS_MISMATCH and r.bar_start is None and r.day is not None:
            if ctx.start <= r.day <= ctx.end:
                refused[(r.instrument_id, r.day)] = r.symbol
    return refused, screened, rules


def run(ctx: Context) -> dict[str, Any]:  # pragma: no cover - real data
    import pyarrow as pa  # noqa: PLC0415

    from ...settings import operator_settings  # noqa: PLC0415
    from ..bars_basis import misfit_days  # noqa: PLC0415
    from ..chains import stored_adjustment  # noqa: PLC0415

    tolerance = operator_settings().history_envelope_tol
    findings: list[dict[str, Any]] = []
    summary: dict[str, Any] = {"tolerance": str(tolerance)}
    misfits = [(i, d) for i, d in misfit_days(ctx.store, tolerance) if ctx.start <= d <= ctx.end]
    summary["stored_days_outside_envelope"] = len(misfits)
    if misfits:
        findings.append(
            finding(
                "stored_outside_envelope",
                FAIL,
                [{"instrument_id": i, "day": d.isoformat()} for i, d in misfits],
            )
        )
    refused, screened, rules = _refusals(ctx)
    summary["ledger_rows_by_rule"] = dict(rules)
    summary["refused_days"] = len(refused)
    stored = _stored_pairs(ctx)
    summary["stored_days"] = len(stored)
    first5m = min((d for _, d in stored), default=ctx.start)
    if ctx.kotak is None or not (ctx.kotak / "raw_5m").is_dir():
        ctx.note("D4: no --kotak folder; refusals not classified")
        findings.append(finding("reference_missing", FAIL, ["kotak"]))
    else:
        fetched_on = _folder_date(ctx.kotak) or ctx.end
        actions: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
        for a in stored_adjustment(ctx.session).factors:
            actions[str(a.instrument_id)].append((a.ex_date, a.factor))
        classes, by_year, items, sample_pool = _classify_all(
            ctx, refused, stored, screened, actions, fetched_on, tolerance
        )
        summary["classes"] = dict(classes.most_common())
        causes: Counter[str] = Counter()
        for code, n in classes.items():
            causes[CAUSE[code]] += n
        summary["causes"] = dict(causes.most_common())
        summary["classes_by_year"] = {str(y): dict(c) for y, c in sorted(by_year.items())}
        summary["hand_check_sample"] = seeded_sample(sample_pool, SAMPLE, SAMPLE_SEED)
        summary["bar_compare"], compare_findings = _compare_sample(ctx, stored)
        findings += compare_findings
        for code, level in (
            ("basis_factor", WARN),
            ("fits_now", WARN),
            ("fits_converted", WARN),
            ("no_daily_now", WARN),
        ):
            if classes[code]:
                findings.append(
                    {**finding(f"refused_{code}", level, items[code]), "count": classes[code]}
                )
    with closing(ctx.store.connect()) as con:
        con.register(
            "covered",
            pa.table({"instrument_id": sorted({i for i, _ in stored} | {i for i, _ in refused})}),
        )
        con.register(
            "stored",
            pa.table({"instrument_id": [i for i, _ in stored], "day": [d for _, d in stored]}),
        )
        con.register(
            "refused",
            pa.table({"instrument_id": [i for i, _ in refused], "day": [d for _, d in refused]}),
        )
        rows = con.execute(
            _COVER_SQL, [first5m - timedelta(days=10), ctx.end, GAP_PCT, first5m]
        ).fetchall()
    coverage: dict[str, Counter[str]] = defaultdict(Counter)
    flat = []
    for year, gap, days, n_stored, n_refused in rows:
        c = coverage[str(year)]
        c["daily_bars"] += days
        c["stored"] += n_stored
        c["refused"] += n_refused
        flat.append((bool(gap), days, n_stored, n_refused))
    summary["coverage_by_year"] = {
        y: {**c, "stored_pct": round(100 * c["stored"] / c["daily_bars"], 2)}
        for y, c in sorted(coverage.items())
    }
    summary["gap_bias"] = gap_bias(flat)
    summary["recommendation"] = RECOMMENDATION
    if summary["gap_bias"]["gap"]["refused_pct"] > 2 * summary["gap_bias"]["other"]["refused_pct"]:
        findings.append(finding("refusals_biased_to_gap_days", WARN, [summary["gap_bias"]]))
    return check_result("D4", summary, findings)


_DAY_BARS_SQL = """
SELECT CAST(instrument_id AS VARCHAR), bar_start, open, high, low, close, volume
FROM read_parquet(?) WHERE CAST(instrument_id AS VARCHAR) IN (SELECT unnest(CAST(? AS VARCHAR[])))
"""


def _symbols(ctx: Context) -> dict[str, list[str]]:  # pragma: no cover - the database
    """Every symbol each instrument has had (the raw files are named by symbol)."""
    out: dict[str, list[str]] = defaultdict(list)
    for i, symbol in ctx.rows(
        "SELECT CAST(instrument_id AS TEXT), symbol FROM hat_instrument_symbol_history "
        "UNION SELECT CAST(id AS TEXT), symbol FROM hat_instrument"
    ):
        out[i].append(symbol)
    return out


def fyers_windows(progress: Mapping[str, Any]) -> dict[str, tuple[date, date]]:
    """The Fyers minute-history download windows by symbol (`minute_history/progress.json`
    `done`: `NSE:ABC-EQ` -> [from, to]): the days the store's bars came from Fyers, which Kotak
    can only check as a second vendor."""
    out = {}
    for key, span in (progress.get("done") or {}).items():
        symbol = str(key).removeprefix("NSE:").rsplit("-", 1)[0]
        try:
            out[symbol] = (date.fromisoformat(span[0]), date.fromisoformat(span[1]))
        except (TypeError, ValueError, IndexError):
            continue
    return out


def _compare_sample(
    ctx: Context, stored: set[tuple[str, date]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # pragma: no cover - real data
    """`compare_bars` on a seeded sample of the stored days: the summary and the findings."""
    from ..bars_store import DATASET, FILE_NAME  # noqa: PLC0415

    assert ctx.kotak is not None
    pool = sorted((i, d) for i, d in stored if ctx.start <= d <= ctx.end)
    sample = seeded_sample(pool, COMPARE_SAMPLE, SAMPLE_SEED)
    by_day: dict[date, list[str]] = defaultdict(list)
    for instrument, day in sample:
        by_day[day].append(instrument)
    bars: dict[tuple[str, date], dict[time, tuple[float, ...]]] = defaultdict(dict)
    with closing(ctx.store.connect()) as con:
        for day, instruments in sorted(by_day.items()):
            path = ctx.root / DATASET / f"date={day.isoformat()}" / FILE_NAME
            for i, start, o, h, lo, c, v in con.execute(
                _DAY_BARS_SQL, [str(path), instruments]
            ).fetchall():
                at = start.astimezone(IST)
                bars[(i, at.date())][at.time()] = (float(o), float(h), float(lo), float(c), int(v))
    names = _symbols(ctx)
    try:
        progress = json.loads((ctx.root / "minute_history" / "progress.json").read_text())
    except (OSError, ValueError):
        progress = {}
    windows = fyers_windows(progress)
    by_writer: dict[str, Counter[str]] = defaultdict(Counter)
    by_instrument: dict[str, list[date]] = defaultdict(list)
    for instrument, day in sample:
        by_instrument[instrument].append(day)
    counts: Counter[str] = Counter()
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for instrument, days in sorted(by_instrument.items()):
        rows: list[SourceBar] = []
        for symbol in names.get(instrument, ()):
            for folder in ("raw_5m", "raw_5m_former"):
                path = ctx.kotak / folder / f"{symbol}.parquet"
                if path.is_file():
                    rows += source_rows(path, days)
        for day in days:
            year = by_year[str(day.year)]
            fyers = any(
                (w := windows.get(n)) is not None and w[0] <= day <= w[1]
                for n in names.get(instrument, ())
            )
            writer = by_writer["fyers_window" if fyers else "kotak_import"]
            source = [r for r in rows if r.label.date() == day]
            item = {"instrument_id": instrument, "day": day.isoformat()}
            writer["days"] += 1
            if not source:
                writer["stored_day_without_source"] += 1
                counts["stored_day_without_source"] += 1
                items["stored_day_without_source"].append(item)
                continue
            result = compare_bars(day, bars[(instrument, day)], source)
            counts["days"] += 1
            counts["bars"] += result.bars
            year[f"shift_{result.shift}"] += 1
            counts["scaled_days"] += abs(result.scale - 1) > PRICE_SLACK
            counts["alignment_not_rule"] += not result.rule
            for code, n in (
                ("bar_price_differs", result.price),
                ("bar_volume_differs", result.volume),
                ("stored_bar_not_in_source", result.stored_only),
                ("source_bar_not_stored", result.source_only),
            ):
                if n:
                    counts[code] += n
                    year[code] += n
                    writer[code] += n
                    items[code].append({**item, "bars": n, "scale": round(result.scale, 6)})
    findings = [
        dated(
            {
                **finding(code, FAIL, items[code]),
                "count": counts[code],
                "days_with": len(items[code]),
            },
            [i["day"] for i in items[code]],
        )
        for code in (
            "stored_day_without_source",
            "bar_price_differs",
            "bar_volume_differs",
            "stored_bar_not_in_source",
            "source_bar_not_stored",
        )
        if items[code]
    ]
    summary = {
        "sample": len(sample),
        "of_stored_days": len(pool),
        **{k: counts[k] for k in ("days", "bars", "scaled_days", "alignment_not_rule")},
        "by_year": {y: dict(c) for y, c in sorted(by_year.items())},
        "by_writer": {w: dict(c) for w, c in sorted(by_writer.items())},
    }
    return summary, findings


def _folder_date(path: Path) -> date | None:
    try:
        return date.fromisoformat(path.name.removeprefix("data-"))
    except ValueError:
        return None


def _classify_all(
    ctx: Context,
    refused: Mapping[tuple[str, date], str],
    stored: set[tuple[str, date]],
    screened: set[tuple[str, date]],
    actions: Mapping[str, Sequence[tuple[date, Decimal]]],
    fetched_on: date,
    tolerance: Decimal,
) -> tuple[
    Counter[str], dict[int, Counter[str]], dict[str, list[dict[str, Any]]], list[dict[str, Any]]
]:  # pragma: no cover
    assert ctx.kotak is not None
    by_symbol: dict[str, list[tuple[str, date]]] = defaultdict(list)
    for (instrument, day), symbol in refused.items():
        by_symbol[symbol].append((instrument, day))
    classes: Counter[str] = Counter()
    by_year: dict[int, Counter[str]] = defaultdict(Counter)
    items: dict[str, list[dict[str, Any]]] = defaultdict(list)
    pool: list[dict[str, Any]] = []
    with closing(ctx.store.connect()) as con:
        for symbol, pairs in sorted(by_symbol.items()):
            days: dict[date, DayBars] = {}
            for folder in ("raw_5m", "raw_5m_former"):
                path = ctx.kotak / folder / f"{symbol}.parquet"
                if path.is_file():
                    days = {**session_days(_candles(path)), **days}
            for instrument in sorted({i for i, _ in pairs}):
                wanted = sorted(d for i, d in pairs if i == instrument)
                daily = {
                    d: DailyRange(o, h, lo, c)
                    for _, d, o, h, lo, c in con.execute(
                        _DAILY_SQL, [instrument, wanted[0], wanted[-1]]
                    ).fetchall()
                }
                for day in wanted:
                    bars, bar = days.get(day), daily.get(day)
                    factor = fyers_factor(actions.get(instrument, ()), day, fetched_on)
                    code = classify(
                        bars,
                        bar,
                        (instrument, day) in stored,
                        factor,
                        tolerance,
                        (instrument, day) in screened,
                    )
                    classes[code] += 1
                    by_year[day.year][code] += 1
                    evidence = {
                        "symbol": symbol,
                        "day": day.isoformat(),
                        "class": code,
                        "daily": None
                        if bar is None
                        else [str(bar.open), str(bar.high), str(bar.low), str(bar.close)],
                        "kotak": None
                        if bars is None
                        else [
                            bars.open,
                            bars.high,
                            bars.low,
                            bars.close,
                            bars.candles,
                            bars.high_at,
                            bars.low_at,
                        ],
                        "factor_after": str(factor),
                    }
                    items[code].append(evidence)
                    pool.append(evidence)
    return classes, by_year, items, pool

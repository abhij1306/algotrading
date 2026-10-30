"""D4: the 5-minute history's refused days (`bars_5m_rejects`, rule `basis_mismatch`) traced
back to the Kotak raw files they came from (`--kotak`), and the stored days checked again.

The raw files are read here on their own, with their own time labelling: a day whose traded bars
include one labelled 15:30 is labelled by bar end (each bar starts five minutes before its label),
any other by bar start (`end_labelled`); the importer's switch date is not used. In the export of
2026-10-28 every day to 2022-01-21 has a traded 15:30 row and no later day has one; 4,956 of
those early days also have a traded row labelled 09:15, the pre-open print (the bar ending
09:15), which the importer drops as outside the session. Each refused (instrument, day) is
aggregated from those candles (the session's 5-minute bars from 09:15 to 15:30 IST), and held
against the raw daily bar with the import's envelope (`bars_basis.check_day`, tolerance
`history_envelope_tol`, never loosened here). The first class that fits, in this order:

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
compared with the raw file's bars of that day (`compare_bars`): bars pair by the labelling rule
(`end_labelled`), and a day the other alignment fits better fails (`bar_alignment_not_rule`: the
stored labels are a bar off); the day's scale (the export's prices carry the later actions, one
number a day) is found from the bars themselves, and then every stored bar must be a source bar
at that scale (prices within `PRICE_TICK` of a tick) with the source's volume as it is (the
export does not adjust its volume: `bars_basis.to_raw`), and every traded source bar in the
session must be stored. Independently of both vendors, each sampled day's stored session volume
must reconcile with the bhavcopy's (`day_volume_fits`, else `day_volume_differs`; a short day
inside a known coverage gap, `bars5m_gaps`, warns as `day_volume_in_gap`). A stored day with no
raw file, a bar that differs, or a bar on one side only fails (dated), except (`judge_day`):
- the session's last bar (15:25) differing only in its close, one side the daily close
  (`last_bar_close`, warn): NSE's close is the closing auction's price, which one copy of the
  vendor's history stamps on the last bar and the other does not (`close_convention`);
- a day whose differences are all a trade at a bucket boundary filed in the neighbouring bar
  (`vendor_bucket_boundary`, warn: the same bars, day volume, high and low, prices within
  `PRICE_BOUND`);
- a day inside a Fyers window (the store's bars came from Fyers' minute history there,
  `minute_history/progress.json`, or a day `bars5m_refetch` wrote; Kotak is a second vendor, not
  their source): prices within `PRICE_BOUND`, and bar volumes or bars Kotak lacks on a day
  whose volume reconciles with the bhavcopy, warn as `second_vendor_differs`; a traded Kotak bar
  the store lacks still fails. A Fyers window day of a symbol the export has no file for at all
  warns as `second_vendor_missing`; a day missing from a file the export has fails;
- a failed day the reviewed list names (`VERIFY_EXCEPTIONS["d4"]`: code, ISIN, day, the item's
  values when reviewed, such as `bars` with `worst` or `scale`, and the reason with its evidence),
  warned as `excepted` (`exceptions.py`).
Every day `bars5m_refetch` wrote opens at NSE's daily open, the pre-open auction's price, to
within `PRICE_TICK` (`open_off_auction`), else it fails (`open_not_auction`: Fyers' 09:15 bar
can open at the first continuous trade) unless a `bars5m_gaps` row refuses it to the replay
(`open_not_auction_in_gap`, warn). The whole store's 09:15 opens are summarised by year against
NSE's open (`first_open_vs_nse`): most vendor days before November 2025 open at the first trade.
The envelope check above holds each stored day to the raw daily bar, so the scale itself is
checked there. The summary splits the compare by writer (`by_writer`) and gives the volume ratio
by writer.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.paths import BARS5M_GAPS, VERIFY_EXCEPTIONS

from ...money import require_exact
from ...time import IST
from ..audit import FAIL, WARN, check_result, finding
from ..bars5m_gaps import gap_spans, in_gap, read_gaps, refetched_days
from ..bars_basis import BASIS_MISMATCH, DailyRange, check_day, fyers_factor
from . import dated, seeded_sample
from .context import Context
from .exceptions import Exceptions, read_exceptions

if TYPE_CHECKING:
    from ..store import DataStore

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
# The largest relative price difference a second vendor's bar or a bucket-boundary trade may show
# and still only warn: a few ticks on a cheap stock, never a wrong level (`bucket_boundary`).
PRICE_BOUND = 0.005
# The stored session volume against the bhavcopy's (`day_volume_fits`): the bhavcopy also counts
# the pre-open auction and the closing session, so a day may fall short of it by this much and
# never exceed it by more than `DAY_VOLUME_OVER` (a share counted twice at a bucket edge).
DAY_VOLUME_SHORT = 0.10
DAY_VOLUME_OVER = 0.01
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
    bar's end). A traded 09:15 row does not say otherwise: on an end-labelled day it is the
    pre-open print."""
    return any(b.volume > 0 and b.label.time() == time(15, 30) for b in bars)


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
    rule: bool  # no other alignment matches more bars than `end_labelled`'s
    close_convention: int = (
        0  # price differences that are the last bar's close (`close_convention`)
    )
    worst: float = 0.0  # the largest relative price difference of a differing pair
    stored_volume: int = 0  # the day's stored session volume
    source_volume: int = 0  # the source's traded session volume
    extremes: bool = True  # the day's high and low the same in both, at the scale


def bucket_boundary(result: BarCompare) -> bool:
    """Whether a day's differences are only trades at a bucket boundary filed in the neighbouring
    bar by one copy (`vendor_bucket_boundary`, warn): the same bars on both sides, the same day
    volume (`VOLUME_SLACK`), the same day high and low, and no price off by more than
    `PRICE_BOUND`. Such a trade moves an open, a close or a volume between adjacent bars and
    nothing else; neither copy is wrong."""
    return (
        not result.stored_only
        and not result.source_only
        and result.extremes
        and result.worst <= PRICE_BOUND
        and abs(result.stored_volume - result.source_volume)
        <= max(1.0, VOLUME_SLACK * result.source_volume)
    )


def judge_day(result: BarCompare, fyers: bool, volume_fits: bool) -> list[tuple[str, int, bool]]:
    """A compared day's findings, (code, bars, warn only): every difference fails, except
    - a day whose differences are all at bucket boundaries (`bucket_boundary`):
      `vendor_bucket_boundary`, warn;
    - inside a Fyers window (`fyers`: Kotak is a second vendor there, not the bars' source),
      prices off by at most `PRICE_BOUND`, and bar volumes or bars Kotak lacks on a day whose
      stored volume reconciles with the bhavcopy's (`volume_fits`, independent of both vendors):
      `second_vendor_differs`, warn. A traded source bar the store lacks always fails."""
    found = {
        "bar_price_differs": result.price,
        "bar_volume_differs": result.volume,
        "stored_bar_not_in_source": result.stored_only,
        "source_bar_not_stored": result.source_only,
    }
    if bucket_boundary(result) and (result.price or result.volume):
        return [("vendor_bucket_boundary", result.price + result.volume, True)]
    out = []
    for code, n in found.items():
        if not n:
            continue
        excused = fyers and (
            result.worst <= PRICE_BOUND
            if code == "bar_price_differs"
            else volume_fits and code != "source_bar_not_stored"
        )
        out.append(("second_vendor_differs", n, True) if excused else (code, n, False))
    return out


def day_volume_ratio(stored: int, daily: int | None) -> float | None:
    """The day's stored session volume over the bhavcopy's (the raw daily bar's), or None
    without a daily volume."""
    return None if not daily else stored / daily


def day_volume_fits(ratio: float) -> bool:
    """Whether a day's stored volume reconciles with the bhavcopy's: at most `DAY_VOLUME_OVER`
    above it and `DAY_VOLUME_SHORT` below (the bhavcopy also counts the pre-open auction and the
    closing session, which no session bar holds)."""
    return 1 - DAY_VOLUME_SHORT <= ratio <= 1 + DAY_VOLUME_OVER


def open_off_auction(first_open: float, nse_open: float) -> bool:
    """Whether a session's 09:15 bar opens off NSE's daily open, the pre-open auction's price, by
    more than `PRICE_TICK` (both on the raw basis): the bar opens at the first continuous trade."""
    return abs(first_open - nse_open) > PRICE_TICK


def judge_opens(
    opens: Mapping[tuple[str, date], tuple[float, float]],
    isins: Mapping[str, str],
    gaps: Mapping[str, tuple[tuple[date, date], ...]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The days of `opens` ((instrument, day): the stored 09:15 open and NSE's open) whose 09:15
    bar is off the auction (`open_off_auction`), split into those outside any known coverage gap
    (`bars5m_gaps`: the replay would read them) and those inside one (the replay refuses them)."""
    outside: list[dict[str, Any]] = []
    inside: list[dict[str, Any]] = []
    for (instrument, day), (first, nse) in sorted(opens.items(), key=lambda kv: kv[0][::-1]):
        if not open_off_auction(first, nse):
            continue
        isin = isins.get(instrument, "")
        item = {
            "instrument_id": instrument,
            "isin": isin,
            "day": day.isoformat(),
            "open_915": first,
            "nse_open": nse,
        }
        (inside if in_gap(gaps, isin, day) else outside).append(item)
    return outside, inside


LAST_BAR = time(15, 25)


def close_convention(
    start: time, stored: Sequence[float], source: SourceBar, scale: float, daily_close: float | None
) -> bool:
    """Whether a pair differs only as the closing auction makes it: the session's last bar, every
    price but the close matching, and one side's close the daily close (NSE's close is the
    auction's price, which one copy of the history stamps on the last bar)."""
    if start != LAST_BAR or daily_close is None:
        return False
    rest = SourceBar(source.label, source.open, source.high, source.low, stored[3] / scale, 0)
    if not _matches(stored, rest, scale):
        return False
    tick = max(PRICE_TICK * scale, PRICE_SLACK * abs(daily_close))
    return abs(stored[3] - daily_close) <= tick or abs(source.close * scale - daily_close) <= tick


def _off(stored: Sequence[float], source: SourceBar, scale: float) -> float:
    """The largest relative difference of a pair's prices at the scale."""
    return max(
        abs(mine - theirs * scale) / abs(mine) if mine else 0.0
        for mine, theirs in zip(
            stored[:4], (source.open, source.high, source.low, source.close), strict=True
        )
    )


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
    day: date,
    stored: Mapping[time, Sequence[float]],
    source: Sequence[SourceBar],
    daily_close: float | None = None,
) -> BarCompare:
    """A day's stored bars (start time -> open, high, low, close, volume) against the source's
    rows of that day. The bars pair by the labelling rule (`end_labelled`: 0 or 5 minutes from
    start to label), never by the alignment that fits best, so bars stored a bar off still differ;
    `rule` is False when the other alignment would match more bars (the stored labels are off).
    The scale is the median of stored close over source close; then each stored bar's prices
    against the scaled source bar (a `close_convention` pair counted apart) and its volume against
    the source's as it is (the export's volume is raw: `bars_basis.to_raw`), and the bars on one
    side only (a source bar counts when it is traded and starts in the session)."""
    by_label = {b.label.time(): b for b in source if b.label.date() == day}
    shift = 5 if end_labelled(by_label.values()) else 0
    scores = []
    for move in (shift, 5 - shift):
        found = _pairs(stored, by_label, day, move)
        found_scale = _scale(found)
        scores.append(sum(_matches(v, b, found_scale) for v, b in found))
    pairs = _pairs(stored, by_label, day, shift)
    scale = _scale(pairs)
    move = timedelta(minutes=shift)
    timed = [
        (t, v, b)
        for t, v in stored.items()
        if (b := by_label.get((datetime.combine(day, t) + move).time())) is not None
    ]
    differ = [(t, v, b) for t, v, b in timed if not _matches(v, b, scale)]
    conventional = [close_convention(t, v, b, scale, daily_close) for t, v, b in differ]
    convention = sum(conventional)
    volume = sum(abs(v[4] - b.volume) > max(1.0, VOLUME_SLACK * abs(v[4])) for v, b in pairs)
    traded = {(datetime.combine(day, t) - move).time() for t, b in by_label.items() if b.volume > 0}
    in_session = [
        b
        for t, b in by_label.items()
        if b.volume > 0 and SESSION[0] <= (datetime.combine(day, t) - move).time() < SESSION[1]
    ]
    source_only = sum(1 for t in traded if SESSION[0] <= t < SESSION[1] and t not in stored)
    mine = [v for t, v in stored.items() if SESSION[0] <= t < SESSION[1]]
    extremes = bool(mine and in_session) and (
        abs(max(v[1] for v in mine) - max(b.high for b in in_session) * scale) <= PRICE_TICK * scale
        and abs(min(v[2] for v in mine) - min(b.low for b in in_session) * scale)
        <= PRICE_TICK * scale
    )
    return BarCompare(
        shift,
        scale,
        len(stored),
        len(differ) - convention,
        volume,
        len(stored) - len(pairs),
        source_only,
        scores[0] >= scores[1],
        convention,
        max(
            (_off(v, b, scale) for (_, v, b), c in zip(differ, conventional, strict=True) if not c),
            default=0.0,
        ),
        int(sum(v[4] for v in mine)),
        sum(b.volume for b in in_session),
        extremes,
    )


def restored_volumes(
    day: date,
    stored: Mapping[time, Sequence[float]],
    source: Sequence[SourceBar],
    daily_volume: int | None,
) -> dict[time, int] | None:
    """The export's volume of each stored bar of `day` (by start), when the export proves the
    day: the bars pair by the labelling rule, every stored bar has a source bar of the same
    prices at the day's scale (`compare_bars`: prices within `PRICE_BOUND`, the export's rounding
    of its adjusted prices to the tick; none stored only; the rule's alignment), and the restored
    session volume reconciles with the bhavcopy's (`day_volume_fits`). None otherwise, and when
    a paired export bar has a negative volume (the export has such rows): the day is left as it
    is."""
    result = compare_bars(day, stored, source)
    if result.worst > PRICE_BOUND or result.stored_only or not result.rule or not daily_volume:
        return None
    by_label = {b.label.time(): b for b in source if b.label.date() == day}
    move = timedelta(minutes=result.shift)
    out = {}
    for t in stored:
        found = by_label.get((datetime.combine(day, t) + move).time())
        if found is None or found.volume < 0:
            return None
        out[t] = found.volume
    session = sum(v for t, v in out.items() if SESSION[0] <= t < SESSION[1])
    return out if day_volume_fits(session / daily_volume) else None


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


def one_per_label(rows: Iterable[SourceBar]) -> list[SourceBar]:
    """The first row of each label: a day in both a symbol's file and its former symbol's is
    one day of bars, not two."""
    seen: dict[datetime, SourceBar] = {}
    for row in rows:
        seen.setdefault(row.label, row)
    return list(seen.values())


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


_FIRST_OPENS_SQL = """
WITH f AS (
    SELECT CAST(instrument_id AS VARCHAR) AS instrument_id,
        CAST(timezone('Asia/Kolkata', bar_start) AS DATE) AS day, open
    FROM read_parquet($files)
    WHERE CAST(timezone('Asia/Kolkata', bar_start) AS TIME) = TIME '09:15'
)
SELECT f.instrument_id, f.day, f.open, r.open FROM f
JOIN bars_daily_raw_latest r
    ON CAST(r.instrument_id AS VARCHAR) = f.instrument_id AND r.date = f.day
WHERE r.date BETWEEN $first AND $final AND r.open > 0
"""


def first_opens(
    store: DataStore,
    wanted: Collection[tuple[str, date]],
    span: tuple[date, date] = (date.min, date.max),
    *,
    whole_store: bool = True,
) -> tuple[dict[str, dict[str, int]], dict[tuple[str, date], tuple[float, float]]]:
    """The stored 09:15 opens against NSE's daily open inside `span`: the days and those off the
    auction (`open_off_auction`) by year, and the (09:15 open, NSE open) of the `wanted` days.
    Without `whole_store` only the day files of the `wanted` days are read (no summary)."""
    from ..bars_store import DATASET, FILE_NAME  # noqa: PLC0415

    keep = set(wanted)
    files = sorted((store.root / DATASET).glob(f"date=*/{FILE_NAME}"))
    if not whole_store:
        days = {d.isoformat() for _, d in keep}
        files = [f for f in files if f.parent.name[5:] in days]
    by_year: dict[str, Counter[str]] = defaultdict(Counter)
    found: dict[tuple[str, date], tuple[float, float]] = {}
    with closing(store.connect()) as con:
        for at in range(0, len(files), 60):
            chunk = files[at : at + 60]
            first, final = (date.fromisoformat(f.parent.name[5:]) for f in (chunk[0], chunk[-1]))
            rows = con.execute(
                _FIRST_OPENS_SQL, {"files": [str(f) for f in chunk], "first": first, "final": final}
            ).fetchall()
            for instrument, day, opened, nse in rows:
                if not span[0] <= day <= span[1]:
                    continue
                pair = (float(opened), float(nse))
                if whole_store:
                    year = by_year[str(day.year)]
                    year["days"] += 1
                    year["off_auction"] += open_off_auction(*pair)
                if (instrument, day) in keep:
                    found[(instrument, day)] = pair
    return {y: dict(c) for y, c in sorted(by_year.items())}, found


def _check_opens(ctx: Context) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # pragma: no cover
    """The 09:15 open of every day `bars5m_refetch` wrote against NSE's daily open: one off the
    auction outside a known coverage gap fails (`open_not_auction`; the replay would enter at a
    price the auction did not make), one inside a gap warns (`open_not_auction_in_gap`). The
    whole store's 09:15 opens are summarised by year (`by_year`)."""
    refetched = refetched_days(ctx.root)
    by_year, opens = first_opens(ctx.store, refetched, (ctx.start, ctx.end))
    isins = dict(ctx.rows("SELECT CAST(id AS TEXT), isin FROM hat_instrument"))
    outside, inside = judge_opens(opens, isins, gap_spans(read_gaps(BARS5M_GAPS)))
    summary = {
        "by_year": by_year,
        "refetched_days": len(opens),
        "refetched_off_auction": len(outside) + len(inside),
        "refetched_off_auction_in_gap": len(inside),
        "tick": PRICE_TICK,
    }
    findings = []
    if outside:
        findings.append(
            dated(finding("open_not_auction", FAIL, outside), [i["day"] for i in outside])
        )
    if inside:
        findings.append({**finding("open_not_auction_in_gap", WARN, inside), "count": len(inside)})
    return summary, findings


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
    summary["first_open_vs_nse"], open_findings = _check_opens(ctx)
    findings += open_findings
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


_DAILY_DAY_SQL = """
SELECT CAST(instrument_id AS VARCHAR), high, low, close, volume FROM bars_daily_raw_latest
WHERE date = ? AND CAST(instrument_id AS VARCHAR) IN (SELECT unnest(CAST(? AS VARCHAR[])))
"""

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


FAILS = (
    "stored_day_without_source",
    "bar_alignment_not_rule",
    "bar_price_differs",
    "bar_volume_differs",
    "stored_bar_not_in_source",
    "source_bar_not_stored",
    "day_volume_differs",
)
WARNS = (
    "last_bar_close",
    "vendor_bucket_boundary",
    "second_vendor_differs",
    "second_vendor_missing",
    "day_volume_in_gap",
)


@dataclass
class _Tally:
    counts: Counter[str] = field(default_factory=Counter)
    by_year: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    by_writer: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))
    items: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))
    ratios: dict[str, Counter[str]] = field(default_factory=lambda: defaultdict(Counter))

    def add(self, code: str, n: int, item: Mapping[str, Any], writer: str, year: str) -> None:
        self.counts[code] += n
        self.by_writer[writer][code] += n
        self.by_year[year][code] += n
        self.items[code].append(dict(item))


def _sample_bars(
    ctx: Context, sample: Sequence[tuple[str, date]]
) -> tuple[
    dict[tuple[str, date], dict[time, tuple[float, ...]]],
    dict[tuple[str, date], tuple[float, float, float, int | None]],
]:  # pragma: no cover - real data
    """The sample's stored bars and raw daily bars (high, low, close, volume)."""
    from ..bars_store import DATASET, FILE_NAME  # noqa: PLC0415

    by_day: dict[date, list[str]] = defaultdict(list)
    for instrument, day in sample:
        by_day[day].append(instrument)
    bars: dict[tuple[str, date], dict[time, tuple[float, ...]]] = defaultdict(dict)
    daily: dict[tuple[str, date], tuple[float, float, float, int | None]] = {}
    with closing(ctx.store.connect()) as con:
        for day, instruments in sorted(by_day.items()):
            for i, h, lo, c, v in con.execute(_DAILY_DAY_SQL, [day, instruments]).fetchall():
                daily[(i, day)] = (float(h), float(lo), float(c), None if v is None else int(v))
            path = ctx.root / DATASET / f"date={day.isoformat()}" / FILE_NAME
            for i, start, o, h, lo, c, v in con.execute(
                _DAY_BARS_SQL, [str(path), instruments]
            ).fetchall():
                at = start.astimezone(IST)
                bars[(i, at.date())][at.time()] = (float(o), float(h), float(lo), float(c), int(v))
    return bars, daily


def _kotak_rows(
    kotak: Path, symbols: Sequence[str], days: Sequence[date]
) -> tuple[list[SourceBar], bool]:  # pragma: no cover - files
    """The export's rows of `days` under any of the symbols, and whether it has a file for one."""
    rows: list[SourceBar] = []
    has_file = False
    for symbol in symbols:
        for folder in ("raw_5m", "raw_5m_former"):
            path = kotak / folder / f"{symbol}.parquet"
            if path.is_file():
                has_file = True
                rows += source_rows(path, days)
    return one_per_label(rows), has_file


def _compare_sample(
    ctx: Context, stored: set[tuple[str, date]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # pragma: no cover - real data
    """`compare_bars` on a seeded sample of the stored days, each judged by `judge_day`, and its
    stored volume against the bhavcopy's (`day_volume_fits`; a short day inside a known coverage
    gap, `bars5m_gaps`, warns): the summary and the findings."""
    assert ctx.kotak is not None
    pool = sorted((i, d) for i, d in stored if ctx.start <= d <= ctx.end)
    sample = seeded_sample(pool, COMPARE_SAMPLE, SAMPLE_SEED)
    bars, daily = _sample_bars(ctx, sample)
    names = _symbols(ctx)
    isins = dict(ctx.rows("SELECT CAST(id AS TEXT), isin FROM hat_instrument"))
    gaps = gap_spans(read_gaps(BARS5M_GAPS))
    try:
        progress = json.loads((ctx.root / "minute_history" / "progress.json").read_text())
    except (OSError, ValueError):
        progress = {}
    windows = fyers_windows(progress)
    refetched = refetched_days(ctx.root)
    by_instrument: dict[str, list[date]] = defaultdict(list)
    for instrument, day in sample:
        by_instrument[instrument].append(day)
    tally = _Tally()
    for instrument, days in sorted(by_instrument.items()):
        rows, has_file = _kotak_rows(ctx.kotak, names.get(instrument, ()), days)
        for day in days:
            fyers = (instrument, day) in refetched or any(
                (w := windows.get(n)) is not None and w[0] <= day <= w[1]
                for n in names.get(instrument, ())
            )
            _judge_one(
                tally,
                (instrument, day, isins.get(instrument, "")),
                bars[(instrument, day)],
                daily.get((instrument, day)),
                [r for r in rows if r.label.date() == day],
                fyers=fyers,
                has_file=has_file,
                gaps=gaps,
            )
    return _report(tally, len(sample), len(pool))


def _judge_one(  # noqa: PLR0913 - one day's evidence
    tally: _Tally,
    key: tuple[str, date, str],
    mine: Mapping[time, Sequence[float]],
    bar: tuple[float, float, float, int | None] | None,
    source: Sequence[SourceBar],
    *,
    fyers: bool,
    has_file: bool,
    gaps: Mapping[str, tuple[tuple[date, date], ...]],
) -> None:  # pragma: no cover - real data
    instrument, day, isin = key
    writer, year = ("fyers_window" if fyers else "kotak_import"), str(day.year)
    item: dict[str, Any] = {"instrument_id": instrument, "isin": isin, "day": day.isoformat()}
    tally.by_writer[writer]["days"] += 1
    fits = _judge_volume(tally, item, writer, mine, None if bar is None else bar[3], gaps)
    if not source:
        # a second vendor that never carried the symbol proves nothing; one that has the
        # symbol but not the day, or the bars' own source without the day, fails
        code = "second_vendor_missing" if fyers and not has_file else "stored_day_without_source"
        tally.add(code, 1, item, writer, year)
        return
    result = compare_bars(day, mine, source, None if bar is None else bar[2])
    tally.counts["days"] += 1
    tally.counts["bars"] += result.bars
    tally.counts["scaled_days"] += abs(result.scale - 1) > PRICE_SLACK
    tally.by_year[year][f"shift_{result.shift}"] += 1
    if result.close_convention:
        tally.add(
            "last_bar_close",
            result.close_convention,
            {**item, "bars": result.close_convention},
            writer,
            year,
        )
    if not result.rule:
        tally.add("bar_alignment_not_rule", 1, item, writer, year)
    for code, n, _warn in judge_day(result, fyers, fits):
        found = {
            **item,
            "bars": n,
            "scale": round(result.scale, 6),
            "worst": round(result.worst, 6),
        }
        tally.add(code, n, found, writer, year)


def _judge_volume(
    tally: _Tally,
    item: Mapping[str, Any],
    writer: str,
    mine: Mapping[time, Sequence[float]],
    daily_volume: int | None,
    gaps: Mapping[str, tuple[tuple[date, date], ...]],
) -> bool:  # pragma: no cover - real data
    """Whether the day's stored session volume reconciles with the bhavcopy's (`day_volume_fits`),
    tallied by writer; a day off it fails, or warns inside a known coverage gap when short."""
    day = date.fromisoformat(item["day"])
    stored_volume = int(sum(v[4] for t, v in mine.items() if SESSION[0] <= t < SESSION[1]))
    ratio = day_volume_ratio(stored_volume, daily_volume)
    if ratio is None:
        return False
    tally.ratios[writer][_ratio_bucket(ratio)] += 1
    if day_volume_fits(ratio):
        return True
    short_in_gap = in_gap(gaps, item["isin"], day) and ratio < 1
    code = "day_volume_in_gap" if short_in_gap else "day_volume_differs"
    tally.add(code, 1, {**item, "ratio": round(ratio, 4)}, writer, str(day.year))
    return False


def _ratio_bucket(ratio: float) -> str:
    for edge in (0.5, 0.9, 0.98, 1.0, 1.01):
        if ratio < edge:
            return f"<{edge}"
    return ">=1.01"


def _report(
    tally: _Tally, sample: int, pool: int
) -> tuple[dict[str, Any], list[dict[str, Any]]]:  # pragma: no cover - real data
    exceptions = Exceptions(
        read_exceptions(VERIFY_EXCEPTIONS["d4"], "D4"),
        key_of=lambda _code, i: (i.get("isin"), i["day"]),
    )
    findings = []
    for code in FAILS:
        failed = exceptions.split(code, tally.items[code])
        if failed:
            findings.append(
                dated(
                    {
                        **finding(code, FAIL, failed),
                        "count": sum(i.get("bars", 1) for i in failed),
                        "days_with": len(failed),
                    },
                    [i["day"] for i in failed],
                )
            )
    for code in WARNS:
        if tally.items[code]:
            findings.append(
                {
                    **finding(code, WARN, tally.items[code]),
                    "count": tally.counts[code],
                    "days_with": len({(i["instrument_id"], i["day"]) for i in tally.items[code]}),
                }
            )
    findings += exceptions.findings()
    summary = {
        "sample": sample,
        "of_stored_days": pool,
        **{k: tally.counts[k] for k in ("days", "bars", "scaled_days")},
        "by_year": {y: dict(c) for y, c in sorted(tally.by_year.items())},
        "by_writer": {w: dict(c) for w, c in sorted(tally.by_writer.items())},
        # the stored session volume over the bhavcopy's, by writer (`day_volume_fits`)
        "day_volume_ratio": {w: dict(sorted(c.items())) for w, c in sorted(tally.ratios.items())},
        "price_bound": PRICE_BOUND,
        "day_volume_bounds": [1 - DAY_VOLUME_SHORT, 1 + DAY_VOLUME_OVER],
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

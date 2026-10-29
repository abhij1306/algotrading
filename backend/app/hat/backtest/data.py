"""The market a backtest reads (HAT-BT-011, HAT-BT-012), loaded point in time, and the day loop's
views of it.

Bars come from the price-basis view of the Parquet store (`bars_daily_cap_adj`: raw bars times the
stored price-changing corporate-action factors, the latest ingest run's row per instrument and day,
read for the universe's instruments only), with each bar's `div_factor`, the factor of the dividends
after it (CA3). The run fills, marks and pays dividends on the price basis; every signal read (the
scan's window, the SMA, swing low and ATR) is the total-return series rescaled to the day it is read
for (`MarketView`), as live reads it that day, so an ex-date drop is no signal and a held position
earns the cash (`MarketData.dividends`). Features come from the `features_daily` partitions the
nightly job wrote, the regime index from `index_daily`, and membership is the universe's index
members as of each session (`point_in_time_members`, from `hat_index_member`). Nothing else is
filtered by what happened later: the day loop reads each input only up to the date it decides for
(`MarketView`). The features must be the ones the nightly job wrote for the corporate actions the
bars are adjusted with: the job's stored inputs digest must equal the digest of the stored actions
over the whole store (HAT-DATA-077, PR 67). A run under the `policy` regime rule also reads the
regime engine's inputs: the NIFTY 500 and India VIX closes and the features job's breadth of each
session (PR 68). A name is not scanned or entered on the days its adjusted history shows a factor
not yet knowable (CA5, `MarketData.pending`).
"""

from __future__ import annotations

import hashlib
import math
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import inspect, select
from sqlalchemy.orm import Session

from ..data.bars_store import BarStore
from ..data.ca_versions import digest as versions_digest
from ..data.chains import stored_adjustment
from ..data.demerger_notice import exit_day
from ..data.features import features_read_lock
from ..data.features_state import features_current, inputs_digest
from ..data.identity import series_changes
from ..data.index_history import NIFTY_500_TRI
from ..data.lineage_review import unexplained_jumps
from ..data.point_in_time import chain_pending
from ..data.series_break import segment_start
from ..data.store import (
    DATASET_BARS,
    DATASET_BREADTH,
    DATASET_FEATURES,
    Adjustment,
    DataStore,
    latest_rows,
)
from ..data.ticks import scaled_tick, tick_for_date
from ..engine.portfolio import regime
from ..engine.portfolio.universe import Interval
from ..engine.regime_gate import BREADTH_SESSIONS
from ..engine.stops import mean_true_range
from ..engine.trigger import ResultEvent
from ..models import (
    HatCorporateAction,
    HatFinancialGrowth,
    HatFinancialResult,
    HatIndexMember,
    HatIndexMembership,
    HatIndustry,
    HatInstrument,
    HatInstrumentSymbolHistory,
    HatUniverseVersion,
)
from ..money import exact
from ..time import IST, as_utc
from .spec import BacktestError, membership_key

__all__ = [
    "HINDSIGHT_BREAKOUT_VOL_RATIO",
    "HINDSIGHT_DELISTED_EXIT",
    "HINDSIGHT_EP_CLOSE",
    "HINDSIGHT_EP_VOLUME",
    "HINDSIGHT_FULL_DAY_PACE",
    "JUMP_SKIP_SESSIONS",
    "LOOKAHEAD",
    "LOOKBACK_SESSIONS",
    "STOPPED_SLACK_SESSIONS",
    "Bar",
    "MarketData",
    "MarketView",
    "classifications",
    "industry_on",
    "jump_skips",
    "looks_ahead",
    "public_by",
    "load_market",
    "membership_cover_refusal",
    "point_in_time_members",
    "real_shares",
    "scan_night_members",
    "stopped_trading",
]

LOOKAHEAD = "backtest_lookahead"  # a decision read data stamped after its instant (`MarketView`)
# the reads `MarketView.hindsight` counts, by reason: the run audit warns `lookahead_hindsight`
HINDSIGHT_FULL_DAY_PACE = "volume_pace_full_day"  # a 5m session paced on the day's whole volume
HINDSIGHT_EP_VOLUME = "ep_estimate_volume"  # a daily EP estimate gated on the day's own volume
HINDSIGHT_EP_CLOSE = "ep_estimate_close"  # a daily EP estimate fired on the day's high, low, close
HINDSIGHT_BREAKOUT_VOL_RATIO = "breakout_vol_ratio"  # a feature column, read by no rule
HINDSIGHT_DELISTED_EXIT = "delisted_exit"  # a delisting known from the registry, not on the day


def public_by(filings: Iterable[ResultEvent], day: date) -> tuple[ResultEvent, ...]:
    """The filings public by the end of `day` (IST): what a trigger on `day` may read; it reads
    only those public by its own instant."""
    return tuple(r for r in filings if as_utc(r.public_at).astimezone(IST).date() <= day)


def looks_ahead(day: date, session: date | None, known: date | None, *, inclusive: bool) -> bool:
    """Whether a read of `day` (through it if `inclusive`, else only before it) looks past the
    clock: `session` the session in progress (None: no clock, nothing is refused) and `known` the
    last session whose close is public (None: none is). A decision read through `day` needs
    `day <= known`; a read before `day`, or the in-progress session's own bar, `day <= session`."""
    if session is None:
        return False
    if not inclusive:
        return day > session
    return known is None or day > known


# The scan reads at most 652 sessions back (a 400-session SMA compared 252 sessions earlier);
# the window it is given holds more, so its watchlist equals a scan of the full history.
LOOKBACK_SESSIONS = 700
_PRICE_DIGITS = 4
_ZERO_CASH = Decimal(0)
_ALWAYS = date(1900, 1, 1)
_SHARE_SLACK = 1e-6  # `real_shares`: a double's error on a whole number of shares
_EPOCH = date(1970, 1, 1)  # polars' Date is days since this day
# Sessions without a bar, up to the store's last day, after which an instrument that never trades
# again counts as delisted (`MarketData.last_traded`).
STOPPED_SLACK_SESSIONS = 20
# Sessions either side of an open review jump whose bars are not traded (`MarketData.skipped`).
JUMP_SKIP_SESSIONS = 10
# The exact prices in 1/10,000 of a rupee (the store's 4 places), beside the scan's doubles.
_EXACT = ("open_e4", "high_e4", "low_e4", "close_e4")
_BAR_COLUMNS = (
    "instrument_id",
    "date",
    "open",
    "high",
    "low",
    "close",
    "prev_close",
    "volume",
    "traded_value",
    "raw_close",
    *_EXACT,
    "div_factor",
)


@dataclass(frozen=True)
class MarketData:
    """Everything a run reads, point in time.

    `days`: every session of the store, sorted. `bars`: the universe's adjusted daily bars,
    sorted by instrument and date (`instrument_id` text, `date`, `open`, `high`, `low`, `close`,
    `prev_close`, `volume`, `traded_value`, and `raw_close`, the unadjusted close the rules on
    a rupee price read (`min_price`, the tick slab), as doubles, for the scan, and `open_e4` ..
    `close_e4`, the exact 4-place prices as integers of 1/10,000, for the day loop's Decimals).
    `features`: the daily features (`date`, `instrument_id`, columns). `index_closes`:
    per index name, (date, close) oldest first. `members`: the universe's membership intervals,
    point in time, by chain id (`point_in_time_members`). `industries`: each instrument's
    industry, either one name or its classifications as (effective_from, industry), read point
    in time. `series`: each instrument's series as change points (first day, series) from its
    bars, so a scan reads the series of its own day (`identity.series_on`), not today's.
    `breadth`: the features job's `breadth_pct` per session, read with the `regime.INDEX_NAME`
    and `regime.VIX_NAME` closes of `index_closes` only by a run under the `policy` regime rule
    (PR 68), and alone by a strategy whose regime gate reads it (`breadth_above`).

    `last_traded`: the last session of each instrument delisted by the period's end, where the
    day loop closes a position in it (HAT-BT-012). It is read from the bars (`stopped_trading`),
    by chain id, so a linked older ISIN is not a delisting: an instrument whose last bar is on or
    before the period's end, which has no bar after it anywhere in the store, and whose last bar
    is more than `STOPPED_SLACK_SESSIONS` sessions before the store's last day, has stopped
    trading. A gap of up to the slack before the store's end may be a suspension that resumes,
    so it is not called a delisting (the position is carried and marked down, `fills`); a
    suspension that resumes later in the store is never one. `delisted_on`, set by the live
    listing refresh, wins when it is set and gives an earlier last session.

    `skipped`: the instrument-days with a bar within `JUMP_SKIP_SESSIONS` sessions (either side,
    the jump day included) of an open jump of the lineage review list
    (`lineage_review.unexplained_jumps`: a price-changing action with no stored factor, such as a
    demerger whose ratio the owner has not set; a reviewed one has its factor and is no longer a
    jump). Their bars stay in `bars` for the scan, but the day loop does not trade or mark on
    them (`MarketView.bar`), though their closes still set the tick and the indicators (they
    are real sessions), and they do not count as sessions without a bar for the suspension
    mark. An interim rule (audit H7) until the review list's actions are loaded; the run's
    snapshot reports how many instrument-days it skipped.

    `pending`: the instrument-days factor-pending (CA5, CA6 `ca_versions.pending_days`): from
    the ex-date of a stored factor, every day no version of it was available yet or the value
    available then differs from the one the series applies (so on and on when it is never
    dated). The adjusted history there shows a factor nobody had yet, so the scan reads no bars
    of the name that night and its entries skip it that day; an open position keeps its marks,
    stops and exits on the price basis. The snapshot reports the count.
    """

    days: tuple[date, ...]
    bars: pl.DataFrame
    features: pl.DataFrame
    index_closes: Mapping[str, Sequence[tuple[date, Decimal]]]
    members: tuple[Interval, ...]
    industries: Mapping[str, str | Sequence[tuple[date, str]]] = field(default_factory=dict)
    series: Mapping[str, Sequence[tuple[date, str]]] = field(default_factory=dict)
    last_traded: Mapping[str, date] = field(default_factory=dict)
    # The store's raw 5-minute bars, read one session at a time for the candidates the loop
    # decides on them (`intraday.session_bars`); None: every session uses the daily estimate.
    intraday: BarStore | None = None
    breadth: Mapping[date, Decimal] = field(default_factory=dict)
    skipped: frozenset[tuple[str, date]] = frozenset()
    pending: frozenset[tuple[str, date]] = frozenset()
    # The cash each dividend pays per share of the price basis, (ex-date, rupees) oldest first,
    # by instrument: the stored amount times the basis factor of the session before the ex-date
    # (a split after it makes more, cheaper shares; each earns its share of the cash).
    dividends: Mapping[str, Sequence[tuple[date, Decimal]]] = field(default_factory=dict)
    # CA8: (chain id, session) pairs where a position held in the chain exits at the close
    # before a demerger whose record date was announced before that session
    # (`demerger_notice.exit_day`); a demerger never dated holds through.
    demerger_exits: frozenset[tuple[str, date]] = frozenset()
    # Each member's ISIN, by instrument id, so the ledger's instruments are classed as live's are
    # (decisions.md "Stocks only", `watchlist._tradable_members`).
    isins: Mapping[str, str] = field(default_factory=dict)
    # For the run's result files (`folder`), not the day loop: each member's symbols as
    # (valid_from, symbol) oldest first (`hat_instrument_symbol_history`, else its one symbol),
    # and its listing date.
    symbols: Mapping[str, Sequence[tuple[date, str]]] = field(default_factory=dict)
    listed_on: Mapping[str, date] = field(default_factory=dict)
    # Each member's results filings public in the period (oldest first), with the growth of the
    # filing's quarter as known when it became public: what `earnings_catalyst` and the EP log
    # read (`results_events`); empty unless the run loads them (`load_market(results=True)`).
    results: Mapping[str, Sequence[ResultEvent]] = field(default_factory=dict)


def symbol_on(data: MarketData, instrument_id: str, day: date) -> str:
    """The instrument's symbol on `day`: the last one that took effect by then (its first one
    before any), the id itself without one."""
    found = data.symbols.get(instrument_id, ())
    at = bisect_right([d for d, _ in found], day)
    if not found:
        return instrument_id
    return found[max(0, at - 1)][1]


def classifications(data: MarketData, instrument_id: str) -> list[tuple[date, str]]:
    """The instrument's industry classifications, oldest first."""
    found = data.industries.get(instrument_id, "unknown")
    if isinstance(found, str):
        return [(_ALWAYS, found)]
    return sorted(found) or [(_ALWAYS, "unknown")]


def industry_on(data: MarketData, instrument_id: str, day: date) -> str:
    """The industry in force on `day`: the newest classification effective on or before it."""
    rows = [name for start, name in classifications(data, instrument_id) if start <= day]
    return rows[-1] if rows else "unknown"


def _price(value: object) -> Decimal | None:
    """A stored price as an exact Decimal of the store's 4 places; None when absent."""
    return exact(value, floats=True, places=_PRICE_DIGITS)


@dataclass(frozen=True)
class Bar:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


def _ordinal(day: date) -> int:
    return (day - _EPOCH).days


def _exact(units: object) -> Decimal:
    """A price held in 1/10,000 as the Decimal of the store's 4 places."""
    return Decimal(int(units)).scaleb(-4)  # type: ignore[call-overload]


@dataclass(frozen=True)
class _Series:
    """One instrument's bars with four positive prices, columnar: the dates as days since
    `_EPOCH`, ascending, (open, high, low, close) in 1/10,000 per row, and per row the row of
    the last bar on or before it that is not skipped near a jump (-1 before one)."""

    days: np.ndarray
    prices: np.ndarray
    traded: np.ndarray
    div: np.ndarray  # per row, the factor of the dividends after it (`div_factor`)

    def bars(self, start: int, stop: int) -> list[Bar]:
        return [Bar(*(_exact(v) for v in row)) for row in self.prices[start:stop]]

    def signal_bars(self, start: int, stop: int, anchor: int) -> list[Bar]:
        """The rows as the total-return series on the scale of row `anchor`: each price times
        its dividend factor over the anchor's (`rescaled`), exact to the store's 4 places."""
        if start >= stop:
            return []
        scale = self.div[anchor]
        return [
            Bar(*(rescaled(int(v), self.div[n] / scale) for v in self.prices[n]))
            for n in range(start, stop)
        ]


_NO_VOLUMES = (np.empty(0, dtype=np.int32), np.empty(0, dtype=np.float64))


def last_through(days: np.ndarray, values: np.ndarray, through: int, count: int) -> list[float]:
    """The last `count` of `values` whose `days` (ascending) are on or before `through`."""
    end = int(np.searchsorted(days, through, side="right"))
    return [float(v) for v in values[max(0, end - count) : end]]


def rescaled(units: int, ratio: float) -> Decimal:
    """A price of 1/10,000 units times a dividend ratio, rounded half to even to the store's 4
    places; a ratio of 1 is the price itself (a price is far below 2**53 units, so exact)."""
    return _exact(round(units * ratio))


_NO_SERIES = _Series(
    np.empty(0, dtype=np.int32),
    np.empty((0, 4), dtype=np.int64),
    np.empty(0, dtype=np.int64),
    np.empty(0, dtype=np.float64),
)
_DIV = "div_factor"
_SCALED = ("open", "high", "low", "close", "prev_close")  # the scan's double prices


@dataclass(frozen=True)
class _RawCloses:
    """One instrument's bars with a positive raw close, whatever their other prices (live reads
    every raw close for the tick, `daily.market._RAW_SQL`): the dates as days since `_EPOCH`,
    ascending, the raw and adjusted closes in 1/10,000 (adjusted 0 when absent) and the price
    basis's factor (the adjusted close over the raw one, from the unrounded close; NaN without
    an adjusted close)."""

    days: np.ndarray
    raw: np.ndarray
    adjusted: np.ndarray
    factor: np.ndarray


_NO_RAW = _RawCloses(
    np.empty(0, dtype=np.int32),
    np.empty(0, dtype=np.int64),
    np.empty(0, dtype=np.int64),
    np.empty(0, dtype=np.float64),
)


class MarketView:
    """The point-in-time reads the day loop makes of `MarketData` (HAT-BT-011): each session's
    tradeable bars as exact Decimals, an instrument's bars up to a day and the SMA, swing low,
    ATR and NSE tick live computes from them, the features of a session and the closes of the
    strategy's regime index (`index`). A bar without four positive prices is not tradeable and
    is left out; a skipped one (`MarketData.skipped`) is not traded on (`bar`, `last_close`)
    but stays in the history the indicators and the tick read. The bars are held per instrument as
    arrays; a `Bar` of Decimals is built only for the bars a read returns.

    The as-of clock (`open`, `close`, `release`) refuses a read past the run's instant
    (`LOOKAHEAD`, `looks_ahead`): a decision read (the scan's window, features, breadth, the
    regime closes, an instrument's bars, volumes and ATR through a day) through the last session
    whose close is public, the session in progress before its close; a simulation read (a
    session's bar, previous close, tick, dividends, a mark) through the session in progress. The
    loop releases it after the period end, for the result's after-the-fact analytics."""

    def __init__(self, data: MarketData, index: str) -> None:
        self.session: date | None = None  # the clock: the session in progress
        self.known: date | None = None  # the last session whose close is public
        self.days = tuple(sorted(set(data.days)))
        self.index = {d: n for n, d in enumerate(self.days)}
        self.bars = data.bars
        self.series = self._series(data.skipped)
        self.raw_closes = self._raw_closes()
        self.volumes = self._volumes()
        self.skipped: dict[str, list[date]] = {}
        for instrument_id, day in sorted(data.skipped):
            self.skipped.setdefault(instrument_id, []).append(day)
        self.feature_rows: dict[date, dict[str, dict[str, Any]]] = {}
        self.features = {
            key[0]: part for key, part in data.features.partition_by("date", as_dict=True).items()
        }
        self.closes = list(data.index_closes.get(index, ()))
        self.breadth = data.breadth
        self.dividends = data.dividends
        self.results = data.results
        # the reads made past the clock on purpose (`hindsight`), by reason
        self.hindsight_reads: dict[str, int] = {}
        by_day: dict[date, set[str]] = defaultdict(set)
        for instrument_id, day in data.pending:
            by_day[day].add(instrument_id)
        self.pending: dict[date, frozenset[str]] = {d: frozenset(ids) for d, ids in by_day.items()}

    def open(self, day: date) -> None:
        """The clock at `day`'s open: its own bar is in progress, the one before it closed."""
        self.session, self.known = day, self.prev(day)

    def close(self, day: date) -> None:
        """The clock at `day`'s close: its bar is public."""
        self.session, self.known = day, day

    @contextmanager
    def hindsight(self, reason: str) -> Iterator[None]:
        """No clock inside, the read counted under `reason` (`hindsight_reads`): a read past the
        run's instant made on purpose, a known gap the run records (the day's own volume where
        no 5-minute bars show it) or a result column described as read after the fact."""
        self.count_hindsight(reason)
        clock = self.session, self.known
        self.session = self.known = None
        try:
            yield
        finally:
            self.session, self.known = clock

    def count_hindsight(self, reason: str) -> None:
        """Count a use of data past the clock that is not a read through the view (a delisting
        the registry knows before the day shows it, `HINDSIGHT_DELISTED_EXIT`)."""
        self.hindsight_reads[reason] = self.hindsight_reads.get(reason, 0) + 1

    def release(self) -> None:
        """No clock: the run is over (the result's analytics read the sessions after a trade)."""
        self.session = self.known = None

    def as_of(self, day: date, *, inclusive: bool = True) -> None:
        """Refuse (`LOOKAHEAD`) a read of `day` past the clock (`looks_ahead`): what a reader
        of the run's other data (the rotation's closes, the 5-minute store, the overlay's ETF
        data) calls before it reads."""
        if looks_ahead(day, self.session, self.known, inclusive=inclusive):
            raise BacktestError(LOOKAHEAD)

    def _check(self, day: date, *, inclusive: bool = True) -> None:
        self.as_of(day, inclusive=inclusive)

    def results_on(self, instrument_id: str, day: date) -> tuple[ResultEvent, ...]:
        """The instrument's results filings public by the end of `day` (IST), the session in
        progress at most: a trigger reads only those public by its instant."""
        self._check(day, inclusive=False)
        return public_by(self.results.get(instrument_id, ()), day)

    def _series(self, skipped: Collection[tuple[str, date]]) -> dict[str, _Series]:
        # a bar without four positive prices cannot be traded on
        div = pl.col(_DIV) if _DIV in self.bars.columns else pl.lit(1.0)
        frame = self.bars.select("instrument_id", "date", *_EXACT, div.alias(_DIV)).filter(
            pl.all_horizontal(pl.col(c) > 0 for c in _EXACT).fill_null(value=False)
        )
        # A skipped bar stays in the history: it is a real session, whose close sets the tick and
        # the SMA, ATR and swing low; only the loop's trades and marks leave it out.
        left_out = pl.DataFrame(
            sorted(skipped), schema={"instrument_id": pl.String, "date": pl.Date}, orient="row"
        ).with_columns(skipped=pl.lit(value=True))
        frame = frame.join(left_out, on=["instrument_id", "date"], how="left")
        found: dict[str, _Series] = {}
        for key, part in frame.partition_by("instrument_id", as_dict=True).items():
            rows = np.arange(part.height)
            kept = np.where(part["skipped"].is_null().to_numpy(), rows, -1)
            found[str(key[0])] = _Series(
                part["date"].to_physical().to_numpy(),
                part.select(_EXACT).to_numpy(),
                np.maximum.accumulate(kept) if part.height else kept,
                part[_DIV].cast(pl.Float64).to_numpy(),
            )
        return found

    def _raw_closes(self) -> dict[str, _RawCloses]:
        frame = self.bars.select(
            "instrument_id",
            "date",
            # a raw price has at most the store's 4 places, so the double rounds back exactly
            raw=(pl.col("raw_close") * 10_000).round(0).cast(pl.Int64),
            adjusted=pl.col("close_e4").fill_null(0),
            factor=(pl.col("close") / pl.col("raw_close")).fill_null(float("nan")),
        ).filter(pl.col("raw") > 0)
        return {
            str(key[0]): _RawCloses(
                part["date"].to_physical().to_numpy(),
                part["raw"].to_numpy(),
                part["adjusted"].to_numpy(),
                part["factor"].to_numpy(),
            )
            for key, part in frame.partition_by("instrument_id", as_dict=True).items()
        }

    def _volumes(self) -> dict[str, tuple[np.ndarray, np.ndarray]]:
        """Each instrument's (dates as days since `_EPOCH`, volumes) over every stored session,
        whatever its prices (`_series` keeps only rows with four positive prices), so a volume
        average never skips a session; NaN where the store has no volume."""
        if "volume" not in self.bars.columns:
            return {}
        frame = self.bars.select(
            "instrument_id", "date", pl.col("volume").cast(pl.Float64).fill_null(float("nan"))
        )
        return {
            str(key[0]): (part["date"].to_physical().to_numpy(), part["volume"].to_numpy())
            for key, part in frame.partition_by("instrument_id", as_dict=True).items()
        }

    def _find(self, instrument_id: str, day: date, *, inclusive: bool) -> tuple[_Series, int]:
        """The instrument's bars and the count of them dated on or before `day` (before it, if
        not `inclusive`)."""
        series = self.series.get(instrument_id, _NO_SERIES)
        side = "right" if inclusive else "left"
        return series, int(np.searchsorted(series.days, _ordinal(day), side=side))

    def bar(self, instrument_id: str, day: date) -> Bar | None:
        """The instrument's tradeable bar of `day`; None without one."""
        self._check(day, inclusive=False)
        series, at = self._find(instrument_id, day, inclusive=False)
        if at < len(series.days) and series.days[at] == _ordinal(day) and series.traded[at] == at:
            return series.bars(at, at + 1)[0]
        return None

    def last_close(self, instrument_id: str, day: date) -> tuple[date, Decimal] | None:
        """The instrument's last tradeable session on or before `day` and its close; None
        without one."""
        self._check(day, inclusive=False)
        series, end = self._find(instrument_id, day, inclusive=True)
        at = int(series.traded[end - 1]) if end else -1
        if at < 0:
            return None
        return _EPOCH + timedelta(days=int(series.days[at])), _exact(series.prices[at][3])

    def skipped_after(self, instrument_id: str, after: date, through: date) -> int:
        """The count of the instrument's skipped sessions (`MarketData.skipped`) after `after`, up
        to `through`."""
        self._check(through, inclusive=False)
        days = self.skipped.get(instrument_id, [])
        return bisect_right(days, through) - bisect_right(days, after)

    def prev(self, day: date) -> date | None:
        n = self.index[day]
        return self.days[n - 1] if n > 0 else None

    def closes_through(self, day: date) -> list[Decimal]:
        self._check(day)
        return [c for d, c in self.closes if d <= day]

    def breadth_through(self, day: date) -> list[Decimal | None]:
        """The breadth of the last `BREADTH_SESSIONS` sessions through `day`, as live reads it
        (`daily.market.breadth_series`): None for a session without one."""
        self._check(day)
        end = self.index[day] + 1
        return [self.breadth.get(d) for d in self.days[max(0, end - BREADTH_SESSIONS) : end]]

    def features_on(self, day: date) -> pl.DataFrame | None:
        """The stored features of `day` (the scan's), None without any."""
        self._check(day)
        return self.features.get(day)

    def breadth_on(self, day: date) -> Decimal | None:
        """The market breadth of `day`, None without one."""
        self._check(day)
        return self.breadth.get(day)

    def feature(
        self, day: date, instrument_id: str, name: str, *, rounded: bool = True
    ) -> Decimal | None:
        """The stored feature on `day`, at the store's 4 places, or with `rounded` False as
        stored (a score ranked against others, where 4 places would make ties)."""
        self._check(day)
        rows = self.feature_rows.get(day)
        if rows is None:
            frame = self.features.get(day)
            rows = {} if frame is None else {str(r["instrument_id"]): r for r in frame.to_dicts()}
            self.feature_rows[day] = rows
        value = rows.get(instrument_id, {}).get(name)
        return _price(value) if rounded else exact(value, floats=True)

    def bars_through(
        self,
        instrument_id: str,
        day: date,
        count: int,
        *,
        inclusive: bool = True,
        since_break: bool = False,
    ) -> list[Bar]:
        """The instrument's last `count` bars dated on or before `day` (before it, if not
        `inclusive`), oldest first: the total-return series on the scale of its last bar on or
        before `day`, what a signal reads that day (CA3), so a rule comparing them with the day's
        close sees no ex-date drop; the price basis where no dividend went ex among them. With
        `since_break`, none before the last series break up to `day` (`series_break`), as the
        live trigger reads the stop inputs."""
        self._check(day, inclusive=inclusive)
        series, end = self._find(instrument_id, day, inclusive=inclusive)
        _, through = self._find(instrument_id, day, inclusive=True)
        start = max(0, end - count)
        if since_break:
            days = [_EPOCH + timedelta(days=int(n)) for n in series.days[start:end]]
            start += segment_start(days, None if inclusive else day)
        return series.signal_bars(start, end, through - 1)

    def volumes_through(self, instrument_id: str, day: date, count: int) -> list[float]:
        """The volumes of the instrument's last `count` stored sessions on or before `day`,
        oldest first, a session with bad prices included (NaN where the store has none), none
        before the last series break (`series_break`)."""
        self._check(day)
        days, volumes = self.volumes.get(instrument_id, _NO_VOLUMES)
        window = last_through(days, volumes, _ordinal(day), count)
        end = int(np.searchsorted(days, _ordinal(day), side="right"))
        dates = [_EPOCH + timedelta(days=int(n)) for n in days[end - len(window) : end]]
        return window[segment_start(dates) :]

    def bars_since(self, instrument_id: str, first: date, day: date) -> list[Bar]:
        """The instrument's bars dated from `first` on and before `day`, oldest first, on the
        scale `bars_through` reads them at `day`."""
        self._check(day, inclusive=False)
        _, start = self._find(instrument_id, first, inclusive=False)
        _, end = self._find(instrument_id, day, inclusive=False)
        return self.bars_through(instrument_id, day, end - start, inclusive=False)

    def tick(self, instrument_id: str, day: date) -> Decimal | None:
        """The instrument's NSE tick on `day` (HAT-EXEC-053) in the units of its adjusted bars.
        The slab is picked from its raw closes before `day`, as the live trigger prices it (a
        later split must not move a day to a finer slab), and the tick is scaled by the
        adjustment of its bar of `day` (or the last before it, `ticks.scaled_tick`): on a bar no
        action adjusts it is the NSE tick itself. None without an earlier raw close."""
        self._check(day, inclusive=False)
        closes = self.raw_closes.get(instrument_id, _NO_RAW)
        end = int(np.searchsorted(closes.days, _ordinal(day), side="left"))
        month = int(np.searchsorted(closes.days[:end], _ordinal(day.replace(day=1)), side="left"))
        # the only closes the tick reads: the last before the month, and the last before `day`
        rows = sorted({n for n in (month - 1, end - 1) if n >= 0})
        dates = [_EPOCH + timedelta(days=int(closes.days[n])) for n in rows]
        tick = tick_for_date(dates, [_exact(closes.raw[n]) for n in rows], day)
        through = int(np.searchsorted(closes.days, _ordinal(day), side="right"))
        if tick is None or not through:
            return None
        last = through - 1
        return scaled_tick(tick, _exact(closes.adjusted[last]), _exact(closes.raw[last]))

    def price_scale(self, instrument_id: str, day: date) -> Decimal | None:
        """The instrument's adjusted close over its raw close on `day`, exactly: what puts a
        raw price of that session on the run's price basis (`intraday.session_bars`); None
        without a raw close that day."""
        self._check(day, inclusive=False)
        closes = self.raw_closes.get(instrument_id, _NO_RAW)
        at = int(np.searchsorted(closes.days, _ordinal(day), side="left"))
        if at >= len(closes.days) or closes.days[at] != _ordinal(day) or not closes.raw[at]:
            return None
        return Decimal(int(closes.adjusted[at])) / Decimal(int(closes.raw[at]))

    def real_shares(self, instrument_id: str, day: date, qty: int) -> int | None:
        """The whole shares `qty` price-basis shares were on `day` (`real_shares`), by the factor
        of its bar of `day` (or the last before it); None without one."""
        self._check(day, inclusive=False)
        closes = self.raw_closes.get(instrument_id, _NO_RAW)
        through = int(np.searchsorted(closes.days, _ordinal(day), side="right"))
        return real_shares(qty, float(closes.factor[through - 1])) if through else None

    def atr(self, instrument_id: str, day: date, length: int) -> Decimal | None:
        """The mean true range of the `length` sessions up to `day` (since a series break)."""
        window = self.bars_through(instrument_id, day, length + 1, since_break=True)
        return mean_true_range(
            [b.high for b in window], [b.low for b in window], [b.close for b in window], length
        )

    def pending_on(self, day: date) -> frozenset[str]:
        """The instruments factor-pending on `day` (`MarketData.pending`)."""
        self._check(day, inclusive=False)
        return self.pending.get(day, frozenset())

    def window(self, as_of: date) -> pl.DataFrame:
        """The bars of the `LOOKBACK_SESSIONS` sessions up to `as_of`: what the scan reads, as
        the total-return series on each instrument's scale of its last bar by `as_of` (CA3),
        without the instruments factor-pending on `as_of` (CA5: no signal)."""
        self._check(as_of)
        first = self.days[max(0, self.index[as_of] - LOOKBACK_SESSIONS)]
        frame = self.bars.filter((pl.col("date") >= first) & (pl.col("date") <= as_of))
        pending = self.pending_on(as_of)
        if pending:
            frame = frame.filter(~pl.col("instrument_id").cast(pl.String).is_in(sorted(pending)))
        if _DIV not in frame.columns:
            return frame
        ratio = pl.col(_DIV) / pl.col(_DIV).last().over("instrument_id")
        return frame.with_columns(pl.col(c) * ratio for c in _SCALED)

    def dividends_on(self, instrument_id: str, day: date) -> Decimal:
        """The cash per price-basis share of the instrument's dividends that go ex on `day`."""
        self._check(day, inclusive=False)
        return sum(
            (cash for ex, cash in self.dividends.get(instrument_id, ()) if ex == day), _ZERO_CASH
        )

    def prev_close(self, instrument_id: str, day: date) -> Decimal | None:
        """The instrument's last close before `day`, on the price basis (the circuit bands' base);
        None without one."""
        self._check(day, inclusive=False)
        series, end = self._find(instrument_id, day, inclusive=False)
        return _exact(series.prices[end - 1][3]) if end else None


_DIGEST_SLICE = 100_000  # rows per CSV slice of the content digest

# The universe's adjusted rows of every ingest run, deduped in polars (`latest_rows`, as
# `features.load_bars` does: a window in SQL costs several times the memory of the rows), with
# the series each bar traded under (a dated attribute, audit D5), sorted by DuckDB within its
# memory limit (a sort in polars would copy the rows). The prices come as doubles for
# the scan, as live reads them, and exactly, as integers of 1/10,000 (`_PRICE_E4_SQL`).
_BARS_SQL = """
SELECT instrument_id, date, series,
       CAST(open AS DOUBLE) AS open, CAST(high AS DOUBLE) AS high, CAST(low AS DOUBLE) AS low,
       CAST(close AS DOUBLE) AS close, CAST(prev_close AS DOUBLE) AS prev_close,
       CAST(volume AS DOUBLE) AS volume, CAST(traded_value AS DOUBLE) AS traded_value,
       CAST(raw_close AS DOUBLE) AS raw_close,
       price_e4(open) AS open_e4,
       price_e4(high) AS high_e4,
       price_e4(low) AS low_e4,
       price_e4(close) AS close_e4,
       CAST(div_factor AS DOUBLE) AS div_factor,
       ingest_run_id
FROM bars_daily_cap_adj
WHERE date BETWEEN ? AND ? AND instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
ORDER BY instrument_id, date
"""
# An adjusted price rounded to the store's 4 places half to even, as `Decimal.quantize` (and so
# `_price`) rounds, in exact decimal arithmetic, as an integer of 1/10,000. A cast to DECIMAL(18,4)
# would round a half away from zero.
_PRICE_E4_SQL = """
CREATE TEMP MACRO price_e4(p) AS (
    CAST(floor(p * 10000) AS BIGINT) + CASE
        WHEN p * 10000 - floor(p * 10000) > 0.5 THEN 1
        WHEN p * 10000 - floor(p * 10000) = 0.5 THEN CAST(floor(p * 10000) AS BIGINT) % 2
        ELSE 0 END)
"""
# the universe's instruments that still trade after the run's end
_LATER_SQL = """
SELECT DISTINCT instrument_id FROM bars_daily_adj
WHERE date > ? AND instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
"""
# The universe's dividends the store priced (`store.dividend`), each with the price-basis factor
# of the session before its ex-date, which turns the stored cash into cash per basis share.
_DIVIDENDS_SQL = """
SELECT d.instrument_id, d.ex_date, CAST(d.amount AS VARCHAR),
       CAST(COALESCE(c.factor, CAST(1 AS DECIMAL(20,18))) AS VARCHAR)
FROM dividend d
LEFT JOIN ca_segment c ON c.instrument_id = d.instrument_id
     AND d.prev_date >= COALESCE(c.from_date, DATE '0001-01-01') AND d.prev_date < c.to_date
WHERE d.ex_date BETWEEN ? AND ? AND d.instrument_id IN (SELECT unnest(CAST(? AS VARCHAR[])))
ORDER BY d.instrument_id, d.ex_date
"""
# every index the run reads, in one statement, so a concurrent upsert cannot split them
_INDEX_SQL = """
SELECT index_name, date, CAST(close AS VARCHAR) FROM index_daily
WHERE list_contains(?, index_name) AND date <= ? ORDER BY index_name, date
"""


def _members(
    session: Session,
    universe_version_id: uuid.UUID,
    lineage: Mapping[str, str],
    since: date,
    start: date,
    until: date,
) -> tuple[tuple[Interval, ...], dict[str, Any]]:
    """The universe's members over `since..until` (`since` the first scan's night, `start` the
    first session), point in time (`point_in_time_members`), and what they were read from (the
    snapshot's `membership`). The universe version names its index; `backtest_universe_no_index`
    when it names none, and the index's loaded history must cover `start..until`
    (`membership_cover_refusal`): never today's list in its place."""
    version = session.get(HatUniverseVersion, universe_version_id)
    definition = {} if version is None else version.definition
    index_name = definition.get("index")
    if not index_name:
        raise BacktestError("backtest_universe_no_index")
    record = session.get(HatIndexMembership, index_name)
    refusal = membership_cover_refusal(
        None if record is None else (record.covered_from, record.covered_to), start, until
    )
    if refusal is not None:
        raise BacktestError(refusal)
    rows = session.scalars(select(HatIndexMember).where(HatIndexMember.index_name == index_name))
    members = point_in_time_members(
        (Interval(r.instrument_id, r.effective_from, r.effective_to) for r in rows),
        include=[uuid.UUID(i) for i in definition.get("include", ())],
        exclude=[uuid.UUID(i) for i in definition.get("exclude", ())],
        lineage=lineage,
        since=since,
        until=until,
        history_from=record.covered_from,
    )
    source = {
        "index": index_name,
        "source": record.source,
        "digest": record.digest,
        "rows": record.rows,
        "covered_from": record.covered_from.isoformat(),
        "covered_to": record.covered_to.isoformat(),
        "off_size_dates": len(record.coverage.get("off_size", {})),
        "universe_version_id": str(universe_version_id),
        "universe_key": membership_key(definition),
    }
    return members, source


def real_shares(qty: int, factor: float) -> int | None:
    """The whole shares a quantity of price-basis shares was on a day whose bar the stored
    corporate actions adjust by `factor` (adjusted price over raw price): a split after the day
    makes each real share `1 / factor` basis shares, so a 5:1 split's 858 basis shares were 171
    real ones (171.6, rounded down to whole shares as an order would be). A product within a
    millionth of a share below a whole number is that number, as the factor is read through
    doubles. None for a factor that is not a positive finite number."""
    if not (math.isfinite(factor) and factor > 0):
        return None
    return math.floor(qty * factor + _SHARE_SLACK)


def membership_cover_refusal(
    cover: tuple[date, date] | None, start: date, until: date
) -> str | None:
    """Why a run over `start..until` cannot read an index history covering `cover` (first and
    last day), or None when it can: `backtest_no_index_membership` without a history,
    `backtest_starts_before_membership@<first day>` for a run starting before the history (the
    NIFTY 500's starts 2016-01-01; earlier bars still warm the indicators up), and
    `backtest_ends_after_membership@<last day>` for one ending after it."""
    if cover is None:
        return "backtest_no_index_membership"
    if start < cover[0]:
        return f"backtest_starts_before_membership@{cover[0].isoformat()}"
    if until > cover[1]:
        return f"backtest_ends_after_membership@{cover[1].isoformat()}"
    return None


def point_in_time_members(
    rows: Iterable[Interval],
    *,
    include: Iterable[uuid.UUID],
    exclude: Iterable[uuid.UUID],
    lineage: Mapping[str, str],
    since: date,
    until: date,
    history_from: date | None = None,
) -> tuple[Interval, ...]:
    """The universe's members: the index's membership intervals that overlap `since..until`,
    plus the owner's includes (members from the first day there is), less the excludes, by
    chain id (a linked older ISIN is its chain's, whose id its bars carry in `bars_daily`). One
    chain's intervals that touch or overlap are merged, so a move to a new ISIN on a day the
    stock stays in the index is one membership. A stock is a candidate only inside an interval:
    it enters on its effective date and leaves on its exclusion's. An interval starting on
    `history_from`, the history's first day, is a member from before it (no change starts on
    that day), so the first scan's night before it reads it too."""
    dropped = {_chain_id(i, lineage) for i in exclude}
    spans: dict[uuid.UUID, list[tuple[date, date | None]]] = defaultdict(list)
    for r in rows:
        if r.effective_from <= until and (r.effective_to is None or r.effective_to > since):
            start = _ALWAYS if r.effective_from == history_from else r.effective_from
            spans[_chain_id(r.instrument_id, lineage)].append((start, r.effective_to))
    for i in include:
        spans[_chain_id(i, lineage)].append((_ALWAYS, None))
    out: list[Interval] = []
    for chain in sorted(spans.keys() - dropped):
        merged: list[tuple[date, date | None]] = []
        for start, end in sorted(spans[chain], key=lambda s: s[0]):
            if merged and (merged[-1][1] is None or merged[-1][1] >= start):
                first, last = merged[-1]
                merged[-1] = (first, None if last is None or end is None else max(last, end))
            else:
                merged.append((start, end))
        out.extend(Interval(chain, start, end) for start, end in merged)
    return tuple(out)


def scan_night_members(members: Iterable[Interval], days: Sequence[date]) -> tuple[Interval, ...]:
    """The members as the run's universe stores them: the scan for trade date D runs on the
    night of the session before D and reads the universe on that night (the loop's `prev`), so
    each interval moves back to the night before its first and its end session (`days`, the
    sessions in order). The scan for D then reads exactly D's members: an entrant effective D
    is on D's watchlist, and a stock excluded on D is not. The entry gate reads the trade date
    on the same rows, so a stock is not newly entered on its last session as a member (the
    conservative side of the one-day boundary). An interval from before the first session
    keeps its start; one that moves to nothing is dropped."""

    def night(day: date) -> date:
        at = bisect_left(days, day)
        return day if at == 0 else days[at - 1]

    out = []
    for m in members:
        start = m.effective_from if m.effective_from == _ALWAYS else night(m.effective_from)
        end = None if m.effective_to is None else night(m.effective_to)
        if end is None or end > start:
            out.append(Interval(m.instrument_id, start, end))
    return tuple(out)


def _chain_id(instrument_id: uuid.UUID, lineage: Mapping[str, str]) -> uuid.UUID:
    chain = lineage.get(str(instrument_id))
    return instrument_id if chain is None else uuid.UUID(chain)


def _industries(
    session: Session, instruments: Iterable[uuid.UUID], lineage: Mapping[str, str]
) -> dict[str, list[tuple[date, str]]]:
    """Each instrument's industry classifications as (effective_from, industry), oldest first:
    the run reads the one in force on each trade date, never a later reclassification. A
    linked older ISIN's classifications count for its chain (the id the run knows it by)."""
    rows = session.scalars(select(HatIndustry))
    return chain_industries(
        ((r.instrument_id, r.effective_from, r.industry) for r in rows), instruments, lineage
    )


def chain_industries(
    rows: Iterable[tuple[uuid.UUID, date, str]],
    instruments: Iterable[uuid.UUID],
    lineage: Mapping[str, str],
) -> dict[str, list[tuple[date, str]]]:
    """(instrument, effective_from, industry) rows grouped under each wanted instrument's id,
    oldest first, a linked older ISIN's under its chain's id."""
    wanted = set(instruments)
    found: dict[str, list[tuple[date, str]]] = {}
    for instrument_id, effective_from, industry in rows:
        chain = _chain_id(instrument_id, lineage)
        if chain in wanted:
            found.setdefault(str(chain), []).append((effective_from, industry))
    return {k: sorted(v) for k, v in found.items()}


def results_events(
    rows: Iterable[tuple[uuid.UUID, datetime, date, uuid.UUID]],
    growth: Mapping[uuid.UUID, tuple[Decimal | None, Decimal | None, bool | None]],
    instruments: Collection[uuid.UUID],
    lineage: Mapping[str, str],
) -> dict[str, list[ResultEvent]]:
    """Each run instrument's filings, oldest first, from result rows (id, public_at, period_end,
    instrument) and their growth (sales, eps, turnaround) by result id; a linked older ISIN's
    filings count for its chain (the id the run knows it by). Every row is kept, revisions
    included: the criterion reads the newest public by its instant (`live.results_filing`)."""
    out: dict[str, list[ResultEvent]] = defaultdict(list)
    for result_id, public_at, period_end, instrument_id in rows:
        chain = _chain_id(instrument_id, lineage)
        if chain not in instruments:
            continue
        sales, eps, turnaround = growth.get(result_id, (None, None, None))
        out[str(chain)].append(ResultEvent(public_at, period_end, sales, eps, turnaround))
    for events in out.values():
        events.sort(key=lambda e: (e.public_at, e.period_end))
    return dict(out)


def _results(
    session: Session,
    instruments: Collection[uuid.UUID],
    lineage: Mapping[str, str],
    first: date,
    end: date,
    *,
    wanted: bool = True,
) -> dict[str, list[ResultEvent]]:
    """`results_events` of the filings public from the previous session of `first` through
    `end` (never after the period: no hold-out read). None stored is no filing; nothing when
    the run did not ask for them (`wanted`)."""
    if not wanted or not inspect(session.get_bind(HatFinancialResult)).has_table(
        HatFinancialResult.__tablename__
    ):
        return {}
    since = as_utc(datetime.combine(first - timedelta(days=7), time(0), tzinfo=IST))
    until = as_utc(datetime.combine(end + timedelta(days=1), time(0), tzinfo=IST))
    rows = session.execute(
        select(
            HatFinancialResult.id,
            HatFinancialResult.public_at,
            HatFinancialResult.period_end,
            HatFinancialResult.instrument_id,
        ).where(HatFinancialResult.public_at >= since, HatFinancialResult.public_at < until)
    ).all()
    growth = {
        g.result_id: (g.sales_yoy, g.eps_yoy, g.turnaround)
        for g in session.scalars(
            select(HatFinancialGrowth).where(
                HatFinancialGrowth.as_of >= since, HatFinancialGrowth.as_of < until
            )
        )
    }
    return results_events(
        ((r[0], as_utc(r[1]), r[2], r[3]) for r in rows), growth, instruments, lineage
    )


def _isins(session: Session, instruments: Iterable[uuid.UUID]) -> dict[str, str]:
    wanted = set(instruments)
    return {
        str(row.id): row.isin
        for row in session.execute(select(HatInstrument.id, HatInstrument.isin))
        if row.id in wanted
    }


def _symbols(
    session: Session, instruments: Iterable[uuid.UUID]
) -> tuple[dict[str, list[tuple[date, str]]], dict[str, date]]:
    """Each instrument's dated symbols (`MarketData.symbols`) and its listing date."""
    wanted = set(instruments)
    found: dict[str, list[tuple[date, str]]] = defaultdict(list)
    for row in session.scalars(select(HatInstrumentSymbolHistory)):
        if row.instrument_id in wanted:
            found[str(row.instrument_id)].append((row.valid_from, row.symbol))
    listed: dict[str, date] = {}
    for row in session.execute(
        select(HatInstrument.id, HatInstrument.symbol, HatInstrument.listed_on)
    ):
        if row.id not in wanted:
            continue
        if str(row.id) not in found:
            found[str(row.id)].append((date.min, row.symbol))
        if row.listed_on is not None:
            listed[str(row.id)] = row.listed_on
    return {k: sorted(v) for k, v in found.items()}, listed


def _delisted(session: Session, instruments: Iterable[uuid.UUID]) -> dict[str, date]:
    wanted = set(instruments)
    return {
        str(row.id): row.delisted_on
        for row in session.scalars(select(HatInstrument))
        if row.id in wanted and row.delisted_on is not None
    }


def stopped_trading(
    last_bar: Mapping[str, date], later: Collection[str], days: Sequence[date], slack: int
) -> dict[str, date]:
    """Each instrument that stopped trading, at its last bar (`MarketData.last_traded`):
    `last_bar` is each instrument's last bar up to the run's end, `later` the instruments with a
    bar after the end, `days` every session of the store. One with a later bar, or whose last
    bar is no more than `slack` sessions before the store's last day, has not stopped."""
    if not days:
        return {}
    last = len(days) - 1
    return {
        instrument_id: day
        for instrument_id, day in last_bar.items()
        if instrument_id not in later and last - bisect_left(days, day) > slack
    }


def jump_skips(
    jumps: Iterable[tuple[str, date]], days: Sequence[date], sessions: int
) -> set[tuple[str, date]]:
    """The (instrument, session) pairs within `sessions` sessions of a jump of (instrument, day),
    either side, the jump day included (`MarketData.skipped`); `days` are the store's sessions."""
    found: set[tuple[str, date]] = set()
    for instrument_id, day in jumps:
        at = bisect_left(days, day)
        found.update((instrument_id, d) for d in days[max(0, at - sessions) : at + sessions + 1])
    return found


def _series_points(frame: pl.DataFrame) -> dict[str, list[tuple[date, str]]]:
    """`identity.series_changes` of the bars, fed only each instrument's first row and the rows
    whose series differs from the row before (`frame` sorted by instrument and date)."""
    by = pl.col("series").shift(1).over("instrument_id")
    first = pl.int_range(pl.len()).over("instrument_id") == 0
    changed = frame.select("instrument_id", "date", "series").filter(
        first | pl.col("series").ne_missing(by)
    )
    return series_changes(changed.iter_rows())


def _skipped(
    store: DataStore, frame: pl.DataFrame, adjustment: Adjustment, window: Sequence[date], end: date
) -> tuple[frozenset[tuple[str, date]], int]:
    """The universe's instrument-days with a bar near an open review jump (`MarketData.skipped`),
    and the count of those jumps."""
    wanted = set(frame["instrument_id"].unique().to_list())
    jumps = [
        (j.instrument_id, j.day)
        for j in unexplained_jumps(store, adjustment, window[0], until=end)
        if j.instrument_id in wanted
    ]
    near = jump_skips(jumps, window, JUMP_SKIP_SESSIONS)
    if not near:
        return frozenset(), len(jumps)
    pairs = pl.DataFrame(
        sorted(near), schema={"instrument_id": pl.String, "date": pl.Date}, orient="row"
    )
    found = frame.select("instrument_id", "date").join(pairs, on=["instrument_id", "date"])
    return frozenset(found.iter_rows()), len(jumps)


def _features(store: DataStore, days: Iterable[date], wanted: Sequence[str]) -> pl.DataFrame:
    """The features of `days` of the `wanted` instruments (the scan ranks within the universe, so
    no other row is read)."""
    frames = [
        part.with_columns(pl.lit(day).alias("date"))
        for day in days
        if (
            part := store.read_partition(DATASET_FEATURES, day)
            .with_columns(pl.col("instrument_id").cast(pl.String))
            .filter(pl.col("instrument_id").is_in(wanted))
        ).height
    ]
    if not frames:
        raise BacktestError("backtest_no_features")
    return pl.concat(frames, how="diagonal_relaxed")


def _current_features(
    store: DataStore,
    digest: str,
    days: list[date],
    breadth_days: list[date],
    wanted: Sequence[str],
    read: Mapping[date, str],
) -> tuple[pl.DataFrame, dict[date, Decimal]]:
    """The features of `days` and the breadth of `breadth_days`, read under the features job's
    lock (shared), so a run that is rewriting partitions finishes first and both come from one
    generation; `backtest_features_stale` unless the job recorded every day as written for the
    inputs `digest` from the bars `read` fingerprints (taken before the bars were read) and the
    store still holds those bars (`features_state.features_current`)."""
    with features_read_lock(store):
        if not features_current(store, digest, days, read):
            raise BacktestError("backtest_features_stale")
        return _features(store, days, wanted), _breadth(store, breadth_days)


def _breadth(store: DataStore, days: Iterable[date]) -> dict[date, Decimal]:
    """The features job's `breadth_pct` of each of `days` that has one, as the regime job
    reads it (`portfolio.jobs.read_breadth`)."""
    found: dict[date, Decimal] = {}
    for day in days:
        frame = store.read_partition(DATASET_BREADTH, day)
        value = exact(frame["breadth_pct"][0], floats=True) if frame.height else None
        if value is not None:
            found[day] = value
    return found


def demerger_exits(
    rows: Iterable[tuple[uuid.UUID, date, date | None]],
    lineage: Mapping[str, str],
    sessions: Sequence[date],
    first: date,
    end: date,
) -> frozenset[tuple[str, date]]:
    """The (chain id, exit session) of each demerger (instrument, ex-date, announced_on) whose
    forced exit falls in `first..end` over the sorted store `sessions`."""
    found = set()
    for instrument_id, ex_date, announced_on in rows:
        day = exit_day(sessions, ex_date, announced_on)
        if day is not None and first <= day <= end:
            found.add((str(_chain_id(instrument_id, lineage)), day))
    return frozenset(found)


def _demerger_rows(session: Session) -> list[tuple[uuid.UUID, date, date | None]]:
    return [
        (a.instrument_id, a.ex_date, a.announced_on)
        for a in session.scalars(select(HatCorporateAction).where(HatCorporateAction.demerger))
    ]


def _content_digest(market: MarketData) -> str:
    """sha256 of what the run read: the bars, features, index closes, membership intervals,
    industry history, dividends and, when loaded, the results filings, each in a fixed order.
    Two snapshots with the same row counts but different prices or members have different
    digests."""
    digest = hashlib.sha256()
    for frame in (market.bars, market.features):
        ordered = frame.select(sorted(frame.columns)).sort(["date", "instrument_id"])
        # the CSV of the whole frame, written a slice at a time
        for at in range(0, max(1, ordered.height), _DIGEST_SLICE):
            part = ordered.slice(at, _DIGEST_SLICE)
            digest.update(part.write_csv(include_header=at == 0).encode())
    for name, points in sorted(market.index_closes.items()):
        digest.update(repr((name, [(d.isoformat(), str(c)) for d, c in points])).encode())
    digest.update(
        repr(
            sorted((str(m.instrument_id), m.effective_from, m.effective_to) for m in market.members)
        ).encode()
    )
    digest.update(repr(sorted(market.industries.items())).encode())
    if market.dividends:
        digest.update(
            repr(
                sorted(
                    (k, [(d.isoformat(), str(c)) for d, c in v])
                    for k, v in market.dividends.items()
                )
            ).encode()
        )
    if market.pending:
        digest.update(repr(sorted((i, d.isoformat()) for i, d in market.pending)).encode())
    if market.demerger_exits:
        digest.update(repr(sorted((i, d.isoformat()) for i, d in market.demerger_exits)).encode())
    if market.breadth:
        digest.update(
            repr(sorted((d.isoformat(), str(v)) for d, v in market.breadth.items())).encode()
        )
    if market.results:
        digest.update(results_digest_text(market.results).encode())
    return digest.hexdigest()


def _results_snapshot(filings: Mapping[str, Sequence[ResultEvent]], loaded: bool) -> dict[str, int]:
    """The snapshot's count of the results filings read, only when the run loaded them."""
    if not loaded:
        return {}
    return {"financial_results": sum(len(v) for v in filings.values())}


def results_digest_text(results: Mapping[str, Sequence[ResultEvent]]) -> str:
    """The results filings a run read, in a fixed order, for its content digest: a changed
    filing or growth is a different snapshot."""
    return repr(
        sorted(
            (
                instrument_id,
                [
                    (
                        e.public_at.isoformat(),
                        e.period_end.isoformat(),
                        str(e.sales_yoy),
                        str(e.eps_yoy),
                        e.turnaround,
                    )
                    for e in events
                ],
            )
            for instrument_id, events in results.items()
        )
    )


def load_market(
    store: DataStore,
    session: Session,
    *,
    universe_version_id: uuid.UUID,
    index: str,
    start: date,
    end: date,
    regime_inputs: bool = False,
    results: bool = False,
) -> tuple[MarketData, dict[str, Any]]:
    """The market of a run of `start..end` on the universe, and its data snapshot (what was
    read, for `run.json` `data.data_snapshot_id` and `--rerun`). Raises `BacktestError` when the
    store has no
    session in the period or no features, and `backtest_features_stale` when the features were
    not written for the stored corporate actions (the nightly job's digest) from the bars the
    store holds now, or a session of the window has none: the loop would read prices adjusted
    one way and features computed another, or miss a night's scan, which the nightly job never
    does. With `regime_inputs` it also reads the regime engine's index and VIX closes
    and the window's breadth, and raises `backtest_no_regime_inputs` when the store has none
    of one of them (PR 68). The window's breadth is always read: a breadth regime gate reads it
    (a session without one turns that night's gate off, as live) and the run's result files
    record it (`folder`)."""
    read = store.fingerprints(DATASET_BARS)  # the bars about to be read, for the features gate
    every_day = sorted(read)
    days = [d for d in every_day if d <= end]
    first = bisect_left(days, start)
    if first >= len(days):
        raise BacktestError("backtest_no_data")
    window = days[max(0, first - LOOKBACK_SESSIONS) :]
    adjustment = stored_adjustment(session)
    expected = inputs_digest(adjustment)
    # the first scan runs on the night before the first session (the loop's `prev`)
    members, membership = _members(
        session, universe_version_id, adjustment.lineage, days[max(0, first - 1)], days[first], end
    )
    instruments = {m.instrument_id for m in members}
    wanted = sorted(str(i) for i in instruments)
    with store.connect(adjustment) as con:
        con.execute(_PRICE_E4_SQL)
        frame = con.execute(_BARS_SQL, [window[0], end, wanted]).pl()
        later = (
            {r[0] for r in con.execute(_LATER_SQL, [end, wanted]).fetchall()}
            if every_day[-1] > end
            else set()
        )
        dividends: dict[str, list[tuple[date, Decimal]]] = defaultdict(list)
        for instrument_id, ex_date, amount, basis in con.execute(
            _DIVIDENDS_SQL, [window[0], end, wanted]
        ).fetchall():
            dividends[str(instrument_id)].append((ex_date, Decimal(amount) * Decimal(basis)))
        # NIFTY 500 and its TRI always: the run folder splits trades by the one's regime and
        # benchmarks against the other (`folder`)
        names = sorted(
            {
                index,
                regime.INDEX_NAME,
                NIFTY_500_TRI,
                *((regime.VIX_NAME,) if regime_inputs else ()),
            }
        )
        rows = con.execute(_INDEX_SQL, [names, end]).fetchall()
    # read once DuckDB has let go of the bars' memory
    features, breadth = _current_features(
        store,
        expected,
        days[max(0, first - 1) :],
        window,  # every run's result files read the breadth (`folder`)
        wanted,
        read,
    )
    index_closes: dict[str, list[tuple[date, Decimal]]] = {name: [] for name in names}
    for name, day, close in rows:
        index_closes[name].append((day, Decimal(close)))
    closes = index_closes[index]
    if regime_inputs and not (
        breadth and all(index_closes[n] for n in (regime.INDEX_NAME, regime.VIX_NAME))
    ):
        raise BacktestError("backtest_no_regime_inputs")
    frame = latest_rows(frame)
    series = _series_points(frame)
    bars = frame.select(_BAR_COLUMNS)
    del frame
    last_bar = dict(bars.group_by("instrument_id").agg(pl.col("date").max()).iter_rows())
    last_traded = stopped_trading(last_bar, later, every_day, STOPPED_SLACK_SESSIONS)
    # Only an instrument delisted by the period's end has a last traded session in the run; one
    # delisted later simply has no more bars after `end`, which is not a delisting.
    for instrument_id, delisted_on in _delisted(session, instruments).items():
        if delisted_on > end:
            continue
        dates = bars.filter(
            (pl.col("instrument_id") == instrument_id) & (pl.col("date") <= delisted_on)
        )["date"]
        if dates.len():
            found: date = dates.max()  # type: ignore[assignment]
            last_traded[instrument_id] = min(found, last_traded.get(instrument_id, found))
    skipped, jumps = _skipped(store, bars, adjustment, window, end)
    pending = frozenset(chain_pending(session, window, wanted))
    exits = demerger_exits(_demerger_rows(session), adjustment.lineage, every_day, window[0], end)
    exits = frozenset((i, d) for i, d in exits if i in wanted)
    versions_sha, versions = versions_digest(session)
    symbols, listed_on = _symbols(session, instruments)
    intraday = BarStore(store.root)
    sessions_5m = [d for d in intraday.days() if start <= d <= end]
    filings = _results(session, instruments, adjustment.lineage, window[0], end, wanted=results)
    market = MarketData(
        days=tuple(window),
        bars=bars,
        features=features,
        index_closes=index_closes,
        members=members,
        industries=_industries(session, instruments, adjustment.lineage),
        series=series,
        last_traded=last_traded,
        breadth=breadth,
        skipped=skipped,
        pending=pending,
        dividends=dict(dividends),
        demerger_exits=exits,
        isins=_isins(session, instruments),
        symbols=symbols,
        listed_on=listed_on,
        intraday=intraday if sessions_5m else None,
        results=filings,
    )
    snapshot = {
        "store_first": window[0].isoformat(),
        "store_last": window[-1].isoformat(),
        "sessions": len(window),
        "bars": bars.height,
        "features": features.height,
        "index": index,
        "index_points": len(closes),
        "breadth_points": len(breadth),
        # the period's sessions with a `bars_5m` day file: the sessions a run may decide on them
        "bars_5m_sessions": len(sessions_5m),
        **(
            {
                "regime_index_points": len(index_closes[regime.INDEX_NAME]),
                "vix_points": len(index_closes[regime.VIX_NAME]),
            }
            if regime_inputs
            else {}
        ),
        "members": len({m.instrument_id for m in members}),
        "membership": membership,
        "dividends": sum(len(v) for v in dividends.values()),
        "last_traded": len(last_traded),
        "jump_skip": {
            "rule": f"no trade or mark within {JUMP_SKIP_SESSIONS} sessions of an open jump",
            "jumps": jumps,
            "instrument_days": len(skipped),
        },
        "factor_pending": {
            "rule": "no scan or entry from an ex-date while no version is available or the"
            " available one differs from the series' (CA6)",
            "instruments": len({i for i, _ in pending}),
            "instrument_days": len(pending),
        },
        "demerger_exits": {
            "rule": "a position held into a demerger whose record date was announced before the"
            " last session before its ex-date exits at that session's close (CA8)",
            "exits": len(exits),
        },
        "actions_digest": expected,
        # the series is the hindsight one; the pending days make the run point in time against
        # these versions (CA6)
        "point_in_time": {"versions_digest": versions_sha, "versions": versions},
        "content_digest": _content_digest(market),
        **_results_snapshot(filings, results),
    }
    return market, snapshot

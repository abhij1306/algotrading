"""The stock and setup features of a signal, read point in time when it fires: what the run's
result files (`folder`, docs/backtests/result-standard.md) report per trade, refusal and
signal, so taken, refused and ignored setups compare on the same columns. Nothing here decides
a trade.

Every feature of a signal on trade date d reads the session before d (`prev`, the night the
watchlist was scanned) or the entry itself, never a later bar, except `breakout_vol_ratio`,
the trade date's own volume over its 50 sessions before (known at d's close; a descriptive
column, not an input of any rule). The stock's levels come from `features_daily` as stored
(each date's levels on that date's dividend scale).

- `adv_20d_cr`: `turnover20` (20-session mean traded value) in ₹ crore; `liquidity_bucket` its
  quintile (`standard.quintile`) among the members of the run's universe on d that have one.
- `mcap_cr`, `cap_bucket`, `index_membership`: None. The store has no point-in-time market cap
  or shares outstanding, and no index membership besides NIFTY 500 (the universe itself).
- `listing_age_years`: from `hat_instrument.listed_on` to d.
- The VCP columns read the scan's `vcp` or `book_vcp` result on the watchlist row
  (`scan._stage_json` extras); a playbook without one leaves them None. The book columns
  (`footprint` to `stage2_start_unknown`) are `book_vcp`'s log: `contraction_depths` each
  contraction's depth in percent, oldest first, joined by `/`.
- `rs_rank`: the value of the setup's `rs_rank` criterion on the row (the rank of its `basis`,
  `rs_eligible` 1..99 within the bot's universe stage under `eligible`); without one, the
  stored `rs_pct`.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

from ..engine.portfolio.universe import members_on
from .data import (
    HINDSIGHT_BREAKOUT_VOL_RATIO,
    Bar,
    MarketData,
    MarketView,
    industry_on,
    symbol_on,
)
from .standard import quintile

__all__ = ["FEATURE_COLUMNS", "STOCK_COLUMNS", "SETUP_COLUMNS", "SignalFeatures", "vcp_extras"]

CRORE = Decimal(10_000_000)
SLOPE_SESSIONS = 20
VOLUME_BASELINE = 50
# `book_vcp`'s log, as its extras name it (`contraction_depths` built from its contractions).
BOOK_EXTRAS = (
    "footprint",
    "shakeout",
    "dryup_mean_ratio",
    "dryup_min_ratio",
    "tc_low_sessions",
    "tc_after_sessions",
    "rc_ratio",
    "index_depth_pct",
    "base_number",
    "stage2_start_unknown",
)
BOOK_COLUMNS = ("footprint", "contraction_depths", *BOOK_EXTRAS[1:])
PATTERN_TYPES = ("vcp", "book_vcp")
# The stock and setup columns, in the order the result files write them.
STOCK_COLUMNS = (
    "mcap_cr",
    "cap_bucket",
    "index_membership",
    "adv_20d_cr",
    "liquidity_bucket",
    "price",
    "listing_age_years",
)
SETUP_COLUMNS = (
    "vcp_contractions",
    "base_length_sessions",
    "base_depth_pct",
    "final_contraction_pct",
    "depth_ratio",
    "pivot_vs_base_high_pct",
    "entry_vs_pivot_pct",
    "breakout_vol_ratio",
    "stop_pct",
    "rs_rank",
    "dist_52w_high_pct",
    "sma50_slope_20d_pct",
    "setup_score",
    *BOOK_COLUMNS,
)
FEATURE_COLUMNS = ("symbol", "industry", *STOCK_COLUMNS, *SETUP_COLUMNS)
_PLACES = 4


def _round(value: float | Decimal | None) -> float | None:
    return None if value is None else round(float(value), _PLACES)


def _first(*values: float | Decimal | None) -> float | Decimal | None:
    return next((v for v in values if v is not None), None)


def _plain(value: object) -> object:
    """A logged extra as a cell: a number rounded, a flag or text as is, anything else None."""
    if isinstance(value, bool | str):
        return value
    if isinstance(value, int | float):
        return _round(value)
    return None


def _pct(over: Decimal | None, under: Decimal | None) -> float | None:
    """(over / under - 1) x 100; None without both or a non-positive base."""
    if over is None or under is None or under <= 0:
        return None
    return _round((over / under - 1) * 100)


def _criteria(criteria_results: object) -> list[Mapping[str, Any]]:
    """The criterion items of a watchlist row's `criteria_results`, in stage order."""
    stages = criteria_results.get("stages") if isinstance(criteria_results, Mapping) else None
    return [
        item
        for stage in (stages if isinstance(stages, list) else [])
        for item in (stage.get("criteria") or [] if isinstance(stage, Mapping) else [])
        if isinstance(item, Mapping)
    ]


def vcp_extras(criteria_results: object) -> Mapping[str, Any] | None:
    """The extras of the passing `vcp` or `book_vcp` criterion in a watchlist row's
    `criteria_results`."""
    for item in _criteria(criteria_results):
        extras = item.get("extras")
        if item.get("type") in PATTERN_TYPES and isinstance(extras, Mapping):
            return extras
    return None


def rs_value(criteria_results: object) -> float | None:
    """The value of the first `rs_rank` criterion evaluated on the row, None without one."""
    for item in _criteria(criteria_results):
        value = item.get("value")
        if item.get("type") == "rs_rank" and isinstance(value, (int, float)):
            return float(value)
    return None


class SignalFeatures:
    """The features of signals of one run, over its `MarketData` and the loop's `MarketView`."""

    def __init__(self, data: MarketData, view: MarketView) -> None:
        self.data = data
        self.view = view
        self._turnover: dict[date, list[float]] = {}

    def _universe_turnover(self, day: date, prev: date) -> list[float]:
        """The sorted `turnover20` of `prev` of the universe's members on `day`."""
        found = self._turnover.get(day)
        if found is None:
            values = (
                self.view.feature(prev, str(i), "turnover20")
                for i in members_on(self.data.members, day)
            )
            found = sorted(float(v) for v in values if v is not None)
            self._turnover[day] = found
        return found

    def at(
        self,
        instrument_id: str,
        day: date,
        prev: date,
        *,
        entry: Decimal,
        stop: Decimal,
        score: Decimal | None,
        pivot: Decimal | None,
        criteria_results: object,
    ) -> dict[str, Any]:
        """The signal's `FEATURE_COLUMNS` (None where unknown) for an entry at `entry` with
        `stop` on `day`, the watchlist row scanned on `prev` with its `score`, `pivot` and
        `criteria_results`."""
        feature = self.view.feature
        turnover = feature(prev, instrument_id, "turnover20")
        listed = self.data.listed_on.get(instrument_id)
        out: dict[str, Any] = {
            "symbol": symbol_on(self.data, instrument_id, day),
            "industry": industry_on(self.data, instrument_id, day),
            "mcap_cr": None,
            "cap_bucket": None,
            "index_membership": None,
            "adv_20d_cr": None if turnover is None else _round(turnover / CRORE),
            "liquidity_bucket": None
            if turnover is None
            else quintile(float(turnover), self._universe_turnover(day, prev)),
            "price": entry,
            "listing_age_years": None
            if listed is None
            else _round(Decimal((day - listed).days) / Decimal("365.25")),
            "stop_pct": _round((entry - stop) / entry * 100) if entry > 0 else None,
            "rs_rank": _round(
                _first(rs_value(criteria_results), feature(prev, instrument_id, "rs_pct"))
            ),
            "setup_score": _round(score),
            "entry_vs_pivot_pct": _pct(entry, pivot),
            "breakout_vol_ratio": self._volume_ratio(instrument_id, day),
        }
        close = self.view.last_close(instrument_id, prev)
        out["dist_52w_high_pct"] = _pct(
            None if close is None else close[1], feature(prev, instrument_id, "high_52w")
        )
        at = self.view.index[prev]
        earlier = self.view.days[at - SLOPE_SESSIONS] if at >= SLOPE_SESSIONS else None
        out["sma50_slope_20d_pct"] = _pct(
            feature(prev, instrument_id, "sma50"),
            None if earlier is None else feature(earlier, instrument_id, "sma50"),
        )
        out.update(self._vcp(instrument_id, prev, pivot, vcp_extras(criteria_results)))
        return {name: out.get(name) for name in FEATURE_COLUMNS}

    def _volume_ratio(self, instrument_id: str, day: date) -> float | None:
        with self.view.hindsight(
            HINDSIGHT_BREAKOUT_VOL_RATIO
        ):  # the day's own volume: known at its close (the docstring)
            volumes = self.view.volumes_through(instrument_id, day, VOLUME_BASELINE + 1)
        if len(volumes) <= VOLUME_BASELINE or any(math.isnan(v) for v in volumes):
            return None
        base = sum(volumes[:-1]) / VOLUME_BASELINE
        return None if base <= 0 else _round(volumes[-1] / base)

    def _vcp(
        self,
        instrument_id: str,
        prev: date,
        pivot: Decimal | None,
        extras: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """The VCP columns from the scan's `vcp` extras: the contraction count, the base's
        length in sessions through `prev`, its depth (the deepest contraction), the final
        contraction's depth and its ratio to the first's, and the pivot against the base's
        highest high."""
        if extras is None:
            return {}
        depths = [
            float(c["depth_pct"])
            for c in extras.get("contractions") or []
            if isinstance(c, Mapping) and isinstance(c.get("depth_pct"), (int, float))
        ]
        out: dict[str, Any] = {
            **{name: _plain(extras.get(name)) for name in BOOK_EXTRAS},
            "contraction_depths": "/".join(f"{d:.2f}" for d in depths)
            if "footprint" in extras
            else None,
            "vcp_contractions": extras.get("contraction_count"),
            "base_depth_pct": _round(max(depths)) if depths else None,
            "final_contraction_pct": _round(depths[-1]) if depths else None,
            "depth_ratio": _round(depths[-1] / depths[0]) if depths and depths[0] > 0 else None,
        }
        start = extras.get("base_start")
        try:
            first = date.fromisoformat(start) if isinstance(start, str) else None
        except ValueError:
            first = None
        if first is not None and first in self.view.index:
            out["base_length_sessions"] = self.view.index[prev] - self.view.index[first] + 1
            bars: Sequence[Bar] = self.view.bars_through(
                instrument_id, prev, out["base_length_sessions"]
            )
            high = max((b.high for b in bars), default=None)
            out["pivot_vs_base_high_pct"] = _pct(pivot, high)
        return out

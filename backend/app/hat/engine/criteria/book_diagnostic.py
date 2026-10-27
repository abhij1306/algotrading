"""A diagnostic view of `book_vcp` for audits: every rule of the base (spec C) and of the later
gates (D, E, F) evaluated on its own, without the criterion's short-circuit, from the same
helpers (`book`). The criterion itself is `book.book_vcp`; nothing here decides a signal.

A rule that needs a contraction fails without one, and a rule whose data is missing (the dry-up
volumes, the index closes) fails, as the criterion rejects such a stock-day.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from .base import dec
from .book import (
    BookVcpParams,
    Contraction,
    Swing,
    contractions_of,
    dryup_ratios,
    h0_index,
    relative_fault,
    time_compression,
    zigzag,
)

# The rules of C in the order `book.geometry_fault` checks them (base length both ways is one).
GEOMETRY_RULES = (
    "base_length",
    "swing_high_above_h0",
    "contraction_count",
    "first_depth_band",
    "depth_ratio",
    "final_depth",
    "final_sessions",
)
# The rules of D, E and F (`book.time_fault`, `book.dryup_fault`, `book.relative_fault`).
LATER_RULES = ("tc_ratio", "tc_min_sessions", "dryup_mean", "dryup_session", "relative_correction")


@dataclass(frozen=True)
class GeometryCheck:
    """The base of one signal bar: H0, the confirmed swings and contractions, and each rule of
    C as passed (True) or failed."""

    h0: int
    swings: tuple[Swing, ...]
    contractions: tuple[Contraction, ...]
    rules: Mapping[str, bool]

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(r for r in GEOMETRY_RULES if not self.rules[r])

    @property
    def depths(self) -> tuple[Decimal, ...]:
        return tuple(c.depth_pct for c in self.contractions)


def geometry_rules(
    h0: int,
    contractions: Sequence[Contraction],
    swing_highs_above_h0: int,
    end: int,
    params: BookVcpParams,
) -> dict[str, bool]:
    """Each rule of C for a base from bar `h0` to the signal bar `end`, independently."""
    length = end - h0 + 1
    depths = [c.depth_pct for c in contractions]
    low, high = (dec(v) for v in params.first_depth_pct)
    ratio = dec(params.depth_ratio_max)
    return {
        "base_length": params.base_sessions[0] <= length <= params.base_sessions[1],
        "swing_high_above_h0": swing_highs_above_h0 == 0,
        "contraction_count": params.contractions[0] <= len(depths) <= params.contractions[1],
        "first_depth_band": bool(depths) and low <= depths[0] <= high,
        "depth_ratio": all(b <= ratio * a for a, b in zip(depths, depths[1:], strict=False)),
        "final_depth": bool(depths) and depths[-1] <= dec(params.final_depth_max_pct),
        "final_sessions": bool(depths) and contractions[-1].sessions >= params.final_min_sessions,
    }


def geometry_check(
    high: Sequence[float],
    low: Sequence[float],
    threshold: Sequence[float],
    end: int,
    params: BookVcpParams,
) -> GeometryCheck | None:
    """The base of the signal bar `end` with every rule of C evaluated (`geometry_rules`); None
    without an H0 (`no_base`), where no other rule can be read."""
    h0 = h0_index(high, low, end, params)
    if h0 is None:
        return None
    swings = zigzag(high, low, threshold, h0, end)
    top = high[h0]
    above = sum(1 for s in swings[1:] if s.high and s.price > top)
    contractions = contractions_of(high, low, swings)
    rules = geometry_rules(h0, contractions, above, end, params)
    return GeometryCheck(h0, tuple(swings), tuple(contractions), rules)


@dataclass(frozen=True)
class LaterCheck:
    """The values of D, E and F for a base, and each of their rules passed (True) or failed."""

    tc_low_sessions: int
    tc_after_sessions: int
    dryup_mean_ratio: Decimal | None
    dryup_min_ratio: Decimal | None
    idx_depth_pct: Decimal | None
    rc_ratio: Decimal | None
    rules: Mapping[str, bool]

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(r for r in LATER_RULES if not self.rules[r])


def later_check(
    geo: GeometryCheck,
    low: Sequence[float],
    volume: Sequence[float | None],
    end: int,
    idx_depth: Decimal | None,
    params: BookVcpParams,
) -> LaterCheck:
    """D, E and F for `geo` (which needs a contraction), each rule on its own; `idx_depth` the
    NIFTY 500's largest fall from H0's date to the signal date (`book.index_depth_pct`)."""
    lows = low[geo.h0 : end + 1]
    deepest = geo.h0 + lows.index(min(lows))
    before, after = time_compression(geo.h0, deepest, end)
    ratios = dryup_ratios(volume, geo.contractions[-1], params.dryup_avg_days)
    first = geo.contractions[0].depth_pct
    rules = {
        "tc_ratio": params.tc_min_ratio is None or dec(after) >= dec(params.tc_min_ratio) * before,
        "tc_min_sessions": params.tc_min_sessions is None or after >= params.tc_min_sessions,
        "dryup_mean": params.dryup_mean_max is None
        or (ratios is not None and ratios[0] < dec(params.dryup_mean_max)),
        "dryup_session": params.dryup_session_max is None
        or (ratios is not None and ratios[1] < dec(params.dryup_session_max)),
        "relative_correction": params.rc_max_ratio is None
        or (idx_depth is not None and not relative_fault(first, idx_depth, params)),
    }
    return LaterCheck(
        before,
        after,
        None if ratios is None else ratios[0],
        None if ratios is None else ratios[1],
        idx_depth,
        None if not idx_depth else first / idx_depth,
        rules,
    )

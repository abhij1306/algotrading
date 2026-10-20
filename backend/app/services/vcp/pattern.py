"""Pure VCP pattern detection: relative strength, stage 2, base, contractions,
volume dry-up, breakout and grade.

Every function takes DataFrames or plain values and returns a value; none reads
the database, the clock or mutable module state. Frames are daily bars with
`close`, `close_ref` (adjusted close), `high`, `low` and `volume` columns, oldest first.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

# The stop sits this fraction below the final contraction low.
STOP_OFFSET_PCT = 0.001
BREAKOUT_VOLUME_MULTIPLIER = 1.5
A_GRADE_BREAKOUT_VOLUME_MULTIPLIER = BREAKOUT_VOLUME_MULTIPLIER * (4.0 / 3.0)

MIN_HISTORY_BARS = 260
MIN_RS_RATING = 80


@dataclass(frozen=True)
class Stage2Metrics:
    """How many of the five stage-2 trend-template conditions hold, and the averages behind them."""

    conditions_met: int
    ma_50: float
    ma_150: float
    ma_200: float
    high_52: float

    def averages(self) -> dict[str, float]:
        return {
            "ma_50": self.ma_50,
            "ma_150": self.ma_150,
            "ma_200": self.ma_200,
            "high_52": self.high_52,
        }


@dataclass(frozen=True)
class Base:
    """The base: bars from the 60-day closing high onward, and its depth in percent."""

    frame: pd.DataFrame
    depth_pct: float


@dataclass(frozen=True)
class Contraction:
    """One pivot-high to pivot-low pullback inside the base (indices into the base frame)."""

    high_idx: int
    high_price: float
    low_idx: int
    low_price: float
    depth_pct: float


@dataclass(frozen=True)
class VolumeDryUp:
    avg_volume_50: float
    # Final-contraction average volume as a percentage of the 50-day average.
    dry_up_pct: float
    # Least-squares slope of volume across the base (negative means falling).
    slope: float


@dataclass(frozen=True)
class Breakout:
    is_breakout: bool
    volume_mult: float
    # Where the close sits in the day's range, 0 (low) to 1 (high).
    close_position: float


@dataclass(frozen=True)
class PatternResult:
    """A valid VCP setup on the frame's last bar."""

    stage2: Stage2Metrics
    base_depth_pct: float
    days_in_base: int
    contractions: tuple[Contraction, ...]
    # Contraction depths rounded to two decimals; the tightening and grade rules use these.
    contraction_depths: list[float]
    volume: VolumeDryUp
    breakout: Breakout
    pivot_high: float
    final_contraction_low: float
    stop_level: float
    grade: str
    latest_close: float


def percentile_rating(series: pd.Series) -> pd.Series:
    """Percentile rank of each value scaled to an integer 0-99."""
    ranked = series.rank(method="average", pct=True)
    return ((ranked * 99).round().clip(lower=0, upper=99)).astype(int)


def compute_rs_map(symbol_frames: dict[str, pd.DataFrame]) -> dict[str, int]:
    """IBD-style relative-strength rating per symbol (weighted 3/6/9/12-month returns).

    Symbols with fewer than 253 bars are left out.
    """
    records: dict[str, float] = {}
    for symbol, frame in symbol_frames.items():
        closes = frame["close_ref"].reset_index(drop=True)
        if len(closes) < 253:
            continue
        records[symbol] = (
            (0.40 * float(closes.iloc[-1] / closes.iloc[-64] - 1.0))
            + (0.20 * float(closes.iloc[-1] / closes.iloc[-127] - 1.0))
            + (0.20 * float(closes.iloc[-1] / closes.iloc[-190] - 1.0))
            + (0.20 * float(closes.iloc[-1] / closes.iloc[-253] - 1.0))
        )
    if not records:
        return {}
    rating = percentile_rating(pd.Series(records))
    return {symbol: int(value) for symbol, value in rating.items()}


def stage2_metrics(frame: pd.DataFrame) -> Stage2Metrics:
    closes = frame["close_ref"].reset_index(drop=True)
    ma_50 = closes.rolling(50).mean()
    ma_150 = closes.rolling(150).mean()
    ma_200 = closes.rolling(200).mean()
    high_52 = closes.rolling(252).max()
    latest_index = len(closes) - 1
    conditions = [
        closes.iloc[-1] > ma_150.iloc[-1] and closes.iloc[-1] > ma_200.iloc[-1],
        ma_150.iloc[-1] > ma_200.iloc[-1],
        bool(latest_index >= 20 and ma_200.iloc[-1] > ma_200.iloc[-21]),
        closes.iloc[-1] >= (0.75 * high_52.iloc[-1]),
        ma_50.iloc[-1] > ma_150.iloc[-1] > ma_200.iloc[-1],
    ]
    return Stage2Metrics(
        conditions_met=sum(bool(item) for item in conditions),
        ma_50=float(ma_50.iloc[-1]) if not pd.isna(ma_50.iloc[-1]) else 0.0,
        ma_150=float(ma_150.iloc[-1]) if not pd.isna(ma_150.iloc[-1]) else 0.0,
        ma_200=float(ma_200.iloc[-1]) if not pd.isna(ma_200.iloc[-1]) else 0.0,
        high_52=float(high_52.iloc[-1]) if not pd.isna(high_52.iloc[-1]) else 0.0,
    )


def pivot_points(base_df: pd.DataFrame, window: int = 3) -> tuple[list[int], list[int]]:
    """(pivot-high indices, pivot-low indices): bars beyond `window` neighbours on each side."""
    highs = base_df["high"].tolist()
    lows = base_df["low"].tolist()
    pivot_highs: list[int] = []
    pivot_lows: list[int] = []
    for idx in range(window, len(base_df) - window):
        hi = highs[idx]
        lo = lows[idx]
        if all(hi > value for value in highs[idx - window : idx]) and all(
            hi > value for value in highs[idx + 1 : idx + window + 1]
        ):
            pivot_highs.append(idx)
        if all(lo < value for value in lows[idx - window : idx]) and all(
            lo < value for value in lows[idx + 1 : idx + window + 1]
        ):
            pivot_lows.append(idx)
    return pivot_highs, pivot_lows


def has_no_overhead_resistance(frame: pd.DataFrame, pivot_high: float) -> bool:
    """False if, in the last 252 bars, closes up to 3% above the pivot held a level for 5+ bars."""
    if pivot_high <= 0:
        return False
    prior = frame.tail(252).copy()
    upper = pivot_high * 1.03
    candidates = prior[(prior["close_ref"] > pivot_high) & (prior["close_ref"] <= upper)]
    if candidates.empty:
        return True
    # Use a sliding observed-level check with a 2% band around each candidate level.
    closes = prior["close_ref"].tolist()
    levels = sorted({round(value, 2) for value in candidates["close_ref"].tolist()})
    for level in levels:
        streak = 0
        for close in closes:
            if abs(close - level) / level <= 0.02:
                streak += 1
                if streak >= 5:
                    return False
            else:
                streak = 0
    return True


def base_window(frame: pd.DataFrame) -> Base | None:
    """The base from the 60-day high onward, or None if shorter than 15 bars or deeper than 30%."""
    window = frame.tail(60).reset_index(drop=True)
    base_high_idx = int(window["close_ref"].idxmax())
    base_df = window.iloc[base_high_idx:].reset_index(drop=True)
    if len(base_df) < 15:
        return None

    base_high = float(base_df["close_ref"].iloc[0])
    base_low = float(base_df["close_ref"].min())
    base_depth = ((base_high - base_low) / base_high) * 100 if base_high > 0 else 100.0
    if base_depth > 30.0:
        return None
    return Base(frame=base_df, depth_pct=base_depth)


def find_contractions(base_df: pd.DataFrame) -> list[Contraction]:
    """Successive pivot-high to pivot-low pullbacks in the base (at most four)."""
    pivot_highs, pivot_lows = pivot_points(base_df, window=3)
    current_high_idx = 0
    current_high_price = float(base_df["close_ref"].iloc[0])
    contractions: list[Contraction] = []
    while True:
        low_candidates = [idx for idx in pivot_lows if idx > current_high_idx]
        if not low_candidates:
            break
        low_idx = low_candidates[0]
        low_price = float(base_df["low"].iloc[low_idx])
        depth = (
            ((current_high_price - low_price) / current_high_price) * 100
            if current_high_price > 0
            else 100.0
        )
        contractions.append(
            Contraction(
                high_idx=current_high_idx,
                high_price=current_high_price,
                low_idx=low_idx,
                low_price=low_price,
                depth_pct=depth,
            )
        )
        next_highs = [idx for idx in pivot_highs if idx > low_idx]
        if not next_highs or len(contractions) >= 4:
            break
        current_high_idx = next_highs[0]
        current_high_price = float(base_df["high"].iloc[current_high_idx])
    return contractions


def contractions_tighten(depths: list[float]) -> bool:
    """2-4 contractions, first <= 25%, second <= 15%, last <= 10%, each shallower."""
    if not (2 <= len(depths) <= 4):
        return False
    if depths[0] > 25.0 or depths[-1] > 10.0:
        return False
    if len(depths) > 1 and depths[1] > 15.0:
        return False
    return not any(depths[idx] >= depths[idx - 1] for idx in range(1, len(depths)))


def volume_dry_up(
    frame: pd.DataFrame, base_df: pd.DataFrame, final_contraction: Contraction
) -> VolumeDryUp | None:
    """None unless final-contraction volume is at most 40% of the 50-day average
    and volume trends down across the base."""
    final_df = base_df.iloc[final_contraction.high_idx : final_contraction.low_idx + 1].copy()
    if final_df.empty:
        return None

    avg_volume_50 = float(frame["volume"].tail(50).mean())
    final_avg_volume = float(final_df["volume"].mean())
    dry_up_pct = ((final_avg_volume / avg_volume_50) * 100.0) if avg_volume_50 > 0 else 1000.0
    if dry_up_pct > 40.0:
        return None

    slope = np.polyfit(np.arange(len(base_df)), base_df["volume"].to_numpy(dtype=float), 1)[0]
    if slope >= 0:
        return None
    return VolumeDryUp(avg_volume_50=avg_volume_50, dry_up_pct=dry_up_pct, slope=float(slope))


def breakout_state(latest: pd.Series, pivot_high: float, avg_volume_50: float) -> Breakout:
    """A breakout closes above the pivot on 1.5x average volume in the top quarter of its range."""
    volume_mult = float(latest["volume"] / avg_volume_50) if avg_volume_50 > 0 else 0.0
    day_range = float(latest["high"] - latest["low"])
    close_position = (
        ((float(latest["close"]) - float(latest["low"])) / day_range) if day_range > 0 else 0.0
    )
    is_breakout = bool(
        float(latest["close"]) > pivot_high
        and volume_mult >= BREAKOUT_VOLUME_MULTIPLIER
        and close_position >= 0.75
    )
    return Breakout(is_breakout=is_breakout, volume_mult=volume_mult, close_position=close_position)


def grade(
    rs_rating: int,
    final_depth: float,
    volume_dry_up_pct: float,
    is_breakout: bool,
    breakout_volume_mult: float,
) -> str:
    if (
        rs_rating >= 90
        and final_depth <= 5.0
        and volume_dry_up_pct <= 40.0
        and ((not is_breakout) or breakout_volume_mult >= A_GRADE_BREAKOUT_VOLUME_MULTIPLIER)
    ):
        return "A"
    if rs_rating >= 80 and final_depth <= 10.0 and volume_dry_up_pct <= 60.0:
        return "B"
    return "C"


def analyze_pattern(frame: pd.DataFrame, rs_rating: int) -> PatternResult | None:
    """The VCP setup on the frame's last bar, or None if any gate fails.

    Gates: 260+ bars, all five stage-2 conditions, a base no deeper than 30%,
    2-4 tightening contractions, volume dry-up, RS rating 80+, and no overhead
    resistance above the pivot.
    """
    if len(frame) < MIN_HISTORY_BARS:
        return None

    stage2 = stage2_metrics(frame)
    if stage2.conditions_met < 5:
        return None

    base = base_window(frame)
    if base is None:
        return None

    contractions = find_contractions(base.frame)
    depths = [round(item.depth_pct, 2) for item in contractions]
    if not contractions_tighten(depths):
        return None

    final_contraction = contractions[-1]
    volume = volume_dry_up(frame, base.frame, final_contraction)
    if volume is None:
        return None

    pivot_high = float(final_contraction.high_price)
    if rs_rating < MIN_RS_RATING or not has_no_overhead_resistance(frame, pivot_high):
        return None

    latest = frame.iloc[-1]
    final_contraction_low = float(final_contraction.low_price)
    breakout = breakout_state(latest, pivot_high, volume.avg_volume_50)
    return PatternResult(
        stage2=stage2,
        base_depth_pct=base.depth_pct,
        days_in_base=len(base.frame),
        contractions=tuple(contractions),
        contraction_depths=depths,
        volume=volume,
        breakout=breakout,
        pivot_high=pivot_high,
        final_contraction_low=final_contraction_low,
        stop_level=round(final_contraction_low * (1.0 - STOP_OFFSET_PCT), 2),
        grade=grade(
            rs_rating, depths[-1], volume.dry_up_pct, breakout.is_breakout, breakout.volume_mult
        ),
        latest_close=float(latest["close"]),
    )

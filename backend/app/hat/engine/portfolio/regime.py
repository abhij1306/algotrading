"""Regime engine v1 (05 §3, HAT-PORT-030..034): rules only, as pure functions.

Three inputs (trend, volatility, breadth) classify the market into four states each night; a
state is confirmed only on the third consecutive day it is read; each sleeve has a multiplier per
confirmed state. Money-like numbers are `Decimal`. The model of the confirmation rule is
`verification/tla/RegimeConfirmation.tla`.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from app.hat.errors import HatError

BULL_QUIET = "bull_quiet"
BULL_VOLATILE = "bull_volatile"
CORRECTION = "correction"
BEAR = "bear"
# Least to most defensive: the order the classifier is monotone in (HAT-PORT-032).
STATES = (BULL_QUIET, BULL_VOLATILE, CORRECTION, BEAR)

SMA_SLOW = 200
SMA_FAST = 50
VIX_WINDOW = 252
CONFIRM_DAYS = 3  # [A] HAT-PORT-030
VIX_VOLATILE_PCT = Decimal(67)  # [A] HAT-PORT-032
BREADTH_QUIET_PCT = Decimal(50)
BREADTH_CORRECTION_PCT = Decimal(40)
# Momentum-sleeve multiplier per confirmed state: placeholders (05 §3, HAT-PORT-033).
DEFAULT_MULTIPLIERS: Mapping[str, Decimal] = {
    BULL_QUIET: Decimal("1.0"),
    BULL_VOLATILE: Decimal("0.75"),
    CORRECTION: Decimal("0.5"),
    BEAR: Decimal("0.25"),
}


class RegimeError(HatError):
    """A regime rule was broken or an input is unusable; `code` is stable and carries no data."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- Inputs (HAT-PORT-031) --------------------------------------------------------------------


@dataclass(frozen=True)
class RegimeInputs:
    """The three inputs of one trade date. `ad_ratio` (advance/decline) is recorded when the
    ingest has it (PR 7) and does not enter the v1 classification."""

    close: Decimal
    sma50: Decimal
    sma200: Decimal
    vix: Decimal
    vix_pct: Decimal
    breadth_pct: Decimal
    ad_ratio: Decimal | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "close": str(self.close),
            "sma50": str(self.sma50),
            "sma200": str(self.sma200),
            "vix": str(self.vix),
            "vix_pct": str(self.vix_pct),
            "breadth_pct": str(self.breadth_pct),
            "ad_ratio": None if self.ad_ratio is None else str(self.ad_ratio),
        }


def sma(values: Sequence[Decimal], window: int) -> Decimal | None:
    """Mean of the last `window` values; None while fewer exist."""
    if window < 1 or len(values) < window:
        return None
    return sum(values[-window:], Decimal(0)) / window


def vix_percentile(values: Sequence[Decimal], window: int = VIX_WINDOW) -> Decimal | None:
    """Percentile rank of the last value among the last `window` values (the last included):
    100 x (how many of them are strictly below it) / window. None while fewer than `window`
    values exist. [OWNER-PENDING] definition, HAT-PORT-031."""
    if window < 1 or len(values) < window:
        return None
    recent = values[-window:]
    today = recent[-1]
    return Decimal(100) * sum(1 for v in recent if v < today) / window


def series_until(series: Mapping[date, Decimal], day: date) -> list[Decimal]:
    """The values dated on or before `day`, oldest first: the only data an input of `day` may
    read (HAT-PORT-031)."""
    return [series[d] for d in sorted(series) if d <= day]


def compute_inputs(
    day: date,
    index_close: Mapping[date, Decimal],
    vix: Mapping[date, Decimal],
    breadth_pct: Decimal | None,
    ad_ratio: Decimal | None = None,
) -> RegimeInputs:
    """The inputs of `day` from the NIFTY 500 close and India VIX series and that day's breadth.

    Reads only values dated on or before `day`. Raises `RegimeError` when `day` has no index or
    VIX value, there is too little history for the two SMAs or the VIX window, or breadth is
    missing or outside [0, 100]: the job fails and no allocation is made on guessed inputs.
    """
    if day not in index_close:
        raise RegimeError("regime_no_index_today")
    if day not in vix:
        raise RegimeError("regime_no_vix_today")
    if breadth_pct is None or not breadth_pct.is_finite() or not 0 <= breadth_pct <= 100:
        raise RegimeError("regime_no_breadth")
    closes = series_until(index_close, day)
    vix_values = series_until(vix, day)
    sma200 = sma(closes, SMA_SLOW)
    sma50 = sma(closes, SMA_FAST)
    pct = vix_percentile(vix_values)
    if sma200 is None or sma50 is None or pct is None:
        raise RegimeError("regime_history_short")
    return RegimeInputs(
        close=closes[-1],
        sma50=sma50,
        sma200=sma200,
        vix=vix_values[-1],
        vix_pct=pct,
        breadth_pct=breadth_pct,
        ad_ratio=ad_ratio,
    )


# --- Classification (HAT-PORT-032) ------------------------------------------------------------


def classify(
    close: Decimal, sma50: Decimal, sma200: Decimal, vix_pct: Decimal, breadth_pct: Decimal
) -> str:
    """The one state of the inputs. Precedence Bear > Correction > Bull volatile > Bull quiet; a
    close equal to an SMA counts as not above it, so the four rows cover every input
    ([OWNER-PENDING] boundary rule)."""
    if close <= sma200:
        return BEAR
    if close <= sma50 or breadth_pct < BREADTH_CORRECTION_PCT:
        return CORRECTION
    if vix_pct >= VIX_VOLATILE_PCT or breadth_pct < BREADTH_QUIET_PCT:
        return BULL_VOLATILE
    return BULL_QUIET


def classify_inputs(inputs: RegimeInputs) -> str:
    return classify(inputs.close, inputs.sma50, inputs.sma200, inputs.vix_pct, inputs.breadth_pct)


# --- The 3-day rule (HAT-PORT-030) ------------------------------------------------------------


@dataclass(frozen=True)
class Confirmation:
    """The confirmation state after a day: the confirmed state, the latest raw state and how many
    days in a row the raw state has been that value."""

    confirmed: str
    last_raw: str
    run: int

    def to_json(self) -> dict[str, Any]:
        return {"confirmed_state": self.confirmed, "raw_state": self.last_raw, "run": self.run}


def confirm_step(previous: Confirmation | None, raw: str, days: int = CONFIRM_DAYS) -> Confirmation:
    """The confirmation state after reading `raw`: the confirmed state changes to `raw` only
    when `raw` has been read on `days` consecutive days (this one the last). With no earlier day
    the first reading is the confirmed state (bootstrap)."""
    if raw not in STATES:
        raise RegimeError("regime_unknown_state")
    if previous is None:
        return Confirmation(raw, raw, 1)
    run = previous.run + 1 if raw == previous.last_raw else 1
    confirmed = raw if raw != previous.confirmed and run >= days else previous.confirmed
    return Confirmation(confirmed, raw, run)


def confirm_sequence(raws: Iterable[str], days: int = CONFIRM_DAYS) -> list[Confirmation]:
    """`confirm_step` over a whole series of raw states."""
    out: list[Confirmation] = []
    previous: Confirmation | None = None
    for raw in raws:
        previous = confirm_step(previous, raw, days)
        out.append(previous)
    return out


# --- Multipliers (HAT-PORT-033) ---------------------------------------------------------------


def multiplier_for(state: str, table: Mapping[str, Decimal] = DEFAULT_MULTIPLIERS) -> Decimal:
    """The sleeve multiplier of a confirmed state; a state missing from the table is an error,
    never a default."""
    try:
        return table[state]
    except KeyError:
        raise RegimeError("regime_unknown_state") from None


def derive_multipliers(
    estimates: Mapping[str, Decimal], shrink: Decimal, lo: Decimal, hi: Decimal
) -> dict[str, Decimal]:
    """A replacement multiplier table from backtested per-state estimates, shrunk toward 1.0
    (`estimate + shrink x (1 - estimate)`) and held within the policy bounds [lo, hi]. With
    `shrink` in [0, 1] a value lies between its estimate and 1.0 unless a bound holds it."""
    if not Decimal(0) <= shrink <= Decimal(1) or lo > hi:
        raise RegimeError("regime_bad_shrink")
    out = {}
    for state in STATES:
        if state not in estimates:
            raise RegimeError("regime_unknown_state")
        value = estimates[state] + shrink * (Decimal(1) - estimates[state])
        out[state] = min(max(value, lo), hi)
    return out


def rules_v1_config() -> dict[str, Any]:
    """The `hat_regime_model_version.config` of the rules model, version 1."""
    return {
        "confirm_days": CONFIRM_DAYS,
        "sma": {"fast": SMA_FAST, "slow": SMA_SLOW},
        "vix": {"window": VIX_WINDOW, "volatile_pct": str(VIX_VOLATILE_PCT)},
        "breadth": {
            "quiet_pct": str(BREADTH_QUIET_PCT),
            "correction_pct": str(BREADTH_CORRECTION_PCT),
        },
        "multipliers": {"momentum": {s: str(m) for s, m in DEFAULT_MULTIPLIERS.items()}},
    }

"""The confidence label of a signal (D132, HAT-EXEC-015, PR 71).

The label states how trades of the same kind did in the bot's backtest: the trades of the live
strategy version's evidence run in the signal's bucket, (playbook, setup-score band). It is not
the chance that this trade wins. `high` needs at least `MIN_TRADES` trades in the bucket and a
positive lower bound of the 90% interval of their mean R; `low` is no evidence, too few trades or
a mean at or below 0 R; `medium` is the rest. Missing or unreadable evidence is always `low`.

Auto mode places only a `high` signal; any other goes to the owner (D149, `modes.route_mode`).
The label is written once, with its basis, when the signal is created and never changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from ..money import exact
from ..risk.policy import Z_90

HIGH = "high"
MEDIUM = "medium"
LOW = "low"
LABELS = (HIGH, MEDIUM, LOW)

MIN_TRADES = 30  # OWNER-PENDING default (D132)

# The setup-score bands: the setup threshold is 0.6 and a stage with no score criteria scores 1.0.
BANDS: tuple[tuple[str, Decimal, Decimal], ...] = (
    ("0.60-0.75", Decimal("0.60"), Decimal("0.75")),
    ("0.75-0.90", Decimal("0.75"), Decimal("0.90")),
    ("0.90-1.00", Decimal("0.90"), Decimal("1.00")),
)
BAND_NAMES = tuple(name for name, _, _ in BANDS)

_PLACES = Decimal("0.000001")  # the stored statistics, so a reread compares equal

# Why a signal got its label (`confidence_basis.reason`).
NO_RUN = "no_run"
NO_POLICY = "no_policy"
NOT_BACKTESTED = "playbook_not_backtested"
NO_SCORE = "no_score"
NO_BUCKET = "no_bucket"
FEW_TRADES = "few_trades"
MEAN_NOT_POSITIVE = "mean_not_positive"
LOWER_BOUND_NOT_POSITIVE = "lower_bound_not_positive"
LOWER_BOUND_POSITIVE = "lower_bound_positive"
UNKNOWN_LABEL = "unknown_label"
BACKTEST = "backtest"  # a signal of a backtest run, which has no evidence of its own


def band_of(score: object) -> str | None:
    """The band of a setup score in [0.60, 1.00]; None for any other value (no bucket)."""
    if isinstance(score, bool) or not isinstance(score, (Decimal, int, float)):
        return None
    value = exact(score, floats=True)
    if value is None:
        return None
    for name, low, high in BANDS:
        if low <= value < high or (high == BANDS[-1][2] and value == high):
            return name
    return None


@dataclass(frozen=True)
class BucketStats:
    """The trades of one bucket: their count, mean R, the 90% interval of the mean (normal,
    `Z_90`; None below two trades) and the share of trades above 0 R."""

    trades: int
    mean_r: Decimal
    ci90_low: Decimal | None
    ci90_high: Decimal | None
    win_rate: Decimal


def bucket_stats(r_multiples: Sequence[Decimal]) -> BucketStats | None:
    """The statistics of a bucket's R multiples; None for an empty bucket."""
    count = len(r_multiples)
    if count == 0:
        return None
    mean = sum(r_multiples, Decimal(0)) / count
    wins = Decimal(sum(1 for r in r_multiples if r > 0)) / count
    if count < 2:
        return BucketStats(count, mean.quantize(_PLACES), None, None, wins.quantize(_PLACES))
    variance = sum(((r - mean) ** 2 for r in r_multiples), Decimal(0)) / (count - 1)
    half = Z_90 * (variance / count).sqrt()
    return BucketStats(
        count,
        mean.quantize(_PLACES),
        (mean - half).quantize(_PLACES),
        (mean + half).quantize(_PLACES),
        wins.quantize(_PLACES),
    )


def buckets(
    trades: Iterable[tuple[str, object, Decimal]],
) -> dict[tuple[str, str], BucketStats]:
    """The buckets of a run's closed trades, each given as (playbook, score, R multiple); a trade
    without a score in a band is in no bucket."""
    grouped: dict[tuple[str, str], list[Decimal]] = {}
    for playbook, score, r in trades:
        band = band_of(score)
        if band is not None:
            grouped.setdefault((playbook, band), []).append(r)
    return {
        key: stats for key, rs in sorted(grouped.items()) if (stats := bucket_stats(rs)) is not None
    }


def label_of(stats: BucketStats | None) -> tuple[str, str]:
    """The label of a bucket and its reason. No bucket, fewer than `MIN_TRADES` trades or a mean
    at or below 0 R is low; a positive 90% lower bound is high; the rest is medium."""
    if stats is None:
        return LOW, NO_BUCKET
    if stats.trades < MIN_TRADES:
        return LOW, FEW_TRADES
    if stats.mean_r <= 0:
        return LOW, MEAN_NOT_POSITIVE
    if stats.ci90_low is not None and stats.ci90_low > 0:
        return HIGH, LOWER_BOUND_POSITIVE
    return MEDIUM, LOWER_BOUND_NOT_POSITIVE


@dataclass(frozen=True)
class Confidence:
    """A signal's label and its basis, as `hat_signal` stores them."""

    label: str
    basis: Mapping[str, Any]

    @classmethod
    def low(cls, reason: str, **facts: str) -> Confidence:
        return cls(LOW, {"reason": reason, **facts})


def confidence_of(
    stats: BucketStats | None, *, run_id: str, playbook: str, band: str, score: Decimal
) -> Confidence:
    """The label of a signal whose bucket in the evidence run `run_id` has `stats`."""
    label, reason = label_of(stats)
    basis: dict[str, Any] = {
        "reason": reason,
        "run_id": run_id,
        "playbook": playbook,
        "band": band,
        "score": str(score),
    }
    if stats is not None:
        basis.update(
            trades=stats.trades,
            mean_r=str(stats.mean_r),
            ci90_low=None if stats.ci90_low is None else str(stats.ci90_low),
            ci90_high=None if stats.ci90_high is None else str(stats.ci90_high),
            win_rate=str(stats.win_rate),
        )
    return Confidence(label, basis)


BACKTEST_CONFIDENCE = Confidence.low(BACKTEST)


def stored_basis(raw: object) -> dict[str, Any]:
    """A stored `confidence_basis` as read back: anything but an object reads as none."""
    return dict(raw) if isinstance(raw, dict) else {}

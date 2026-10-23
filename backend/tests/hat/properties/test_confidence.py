"""PBT for the confidence label of a signal (D132): the label rule, the
setup-score bands, the bucket statistics a completed run stores, the label a signal is created
with and the line the owner reads on its card."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.hat.backtest.report import TradeRow
from app.hat.engine import confidence
from app.hat.engine.confidence import (
    BANDS,
    HIGH,
    LOW,
    MEDIUM,
    MIN_TRADES,
    BucketStats,
    band_of,
    bucket_stats,
    label_of,
)
from app.hat.risk.policy import Z_90
from app.hat.telegram import render

RANK = {LOW: 0, MEDIUM: 1, HIGH: 2}
R = st.decimals(min_value=-5, max_value=10, places=3, allow_nan=False, allow_infinity=False)
STATS = st.builds(
    BucketStats,
    trades=st.integers(1, 200),
    mean_r=R,
    ci90_low=st.one_of(st.none(), R),
    ci90_high=st.one_of(st.none(), R),
    win_rate=st.decimals(0, 1, places=3),
)
SCORE = st.decimals(min_value="0.4", max_value="1.2", places=4)


# --- the rule -----------------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(st.one_of(st.none(), STATS))
@example(None)
@example(BucketStats(MIN_TRADES - 1, Decimal(1), Decimal("0.5"), Decimal(2), Decimal(1)))
@example(BucketStats(MIN_TRADES, Decimal(0), Decimal("0.1"), Decimal(1), Decimal(1)))
@example(BucketStats(MIN_TRADES, Decimal("0.1"), Decimal(0), Decimal(1), Decimal(1)))
@example(BucketStats(MIN_TRADES, Decimal("0.1"), Decimal("0.0001"), Decimal(1), Decimal(1)))
@example(BucketStats(MIN_TRADES, Decimal("0.1"), None, None, Decimal(1)))
def test_label_rule(stats: BucketStats | None) -> None:
    """D132: no bucket, fewer than 30 trades or a mean at or below 0 R is low; otherwise a 90%
    lower bound above 0 is high and anything else medium. Harm: thin or losing evidence reads
    as high and auto places it without the owner."""
    label, reason = label_of(stats)
    if stats is None or stats.trades < MIN_TRADES or stats.mean_r <= 0:
        assert label == LOW
    elif stats.ci90_low is not None and stats.ci90_low > 0:
        assert label == HIGH
    else:
        assert label == MEDIUM
    assert reason


@settings(max_examples=300, deadline=None)
@given(STATS, st.integers(0, 100), R.filter(lambda r: r >= 0), R.filter(lambda r: r >= 0))
@example(
    BucketStats(MIN_TRADES - 1, Decimal("0.5"), Decimal("0.1"), Decimal(1), Decimal(1)),
    1,
    Decimal(0),
    Decimal(0),
)
def test_label_monotone(stats: BucketStats, more: int, mean_up: Decimal, low_up: Decimal) -> None:
    """D132: better evidence (more trades, a higher mean, a higher lower bound) never gives a
    lower label. Harm: adding winning trades to the backtest takes a setup off auto."""
    better = BucketStats(
        stats.trades + more,
        stats.mean_r + mean_up,
        None if stats.ci90_low is None else stats.ci90_low + low_up,
        stats.ci90_high,
        stats.win_rate,
    )
    assert RANK[label_of(better)[0]] >= RANK[label_of(stats)[0]]


@settings(max_examples=300, deadline=None)
@given(st.one_of(SCORE, st.floats(allow_nan=True, allow_infinity=True), st.none(), st.text()))
@example(Decimal("0.60"))
@example(Decimal("0.5999"))
@example(Decimal("0.75"))
@example(Decimal("0.90"))
@example(Decimal("1.00"))
@example(Decimal("1.0001"))
@example(True)
def test_band_of(score: object) -> None:
    """D132: the three bands split [0.60, 1.00] with no gap or overlap; any other score, or a
    value that is not a number, is in no band. Harm: a signal is compared with trades of
    another score band, or an unscored one gets a bucket."""
    band = band_of(score)
    number = isinstance(score, Decimal | float) and not isinstance(score, bool)
    value = Decimal(str(score)) if number and Decimal(str(score)).is_finite() else None
    if value is None or not Decimal("0.60") <= value <= Decimal("1.00"):
        assert band is None
        return
    inside = [
        name for name, low, high in BANDS if low <= value < high or (value == high == Decimal(1))
    ]
    assert inside == [band]


# --- the bucket statistics ----------------------------------------------------------------------


@settings(max_examples=200, deadline=None)
@given(st.lists(R, min_size=1, max_size=80), st.randoms())
def test_bucket_stats(rs: list[Decimal], rnd: object) -> None:
    """D132: a bucket counts its trades, its mean R is their mean, its 90% interval is the mean
    plus or minus Z_90 standard errors (none below two trades), its win rate the share above
    0 R, and the order of the trades changes nothing. Harm: the label is read from numbers the
    trades do not have."""
    stats = bucket_stats(rs)
    assert stats is not None
    assert stats.trades == len(rs)
    mean = sum(rs, Decimal(0)) / len(rs)
    assert abs(stats.mean_r - mean) <= Decimal("0.000001")
    assert stats.win_rate == (Decimal(sum(r > 0 for r in rs)) / len(rs)).quantize(
        Decimal("0.000001")
    )
    if len(rs) < 2:
        assert stats.ci90_low is stats.ci90_high is None
    else:
        assert stats.ci90_low is not None
        assert stats.ci90_high is not None
        sd = (sum(((r - mean) ** 2 for r in rs), Decimal(0)) / (len(rs) - 1)).sqrt()
        half = Z_90 * sd / Decimal(len(rs)).sqrt()
        assert abs(stats.ci90_low - (mean - half)) <= Decimal("0.000002")
        assert abs(stats.ci90_high - (mean + half)) <= Decimal("0.000002")
    shuffled = list(rs)
    rnd.shuffle(shuffled)  # type: ignore[attr-defined]
    assert bucket_stats(shuffled) == stats
    assert bucket_stats([]) is None


def _trade(n: int, playbook: str, score: Decimal | None, r: Decimal) -> TradeRow:
    day = date(2024, 1, 1)
    return TradeRow(
        entry_date=day,
        exit_date=day,
        playbook=playbook,
        instrument_id=str(uuid.UUID(int=n)),
        qty=1,
        entry_price=Decimal(100),
        initial_stop=Decimal(95),
        exit_reason="stop",
        realized_pnl=r * 5,
        fees=Decimal(0),
        r_multiple=r,
        mfe_r=Decimal(0),
        mae_r=Decimal(0),
        holding_days=1,
        strategy_version_id=uuid.UUID(int=1),
        risk_policy_version_id=uuid.UUID(int=2),
        score=score,
    )


# --- the label of a signal ----------------------------------------------------------------------

EVIDENCE = st.sampled_from(("none", "no_bucket", HIGH, MEDIUM, LOW))


def test_backtest_label_is_fixed() -> None:
    """A backtest's own signals carry the fixed `backtest` basis, labelled low: a run is never
    its own evidence."""
    assert confidence.BACKTEST_CONFIDENCE.label == LOW
    assert confidence.BACKTEST_CONFIDENCE.basis == {"reason": confidence.BACKTEST}


# --- what the owner reads -----------------------------------------------------------------------

BASIS = st.fixed_dictionaries(
    {"reason": st.text(max_size=20)},
    optional={
        "trades": st.one_of(st.integers(0, 500), st.text(max_size=5), st.booleans()),
        "mean_r": st.one_of(R.map(str), st.text(max_size=8), st.none()),
        "ci90_low": st.one_of(R.map(str), st.none(), st.just("NaN")),
        "ci90_high": st.one_of(R.map(str), st.none()),
    },
)


@settings(max_examples=300, deadline=None)
@given(st.one_of(st.sampled_from((HIGH, MEDIUM, LOW)), st.none(), st.text(max_size=8)), BASIS)
@example(HIGH, {"reason": "lower_bound_positive", "trades": 142, "mean_r": "0.41"})
@example(None, {})
def test_card_shows_label(label: str | None, basis: dict[str, object]) -> None:
    """D132, the approval card and the shadow message carry one confidence line: the
    stored label (anything else reads LOW), and the bucket's trades and mean R when the basis
    has them, else "no backtest evidence" with a reason that is a plain token. Harm: the owner
    approves a trade without seeing how thin its evidence is, or free text reaches the chat."""
    line = render.confidence_line(label, basis)
    shown = label.upper() if label in (HIGH, MEDIUM, LOW) else "LOW"
    assert line.startswith(f"Confidence: {shown} - ")
    trades_ = basis.get("trades")
    has_stats = isinstance(trades_, int) and not isinstance(trades_, bool)
    try:
        has_stats = has_stats and Decimal(str(basis.get("mean_r"))).is_finite()
    except ArithmeticError:
        has_stats = False
    if has_stats:
        assert f"backtest {trades_} similar trades" in line
    else:
        assert "no backtest evidence" in line
        reason = line.rsplit("(", 1)[1].rstrip(")")
        assert reason == "?" or reason == basis.get("reason", "no_label")
    card = render.CardView(
        signal_id=uuid.uuid4(),
        symbol="ABC",
        playbook="pb",
        entry=Decimal(100),
        stop=Decimal(95),
        qty=1,
        risk=Decimal(5),
        confidence=label,
        basis=basis,
    )
    assert line in render.card_text(card).splitlines()
    assert line in render.shadow_text(card).splitlines()

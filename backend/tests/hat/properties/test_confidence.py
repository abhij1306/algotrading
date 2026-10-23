"""PBT for the confidence label of a signal (D132, HAT-EXEC-015, PR 71): the label rule, the
setup-score bands, the bucket statistics a completed run stores, the label a signal is created
with and the line the owner reads on its card."""

from __future__ import annotations

import io
import tempfile
import uuid
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
from hat_engine_support import (
    AT,
    LABEL_STATS,
    TRADE_DAY,
    request_for,
    seed_engine_world,
    seed_evidence,
)
from hat_support import memory_engine
from hypothesis import example, given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.hat.backtest import runs
from app.hat.engine import confidence, trades
from app.hat.engine.backtest import BacktestResult, TradeRow
from app.hat.engine.confidence import (
    BANDS,
    HIGH,
    LOW,
    MEDIUM,
    MIN_TRADES,
    BucketStats,
    band_of,
    bucket_stats,
    buckets,
    label_of,
)
from app.hat.engine.scan import EP_PLAYBOOK
from app.hat.models import HatBacktestBucket, HatEvent, HatSignal
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


TRADE = st.tuples(
    st.sampled_from(("pb", "other")), st.one_of(st.none(), SCORE), R.map(lambda r: r / 2)
)


@settings(max_examples=40, deadline=None)
@given(st.lists(TRADE, max_size=60))
def test_buckets_match_trades(rows: list[tuple[str, Decimal | None, Decimal]]) -> None:
    """D132: the buckets a completed run stores are exactly those recomputed from its
    `trades.parquet`, one per (playbook, band) with a trade and none for an unscored trade or
    a score outside the bands. Harm: the label is read from a bucket the run's trades do not
    support."""
    engine = memory_engine()
    with Session(engine) as session, tempfile.TemporaryDirectory() as tmp:
        world = seed_engine_world(session)
        run = runs.start_run(
            session,
            strategy_version_id=world.version_id,
            risk_policy_version_id=world.policy_version_id,
            universe_id=world.universe_id,
            start=date(2020, 1, 1),
            end=date(2025, 12, 31),
            data_snapshot={},
        )
        trade_rows = tuple(_trade(n, *row) for n, row in enumerate(rows))
        metrics = {"returns": {"sr": 0.1, "t": 60, "skew": 0.0, "kurtosis": 3.0}}
        result = BacktestResult(trade_rows, (), (), (), (), {}, metrics)
        runs.complete_run(session, run, result, root=Path(tmp))
        stored = {
            (b.playbook, b.band): BucketStats(
                b.trades, b.mean_r, b.ci90_low, b.ci90_high, b.win_rate
            )
            for b in session.scalars(
                select(HatBacktestBucket).where(HatBacktestBucket.run_id == run.id)
            )
        }
        table = pq.read_table(io.BytesIO(runs.trades_path(Path(tmp), run.id).read_bytes()))
        reread = buckets(
            (p, s, Decimal(str(r)))
            for p, s, r in zip(
                table["playbook"].to_pylist(),
                table["score"].to_pylist(),
                table["r_multiple"].to_pylist(),
                strict=True,
            )
        )
    assert stored == reread
    scored = {(p, band_of(s)) for p, s, _ in rows if band_of(s) is not None}
    assert set(stored) == scored


# --- the label of a signal ----------------------------------------------------------------------

EVIDENCE = st.sampled_from(("none", "no_bucket", HIGH, MEDIUM, LOW))


@settings(max_examples=60, deadline=None)
@given(
    evidence=EVIDENCE,
    playbook=st.sampled_from(("pb", EP_PLAYBOOK)),
    gaps=st.sampled_from(([], ["episodic_pivot_not_backtested"], "unreadable")),
    score=SCORE,
    same_day=st.booleans(),
    late=st.sampled_from(("no", "after", "unreadable")),
)
@example(evidence=HIGH, playbook="pb", gaps=[], score=Decimal("0.8"), same_day=True, late="no")
@example(evidence=LOW, playbook="pb", gaps=[], score=Decimal("0.8"), same_day=True, late="after")
@example(
    evidence=LOW, playbook="pb", gaps=[], score=Decimal("0.8"), same_day=True, late="unreadable"
)
@example(
    evidence=HIGH,
    playbook=EP_PLAYBOOK,
    gaps=["episodic_pivot_not_backtested"],
    score=Decimal("0.8"),
    same_day=True,
    late="no",
)
@example(
    evidence=HIGH, playbook="pb", gaps="unreadable", score=Decimal("0.8"), same_day=True, late="no"
)
@example(evidence=HIGH, playbook="pb", gaps=[], score=Decimal("0.5"), same_day=True, late="no")
@example(evidence=HIGH, playbook="pb", gaps=[], score=Decimal("0.8"), same_day=False, late="no")
def test_signal_label_from_evidence(
    evidence: str,
    playbook: str,
    gaps: object,
    score: Decimal,
    same_day: bool,
    late: str,
) -> None:
    """D132, HAT-EXEC-015: a signal is created with the label of its bucket (playbook, the band
    of its watchlist score) in the bot's evidence run, with that bucket as its basis, and the
    same label on `signal.triggered`. No run, a playbook the run's known gaps name (EP until
    PR 70) or an unreadable gap list, or no watchlist score in a band gives low. A run completed
    after the signal triggered, or at no readable time (`late`, here labelled high), is never
    its evidence. Harm: a
    signal without evidence is labelled high and auto places it."""
    engine = memory_engine()
    with Session(engine) as session:
        world = seed_engine_world(session)
        band = band_of(score)
        stats = LABEL_STATS.get(evidence)
        if evidence != "none":
            seed_evidence(
                session,
                world,
                stats,
                playbook=playbook,
                score=score,
                band=band or "0.75-0.90",
                known_gaps=gaps,
                trade_date=TRADE_DAY if same_day else date(2020, 1, 2),
            )
        if late != "no":
            seed_evidence(
                session,
                world,
                LABEL_STATS[HIGH],
                playbook=playbook,
                score=score,
                band=band or "0.75-0.90",
                known_gaps=gaps,
                trade_date=TRADE_DAY if same_day else date(2020, 1, 2),
                completed_at=AT + timedelta(minutes=1) if late == "after" else "not a time",
            )
        outcome = trades.open_entry(session, request_for(world, world.instruments[0], playbook))
        signal = session.get_one(HatSignal, outcome.signal_id)
        payload = session.scalar(
            select(HatEvent.payload).where(
                HatEvent.aggregate_id == signal.id, HatEvent.event_type == "signal.triggered"
            )
        )
    gapped = gaps == "unreadable" or (playbook == EP_PLAYBOOK and gaps != [])
    if evidence == "none" or gapped or band is None or not same_day:
        expected = LOW
    else:
        expected = label_of(stats)[0]
    assert signal.confidence == expected
    assert payload["confidence"] == expected
    basis = signal.confidence_basis
    assert basis["reason"]
    if expected != LOW or basis.get("trades") is not None:
        assert stats is not None
        assert basis["band"] == band
        assert basis["trades"] == stats.trades
        assert Decimal(basis["mean_r"]) == stats.mean_r


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
    """D132, HAT-TG-023: the approval card and the shadow message carry one confidence line: the
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

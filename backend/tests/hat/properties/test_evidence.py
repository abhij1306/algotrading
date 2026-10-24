"""`evidence.RunMetrics`, the one reader of the run metrics the gates read (A2, E2): it reads back
every fact `report.compute_metrics` writes, through the JSON a run is stored as, and reads a fact
missing or malformed as absent (fail closed). No run, market or database is built."""

from __future__ import annotations

import dataclasses
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import hat_backtest_support as s
from hat_trigger_support import S1, S2, seed_config

from app.hat.backtest.evidence import (
    RunMetrics,
    history_holds,
    unbacktested,
    unbacktested_trades,
)
from app.hat.backtest.report import TradeRow, compute_metrics
from app.hat.backtest.spec import KNOWN_GAPS, sleeve_weight

D = Decimal
S2_BOOKS = tuple(seed_config(S2).playbooks)  # breakout, episodic_pivot
BREAKOUT = S2_BOOKS[:1]
# a pre-open playbook under another name: named by what it uses, not by its name
GAP_AND_GO = S2_BOOKS[1].model_copy(update={"name": "gap_and_go"})


def _trade(entry: date, exit_: date, r: str) -> TradeRow:
    return TradeRow(
        entry_date=entry,
        exit_date=exit_,
        playbook="pb",
        instrument_id=s.instrument(1),
        qty=10,
        entry_price=D(100),
        initial_stop=D(95),
        exit_reason="hard_stop",
        realized_pnl=D(r) * 50,
        fees=D(1),
        r_multiple=D(r),
        mfe_r=D(2),
        mae_r=D(-1),
        holding_days=2,
        strategy_version_id=uuid.uuid4(),
        risk_policy_version_id=uuid.uuid4(),
    )


def test_run_metrics_reads_what_compute_metrics_writes() -> None:
    days = s.sessions(8)
    spec = s.spec(s.config(), days, start=0)
    equity = [(d, D(1_000_000) + n * 1_000 - (n % 3) * 700) for n, d in enumerate(days)]
    trades = [_trade(days[1], days[3], "1.5"), _trade(days[4], days[6], "-1")]
    written = compute_metrics(
        trades,
        equity,
        spec,
        [D(0)] * len(days),
        signals=(),
        skips={},
        ambiguous_days=3,
        lower_circuit_days=0,
    )
    at = datetime(2026, 10, 23, 12, 30, tzinfo=UTC)
    pbo = {"prob": "0.25", "runs": 2, "variants": 2, "sessions": 8, "blocks": 2}
    stored = json.loads(json.dumps({**written, "pbo": pbo, "completed_at": at.isoformat()}))
    read = RunMetrics.from_json(stored)
    assert read.trades == 2
    assert read.expectancy_r == D("0.25")
    assert (read.first_session, read.last_session) == (days[0], days[-1])
    assert read.held_sessions == written["held_sessions"]
    assert read.held_sessions > 0
    assert read.ambiguous_days == 3
    assert read.known_gaps == KNOWN_GAPS
    assert read.sleeve_weight == sleeve_weight(spec)
    assert read.regime_mult == "1"
    assert read.cooldown_sessions == spec.policy.policy.cooldown_sessions
    assert read.for_promotion is False
    assert read.returns is not None
    assert read.returns.t == len(days)
    assert read.initial_equity == spec.initial_equity
    assert read.pbo == D("0.25")
    assert read.completed_at == at


def test_run_metrics_fail_closed() -> None:
    assert RunMetrics.from_json(None) == RunMetrics()
    assert RunMetrics.from_json({"error": "backtest_error"}) == RunMetrics()  # a failed run
    garbled = {
        "trades": True,
        "expectancy_r": 0.25,  # a float is not the stored text
        "first_session": "2025-13-01",
        "held_sessions": "12",
        "known_gaps": "episodic_pivot_not_backtested",
        "sleeve_weight": "NaN",
        "regime_mult": 1,
        "cooldown_sessions": None,
        "for_promotion": "true",
        "returns": {"sr": 0.1},
        "pbo": {"prob": None, "reason": "pbo_too_few_variants"},
        "completed_at": "yesterday",
    }
    assert RunMetrics.from_json(garbled) == RunMetrics()


def test_unbacktested() -> None:
    ep = frozenset({"episodic_pivot"})
    assert unbacktested(None, S2_BOOKS) == ep
    assert unbacktested(RunMetrics.from_json({}), S2_BOOKS) == ep  # no gap list
    assert unbacktested(RunMetrics.from_json({"known_gaps": "garbled"}), S2_BOOKS) == ep
    assert unbacktested(RunMetrics.from_json({"known_gaps": [KNOWN_GAPS[1]]}), S2_BOOKS) == ep
    assert unbacktested(RunMetrics.from_json({"known_gaps": []}), S2_BOOKS) == frozenset()  # PR 70
    assert unbacktested(None, (*BREAKOUT, GAP_AND_GO)) == frozenset({"gap_and_go"})
    assert unbacktested(None, seed_config(S1).playbooks) == frozenset()


def test_unbacktested_trades_by_version() -> None:
    """A name pre-open in one version leaves out only that version's trades of it."""
    v1, v2 = uuid.UUID(int=1), uuid.UUID(int=2)
    nightly = BREAKOUT[0].model_copy(update={"name": "gap_and_go"})
    assert unbacktested_trades(None, {v1: (nightly,), v2: (GAP_AND_GO,)}) == {(v2, "gap_and_go")}
    assert unbacktested_trades(RunMetrics.from_json({"known_gaps": []}), {v2: S2_BOOKS}) == set()


def test_history_holds() -> None:
    """The risk-increase gate's run passes the lifecycle's span, gap and ambiguity conditions
    (QA8): five years of sessions, no known gap naming an enabled playbook, ambiguous days at
    most 10% of the held sessions; an absent fact fails."""
    good = RunMetrics(
        first_session=date(2018, 1, 1),
        last_session=date(2023, 1, 1),
        held_sessions=100,
        ambiguous_days=10,
        known_gaps=KNOWN_GAPS,
    )
    breakout = BREAKOUT
    assert history_holds(good, breakout)
    assert not history_holds(dataclasses.replace(good, last_session=date(2022, 6, 1)), breakout)
    assert not history_holds(good, S2_BOOKS)
    assert not history_holds(good, (*BREAKOUT, GAP_AND_GO))
    assert not history_holds(dataclasses.replace(good, known_gaps=None), breakout)
    assert not history_holds(good, ())
    assert not history_holds(dataclasses.replace(good, ambiguous_days=11), breakout)
    assert not history_holds(dataclasses.replace(good, ambiguous_days=None), breakout)

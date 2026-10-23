"""`evidence.RunMetrics`, the one reader of the run metrics the gates read (A2, E2): it reads back
every fact `report.compute_metrics` writes, through the JSON a run is stored as, and reads a fact
missing or malformed as absent (fail closed). No run, market or database is built."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import hat_backtest_support as s

from app.hat.backtest.evidence import RunMetrics, unbacktested
from app.hat.backtest.report import TradeRow, compute_metrics
from app.hat.backtest.spec import KNOWN_GAPS, sleeve_weight
from app.hat.engine.scan import EP_PLAYBOOK

D = Decimal


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
    stored = json.loads(json.dumps({**written, "completed_at": at.isoformat()}))
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
        "completed_at": "yesterday",
    }
    assert RunMetrics.from_json(garbled) == RunMetrics()


def test_unbacktested() -> None:
    ep = frozenset({EP_PLAYBOOK})
    assert unbacktested(None) == ep
    assert unbacktested(RunMetrics.from_json({})) == ep  # no gap list
    assert unbacktested(RunMetrics.from_json({"known_gaps": "garbled"})) == ep
    assert unbacktested(RunMetrics.from_json({"known_gaps": [KNOWN_GAPS[1]]})) == ep
    assert unbacktested(RunMetrics.from_json({"known_gaps": []})) == frozenset()  # PR 70

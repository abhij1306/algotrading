"""CONF checks of the backtest (09; HAT-BT-010, 032, 042, 051, 070; HAT-VER-041)."""

import ast
import json
import math
import re
from pathlib import Path

import hat_backtest_support as s
from hat_conf_repo import APP_ROOT, REPO_ROOT, imports_of

from app.hat.backtest import runs
from app.hat.backtest.dsr import ReturnStats, deflated_sharpe, expected_max_sharpe
from app.hat.engine.backtest import KNOWN_GAPS, TRADE_SCHEMA
from app.hat.models import HatBacktestRun

HAT = APP_ROOT / "hat"
BACKTEST = HAT / "engine" / "backtest.py"
PROPERTIES = Path(__file__).resolve().parents[1] / "properties"
GOLDEN = Path(__file__).resolve().parents[1] / "fixtures" / "backtest_golden.json"

# The engine functions the day loop must reach through, and the logic it must never import
# directly: scan, scoring, criteria, sizing, caps and the risk decision live behind them.
SHARED = {
    "app.hat.engine.watchlist.scan_and_publish": "scan",
    "app.hat.engine.trades.open_entry": "risk decision and sizing",
    "app.hat.engine.trades.evaluate_position": "exit stack",
    "app.hat.engine.trades.close_position": "exit fills",
    "app.hat.engine.trades.expire_watchlist": "watchlist expiry",
    "app.hat.engine.stops.plan_stop": "initial stop",
    "app.hat.engine.stops.check_width": "stop width",
    "app.hat.engine.trigger.evaluate_entry_filters": "entry filters",
}
PRIVATE = (
    "app.hat.engine.scan.scan_bot",
    "app.hat.engine.criteria",
    "app.hat.engine.strategy.scoring",
    "app.hat.risk.sizing",
    "app.hat.risk.caps",
    "app.hat.risk.service",
)
LOGIC_MODULES = ("scan", "exits", "stops", "watchlist", "trades")


def _public_functions(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        n.name for n in tree.body if isinstance(n, ast.FunctionDef) and not n.name.startswith("_")
    }


def _all_functions(path: Path) -> set[str]:
    return {
        n.name
        for n in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(n, ast.FunctionDef)
    }


def test_backtest_shares_engine_imports() -> None:
    """The day loop reaches scan, risk, stop and exit logic only through the engine functions
    live trading calls: it imports them, never the scan, criteria, scoring, sizing, caps or
    decision modules behind them, defines no function of the same name, and passes no other
    decision function to `open_entry` (HAT-BT-010)."""
    imported = imports_of(BACKTEST)
    for name in SHARED:
        module, _, function = name.rpartition(".")
        assert name in imported or module in imported, name
        assert function in BACKTEST.read_text(encoding="utf-8"), name
    assert not [m for m in imported if any(m == p or m.startswith(p + ".") for p in PRIVATE)]
    mine = _all_functions(BACKTEST)
    for module in LOGIC_MODULES:
        assert not mine & _public_functions(HAT / "engine" / f"{module}.py"), module
    # `open_entry` decides with `evaluate_signal` unless a caller passes another `decide`.
    tree = ast.parse(BACKTEST.read_text(encoding="utf-8"))
    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "open_entry"
    ]
    assert calls
    assert all("decide" not in {k.arg for k in c.keywords} for c in calls)
    assert "app.hat.risk.service.evaluate_signal" in imports_of(HAT / "engine" / "trades.py")
    # the cost and fill modules choose prices only: no engine logic behind them either
    for path in sorted((HAT / "backtest").glob("*.py")):
        if path.name in ("runs.py", "market.py"):
            continue  # they run and record the loop; they hold no decision
        assert not [m for m in imports_of(path) if m.startswith("app.hat.engine")], path.name


def test_backtest_run_columns() -> None:
    """`hat_backtest_run` stores the pinned versions, period, data snapshot, status, metrics and
    the non-null family key; a run's trades go to `backtests/run_id=<id>/trades.parquet` with
    the trade columns of 09 §3 (HAT-BT-032)."""
    columns = HatBacktestRun.__table__.columns
    for name in (
        "strategy_version_id",
        "risk_policy_version_id",
        "period_start",
        "period_end",
        "data_snapshot",
        "status",
        "metrics",
        "family_key",
    ):
        assert name in columns, name
        assert not columns[name].nullable, name
    run_id = s.VERSION
    assert runs.trades_path(Path("/root"), run_id) == Path(
        f"/root/backtests/run_id={run_id}/trades.parquet"
    )
    assert {runs.RUNNING, runs.COMPLETED, runs.FAILED} == {"running", "completed", "failed"}
    assert TRADE_SCHEMA.names[:4] == ["entry_date", "exit_date", "playbook", "instrument_id"]
    assert {"r_multiple", "realized_pnl", "fees", "strategy_version_id"} <= set(TRADE_SCHEMA.names)


# --- HAT-BT-042: the published example --------------------------------------------------------


def _phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


def _phi_inv(p: float) -> float:
    lo, hi = -40.0, 40.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _phi(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def test_dsr_published_example() -> None:
    """Bailey & López de Prado (2014), §5: an annualised SR of 2.5 over 5 years of daily returns
    (T = 1250), skew -3, kurtosis 10, N = 100 trials with an annualised Sharpe variance of 0.5
    give SR0 = 0.1132 and DSR = 0.9004 at the paper's printed 4 places. To 1e-6 the code matches
    the same formula computed here independently with `math.erf` and a bisection inverse."""
    sr, t, skew, kurt = 2.5 / math.sqrt(250), 1250, -3.0, 10.0
    trials, variance = 100, 0.5 / 250
    got = deflated_sharpe(ReturnStats(sr, t, skew, kurt), trials, variance)
    assert round(expected_max_sharpe(trials, variance), 4) == 0.1132
    assert round(got.sr0, 4) == 0.1132
    assert round(got.prob, 4) == 0.9004
    gamma = 0.5772156649015329
    sr0 = math.sqrt(variance) * (
        (1 - gamma) * _phi_inv(1 - 1 / trials) + gamma * _phi_inv(1 - 1 / (trials * math.e))
    )
    z = (sr - sr0) * math.sqrt(t - 1) / math.sqrt(1 - skew * sr + (kurt - 1) / 4 * sr**2)
    assert abs(got.sr0 - sr0) < 1e-6
    assert abs(got.prob - _phi(z)) < 1e-6
    assert got.reason is None


# --- HAT-BT-030: a DSR or PBO only for a recorded run (PR 66) ---------------------------------

SCORERS = ("deflated_sharpe", "family_deflated_sharpe", "pbo")


def test_dsr_scored_only_by_runs() -> None:
    """Under `app/hat` only `backtest/runs.py` imports the DSR and PBO scorers of
    `app.hat.backtest.dsr` (`deflated_sharpe`, `family_deflated_sharpe`, `pbo`), and it scores a
    run only through `run_dsr` and `family_pbo`, which read recorded runs; so no other module
    can score a result that never became a trial (G33). Harm: a variant tried outside the
    recorded runs reaches a gate as evidence without counting as a trial."""
    allowed = {APP_ROOT / "hat" / "backtest" / "runs.py", APP_ROOT / "hat" / "backtest" / "dsr.py"}
    offenders = sorted(
        str(path.relative_to(APP_ROOT))
        for path in (APP_ROOT / "hat").rglob("*.py")
        if path not in allowed
        and any(f"app.hat.backtest.dsr.{name}" in imports_of(path) for name in SCORERS)
    )
    assert offenders == []
    assert {f"app.hat.backtest.dsr.{name}" for name in ("family_deflated_sharpe", "pbo")} <= (
        imports_of(APP_ROOT / "hat" / "backtest" / "runs.py")
    )


# --- HAT-BT-070: known gaps ------------------------------------------------------------------


def test_known_gaps_listed() -> None:
    """Spec 09 §7's gap table lists exactly `KNOWN_GAPS`, in order, and the golden runs report
    that list (HAT-BT-070)."""
    spec = (REPO_ROOT / "docs" / "specs" / "hat" / "09-backtest.md").read_text(encoding="utf-8")
    section = spec[spec.index("#### HAT-BT-070") :]
    codes = re.findall(r"^\| `([a-z_]+)` \|", section, re.MULTILINE)
    assert tuple(codes) == KNOWN_GAPS
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert all(seed["metrics"]["known_gaps"] == list(KNOWN_GAPS) for seed in golden.values())


# --- HAT-BT-051: golden replay ----------------------------------------------------------------


def test_backtest_golden_replay() -> None:
    """The committed fixture market run through the two seeded strategies (S1, S2) reproduces
    the committed golden document exactly: metrics, trades, signal and watchlist counts. An
    intended change regenerates it (`python tests/hat/hat_backtest_support.py`) in the same
    commit (HAT-BT-051)."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert s.golden_document() == golden
    assert set(golden) == set(s.GOLDEN_SEEDS)
    assert any(doc["metrics"]["trades"] for doc in golden.values())


# --- HAT-VER-041 ------------------------------------------------------------------------------

MONEY_AREAS = ("sizing", "caps", "exposure", "allocator", "costs")
FLOAT_TOLERANCE = re.compile(r"pytest\.approx|math\.isclose|abs_tol|rel_tol")


def _parent_map(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _is_call(node: ast.AST, name: str) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == name
    )


def test_pbt_money_strategies_decimal() -> None:
    """In the sizing, caps, exposure, allocator and costs properties, money and weights are
    generated as Decimal and compared exactly: no float tolerance appears, and a float strategy
    appears only inside `st.one_of(...)` as one kind of invalid input among others (HAT-VER-041)."""
    for area in MONEY_AREAS:
        path = PROPERTIES / f"test_{area}.py"
        text = path.read_text(encoding="utf-8")
        assert not FLOAT_TOLERANCE.search(text), area
        tree = ast.parse(text)
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            if _is_call(node, "floats"):
                assert _is_call(parents.get(node), "one_of"), (area, node.lineno)

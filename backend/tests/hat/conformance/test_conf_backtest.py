"""CONF checks of the backtest: shared engine imports and DSR scoring."""

import ast
import math
import re
from pathlib import Path

from hat_conf_repo import APP_ROOT, imports_of

from app.hat.backtest.dsr import ReturnStats, deflated_sharpe, expected_max_sharpe

HAT = APP_ROOT / "hat"
BACKTEST = HAT / "backtest" / "loop.py"
# The day loop and the modules it is split into; they alone of `backtest/` read the engine,
# besides `runs.py`, `evidence.py` and `__main__.py`, which run, record and read the runs and
# hold no decision.
LOOP = tuple(
    HAT / "backtest" / f"{name}.py"
    for name in ("loop", "spec", "data", "ledger", "fillrules", "report")
)
RUNNERS = {"runs.py", "evidence.py", "__main__.py"}
PROPERTIES = Path(__file__).resolve().parents[1] / "properties"

# The engine functions the day loop must reach through, and the logic it must never import
# directly: scan, scoring, criteria, sizing, caps and the risk decision live behind them.
SHARED = {
    "app.hat.engine.watchlist.scan_and_publish": "scan",
    "app.hat.engine.entries.open_entry": "risk decision and sizing",
    "app.hat.engine.shadow.evaluate_position": "exit stack",
    "app.hat.engine.shadow.close_position": "exit fills",
    "app.hat.engine.shadow.expire_watchlist": "watchlist expiry",
    "app.hat.engine.stops.plan_stop": "initial stop",
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
LOGIC_MODULES = ("scan", "exits", "stops", "watchlist", "entries", "shadow")


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
    decision function to `open_entry`."""
    imported = imports_of(BACKTEST)
    loop_imports = {m for path in LOOP for m in imports_of(path)}
    for name in SHARED:
        module, _, function = name.rpartition(".")
        assert name in imported or module in imported, name
        assert function in BACKTEST.read_text(encoding="utf-8"), name
    assert not [m for m in loop_imports if any(m == p or m.startswith(p + ".") for p in PRIVATE)]
    mine = {f for path in LOOP for f in _all_functions(path)}
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
    assert "app.hat.risk.service.evaluate_signal" in imports_of(HAT / "engine" / "entries.py")
    # the cost and fill modules choose prices only: no engine logic behind them either
    for path in sorted((HAT / "backtest").glob("*.py")):
        if path in LOOP or path.name in RUNNERS:
            continue
        assert not [m for m in imports_of(path) if m.startswith("app.hat.engine")], path.name


# --- the published example --------------------------------------------------------


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


# --- a DSR or PBO only for a recorded run ---------------------------------

SCORERS = ("deflated_sharpe", "family_deflated_sharpe", "pbo")


def _imports_dsr_module(path: Path) -> bool:
    """Whether `path` binds the module `app.hat.backtest.dsr` itself (not names from it)."""
    package = ".".join(path.relative_to(APP_ROOT.parent).with_suffix("").parts[:-1])
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            if any(a.name == "app.hat.backtest.dsr" for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            parts = package.split(".")
            base = (
                ".".join(
                    parts[: len(parts) - node.level + 1] + ([node.module] if node.module else [])
                )
                if node.level
                else node.module or ""
            )
            if base == "app.hat.backtest" and any(a.name == "dsr" for a in node.names):
                return True
    return False


def test_dsr_scored_only_by_runs() -> None:
    """Under `app/hat` only `backtest/runs.py` and `backtest/evidence.py` import the DSR and PBO
    scorers of `app.hat.backtest.dsr` (`deflated_sharpe`, `family_deflated_sharpe`, `pbo`), and
    they score a run only through `run_dsr` and `family_pbo`, which read recorded runs; so no
    other module can score a result that never became a trial (G33). Harm: a variant tried
    outside the recorded runs reaches a gate as evidence without counting as a trial."""
    allowed = {
        APP_ROOT / "hat" / "backtest" / name for name in ("runs.py", "evidence.py", "dsr.py")
    }
    offenders = sorted(
        str(path.relative_to(APP_ROOT))
        for path in (APP_ROOT / "hat").rglob("*.py")
        if path not in allowed
        and any(f"app.hat.backtest.dsr.{name}" in imports_of(path) for name in SCORERS)
    )
    assert offenders == []
    # nor imports the module whole (`import app.hat.backtest.dsr`, `from ..backtest import
    # dsr`), through which it could call a scorer as an attribute
    whole = sorted(
        str(path.relative_to(APP_ROOT))
        for path in (APP_ROOT / "hat").rglob("*.py")
        if path not in allowed and _imports_dsr_module(path)
    )
    assert whole == []
    assert "app.hat.backtest.dsr.family_deflated_sharpe" in imports_of(
        APP_ROOT / "hat" / "backtest" / "evidence.py"
    )
    assert "app.hat.backtest.dsr.pbo" in imports_of(APP_ROOT / "hat" / "backtest" / "runs.py")


# --- known gaps ------------------------------------------------------------------


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
    appears only inside `st.one_of(...)` as one kind of invalid input among others."""
    for area in MONEY_AREAS:
        path = PROPERTIES / f"test_{area}.py"
        text = path.read_text(encoding="utf-8")
        assert not FLOAT_TOLERANCE.search(text), area
        tree = ast.parse(text)
        parents = _parent_map(tree)
        for node in ast.walk(tree):
            if _is_call(node, "floats"):
                assert _is_call(parents.get(node), "one_of"), (area, node.lineno)

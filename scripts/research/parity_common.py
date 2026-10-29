"""What the independent parity checks (`s1_parity.py`, `s2_ep_parity.py`) share: the engine's
result for a portfolio file over a window, run in memory as `bt_truncation` runs it and kept
in a cache file so the independent side can be rerun without the engine, and the market that
run read (`load_market`: the same stored bars, features and membership).

The cache is keyed by the portfolio text, the window and the engine source (a hash of every
`backend/app/hat` Python file), so an engine change reruns the engine. Cache files live under
`reports/parity/cache/` (gitignored). Hold-out windows (2025 on) are refused.
"""

from __future__ import annotations

import hashlib
import pickle
import sys
from collections.abc import Mapping, Sequence
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import text  # noqa: E402

from app.database import db_session_scope  # noqa: E402
from app.hat.backtest.data import MarketData, load_market  # noqa: E402
from app.hat.backtest.report import BacktestResult  # noqa: E402
from app.hat.backtest.runs import _configs  # noqa: E402
from app.hat.backtest.spec import REGIME_MULT_POLICY, RunSpec  # noqa: E402
from app.hat.data.store import DataStore  # noqa: E402
from app.hat.models import HatEvent  # noqa: E402
from app.hat.settings import operator_settings  # noqa: E402
from app.paths import PROJECT_ROOT  # noqa: E402

HOLD_OUT_START = date(2025, 1, 1)
REPORTS = PROJECT_ROOT / "reports" / "parity"
CACHE = REPORTS / "cache"
ENGINE_SOURCE = PROJECT_ROOT / "backend" / "app" / "hat"


def engine_digest(root: Path = ENGINE_SOURCE) -> str:
    """A hash of the engine's Python source: a change to any file reruns the engine."""
    h = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        h.update(path.relative_to(root).as_posix().encode())
        h.update(path.read_bytes())
    return h.hexdigest()[:16]


def cache_key(portfolio_text: str, start: date, end: date, engine: str) -> str:
    """The cache file stem of one engine run."""
    digest = hashlib.sha256(f"{portfolio_text}\n{start}\n{end}\n{engine}".encode()).hexdigest()
    return f"{start}_{end}_{digest[:16]}"


def _market(session: Any, store: DataStore, spec: RunSpec, target: Any) -> MarketData:  # noqa: ANN401
    data, _ = load_market(
        store,
        session,
        universe_version_id=target.universe_version_id,
        index=spec.config.regime.index,
        start=spec.start,
        end=spec.end,
        regime_inputs=spec.conventions.regime_mult == REGIME_MULT_POLICY,
        results=any(p.preopen for c in _configs(spec) for p in c.playbooks),
    )
    return data


def engine_and_market(
    doc: Mapping[str, Any], portfolio_text: str, start: date, end: date, *, fresh: bool = False
) -> tuple[RunSpec, MarketData, BacktestResult]:
    """The run spec, the market and the engine's result of the portfolio over `start..end`; the
    result from the cache unless `fresh` or the engine changed."""
    if end >= HOLD_OUT_START:
        raise SystemExit("refused: the window reaches the 2025 hold-out")
    import bt_truncation  # the in-memory run (`bt_truncation._run`)

    path = CACHE / f"{cache_key(portfolio_text, start, end, engine_digest())}.pkl"
    settings = operator_settings()
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
        if path.is_file() and not fresh:
            spec, target = bt_truncation._spec_of(session, doc, start, end)
            data = _market(session, store, spec, target)
            with path.open("rb") as f:
                result = pickle.load(f)  # noqa: S301 - our own cache file
            print(f"engine result from cache {path.name}", file=sys.stderr)
            return spec, data, result
        spec, data, result, took = bt_truncation._run(session, store, doc, start, end)
        print(f"engine run {start}..{end}: {took:.0f}s", file=sys.stderr)
    CACHE.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        pickle.dump(result, f)
    return spec, data, result


def multiset_diff(a: Sequence[Any], b: Sequence[Any]) -> tuple[list[Any], list[Any]]:
    """(in a and not b, in b and not a), with multiplicity, sorted."""
    from collections import Counter

    ca, cb = Counter(a), Counter(b)
    return sorted((ca - cb).elements()), sorted((cb - ca).elements())

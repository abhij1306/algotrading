"""What the independent parity checks (`s1_parity.py`, `s2_ep_parity.py`) share: the engine's
result for a portfolio file over a window, run in memory as `bt_truncation` runs it and kept
in a cache file so the independent side can be rerun without the engine, and the market that
run read (`load_market`: the same stored bars, features and membership).

A cache file is named by the portfolio text and the window and holds the result with three
digests, each checked on load, any mismatch rerunning the engine: the engine (`engine_digest`:
every file under `backend/app/hat`, cost models, holiday list and seeds included, and the
in-memory run script), the run spec (`spec_digest`: strategy config, policy version, fills,
conventions, regime table) and the market it read (`market_digest`: bars, features,
membership, results, sessions and the 5-minute files of the window). Cache files live under
`reports/parity/cache/` (gitignored). Hold-out windows (2025 on) are refused.
"""

from __future__ import annotations

import hashlib
import pickle
import re
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
RUN_SCRIPT = Path(__file__).resolve().parent / "bt_truncation.py"


def engine_digest(root: Path = ENGINE_SOURCE) -> str:
    """A hash of every engine file (source, cost models, holiday list, seeds) and of the
    in-memory run script: a change to any of them reruns the engine."""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts):
        h.update(path.relative_to(root).as_posix().encode())
        h.update(path.read_bytes())
    h.update(RUN_SCRIPT.read_bytes())
    return h.hexdigest()[:16]


def spec_digest(spec: RunSpec) -> str:
    """A hash of the resolved run spec: the strategy config, the policy version, the fills, the
    conventions, the regime table, the co-sleeves and the overlay."""
    regime = sorted((k, str(v)) for k, v in spec.regime_table.items())
    # a frozenset's repr follows the process's string hashing: its members sorted instead
    text_ = re.sub(
        r"frozenset\(\{([^{}]*)\}\)",
        lambda m: "frozenset({" + ", ".join(sorted(m.group(1).split(", "))) + "})",
        repr(spec),
    )
    return hashlib.sha256(f"{text_}\n{regime!r}".encode()).hexdigest()[:16]


def cache_key(portfolio_text: str, start: date, end: date) -> str:
    """The cache file stem of one engine run."""
    digest = hashlib.sha256(f"{portfolio_text}\n{start}\n{end}".encode()).hexdigest()
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

    path = CACHE / f"{cache_key(portfolio_text, start, end)}.pkl"
    settings = operator_settings()
    engine = engine_digest()
    with db_session_scope() as session:
        session.execute(text("SET TRANSACTION READ ONLY"), bind_arguments={"mapper": HatEvent})
        store = DataStore(settings.data_root, memory_limit=settings.duckdb_memory)
        spec, target = bt_truncation._spec_of(session, doc, start, end)
        resolved = spec_digest(spec)  # before the run: the spec as resolved, not as run
        if path.is_file() and not fresh:
            data = _market(session, store, spec, target)
            with path.open("rb") as f:
                kept = pickle.load(f)  # noqa: S301 - our own cache file
            now = {"engine": engine, "spec": resolved, "market": market_digest(data)}
            stale = [k for k in now if not isinstance(kept, dict) or kept.get(k) != now[k]]
            if not stale:
                print(f"engine result from cache {path.name}", file=sys.stderr)
                return spec, data, kept["result"]
            print(f"cached run stale ({', '.join(stale)}): rerunning", file=sys.stderr)
        spec, data, result, took = bt_truncation._run(session, store, doc, start, end)
        print(f"engine run {start}..{end}: {took:.0f}s", file=sys.stderr)
    CACHE.mkdir(parents=True, exist_ok=True)
    kept = {
        "engine": engine,
        "spec": resolved,
        "market": market_digest(data),
        "result": result,
    }
    with path.open("wb") as f:
        pickle.dump(kept, f)
    return spec, data, result


def market_digest(data: MarketData) -> str:
    """A hash of the loaded market the engine and the parity side read (bars, features,
    membership, results, sessions, and the size and time of each 5-minute file of the
    window): a cached result is used only on the same market."""
    h = hashlib.sha256()
    for frame in (data.bars, data.features):
        h.update(str(frame.shape).encode())
        h.update(frame.hash_rows().sum().to_bytes(8, "little", signed=False))
    h.update(
        repr(
            sorted((str(m.instrument_id), m.effective_from, m.effective_to) for m in data.members)
        ).encode()
    )
    h.update(repr(sorted((k, len(v)) for k, v in data.results.items())).encode())
    h.update(repr(list(data.days)).encode())
    if data.intraday is not None:  # read lazily by the run: the window's partition files
        for day in data.days:
            if (part := data.intraday.path(day)).is_file():
                st = part.stat()
                h.update(f"{day},{st.st_size},{st.st_mtime_ns}".encode())
    return h.hexdigest()[:16]


def multiset_diff(a: Sequence[Any], b: Sequence[Any]) -> tuple[list[Any], list[Any]]:
    """(in a and not b, in b and not a), with multiplicity, sorted."""
    from collections import Counter

    ca, cb = Counter(a), Counter(b)
    return sorted((ca - cb).elements()), sorted((cb - ca).elements())

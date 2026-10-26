"""The trial registry a run's deflated Sharpe counts its trials from
(`HAT_DATA_ROOT/backtests/trials.json`, docs/backtests/result-standard.md).

A trial is a distinct `resolved_hash` among the completed in-sample runs of one strategy: a run
whose resolved parameters hash like an earlier one's is a duplicate, not a new trial. The file
holds, per strategy, each hash with the first run that tried it and that run's per-session
Sharpe ratio (the deflated Sharpe's trial variance reads them). Every run reads it; a completed
in-sample run adds its hash under an exclusive lock (read, merge, replace in one rename). It is
backtest output, under `HAT_DATA_ROOT` and never in the database.
"""

from __future__ import annotations

import fcntl
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..data.store import atomic_replace

__all__ = ["Trial", "family", "read_registry", "record_trial", "registry_path"]

REGISTRY_VERSION = 1


def registry_path(root: Path) -> Path:
    return root / "backtests" / "trials.json"


@dataclass(frozen=True)
class Trial:
    run_id: str
    sr: float | None


def _parse(raw: object) -> dict[str, dict[str, Trial]]:
    """The registry's trials by strategy and hash; an entry that is not one is left out."""
    strategies = raw.get("strategies") if isinstance(raw, Mapping) else None
    out: dict[str, dict[str, Trial]] = {}
    for strategy, hashes in (strategies or {}).items():
        if not isinstance(hashes, Mapping):
            continue
        for digest, entry in hashes.items():
            if not isinstance(entry, Mapping) or not isinstance(entry.get("run_id"), str):
                continue
            sr = entry.get("sr")
            number = float(sr) if isinstance(sr, (int, float)) else None
            out.setdefault(str(strategy), {})[str(digest)] = Trial(entry["run_id"], number)
    return out


def read_registry(root: Path) -> dict[str, dict[str, Trial]]:
    """The registry (empty when there is no file yet); an unreadable file raises, so a run
    never scores its DSR on a count it could not read."""
    path = registry_path(root)
    if not path.is_file():
        return {}
    return _parse(json.loads(path.read_text(encoding="utf-8")))


def family(
    registry: Mapping[str, Mapping[str, Trial]],
    strategy: str,
    own: tuple[str, float | None] | None,
) -> tuple[int, list[float]]:
    """(the strategy's trial count, the Sharpe ratios of its trials) with `own` (a completed
    in-sample run's hash and Sharpe) counted once more unless its hash is already a trial."""
    trials = dict(registry.get(strategy, {}))
    if own is not None and own[0] not in trials:
        trials[own[0]] = Trial("", own[1])
    return len(trials), [t.sr for t in trials.values() if t.sr is not None]


def _document(registry: Mapping[str, Mapping[str, Trial]]) -> dict[str, Any]:
    return {
        "version": REGISTRY_VERSION,
        "strategies": {
            s: {h: {"run_id": t.run_id, "sr": t.sr} for h, t in sorted(hashes.items())}
            for s, hashes in sorted(registry.items())
        },
    }


def record_trial(root: Path, strategy: str, digest: str, run_id: str, sr: float | None) -> None:
    """Add a completed in-sample run's hash to its strategy (a known hash keeps its first run)."""
    path = registry_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(".trials.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry = read_registry(root)
        hashes = registry.setdefault(strategy, {})
        if digest in hashes:
            return
        hashes[digest] = Trial(run_id, sr)
        text = json.dumps(_document(registry), indent=1)
        atomic_replace(path, lambda tmp: tmp.write_text(text, encoding="utf-8"))

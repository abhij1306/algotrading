"""Where a backtest run folder lives and how its JSON files are read (`folder` writes them). Kept
apart from `folder` so the gates that read a run's evidence (`evidence`) import no result-writing
code."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

__all__ = ["JSON_DOCS", "is_run_id", "read_config", "read_doc", "read_run", "run_dir", "runs_dir"]

# The folder's JSON files beside `run.json` and `config.json`: the run audit (`run_audit`, a
# completed run only), the data audit it ran on (`data.audit`), the facts the gates read of a
# completed run (`evidence.evidence_doc`) and the money reconciled from the fills (`reconcile`,
# a completed run or one failed `backtest_ledger_mismatch`).
JSON_DOCS = ("audit", "data_audit", "evidence", "ledger")

_RUN_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def runs_dir(root: Path) -> Path:
    return root / "backtests" / "runs"


def is_run_id(text: str) -> bool:
    return bool(_RUN_ID.match(text))


def run_dir(root: Path, run_id: str) -> Path | None:
    """The run's folder; None for a run id that is not one (so no path leaves `runs/`)."""
    return runs_dir(root) / run_id if is_run_id(run_id) else None


def _json(path: Path) -> dict[str, Any] | None:
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def read_run(root: Path, run_id: str) -> dict[str, Any] | None:
    """The run's `run.json`; None when there is no such run or it cannot be read."""
    folder = run_dir(root, run_id)
    return None if folder is None else _json(folder / "run.json")


def read_config(root: Path, run_id: str) -> dict[str, Any] | None:
    folder = run_dir(root, run_id)
    return None if folder is None else _json(folder / "config.json")


def read_doc(root: Path, run_id: str, name: str) -> dict[str, Any] | None:
    """One of the run's `JSON_DOCS`; None without it."""
    folder = run_dir(root, run_id)
    if folder is None or name not in JSON_DOCS:
        return None
    return _json(folder / f"{name}.json")

"""CONF checks of the Parquet store: where it writes, which root the command lines hand it, and
that every file it produces has its dataset's partition and exact columns."""

import ast
import inspect
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from hat_bhav_support import bar
from hat_conf_repo import REPO_ROOT

from app import paths
from app.hat.data.bars_store import BarStore
from app.hat.data.store import (
    BAR_SCHEMA,
    DATASET_BARS,
    DATASET_INDEX,
    INDEX_SCHEMA,
    DataStore,
    IndexRow,
    StoreError,
)
from app.hat.settings import load_settings


def test_store__data_root() -> None:
    """The settings' default data root is `app.paths.HAT_DATA_ROOT`, defined only
    there; every path the store hands out resolves under its root (a name that climbs out is
    refused); and git ignores the default location."""
    assert load_settings({}).data_root == paths.HAT_DATA_ROOT
    defining = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in (REPO_ROOT / "backend" / "app").rglob("*.py")
        if re.search(r"^HAT_DATA_ROOT\s*=", p.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert defining == ["backend/app/paths.py"]
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        assert store.partition_dir(DATASET_BARS, date(2024, 1, 1)).is_relative_to(Path(tmp))
        assert store.raw_path("cm01JAN2024bhav.csv.zip").is_relative_to(Path(tmp))
        for bad in ("../escape.zip", "/etc/passwd", "a/../../b"):
            with pytest.raises(StoreError):
                store.raw_path(bad)
        first_day = date(2024, 1, 1)
        with pytest.raises(StoreError):
            store.partition_dir("somewhere_else", first_day)
    ignored = subprocess.run(  # noqa: S603 - fixed arguments, no input
        [
            shutil.which("git") or "git",
            "check-ignore",
            "-q",
            "data_system/hat/bars_daily_raw/date=2024-01-01/x.parquet",
        ],
        cwd=REPO_ROOT,
        check=False,
    )
    assert ignored.returncode == 0


def test_store__cli_root_after_env() -> None:
    """: every HAT command line (a module run with `python -m`) takes its
    settings from `operator_settings()`, which loads `.env` first, and never imports
    `load_settings` (under any name): `app.paths.HAT_DATA_ROOT` is read when `app.paths` is
    first imported, before anything loads `.env`. The stores have no default root, so a
    command can only hand them the root it resolved."""
    commands = {}
    for path in sorted((REPO_ROOT / "backend" / "app" / "hat").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if 'if __name__ == "__main__"' in text:
            commands[path.relative_to(REPO_ROOT).as_posix()] = ast.parse(text)
    assert set(commands) >= {
        "backend/app/hat/data/features.py",
        "backend/app/hat/data/history_build.py",
        "backend/app/hat/data/minute_history.py",
        "backend/app/hat/engine/backtest.py",
    }
    for name, tree in commands.items():
        imported = {
            alias.name: alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("settings")
            for alias in node.names
        }
        assert "load_settings" not in imported, name
        called = {
            node.func.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        assert imported.get("operator_settings") in called, name
    for store in (DataStore, BarStore):
        root = inspect.signature(store).parameters["root"]
        assert root.default is inspect.Parameter.empty, store.__name__


def test_store__layout() -> None:
    """Every Parquet file the store writes sits in `<dataset>/date=YYYY-MM-DD/` and
    has exactly the columns of that dataset's declared schema."""
    spec = {DATASET_BARS: BAR_SCHEMA.names, DATASET_INDEX: INDEX_SCHEMA.names}
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        day = date(2024, 5, 6)
        store.write_bars(day, [bar(uuid.UUID(int=1), uuid.UUID(int=2))])
        store.upsert_index(day, [IndexRow("NIFTY 50", *(Decimal("1.5"),) * 4)])
        files = sorted(Path(tmp).rglob("*.parquet"))
        assert len(files) == 2
        for path in files:
            dataset = path.relative_to(tmp).parts[0]
            assert path.parent.name == "date=2024-05-06"
            assert path.parent.parent.name == dataset
            assert pq.read_schema(path).names == spec[dataset]

"""CONF checks of the Parquet store against the spec (HAT-DATA-080, 081): where the store writes,
which root the command lines hand it, and that every file it produces has the §8 dataset's
partition and exact columns."""

import ast
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
from hat_conf_spec import REPO_ROOT, SPEC_DIR

from app import paths
from app.hat.data.store import DATASET_BARS, DATASET_INDEX, DataStore, IndexRow, StoreError

SECTION8_ROW = re.compile(r"^\| `([\w]+)/` \| `date=` \| ([^|]+) \|", re.MULTILINE)


def _spec_columns() -> dict[str, list[str]]:
    text = (SPEC_DIR / "02-data.md").read_text(encoding="utf-8")
    section = text[text.index("## §8 Parquet layout") :]
    return {
        name: [c.strip().split(" (")[0] for c in cols.split(",")]
        for name, cols in SECTION8_ROW.findall(section)
    }


def test_store__data_root() -> None:
    """HAT-DATA-080: the store's root is `app.paths.HAT_DATA_ROOT`, defined only there; every
    path it hands out resolves under the root (a name that climbs out is refused); and git
    ignores the default location."""
    assert DataStore().root == paths.HAT_DATA_ROOT
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
    """HAT-DATA-080 (PR 43): every HAT command line (a module run with `python -m`) takes its
    settings from `operator_settings()`, which loads `.env` first, and never from
    `load_settings()` alone, nor a store's default root: `app.paths.HAT_DATA_ROOT` is read when
    `app.paths` is first imported, before anything loads `.env`."""
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
        called = [
            (node.func.id if isinstance(node.func, ast.Name) else node.func.attr, node)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name | ast.Attribute)
        ]
        assert any(f == "operator_settings" for f, _ in called), name
        assert not [f for f, _ in called if f == "load_settings"], name
        default_root = [
            f for f, n in called if f in {"DataStore", "BarStore"} and not n.args and not n.keywords
        ]
        assert not default_root, name


def test_store__layout() -> None:
    """HAT-DATA-081: every Parquet file the store writes sits in `<dataset>/date=YYYY-MM-DD/` and
    has exactly the columns the §8 table lists for that dataset."""
    spec = _spec_columns()
    assert {DATASET_BARS, DATASET_INDEX} <= set(spec)
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

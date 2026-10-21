"""CONF checks of the Parquet store against the spec (HAT-DATA-080, 081): where the store writes,
and that every file it produces has the §8 dataset's partition and exact columns."""

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
        with pytest.raises(StoreError):
            store.partition_dir("somewhere_else", date(2024, 1, 1))
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

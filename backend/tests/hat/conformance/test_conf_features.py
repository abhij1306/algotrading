"""CONF checks of the features dataset: every partition the features job writes
has exactly the declared columns, one row per instrument, in `date=` partitions."""

import tempfile
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from app.hat.data.features import run_features
from app.hat.data.store import (
    DATASET_BARS,
    DATASET_BREADTH,
    DATASET_FEATURES,
    FEATURE_NAMES,
    BarRow,
    DataStore,
)
from app.hat.ids import uuid7


def test_features__schema() -> None:
    """Each written partition has exactly the declared feature columns
    (instrument_id text, the rest float64), unique instrument ids, and sits in
    `features_daily/date=YYYY-MM-DD/`."""
    spec = ["instrument_id", *FEATURE_NAMES]
    run = uuid7()
    with tempfile.TemporaryDirectory() as tmp:
        store = DataStore(Path(tmp))
        start = date(2024, 1, 1)
        for i in range(3):
            day = start + timedelta(days=i)
            rows = [
                BarRow(
                    f"i{k}",
                    f"INE{k:09d}",
                    f"S{k}",
                    "EQ",
                    *(Decimal(10 + k + i),) * 5,
                    100,
                    None,
                    "bhavcopy",
                    run,
                )
                for k in range(4)
            ]
            store.write_bars(day, rows)
        report = run_features(store)
        assert len(report.dates) == 3
        files = sorted(Path(tmp, DATASET_FEATURES).rglob("*.parquet"))
        assert len(files) == 3
        for path in files:
            assert path.parent.name.startswith("date=")
            assert path.parent.parent.name == DATASET_FEATURES
            schema = pq.read_schema(path)
            assert schema.names == spec
            assert schema.field("instrument_id").type == pa.string()
            assert all(schema.field(n).type == pa.float64() for n in spec[1:])
            ids = pq.read_table(path)["instrument_id"].to_pylist()
            assert len(ids) == len(set(ids)) == 4
        assert len(list(Path(tmp, DATASET_BREADTH).rglob("*.parquet"))) == 3
        assert store.days(DATASET_BARS) == store.days(DATASET_FEATURES)

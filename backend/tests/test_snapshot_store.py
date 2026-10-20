"""SnapshotStore reads curated snapshot parquet files from a data root it is given, caches
each file per version, and fails with typed errors that the snapshot API maps to its
existing status codes."""

import os
import threading
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.services.snapshots import (
    SnapshotCorruptError,
    SnapshotDateNotFoundError,
    SnapshotKind,
    SnapshotStore,
    SnapshotUnavailableError,
    atomic_write_parquet,
    get_snapshot_store,
    to_records,
)
from app.services.snapshots import store as store_module

STOCK = SnapshotKind.STOCK_DAILY
NIFTY = SnapshotKind.NIFTY50_DAILY


def stock_row(day: str, symbol: str, close: float) -> dict[str, Any]:
    return {
        "date": day,
        "symbol": symbol,
        "open": close - 1,
        "high": close + 1,
        "low": close - 2,
        "close": close,
        "volume": 1000,
        "turnover": close * 1000,
        "adj_open": close - 1,
        "adj_high": close + 1,
        "adj_low": close - 2,
        "adj_close": close,
        "adj_volume": 1000,
        "corporate_action_flag": False,
    }


def nifty_row(day: str, symbol: str, weight: float | None) -> dict[str, Any]:
    return {
        "date": day,
        "universe_id": "NIFTY50",
        "symbol": symbol,
        "in_universe": True,
        "weight": weight,
        "open": 10.0,
        "high": 11.0,
        "low": 9.0,
        "close": 10.5,
        "adj_close": 10.5,
        "volume": 500,
        "corporate_action_flag": False,
    }


def write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_parquet(path, index=False)


def bump_mtime(path: Path) -> None:
    """Move the file's mtime forward so a rewrite is visible even on a coarse clock."""
    stat_result = path.stat()
    later = stat_result.st_mtime_ns + 5_000_000_000
    os.utime(path, ns=(later, later))


@pytest.fixture
def roots(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "curated", tmp_path / "metadata"


@pytest.fixture
def store(roots: tuple[Path, Path]) -> SnapshotStore:
    curated, metadata = roots
    write(
        curated / STOCK.filename,
        [
            stock_row("2025-01-03", "SBIN", 801.0),
            stock_row("2025-01-02", "SBIN", 800.0),
            stock_row("2025-01-02", "TCS", 4000.0),
            stock_row("2025-01-06", "TCS", 4100.0),
        ],
    )
    write(
        curated / NIFTY.filename,
        [nifty_row("2025-01-02", "SBIN", 2.5), nifty_row("2025-01-02", "TCS", None)],
    )
    return SnapshotStore(curated, metadata)


@pytest.fixture
def read_calls(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Every parquet file the store parses, in order."""
    calls: list[Path] = []
    real_read_table = store_module.pq.read_table

    def counting_read_table(path: Path, *args: object, **kwargs: object) -> object:
        calls.append(Path(path))
        return real_read_table(path, *args, **kwargs)

    monkeypatch.setattr(store_module.pq, "read_table", counting_read_table)
    return calls


@pytest.fixture
def api(store: SnapshotStore) -> Generator[TestClient, None, None]:
    app.dependency_overrides[get_snapshot_store] = lambda: store
    yield TestClient(app)
    app.dependency_overrides.pop(get_snapshot_store, None)


def closes(frame: pd.DataFrame) -> list[tuple[str, str, float]]:
    return [(r["date"], r["symbol"], r["close"]) for r in to_records(frame)]


# --- locating and listing ----------------------------------------------------------------


def test_snapshot_files_are_located_under_the_given_root(
    store: SnapshotStore, roots: tuple[Path, Path]
) -> None:
    assert store.path(STOCK) == roots[0] / "snapshot_stock_daily.parquet"
    assert store.path(NIFTY) == roots[0] / "snapshot_nifty50_daily.parquet"


def test_available_dates_are_distinct_and_oldest_first(store: SnapshotStore) -> None:
    assert store.available_dates(STOCK) == [date(2025, 1, 2), date(2025, 1, 3), date(2025, 1, 6)]
    assert store.latest_date(STOCK) == date(2025, 1, 6)


# --- loading -----------------------------------------------------------------------------


def test_load_without_a_date_returns_the_latest_rows(store: SnapshotStore) -> None:
    assert closes(store.load(STOCK)) == [("2025-01-06", "TCS", 4100.0)]


def test_load_for_a_date_returns_only_that_dates_rows(store: SnapshotStore) -> None:
    assert closes(store.load(STOCK, date(2025, 1, 2))) == [
        ("2025-01-02", "SBIN", 800.0),
        ("2025-01-02", "TCS", 4000.0),
    ]


def test_records_carry_plain_values_and_nulls_as_none(store: SnapshotStore) -> None:
    assert to_records(store.load(NIFTY, date(2025, 1, 2))) == [
        nifty_row("2025-01-02", "SBIN", 2.5),
        nifty_row("2025-01-02", "TCS", None),
    ]


def test_a_date_with_no_rows_raises_date_not_found(store: SnapshotStore) -> None:
    with pytest.raises(SnapshotDateNotFoundError) as excinfo:
        store.load(STOCK, date(2025, 1, 4))

    assert str(excinfo.value) == "snapshot_stock_daily.parquet has no rows for 2025-01-04"


def test_a_missing_file_raises_unavailable(tmp_path: Path) -> None:
    empty = SnapshotStore(tmp_path / "nowhere", tmp_path / "meta")

    with pytest.raises(SnapshotUnavailableError) as excinfo:
        empty.available_dates(STOCK)

    expected = f"Phase-1 artifact not available: {tmp_path / 'nowhere' / STOCK.filename}"
    assert str(excinfo.value) == expected


def test_an_unparseable_file_raises_corrupt_without_the_parser_message(
    store: SnapshotStore,
) -> None:
    store.path(STOCK).write_bytes(b"this is not parquet")

    with pytest.raises(SnapshotCorruptError) as excinfo:
        store.load(STOCK)

    assert str(excinfo.value) == "Phase-1 artifact is unreadable: snapshot_stock_daily.parquet"


def test_a_file_missing_contract_columns_is_corrupt(store: SnapshotStore) -> None:
    pd.DataFrame([{"date": "2025-01-02", "symbol": "SBIN"}]).to_parquet(
        store.path(STOCK), index=False
    )

    with pytest.raises(SnapshotCorruptError):
        store.load(STOCK)


def test_a_file_with_malformed_dates_is_corrupt(store: SnapshotStore) -> None:
    write(store.path(STOCK), [stock_row("02/01/2025", "SBIN", 800.0)])

    with pytest.raises(SnapshotCorruptError):
        store.available_dates(STOCK)


# --- caching -----------------------------------------------------------------------------


def test_repeated_reads_of_an_unchanged_file_parse_it_once(
    store: SnapshotStore, read_calls: list[Path]
) -> None:
    store.load(STOCK)
    store.load(STOCK, date(2025, 1, 2))
    store.available_dates(STOCK)

    assert read_calls == [store.path(STOCK)]


def test_a_rewritten_file_is_read_again(store: SnapshotStore, read_calls: list[Path]) -> None:
    assert closes(store.load(STOCK)) == [("2025-01-06", "TCS", 4100.0)]

    write(store.path(STOCK), [stock_row("2025-01-07", "INFY", 1900.0)])
    bump_mtime(store.path(STOCK))

    assert closes(store.load(STOCK)) == [("2025-01-07", "INFY", 1900.0)]
    assert store.available_dates(STOCK) == [date(2025, 1, 7)]
    assert len(read_calls) == 2


def test_invalidate_forces_a_reparse(store: SnapshotStore, read_calls: list[Path]) -> None:
    store.load(STOCK)
    store.load(NIFTY)

    store.invalidate(STOCK)
    store.load(STOCK)
    store.load(NIFTY)
    store.invalidate()
    store.load(NIFTY)

    assert read_calls == [
        store.path(STOCK),
        store.path(NIFTY),
        store.path(STOCK),
        store.path(NIFTY),
    ]


def test_an_invalidate_during_a_parse_is_not_undone(
    store: SnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Path] = []
    real_read_table = store_module.pq.read_table

    def read_then_invalidate(path: Path, *args: object, **kwargs: object) -> object:
        calls.append(Path(path))
        table = real_read_table(path, *args, **kwargs)
        if len(calls) == 1:
            store.invalidate(STOCK)
        return table

    monkeypatch.setattr(store_module.pq, "read_table", read_then_invalidate)

    store.load(STOCK)
    store.load(STOCK)

    assert calls == [store.path(STOCK), store.path(STOCK)]


def test_concurrent_readers_share_one_parse(store: SnapshotStore, read_calls: list[Path]) -> None:
    workers = 16
    barrier = threading.Barrier(workers)

    def read() -> list[tuple[str, str, float]]:
        barrier.wait()
        return closes(store.load(STOCK, date(2025, 1, 2)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda _: read(), range(workers)))

    expected = [("2025-01-02", "SBIN", 800.0), ("2025-01-02", "TCS", 4000.0)]
    assert results == [expected] * workers
    assert read_calls == [store.path(STOCK)]


# --- metadata ----------------------------------------------------------------------------


def test_latest_run_log_skips_malformed_trailing_lines(store: SnapshotStore) -> None:
    store.metadata_root.mkdir(parents=True)
    (store.metadata_root / "run_log.jsonl").write_text(
        '{"run_id": "a"}\n{"run_id": "b"}\n{not json\n\n', encoding="utf-8", newline="\n"
    )

    assert store.latest_run_log() == {"run_id": "b"}
    assert store.metadata_status(["run_log.jsonl", "checksums.json"]) == {
        "run_log.jsonl": {"exists": True, "size_bytes": 43},
        "checksums.json": {"exists": False, "size_bytes": 0},
    }


# --- atomic writes -----------------------------------------------------------------------


def test_atomic_write_replaces_the_file_and_leaves_no_temp_files(
    store: SnapshotStore,
) -> None:
    atomic_write_parquet(pd.DataFrame([stock_row("2025-02-03", "SBIN", 820.0)]), store.path(STOCK))

    assert closes(store.load(STOCK)) == [("2025-02-03", "SBIN", 820.0)]
    assert sorted(p.name for p in store.curated_root.iterdir()) == [NIFTY.filename, STOCK.filename]


def test_a_failed_atomic_write_keeps_the_old_file(
    store: SnapshotStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing_to_parquet(self: pd.DataFrame, path: Path, **_kwargs: object) -> None:
        Path(path).write_bytes(b"half written")
        raise OSError("disk full")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", failing_to_parquet)

    with pytest.raises(OSError, match="disk full"):
        atomic_write_parquet(pd.DataFrame([stock_row("2025-02-03", "X", 1.0)]), store.path(STOCK))

    assert store.latest_date(STOCK) == date(2025, 1, 6)
    assert sorted(p.name for p in store.curated_root.iterdir()) == [NIFTY.filename, STOCK.filename]


# --- the snapshot API over the store -----------------------------------------------------


def test_stock_snapshot_returns_the_row_for_the_date_and_symbol(
    api: TestClient, store: SnapshotStore
) -> None:
    response = api.get("/api/data/snapshot/stock?date=2025-01-02&symbol=SBIN")

    assert response.status_code == 200
    assert response.json() == {
        "date": "2025-01-02",
        "symbol": "SBIN",
        "row": stock_row("2025-01-02", "SBIN", 800.0),
        "artifact": str(store.path(STOCK)),
    }


def test_universe_snapshot_returns_every_row_for_the_date(
    api: TestClient, store: SnapshotStore
) -> None:
    response = api.get("/api/data/snapshot/universe?date=2025-01-02")

    assert response.status_code == 200
    assert response.json() == {
        "date": "2025-01-02",
        "universe": "NIFTY50",
        "count": 2,
        "rows": [nifty_row("2025-01-02", "SBIN", 2.5), nifty_row("2025-01-02", "TCS", None)],
        "artifact": str(store.path(NIFTY)),
    }


@pytest.mark.parametrize(
    ("path", "detail"),
    [
        (
            "/api/data/snapshot/stock?date=2025-01-04&symbol=SBIN",
            "Snapshot row not found for requested date/symbol",
        ),
        (
            "/api/data/snapshot/stock?date=2025-01-02&symbol=INFY",
            "Snapshot row not found for requested date/symbol",
        ),
        (
            "/api/data/snapshot/universe?date=2025-01-04",
            "No universe snapshot rows found for requested date",
        ),
    ],
)
def test_missing_rows_answer_404(api: TestClient, path: str, detail: str) -> None:
    response = api.get(path)

    assert response.status_code == 404
    assert response.json() == {"detail": detail}


def test_a_missing_artifact_answers_503(api: TestClient, store: SnapshotStore) -> None:
    store.path(NIFTY).unlink()

    response = api.get("/api/data/snapshot/universe?date=2025-01-02")

    assert response.status_code == 503
    assert response.json() == {"detail": f"Phase-1 artifact not available: {store.path(NIFTY)}"}


@pytest.mark.parametrize(
    "corrupt",
    [
        lambda path: path.write_bytes(b"PAR1 garbage"),
        lambda path: pd.DataFrame([{"date": "x"}]).to_parquet(path, index=False),
    ],
)
def test_a_corrupt_artifact_answers_503_without_internal_detail(
    api: TestClient, store: SnapshotStore, corrupt: Callable[[Path], object]
) -> None:
    corrupt(store.path(STOCK))

    response = api.get("/api/data/snapshot/stock?date=2025-01-02&symbol=SBIN")

    assert response.status_code == 503
    assert response.json() == {
        "detail": "Phase-1 artifact is unreadable: snapshot_stock_daily.parquet"
    }

"""Golden-master test of the phase-1 pipeline (data_platform/pipelines/phase1_build.py).

Runs every step on a small synthetic data_system tree and compares a normalised view of all
outputs (parquet contents, reports, summary, published DB rows) with
fixtures/phase1_pipeline_golden.json. Regenerate only for intended behaviour changes:
    PHASE1_GOLDEN_UPDATE=1 pytest tests/test_phase1_build_pipeline_golden.py
"""

import importlib.util
import json
import os
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

PIPELINE_PATH = (
    Path(__file__).resolve().parents[2] / "data_platform" / "pipelines" / "phase1_build.py"
)
GOLDEN_PATH = Path(__file__).parent / "fixtures" / "phase1_pipeline_golden.json"

BHAV_HEADER = (
    "SYMBOL, SERIES, DATE1, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, CLOSE_PRICE, "
    "TTL_TRD_QNTY, TURNOVER_LACS\n"
)
BHAV_DAYS = {
    "sec_bhavdata_full_30012025.csv": (
        BHAV_HEADER
        + "AAA, EQ, 30-Jan-2025, 100.5, 110.25, 95.5, 105.5, 1000, 1.5\n"
        + "BBB, EQ, 30-Jan-2025, 50, 55, 45, 52, 500, 0.3\n"
        + "CCC, BE, 30-Jan-2025, 10, 11, 9, 10, 10, 0.01\n"
    ),
    "sec_bhavdata_full_31012025.csv": (
        BHAV_HEADER
        + "AAA, EQ, 31-Jan-2025, 52, 56, 50, 54, 2200, 1.2\n"
        + "BBB, EQ, 31-Jan-2025, 52, 54, 51, 53, 600, 0.31\n"
        + "DDD, EQ, 31-Jan-2025, 7, 8, 6, , 70, 0.01\n"
    ),
    # No DATE1 column: the trade date comes from the file name.
    "sec_bhavdata_full_03022025.csv": (
        "SYMBOL, SERIES, OPEN_PRICE, HIGH_PRICE, LOW_PRICE, CLOSE_PRICE, TTL_TRD_QNTY\n"
        "AAA, EQ, 54, 58, 53, 57, 1800\n"
        "BBB, EQ, 27, 28, 26, 27.5, 1300\n"
    ),
    # Outside the run window.
    "sec_bhavdata_full_15032025.csv": BHAV_HEADER + "AAA, EQ, 15-Mar-2025, 1, 1, 1, 1, 1, 1\n",
    "broken_file.csv": "",
}
CORP_ACTIONS = (
    "SYMBOL,EX-DATE,PURPOSE\n"
    "AAA,31-Jan-2025,Split FV Rs 10 To Rs 5\n"
    "BBB,03-Feb-2025,Bonus 1:1\n"
    "BBB,30-Jan-2025,Dividend Rs 2\n"
    "CCC,31-Jan-2025,Bonus 1:2\n"
)


def _write(path: Path, text: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Bytes, so sizes and hashes in the source manifest match on every OS.
    path.write_bytes(text if isinstance(text, bytes) else text.encode("utf-8"))


def _build_tree(root: Path, weights_src: Path) -> None:
    sources = root / "data_system" / "01_sources"
    for name, text in BHAV_DAYS.items():
        _write(sources / "nse_bhavcopy" / name, text)
    _write(sources / "nse_corporate_actions" / "CF-CA-equities-2025.csv", CORP_ACTIONS)
    _write(
        root / "data_system" / "05_metadata" / "reference" / "equity_list.csv",
        "SYMBOL,SERIES\nAAA,EQ\nZZZ,BE\n",
    )
    raw = root / "data_system" / "03_universe" / "monthly_universe_raw"
    _write(raw / "2025_01" / "nifty50.csv", "symbol,weight\naaa,0.6\nbbb,0.4\n")
    _write(raw / "feb2025" / "banknifty.csv", "symbol,weight\nBBB,70\nAAA,29\nXXX,\n")
    _write(raw / "2025_02" / "nifty50.csv", "ticker,weight\nAAA,1\n")
    # In range (copied; never parsed because the CSV wins), out of range, and unparseable.
    _write(weights_src / "2025" / "NIFTY_50_Jan2025.pdf", b"%PDF-not-really")
    _write(weights_src / "NIFTY_50_Dec2024.pdf", b"%PDF-old")
    _write(weights_src / "NIFTY_50_Foo2025.pdf", b"%PDF-bad-name")


@pytest.fixture
def phase1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("phase1_build_golden", PIPELINE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    root = tmp_path / "project"
    weights_src = tmp_path / "downloads"
    _build_tree(root, weights_src)
    monkeypatch.setattr(module, "PROJECT_ROOT", root)
    monkeypatch.setenv("PHASE1_INDEX_WEIGHTS_SRC", str(weights_src))

    engine = create_engine(f"sqlite:///{tmp_path / 'phase1.db'}")
    module.DatasetRun.metadata.create_all(engine)
    monkeypatch.setattr(module, "SessionLocal", sessionmaker(bind=engine))
    module.test_root = root
    module.test_engine = engine
    return module


def _norm(value: object, root: Path) -> object:
    """JSON-safe value with tmp paths, run ids and timestamps replaced by placeholders."""
    if isinstance(value, dict):
        return {k: _norm_key(k, v, root) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_norm(v, root) for v in value]
    if isinstance(value, float) and value != value:
        return "NaN"
    if isinstance(value, pd.Timestamp | date):
        return value.isoformat()
    if isinstance(value, str):
        return value.replace("\\", "/").replace(root.as_posix(), "<ROOT>")
    if hasattr(value, "item"):
        return _norm(value.item(), root)
    return value


VOLATILE_KEYS = {"generated_at_utc", "generated_at", "run_id", "modified_at_utc"}


def _norm_key(key: str, value: object, root: Path) -> object:
    if key in VOLATILE_KEYS:
        return "<volatile>"
    if key == "checksum":
        return "<sha256>"
    return _norm(value, root)


def _frame(path: Path) -> list[dict]:
    df = pd.read_parquet(path)
    return df.to_dict(orient="records")


def _collect(phase1: ModuleType, builder: object) -> dict:
    root = phase1.test_root
    curated = builder.paths.curated_root
    meta = builder.paths.metadata_root
    out: dict = {"summary": builder.summary, "issues": builder.issues}
    out["parquet"] = {p.name: _frame(p) for p in sorted(curated.glob("*.parquet"))}
    out["json"] = {
        p.name: json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(meta.glob("*.json"))
        if p.name != "data_contract.json"
    }
    audit = builder.paths.corp_action_audit
    out["audit_csv"] = audit.read_text(encoding="utf-8") if audit.exists() else None
    copied = builder.paths.source_index_weights
    out["copied_pdfs"] = sorted(p.relative_to(copied).as_posix() for p in copied.rglob("*.pdf"))
    log_lines = builder.paths.run_log.read_text(encoding="utf-8").splitlines()
    out["run_log"] = [
        (entry["step"], entry["status"], entry["details"])
        for entry in (json.loads(line) for line in log_lines)
    ]
    with phase1.test_engine.connect() as conn:
        out["db"] = {
            table: [dict(row._mapping) for row in conn.exec_driver_sql(query)]
            for table, query in DB_QUERIES.items()
        }
    return _norm(out, root)


DB_QUERIES = {
    "dataset_runs": "SELECT mode, status, asof_date FROM dataset_runs ORDER BY mode",
    "dataset_artifacts": (
        "SELECT dataset_name, row_count, min_date, max_date FROM dataset_artifacts "
        "ORDER BY dataset_name"
    ),
    "snapshot_index_stock": (
        "SELECT snapshot_date, symbol FROM snapshot_index_stock ORDER BY snapshot_date, symbol"
    ),
    "snapshot_index_universe": (
        "SELECT snapshot_date, universe_id, version FROM snapshot_index_universe "
        "ORDER BY snapshot_date, universe_id"
    ),
}


def _run_scenarios(phase1: ModuleType) -> dict:
    happy = phase1.Phase1Builder(
        asof=date(2025, 2, 3),
        mode="full",
        start_date=date(2025, 1, 30),
        universes=["nifty50", "BANKNIFTY"],
    )
    happy.run_all()
    results = {"happy": _collect(phase1, happy)}

    early = phase1.Phase1Builder(
        asof=date(2025, 2, 3), mode="full", start_date=date(2025, 1, 2), universes=["NIFTY50"]
    )
    early.ingest_sources()
    early.build_universe("NIFTY50")
    early.build_equity()
    early.apply_corp_actions()
    early.build_snapshots()
    results["early_start_validate"] = early.validate()
    results["early_start_report"] = _norm(early.summary["validation"], phase1.test_root)

    missing = phase1.Phase1Builder(
        asof=date(2025, 2, 3), mode="full", start_date=date(2025, 1, 30), universes=["CUSTOM IDX"]
    )
    results["missing_validate"] = missing.validate()
    results["missing_report"] = _norm(
        json.loads(missing.paths.validation_report.read_text(encoding="utf-8")), phase1.test_root
    )
    return results


def test_phase1_pipeline_matches_golden(phase1: ModuleType) -> None:
    results = json.loads(json.dumps(_run_scenarios(phase1), sort_keys=True, default=str))

    if os.getenv("PHASE1_GOLDEN_UPDATE") == "1":
        GOLDEN_PATH.write_text(json.dumps(results, indent=1, sort_keys=True) + "\n", "utf-8")
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))

    for key in golden:
        assert results[key] == golden[key], key
    assert results.keys() == golden.keys()

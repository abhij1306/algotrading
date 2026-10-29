"""The data record's pure parts: the manifest line and change detection of `data.record`, and the
exclusion, incremental selection and asset check of `scripts/hat_snapshot.py`. Small hand-built
inputs; nothing is read from a store, a database, git or GitHub."""

from __future__ import annotations

import hashlib
import importlib.util
from datetime import date
from pathlib import Path

from app.hat.data import record
from app.paths import PROJECT_ROOT

_spec = importlib.util.spec_from_file_location(
    "hat_snapshot", PROJECT_ROOT / "scripts" / "hat_snapshot.py"
)
assert _spec is not None
assert _spec.loader is not None
snapshot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snapshot)


def test_a_manifest_line_takes_its_dates_from_the_stats_else_the_partition() -> None:
    row = record.manifest_row("bars_5m/date=2024-12-13/bars.parquet", 47817, None, None, 9, "ab")
    assert row == {
        "path": "bars_5m/date=2024-12-13/bars.parquet",
        "rows": "47817",
        "min_date": "2024-12-13",
        "max_date": "2024-12-13",
        "bytes": "9",
        "sha256": "ab",
    }
    etf = record.manifest_row("etf/daily.parquet", 3, date(2011, 1, 3), date(2026, 10, 28), 1, "c")
    assert (etf["min_date"], etf["max_date"]) == ("2011-01-03", "2026-10-28")
    raw = record.manifest_row("raw/cm27JUN2018bhav.csv.zip", None, None, None, 5, "d")
    assert (raw["rows"], raw["min_date"], raw["max_date"]) == ("", "", "")


def test_the_manifest_round_trips_sorted_by_path() -> None:
    rows = [record.manifest_row(p, 1, None, None, 1, s) for p, s in (("b/x", "2"), ("a/y", "1"))]
    text = record.manifest_text(rows)
    assert text.splitlines()[0] == ",".join(record.MANIFEST_FIELDS)
    assert list(record.parse_manifest(text)) == ["a/y", "b/x"]
    assert record.parse_manifest("") == {}


def test_compare_counts_added_and_names_rewritten_and_removed_files() -> None:
    old = {"d=1": "a", "d=2": "b", "d=3": "c"}
    new = {"d=1": "a", "d=2": "B", "d=4": "e", "d=5": "f"}
    changes = record.compare(old, new)
    assert changes == record.Changes(2, ("d=2",), ("d=3",))
    assert record.summary({"bars_5m": changes, "etf": record.compare(old, old)}) == (
        "bars_5m +2 ~1 -1"
    )
    assert record.summary({"etf": record.compare(old, old)}) == "no change"
    assert record.commit_message("2026-10-29", {"bars_5m": changes}) == (
        "record 2026-10-29: bars_5m +2 ~1 -1\n\nrewritten d=2\nremoved d=3"
    )
    assert record.commit_message("2026-10-29", {}) == "record 2026-10-29: no change"


def test_exclusion_by_component_or_by_prefix_from_the_root() -> None:
    patterns = ["__pycache__", "*.pyc", "/scratch", "backtests/runs"]
    assert snapshot.excluded("a/__pycache__/m.py", patterns)
    assert snapshot.excluded("x.pyc", patterns)
    assert snapshot.excluded("scratch/a.csv", patterns)
    assert not snapshot.excluded("work/scratch/a.csv", patterns)  # `/scratch` is anchored
    assert snapshot.excluded("backtests/runs/r1/a.json", patterns)
    assert not snapshot.excluded("backtests/trials.json", patterns)
    excludes = snapshot.part_excludes(["*:venv", "data:/scratch", "raw:x"], "data")
    assert excludes == [*snapshot.SECRET_PATTERNS, "venv", "/scratch"]
    for secret in (".env", "a/.cookies", "kotak/set_secrets.log", "x/api.key", "fyers_token.json"):
        assert snapshot.excluded(secret, snapshot.SECRET_PATTERNS)
    assert snapshot.excluded("a/.ENV", [])  # credentials in any case, whatever the patterns
    assert snapshot.excluded("MyToken.json", [])


def test_an_incremental_release_carries_only_new_or_changed_files() -> None:
    def entry(sha: str) -> dict[str, int | str]:
        return {"size": 1, "sha256": sha}

    full = {"created": "2026-10-29T00:00:00+00:00", "parts": {"raw": {"files": {
        "a": entry("1"), "b": entry("2"), "c": entry("3")}}}}  # fmt: skip
    week = {"created": "2026-11-01T00:00:00+00:00", "incremental": True, "parts": {"raw": {
        "files": {"b": entry("2x")}, "removed": ["c"]}}}  # fmt: skip
    held = snapshot.held_files([week, full])  # restored in creation order
    assert held == {"raw": {"a": entry("1"), "b": entry("2x")}}
    current = {"a": entry("1"), "b": entry("2"), "d": entry("4")}
    assert snapshot.changes(held["raw"], current) == (["b", "d"], [])
    # a later full release starts the part afresh: a file it lacks is carried again
    again = {"created": "2026-11-08T00:00:00+00:00", "incremental": False,
             "parts": {"raw": {"files": {"b": entry("2x")}}}}  # fmt: skip
    assert snapshot.held_files([full, week, again]) == {"raw": {"b": entry("2x")}}


def test_the_asset_writer_cuts_the_stream_and_hashes_each_part(tmp_path: Path) -> None:
    got: dict[str, dict[str, int | str]] = {}
    writer = snapshot.AssetWriter(tmp_path, "x.tar.gz", lambda p, e: got.update({p.name: e}), 4)
    writer.write(b"abcdefghij")
    writer.close()
    assert [got[n]["size"] for n in sorted(got)] == [4, 4, 2]
    assert got["x.tar.gz.part002"]["sha256"] == hashlib.sha256(b"ij").hexdigest()
    assert snapshot.sums_text(got).splitlines()[0].endswith("  x.tar.gz.part000")


def test_the_asset_check_names_missing_resized_and_changed_assets() -> None:
    recorded = {"a": {"size": 1, "sha256": "x"}, "b": {"size": 2, "sha256": "y"}}
    remote = {"b": {"size": 2, "sha256": "z"}}
    assert snapshot.asset_problems(recorded, remote) == [
        "a is missing from the release",
        "b digest differs from the record",
    ]
    assert snapshot.asset_problems({"b": {"size": 2, "sha256": "y"}}, {"b": {"size": 2}}) == []
    stale = {"b": {"size": 2, "sha256": "y"}, "b.part9": {"size": 1}, "SHA256SUMS": {"size": 1}}
    assert snapshot.asset_problems(recorded, stale, ["SHA256SUMS"]) == [
        "b.part9 is on the release but not in the record",
        "a is missing from the release",
    ]

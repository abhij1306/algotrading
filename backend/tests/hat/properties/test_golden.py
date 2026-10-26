"""The golden sample check's pure seams: tolerance compare, key resolution on hand-built rows,
the check's outcomes, rebase file building, the schema and the audit check
(docs/backtests/golden-sample.md)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.hat.data import audit, golden
from app.hat.data.golden import Stores, Unresolved

OLD, NEW, OTHER = "i-old", "i-new", "i-other"
D1, D2, D3 = date(2024, 3, 1), date(2024, 3, 4), date(2024, 3, 5)
STORES = Stores(
    instruments=[
        (OLD, "INE000A01011", "ABC"),
        (NEW, "INE000A01029", "ABCNEW"),
        (OTHER, "INE9", "XYZ"),
    ],
    history=[(OLD, "ABC", date(2020, 1, 1), D2), (NEW, "ABCNEW", D2, None)],
    chain={OLD: NEW},
    raw={(OLD, D1): {"close": Decimal("100.5000"), "volume": 10}},
    adjusted={("bars_daily", NEW, D1): {"close": Decimal("50.2500")}},
    actions=[
        {"instrument_id": NEW, "ex_date": D2, "action_type": "split", "adjustment_factor": 0.5},
        {"instrument_id": OTHER, "ex_date": D2, "action_type": "split", "adjustment_factor": 0.5},
        {"instrument_id": OTHER, "ex_date": D2, "action_type": "bonus", "adjustment_factor": 0.5},
    ],
    members=[
        {"index_name": "NIFTY 500", "instrument_id": NEW, "effective_from": D1, "effective_to": D3}
    ],
    lineage=[
        {"old_instrument_id": OLD, "new_instrument_id": NEW, "status": "linked", "known_on": D3}
    ],
    index_closes={("NIFTY 500", D1): {"close": Decimal("21000.15")}},
    features={(NEW, D1): {"sma50": 49.75}},
    breadth={("breadth_daily", D1): {"breadth_pct": 61.5}},
    bars_5m={
        (OLD, D1): [
            (datetime(2024, 3, 1, 3, 50, tzinfo=UTC), 10, 12, 9, 11, 100),
            (datetime(2024, 3, 1, 3, 45, tzinfo=UTC), 9.5, 10, 9.4, 10, 50),
        ]
    },
)


def _item(kind: str, key: dict, value: object, **extra: object) -> dict:
    return {
        "id": extra.pop("id", f"{kind}-1"),
        "kind": kind,
        "key": key,
        "value": value,
        "tolerance": extra.pop("tolerance", None),
        "risk": "random",
        "verified_from": "hand",
        **extra,
    }


def _doc(items: list[dict], mismatches: list[dict] | None = None) -> dict:
    return {
        "format": golden.FORMAT,
        "created": "2026-10-26",
        "created_by": "test",
        "seed": 7,
        "data_audit_digest": "abc",
        "items": items,
        "mismatches": mismatches or [],
    }


def test_matches_tolerance() -> None:
    assert golden.matches(100.05, Decimal("100.04"), {"abs": 0.01})
    assert not golden.matches(100.05, Decimal("100.03"), {"abs": 0.01})
    assert golden.matches(200.0, 201.0, {"rel": 0.005})
    assert not golden.matches(200.0, 202.0, {"rel": 0.005})
    assert golden.matches(0.5, Decimal("0.5000"), None)
    assert not golden.matches(0.5, 0.5000001, None)
    assert golden.matches(True, True, None)
    assert not golden.matches(True, 1, None)  # a bool is not a number
    assert golden.matches("2024-03-05", D3, None)


def test_resolve_instrument() -> None:
    assert golden.resolve_instrument({"isin": "INE000A01011"}, None, STORES) == OLD
    assert golden.resolve_instrument({"symbol": "ABC"}, D1, STORES) == OLD  # before the rename
    assert golden.resolve_instrument({"symbol": "ABCNEW"}, D3, STORES) == NEW
    assert golden.resolve_instrument({"old_isin": "INE000A01011"}, None, STORES, "old_") == OLD
    with pytest.raises(Unresolved) as missing:
        golden.resolve_instrument({"isin": "INE404"}, None, STORES)
    assert missing.value.status == golden.MISSING
    with pytest.raises(Unresolved) as shapeless:
        golden.resolve_instrument({"date": "2024-03-01"}, D1, STORES)
    assert shapeless.value.status == golden.UNRESOLVED
    twice = Stores(instruments=[("a", "I1", "DUP"), ("b", "I2", "DUP")])
    with pytest.raises(Unresolved, match="ambiguous"):
        golden.resolve_instrument({"symbol": "DUP"}, None, twice)


@pytest.mark.parametrize(
    ("kind", "key", "value"),
    [
        ("bar_raw", {"isin": "INE000A01011", "date": "2024-03-01", "field": "close"}, 100.5),
        ("bar_adjusted", {"symbol": "ABC", "date": "2024-03-01", "store": "bars_daily_adj"}, 50.25),
        ("corporate_action", {"isin": "INE000A01029", "ex_date": "2024-03-04"}, 0.5),
        (
            "corporate_action",
            {"isin": "INE000A01029", "ex_date": "2024-03-05", "field": "exists"},
            False,
        ),
        ("membership", {"isin": "INE000A01029", "date": "2024-03-04"}, True),
        ("membership", {"isin": "INE000A01029", "date": "2024-03-05"}, False),
        ("isin_lineage", {"old_isin": "INE000A01011", "new_isin": "INE000A01029"}, "linked"),
        ("isin_lineage", {"isin": "INE000A01011", "field": "chain_isin"}, "INE000A01029"),
        ("index_close", {"index": "NIFTY 500", "date": "2024-03-01"}, 21000.15),
        ("feature", {"isin": "INE000A01011", "date": "2024-03-01", "field": "sma50"}, 49.75),
        ("breadth", {"date": "2024-03-01"}, 61.5),
        ("bar_5m_daily_agg", {"isin": "INE000A01011", "date": "2024-03-01", "field": "open"}, 9.5),
        (
            "bar_5m_daily_agg",
            {"isin": "INE000A01011", "date": "2024-03-01", "field": "volume"},
            150,
        ),
    ],
)
def test_lookup_on_hand_built_rows(kind: str, key: dict, value: object) -> None:
    assert golden.matches(value, golden.lookup(_item(kind, key, value), STORES), None)


def test_ambiguous_action_is_unresolved() -> None:
    item = _item("corporate_action", {"isin": "INE9", "ex_date": "2024-03-04"}, 0.5)
    with pytest.raises(Unresolved, match="ambiguous"):
        golden.lookup(item, STORES)


def test_check_outcomes() -> None:
    key = {"isin": "INE000A01011", "date": "2024-03-01"}
    doc = _doc(
        [
            _item("bar_raw", key, 100.5, id="ok"),
            _item("bar_raw", key, 101.0, id="moved"),
            _item("bar_raw", {**key, "date": "2024-03-04"}, 1.0, id="gone"),
        ],
        [
            _item("bar_raw", key, 100.0, id="still", store_value=100.5),
            _item("bar_raw", key, 100.5, id="fixed", store_value=99.0),
        ],
    )
    result = golden.check(doc, STORES)
    assert {o["id"]: o["status"] for o in result["items"]} == {
        "ok": "pass",
        "moved": "drift",
        "gone": "missing",
    }
    assert {o["id"]: o["status"] for o in result["mismatches"]} == {
        "still": "open",
        "fixed": "fixed",
    }
    assert [o["id"] for o in result["drift"]] == ["moved", "gone"]
    assert result["by_kind"] == {"bar_raw": {"drift": 1, "missing": 1, "pass": 1}}
    assert golden.failed(result)
    check = audit.check_golden(
        golden.result_doc(Path("2026-10-26.json"), "d", doc, result, datetime.now(UTC))
    )
    assert check["status"] == audit.FAIL
    assert {f["code"]: f["count"] for f in check["findings"]} == {
        "golden_drift": 2,
        "golden_mismatch_open": 1,
    }


def test_rebase_doc() -> None:
    key = {"isin": "INE000A01011", "date": "2024-03-01"}
    doc = _doc([_item("bar_raw", key, 100.5, id="ok"), _item("bar_raw", key, 101.0, id="moved")])
    result = golden.check(doc, STORES)
    source = ("2026-10-26.json", "d")
    with pytest.raises(golden.GoldenError, match="reason_required"):
        golden.rebase_doc(doc, result, golden.ALL_DRIFTED, " ", D3, source)
    with pytest.raises(golden.GoldenError, match="not_drifted"):
        golden.rebase_doc(doc, result, ["ok"], "fix", D3, source)
    new = golden.rebase_doc(doc, result, golden.ALL_DRIFTED, "corrected factor", D3, source)
    moved = next(i for i in new["items"] if i["id"] == "moved")
    assert moved["value"] == 100.5
    assert moved["rebase"] == {
        "reason": "corrected factor",
        "previous_value": 101.0,
        "from_file": "2026-10-26.json",
        "on": "2024-03-05",
    }
    assert new["rebased_from"] == {"file": "2026-10-26.json", "digest": "d"}
    assert doc["items"][1]["value"] == 101.0  # the old document is untouched
    assert not golden.validate(new)


def test_validate() -> None:
    good = _doc([_item("bar_raw", {"isin": "X"}, 1.0, tolerance={"abs": 0.01})])
    assert golden.validate(good) == []
    assert golden.validate(_doc([_item("bar_raw", {}, 1.0, tolerance={"abs": 0.1, "rel": 0.1})]))
    assert golden.validate(_doc([_item("price", {}, 1.0)]))
    assert golden.validate(_doc([], [_item("bar_raw", {}, 1.0)]))  # a mismatch needs store_value
    twice = _doc([_item("bar_raw", {}, 1.0, id="a"), _item("bar_raw", {}, 2.0, id="a")])
    assert golden.validate(twice) == ["items: id 'a' held 2 times"]


def test_newest_and_next_path(tmp_path: Path) -> None:
    assert golden.newest(tmp_path) is None
    assert audit.check_golden(None)["status"] == audit.WARN
    base = golden.golden_dir(tmp_path)
    base.mkdir(parents=True)
    for name in ("2026-10-26.json", "2026-10-26.2.json", "2026-09-30.9.json", "notes.json"):
        (base / name).write_text("{}")
    assert golden.newest(tmp_path) == base / "2026-10-26.2.json"
    assert golden.next_path(tmp_path, date(2026, 10, 26)) == base / "2026-10-26.3.json"
    assert golden.next_path(tmp_path, date(2026, 10, 27)) == base / "2026-10-27.json"

"""The storage split's seams: config revision files (schema, pointer, immutability), which
database a statement runs on and the read-only hat_live connection."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select

from app.hat.backtest.evidence import audit_admits
from app.hat.configs.records import (
    Bot,
    ConfigRecordError,
    StrategyVersion,
    doc_of,
    kind_rules,
    record_from_doc,
    record_rules,
)
from app.hat.configs.store import (
    ConfigConflictError,
    FileConfigStore,
    Write,
    next_pointer,
    validate_revision,
)
from app.hat.models import HatInstrument, HatPosition
from app.hat.storage import (
    LIVE,
    STATE,
    CrossDatabaseError,
    HatSession,
    live_connect_options,
    session_binds,
    statement_database,
)

AT = datetime(2026, 10, 26, 18, tzinfo=UTC)


def _bot() -> Bot:
    return Bot(
        user_id=uuid.uuid4(),
        strategy_id=uuid.uuid4(),
        broker_account_id=uuid.uuid4(),
        status="active",
        lifecycle_state="research",
        risk_cluster="momentum",
    )


def _version(**changes: object) -> dict[str, object]:
    version = StrategyVersion(
        strategy_id=uuid.uuid4(),
        version=1,
        status="draft",
        config={"bot": "s1"},
        config_hash="h",
    )
    return {**doc_of(version), **changes}


def _revision(kind: str, record: dict[str, object]) -> dict[str, object]:
    return {
        "kind": kind,
        "id": str(record["id"]),
        "rev": 1,
        "written_at": AT.isoformat(),
        "record": record,
    }


# --- schema validation ---------------------------------------------------------------------------


def test_a_valid_revision_file_passes_and_a_broken_one_is_refused() -> None:
    record = doc_of(_bot())
    validate_revision("bot", _revision("bot", record))
    for broken in (
        {**record, "lifecycle_state": "flying"},  # outside the enum
        {**record, "user_id": None},  # not nullable
        {**record, "risk_pct": "1"},  # no such field
    ):
        with pytest.raises(ConfigRecordError) as caught:
            validate_revision("bot", _revision("bot", broken))
        assert caught.value.code == "config_schema"


# --- the pointer -------------------------------------------------------------------------------


def test_the_pointer_moves_each_written_record_one_revision() -> None:
    seq, records = next_pointer({"a": 2}, 7, [Write("bot", "a", 2, {}), Write("bot", "b", 0, {})])
    assert (seq, records) == (8, {"a": 3, "b": 1})


def test_a_write_from_a_stale_revision_is_refused() -> None:
    with pytest.raises(ConfigConflictError) as caught:
        next_pointer({"a": 3}, 7, [Write("bot", "a", 2, {})])
    assert caught.value.code == "config_conflict"
    with pytest.raises(ConfigConflictError):  # a new record whose id is taken
        next_pointer({"a": 1}, 7, [Write("bot", "a", 0, {})])


def test_a_commit_switches_the_pointer_and_keeps_every_revision(tmp_path: object) -> None:
    store = FileConfigStore(tmp_path)  # type: ignore[arg-type]
    first = doc_of(_bot())
    key = str(first["id"])
    store.commit([Write("bot", key, 0, first)])
    second = {**first, "status": "paused"}
    store.commit([Write("bot", key, 1, second)])
    assert store.pointer("bot") == (2, {key: 2})
    assert store.revision("bot", key, 1) == first  # the old revision is never rewritten
    assert store.revision("bot", key, 2) == second
    assert [p["records"] for p in store.history("bot")] == [{key: 1}, {key: 2}]
    on_disk = json.loads((tmp_path / "bot" / key / "1.json").read_text())  # type: ignore[operator]
    assert on_disk["record"] == first


# --- version immutability ----------------------------------------------------------------------


def test_a_fixed_field_never_changes_in_a_later_revision() -> None:
    before = _version()
    record_rules(StrategyVersion, before, {**before, "status": "frozen"})  # mutable
    with pytest.raises(ConfigRecordError) as caught:
        record_rules(StrategyVersion, before, {**before, "config": {"bot": "s2"}})
    assert caught.value.code == "config_immutable"


def test_a_frozen_version_never_returns_to_draft_nor_releases_without_evidence() -> None:
    frozen = _version(status="frozen", backtest_run_id="run-1")
    with pytest.raises(ConfigRecordError) as caught:
        record_rules(StrategyVersion, frozen, {**frozen, "status": "draft"})
    assert caught.value.code == "version_back_to_draft"
    with pytest.raises(ConfigRecordError) as caught:
        record_rules(StrategyVersion, frozen, {**frozen, "status": "released"})
    assert caught.value.code == "release_evidence"


# --- which database ----------------------------------------------------------------------------


def test_a_statement_runs_on_the_database_of_its_tables() -> None:
    assert statement_database([HatInstrument.__table__]) == LIVE  # type: ignore[list-item]
    assert statement_database([HatPosition.__table__]) == STATE  # type: ignore[list-item]
    assert statement_database([]) is None


def test_a_session_sends_each_statement_to_its_database_and_refuses_a_join_of_both() -> None:
    live, state = create_engine("sqlite://"), create_engine("sqlite://")
    session = HatSession(binds=session_binds(live, state))
    assert session.get_bind(clause=select(HatInstrument.symbol)) is live
    assert session.get_bind(clause=select(HatPosition.id)) is state
    counted = select(func.count()).select_from(HatInstrument)  # a column of no table
    assert session.get_bind(clause=counted) is live
    joined = select(HatPosition.id).join(
        HatInstrument, HatInstrument.id == HatPosition.instrument_id
    )
    with pytest.raises(CrossDatabaseError):
        session.get_bind(clause=joined)
    with pytest.raises(CrossDatabaseError):  # an implicit join in the where clause too
        session.get_bind(
            clause=select(HatPosition.id).where(HatPosition.instrument_id == HatInstrument.id)
        )


def test_only_a_data_writer_may_write_hat_live() -> None:
    assert live_connect_options(writer=False) == "-c default_transaction_read_only=on"
    assert live_connect_options(writer=True) == ""


# --- a run's audit gating promotion ----------------------------------------------------------


def test_only_a_run_whose_audit_passed_or_warned_is_evidence() -> None:
    assert audit_admits({"status": "pass"})
    assert audit_admits({"status": "warn"})
    assert not audit_admits({"status": "fail"})
    assert not audit_admits({})  # no status
    assert not audit_admits(None)  # a run from before the audits


# --- rules across a kind's records ----------------------------------------------------------


def test_version_numbers_are_unique_and_one_version_is_released_per_strategy() -> None:
    first = _version()
    kind_rules("strategy_version", [first, {**_version(), "version": 1}])  # another strategy
    with pytest.raises(ConfigRecordError) as caught:
        kind_rules("strategy_version", [first, {**first, "id": str(uuid.uuid4())}])
    assert caught.value.code == "config_unique"
    released = {**first, "status": "released"}
    other = {**released, "id": str(uuid.uuid4()), "version": 2}
    with pytest.raises(ConfigRecordError) as caught:
        kind_rules("strategy_version", [released, other])
    assert caught.value.code == "one_released_version"


def test_a_commit_refuses_a_second_record_of_a_taken_version_number(tmp_path: object) -> None:
    store = FileConfigStore(tmp_path)  # type: ignore[arg-type]
    first = _version()
    store.commit([Write("strategy_version", str(first["id"]), 0, first)])
    twin = {**first, "id": str(uuid.uuid4())}  # a concurrent save of the same number
    with pytest.raises(ConfigRecordError):
        store.commit([Write("strategy_version", str(twin["id"]), 0, twin)])
    assert store.pointer("strategy_version") == (1, {str(first["id"]): 1})


def test_a_revision_file_no_pointer_names_never_blocks_the_next_commit(tmp_path: object) -> None:
    store = FileConfigStore(tmp_path)  # type: ignore[arg-type]
    record = doc_of(_bot())
    key = str(record["id"])
    store.commit([Write("bot", key, 0, record)])
    stray = tmp_path / "bot" / key / "2.json"  # type: ignore[operator]
    stray.write_text("{}")  # left by a commit that stopped before its pointer moved
    paused = {**record, "status": "paused"}
    store.commit([Write("bot", key, 1, paused)])
    assert store.revision("bot", key, 2) == paused


def test_a_record_never_shares_a_nested_value_with_its_revision() -> None:
    doc = _version(config={"exits": {"a": 1}})
    again = record_from_doc(StrategyVersion, doc)
    again.config["exits"]["a"] = 2  # type: ignore[index]
    assert doc["config"] == {"exits": {"a": 1}}

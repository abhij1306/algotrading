"""The storage split's seams: config revision files (schema, pointer, immutability), which
database a statement runs on, the read-only hat_live connection, and the export of a legacy
config row."""

import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, func, select

from app.hat.backtest.evidence import audit_admits
from app.hat.configs.records import (
    Bot,
    ConfigRecordError,
    CriterionType,
    StrategyVersion,
    doc_of,
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
from app.hat.ops.state_split import config_plan, legacy_record_doc, rows_digest
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


# --- the export of legacy rows -----------------------------------------------------------------


def test_a_legacy_row_becomes_its_record_with_the_same_id() -> None:
    bot = _bot()
    row = {**doc_of(bot), "id": bot.id, "user_id": bot.user_id, "extra": "ignored"}
    key, doc = legacy_record_doc(Bot, row)
    assert key == str(bot.id)
    assert doc == doc_of(bot)
    version = {**_version(), "backtest_run_id": uuid.UUID(int=5), "created_at": AT}
    _, doc = legacy_record_doc(StrategyVersion, version)
    assert doc["backtest_run_id"] == str(uuid.UUID(int=5))
    assert doc["created_at"] == AT.isoformat()
    row = {"code": "ema", "version": 2, "stage": "setup", "param_schema": {}, "data_needs": ["d"]}
    assert legacy_record_doc(CriterionType, row)[0] == "ema@2"


def test_a_bad_legacy_row_is_refused() -> None:
    with pytest.raises(ConfigRecordError):
        legacy_record_doc(Bot, {**doc_of(_bot()), "status": "lost"})


def test_the_export_writes_what_is_missing_and_refuses_what_differs() -> None:
    docs = {"a": {"x": 1}, "b": {"x": 2}, "c": {"x": 3}}
    plan = config_plan("bot", docs, {"b": {"x": 2}, "c": {"x": 4}})
    assert [(w.id, w.base) for w in plan.writes] == [("a", 0)]
    assert (plan.same, plan.differs) == (["b"], ["c"])


def test_the_digest_ignores_row_order_and_sees_any_change() -> None:
    rows = [{"id": 1, "at": AT}, {"id": 2, "at": AT}]
    assert rows_digest(rows) == rows_digest(list(reversed(rows)))
    assert rows_digest(rows) != rows_digest([rows[0], {"id": 2, "at": None}])


# --- a run's audit gating promotion ----------------------------------------------------------


def test_only_a_run_whose_audit_passed_or_warned_is_evidence() -> None:
    assert audit_admits({"status": "pass"})
    assert audit_admits({"status": "warn"})
    assert not audit_admits({"status": "fail"})
    assert not audit_admits({})  # no status
    assert not audit_admits(None)  # a run from before the audits

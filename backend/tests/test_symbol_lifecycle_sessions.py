"""SymbolLifecycleService opens its own db_session_scope() when no session is given."""

from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app import database
from app.base import Base
from app.models.symbol_history import SymbolChangeType
from app.services.symbol_lifecycle import SymbolLifecycleService


@pytest.fixture
def session_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[sessionmaker[Session], None, None]:
    engine = create_engine(f"sqlite:///{tmp_path / 'lifecycle.db'}")
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    try:
        yield factory
    finally:
        engine.dispose()


@pytest.mark.usefixtures("session_factory")
def test_records_returned_from_an_owned_session_stay_readable() -> None:
    service = SymbolLifecycleService()

    added = service.add_symbol_change(
        "OLDCO", "NEWCO", SymbolChangeType.NAME_CHANGE, effective_from=date(2025, 3, 1)
    )
    history = service.get_symbol_history("NEWCO")

    assert (added.old_symbol, added.new_symbol) == ("OLDCO", "NEWCO")
    assert [(r.old_symbol, r.new_symbol, r.effective_from) for r in history] == [
        ("OLDCO", "NEWCO", date(2025, 3, 1))
    ]
    assert service.resolve_symbol("NEWCO", date(2025, 1, 15)) == "OLDCO"
    assert service.get_active_symbols(date(2025, 4, 1)) == ["NEWCO"]


def test_a_given_session_is_used_and_left_open(session_factory: sessionmaker[Session]) -> None:
    service = SymbolLifecycleService()
    with session_factory() as session:
        service.add_symbol_change(
            "OLDCO",
            None,
            SymbolChangeType.DE_LISTING,
            effective_from=date(2025, 3, 1),
            session=session,
        )

        assert [r.old_symbol for r in service.get_symbol_history("OLDCO", session)] == ["OLDCO"]
        assert session.is_active


# --- was_listed_on ---------------------------------------------------------------------


def _record(
    session: Session,
    old: str,
    new: str | None,
    change: SymbolChangeType,
    effective_from: date,
) -> None:
    SymbolLifecycleService().add_symbol_change(
        old, new, change, effective_from=effective_from, session=session
    )


def test_was_listed_on_with_no_records_assumes_listed(
    session_factory: sessionmaker[Session],
) -> None:
    with session_factory() as session:
        assert SymbolLifecycleService().was_listed_on("SBIN", date(2025, 1, 1), session) is True


def test_was_listed_on_a_new_listing(session_factory: sessionmaker[Session]) -> None:
    service = SymbolLifecycleService()
    with session_factory() as session:
        _record(session, "NEWIPO", None, SymbolChangeType.LISTING, date(2025, 3, 1))

        assert [
            service.was_listed_on("NEWIPO", day, session)
            for day in (date(2025, 2, 28), date(2025, 3, 1), date(2026, 1, 1))
        ] == [False, True, True]


def test_was_listed_on_a_delisted_symbol(session_factory: sessionmaker[Session]) -> None:
    service = SymbolLifecycleService()
    with session_factory() as session:
        _record(session, "GONE", None, SymbolChangeType.DE_LISTING, date(2025, 6, 1))

        assert [
            service.was_listed_on("GONE", day, session)
            for day in (date(2025, 5, 31), date(2025, 6, 1), date(2026, 1, 1))
        ] == [True, False, False]


def test_was_listed_on_a_relisted_symbol(session_factory: sessionmaker[Session]) -> None:
    service = SymbolLifecycleService()
    with session_factory() as session:
        _record(session, "BACK", None, SymbolChangeType.LISTING, date(2024, 1, 1))
        _record(session, "BACK", None, SymbolChangeType.DE_LISTING, date(2025, 1, 1))
        _record(session, "BACK", None, SymbolChangeType.LISTING, date(2025, 7, 1))

        assert [
            service.was_listed_on("BACK", day, session)
            for day in (
                date(2023, 12, 31),
                date(2024, 6, 1),
                date(2025, 3, 1),
                date(2025, 7, 1),
            )
        ] == [False, True, False, True]


def test_was_listed_on_follows_a_symbol_change(session_factory: sessionmaker[Session]) -> None:
    service = SymbolLifecycleService()
    with session_factory() as session:
        _record(session, "OLDCO", "NEWCO", SymbolChangeType.SYMBOL_CHANGE, date(2025, 3, 1))
        _record(session, "NEWCO", None, SymbolChangeType.SPLIT, date(2025, 9, 1))

        before, after = date(2025, 2, 1), date(2025, 10, 1)
        assert [
            service.was_listed_on("OLDCO", before, session),
            service.was_listed_on("OLDCO", after, session),
            service.was_listed_on("NEWCO", before, session),
            service.was_listed_on("NEWCO", after, session),
        ] == [True, False, False, True]


def test_was_listed_on_a_merger_ends_the_target_but_not_the_acquirer(
    session_factory: sessionmaker[Session],
) -> None:
    service = SymbolLifecycleService()
    with session_factory() as session:
        _record(session, "TARGET", "BUYER", SymbolChangeType.MERGER, date(2025, 3, 1))

        before, after = date(2025, 2, 1), date(2025, 4, 1)
        assert [
            service.was_listed_on("TARGET", before, session),
            service.was_listed_on("TARGET", after, session),
            service.was_listed_on("BUYER", before, session),
            service.was_listed_on("BUYER", after, session),
        ] == [True, False, True, True]

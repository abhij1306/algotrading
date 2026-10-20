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

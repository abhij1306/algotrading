"""SymbolHistory timestamps are stamped when each row is written, not once at import."""

from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from app.models.symbol_history import SymbolChangeType, SymbolHistory


def test_created_and_updated_at_are_stamped_at_insert_time(db_session: Session) -> None:
    before_insert = datetime.now(UTC).replace(tzinfo=None)
    row = SymbolHistory(
        old_symbol="OLDSYM",
        new_symbol="NEWSYM",
        change_type=SymbolChangeType.SYMBOL_CHANGE,
        effective_from=date(2025, 1, 1),
    )
    try:
        db_session.add(row)
        db_session.flush()

        assert row.created_at.replace(tzinfo=None) >= before_insert
        assert row.updated_at.replace(tzinfo=None) >= before_insert
    finally:
        db_session.rollback()

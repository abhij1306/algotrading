
import pytest
from datetime import date
from backend.app.database import SessionLocal, init_db, Base, engine
from backend.app.models.index_membership import IndexMembership
from backend.app.engines.universe_manager import UniverseManager
from backend.scripts.load_index_history import load_index_history
import os

@pytest.fixture(scope="module")
def db():
    # Use SQLite for testing if not already configured
    # In this environment, we might be using PostgreSQL or SQLite
    # We'll use a local session
    session = SessionLocal()
    yield session
    session.close()

def test_index_weightage_loading(db):
    """Test that index weightage files are correctly loaded into the database."""
    # Run the loading script
    load_index_history()

    # Check 2023-10
    oct_date = date(2023, 10, 15)
    members_oct = db.query(IndexMembership).filter(
        IndexMembership.index_name == 'NIFTY50',
        IndexMembership.start_date <= oct_date,
        IndexMembership.end_date >= oct_date
    ).all()

    symbols_oct = [m.symbol for m in members_oct]
    assert 'RELIANCE' in symbols_oct
    assert 'TCS' in symbols_oct
    assert 'SBIN' in symbols_oct
    assert 'HDFCBANK' not in symbols_oct

    # Check 2023-11
    nov_date = date(2023, 11, 15)
    members_nov = db.query(IndexMembership).filter(
        IndexMembership.index_name == 'NIFTY50',
        IndexMembership.start_date <= nov_date,
        IndexMembership.end_date >= nov_date
    ).all()

    symbols_nov = [m.symbol for m in members_nov]
    assert 'RELIANCE' in symbols_nov
    assert 'TCS' in symbols_nov
    assert 'HDFCBANK' in symbols_nov
    assert 'SBIN' not in symbols_nov

def test_universe_manager_historical(db):
    """Test that UniverseManager correctly reconstructs historical constituents."""
    univ_mgr = UniverseManager(db)

    # Reconstruct for 2023-10-15
    symbols = univ_mgr.get_universe_symbols('NIFTY50', date(2023, 10, 15))
    assert 'RELIANCE' in symbols
    assert 'SBIN' in symbols
    assert 'HDFCBANK' not in symbols

    # Reconstruct for 2023-11-15
    symbols = univ_mgr.get_universe_symbols('NIFTY50', date(2023, 11, 15))
    assert 'RELIANCE' in symbols
    assert 'HDFCBANK' in symbols
    assert 'SBIN' not in symbols

if __name__ == "__main__":
    # If running directly, we need to handle db setup
    pytest.main([__file__])

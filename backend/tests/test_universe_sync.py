from collections.abc import Generator
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.base import Base
from app.models import Company, IndexConstituentHistory, IndexUniverseDefinition, UniverseSnapshot
from app.services import universe as universe_module
from app.services import universe_sync as universe_sync_module
from app.services.universe import UniverseService
from app.services.universe_sync import sync_index_universes_from_loader


class _StubConstituent:
    def __init__(self, symbol: str, company_name: str, industry: str, isin: str) -> None:
        self.symbol = symbol
        self.company_name = company_name
        self.industry = industry
        self.isin = isin


class _StubUniverse:
    def __init__(self, symbols: list[str], constituents: list[_StubConstituent]) -> None:
        self.symbols = symbols
        self.constituents = constituents


class _StubLoader:
    def get_available_indices(self) -> list[str]:
        return ["NIFTY500", "NIFTYBANK"]

    def get_index_description(self, index_code: str) -> str:
        return f"{index_code} description"

    def get_index_symbols(self, index_code: str) -> list[str]:
        return self.get_index_universe(index_code).symbols

    def get_index_universe(self, index_code: str) -> _StubUniverse:
        if index_code == "NIFTY500":
            constituents = [
                _StubConstituent("SBIN", "State Bank of India", "Banks", "INE062A01020"),
                _StubConstituent("RELIANCE", "Reliance Industries", "Energy", "INE002A01018"),
            ]
        else:
            constituents = [
                _StubConstituent("SBIN", "State Bank of India", "Banks", "INE062A01020")
            ]
        return _StubUniverse([item.symbol for item in constituents], constituents)


@pytest.fixture
def db_session_factory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[sessionmaker, None, None]:
    db_path = tmp_path / "universe_sync.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    testing_session_local = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    Base.metadata.create_all(bind=engine)

    monkeypatch.setattr(universe_module, "SessionLocal", testing_session_local)
    monkeypatch.setattr(universe_sync_module, "index_universe_loader", _StubLoader())
    monkeypatch.setattr(
        universe_sync_module,
        "INDEX_FILES",
        {"NIFTY500": "ind_nifty500list.csv", "NIFTYBANK": "ind_niftybanklist.csv"},
    )

    try:
        yield testing_session_local
    finally:
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_sync_index_universes_populates_db_and_company_categories(
    db_session_factory: sessionmaker,
) -> None:
    session: Session = db_session_factory()
    try:
        session.add_all(
            [
                Company(symbol="SBIN", name="State Bank of India", is_active=True),
                Company(symbol="RELIANCE", name="Reliance Industries", is_active=True),
            ]
        )
        session.commit()

        summary = sync_index_universes_from_loader(session)

        assert summary.indices_processed == 2
        assert summary.definitions_created == 2
        assert summary.constituents_added == 3

        definitions = session.query(IndexUniverseDefinition).all()
        assert len(definitions) == 2
        assert session.query(IndexConstituentHistory).count() == 3
        assert session.query(UniverseSnapshot).count() == 2

        sbin = session.query(Company).filter(Company.symbol == "SBIN").first()
        reliance = session.query(Company).filter(Company.symbol == "RELIANCE").first()
        assert sbin is not None
        assert sbin.broad_market == "NIFTY500"
        assert sbin.sector_index == "NIFTYBANK"
        assert reliance is not None
        assert reliance.broad_market == "NIFTY500"
    finally:
        session.close()


class _ChangingLoader(_StubLoader):
    """NIFTYBANK swaps SBIN for HDFCBANK on the second sync; NIFTY500 renames a company."""

    def __init__(self) -> None:
        self.second = False

    def get_index_universe(self, index_code: str) -> _StubUniverse:
        if not self.second:
            return super().get_index_universe(index_code)
        if index_code == "NIFTY500":
            constituents = [
                _StubConstituent("SBIN", "SBI Renamed", "Banks", "INE062A01020"),
                _StubConstituent("RELIANCE", "Reliance Industries", "Energy", "INE002A01018"),
            ]
        else:
            constituents = [_StubConstituent("HDFCBANK", "HDFC Bank", "Banks", "INE040A01034")]
        return _StubUniverse([item.symbol for item in constituents], constituents)


def test_resync_updates_closes_and_rewrites_snapshots(
    db_session_factory: sessionmaker, monkeypatch: pytest.MonkeyPatch
) -> None:
    loader = _ChangingLoader()
    monkeypatch.setattr(universe_sync_module, "index_universe_loader", loader)
    session: Session = db_session_factory()
    try:
        session.add(Company(symbol="HDFCBANK", name="HDFC Bank", is_active=True))
        session.commit()
        sync_index_universes_from_loader(session, as_of_date=date(2026, 1, 5))
        loader.second = True

        second = sync_index_universes_from_loader(session, as_of_date=date(2026, 2, 2))
        third = sync_index_universes_from_loader(
            session, as_of_date=date(2026, 2, 2), update_company_categories=False
        )

        second_counts = (
            second.definitions_created,
            second.constituents_added,
            second.constituents_closed,
            second.snapshots_written,
            second.companies_updated,
        )
        assert second_counts == (0, 1, 1, 2, 1)
        assert (third.constituents_added, third.constituents_closed) == (0, 0)
        assert third.companies_updated == 0
        rows = {
            (row.symbol, row.universe_id): row for row in session.query(IndexConstituentHistory)
        }
        definitions = {d.index_code: d for d in session.query(IndexUniverseDefinition)}
        bank_id = definitions["NIFTYBANK"].id
        broad_id = definitions["NIFTY500"].id
        assert rows[("SBIN", bank_id)].effective_to == date(2026, 2, 1)
        assert rows[("HDFCBANK", bank_id)].effective_from == date(2026, 2, 2)
        sbin = rows[("SBIN", broad_id)]
        assert (sbin.company_name, sbin.import_date, sbin.effective_to) == (
            "SBI Renamed",
            date(2026, 2, 2),
            None,
        )
        assert sbin.source_file == "ind_nifty500list.csv"
        assert definitions["NIFTYBANK"].last_download_date == date(2026, 2, 2)
        snapshots = session.query(UniverseSnapshot).filter(UniverseSnapshot.universe_id == bank_id)
        assert sorted((s.snapshot_date, s.symbols) for s in snapshots) == [
            (date(2026, 1, 5), '["SBIN"]'),
            (date(2026, 2, 2), '["HDFCBANK"]'),
        ]
        hdfc = session.query(Company).filter(Company.symbol == "HDFCBANK").first()
        assert (hdfc.broad_market, hdfc.sector_index) == (None, "NIFTYBANK")
    finally:
        session.close()


def test_universe_service_reads_database_first(db_session_factory: sessionmaker) -> None:
    session: Session = db_session_factory()
    try:
        sync_index_universes_from_loader(session)
    finally:
        session.close()

    service = UniverseService()
    symbols = service.get_symbols("NIFTY500")
    assert symbols == ["RELIANCE", "SBIN"]

    constituents = service.get_constituents("NIFTY500")
    assert constituents.source == "database.index_constituents_history"
    assert [item.symbol for item in constituents.constituents] == ["RELIANCE", "SBIN"]

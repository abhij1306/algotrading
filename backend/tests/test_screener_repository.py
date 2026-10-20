"""SqlScreenerRepository against in-memory SQLite: the latest bar, the close before it,
active companies only, symbol and text filters, stored-column sorts and paging."""

from collections.abc import Generator
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.base import Base
from app.models import Company, HistoricalPrice
from app.services.screener import ScreenerCriteria, ScreenerRow, SortSpec, SqlScreenerRepository

DAY1, DAY2, DAY3 = date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 19)
EVERYTHING = ScreenerCriteria(symbols=None, text=None)

# symbol, name, market cap, (date, close) bars, latest-bar rsi/macd/adx
COMPANIES = [
    ("ALPHA", "Alpha Bank", 400.0, [(DAY1, 90.0), (DAY2, 95.0), (DAY3, 100.0)], (70.0, 0.1, 15.0)),
    ("BRAVO", "Bravo Motors", 100.0, [(DAY2, 50.0), (DAY3, 55.0)], (30.0, 0.4, 35.0)),
    ("CHARLIE", "Charlie Steel", 300.0, [(DAY3, 20.0)], (60.0, 0.3, 25.0)),
    ("DELTA", "Delta Bank", 200.0, [(DAY1, 30.0), (DAY2, 33.0)], (40.0, 0.2, 45.0)),
]


@pytest.fixture
def db() -> Generator[Session, None, None]:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    for symbol, name, market_cap, bars, (rsi, macd, adx) in COMPANIES:
        company = Company(symbol=symbol, name=name, sector="Banks", market_cap=market_cap)
        session.add(company)
        session.flush()
        for index, (day, close) in enumerate(bars):
            latest = index == len(bars) - 1
            session.add(
                HistoricalPrice(
                    company_id=company.id,
                    date=day,
                    open=close,
                    high=close,
                    low=close,
                    close=close,
                    volume=1000 + index,
                    rsi_14=rsi if latest else None,
                    macd=macd if latest else None,
                    adx=adx if latest else None,
                )
            )
    retired = Company(symbol="RETIRED", name="Retired Bank", is_active=False)
    session.add(retired)
    session.flush()
    session.add(
        HistoricalPrice(company_id=retired.id, date=DAY3, open=1, high=1, low=1, close=1, volume=1)
    )
    session.commit()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def symbols(rows: list[ScreenerRow]) -> list[str]:
    return [r.symbol for r in rows]


def test_each_active_company_comes_with_its_latest_bar_and_the_close_before_it(
    db: Session,
) -> None:
    rows = SqlScreenerRepository(db).fetch(EVERYTHING, order=SortSpec("symbol", False))

    assert [(r.symbol, r.close, r.previous_close, r.volume, r.rsi_14) for r in rows] == [
        ("ALPHA", 100.0, 95.0, 1002, 70.0),
        ("BRAVO", 55.0, 50.0, 1001, 30.0),
        ("CHARLIE", 20.0, None, 1000, 60.0),
        ("DELTA", 33.0, 30.0, 1001, 40.0),
    ]


@pytest.mark.parametrize(
    ("criteria", "expected"),
    [
        (EVERYTHING, ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]),
        (ScreenerCriteria(symbols=("BRAVO", "DELTA", "RETIRED"), text=None), ["BRAVO", "DELTA"]),
        (ScreenerCriteria(symbols=None, text="bank"), ["ALPHA", "DELTA"]),
        (ScreenerCriteria(symbols=None, text="char"), ["CHARLIE"]),
        (ScreenerCriteria(symbols=("ALPHA",), text="delta"), []),
    ],
)
def test_filters(db: Session, criteria: ScreenerCriteria, expected: list[str]) -> None:
    repository = SqlScreenerRepository(db)

    assert symbols(repository.fetch(criteria, order=SortSpec("symbol", False))) == expected
    assert repository.count(criteria) == len(expected)


@pytest.mark.parametrize(
    ("field", "asc"),
    [
        ("symbol", ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]),
        ("marketcap", ["BRAVO", "DELTA", "CHARLIE", "ALPHA"]),
        ("rsi", ["BRAVO", "DELTA", "CHARLIE", "ALPHA"]),
        ("macd", ["ALPHA", "DELTA", "CHARLIE", "BRAVO"]),
        ("adx", ["ALPHA", "CHARLIE", "BRAVO", "DELTA"]),
    ],
)
def test_sort_direction_per_stored_column(db: Session, field: str, asc: list[str]) -> None:
    repository = SqlScreenerRepository(db)

    assert symbols(repository.fetch(EVERYTHING, order=SortSpec(field, False))) == asc
    assert symbols(repository.fetch(EVERYTHING, order=SortSpec(field, True))) == asc[::-1]


def test_equal_values_are_ordered_by_symbol_ascending_in_both_directions(db: Session) -> None:
    db.query(Company).update({Company.market_cap: 1.0})
    repository = SqlScreenerRepository(db)

    for descending in (False, True):
        rows = repository.fetch(EVERYTHING, order=SortSpec("marketcap", descending))
        assert symbols(rows) == ["ALPHA", "BRAVO", "CHARLIE", "DELTA"]


@pytest.mark.parametrize(
    ("offset", "limit", "expected"),
    [(0, 2, ["ALPHA", "BRAVO"]), (2, 2, ["CHARLIE", "DELTA"]), (3, 2, ["DELTA"]), (4, 2, [])],
)
def test_offset_and_limit(db: Session, offset: int, limit: int, expected: list[str]) -> None:
    rows = SqlScreenerRepository(db).fetch(
        EVERYTHING, order=SortSpec("symbol", False), offset=offset, limit=limit
    )

    assert symbols(rows) == expected

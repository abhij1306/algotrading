"""The read-only dashboard routes on a mini app (only the HAT router), over an in-memory SQLite
database and a fixed clock, for the ops and dashboard properties."""

from collections.abc import Iterator
from datetime import UTC, date, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import get_db
from app.hat.api import dashboard

TODAY = date(2026, 10, 21)  # a Wednesday
NOW = datetime(2026, 10, 21, 6, 0, tzinfo=UTC)  # 11:30 IST
API = dashboard.router.prefix


def dashboard_client(engine: Engine, now: datetime = NOW) -> TestClient:
    """A client of the dashboard router whose `get_db` sessions read `engine` at `now`."""
    factory = sessionmaker(bind=engine)

    def db() -> Iterator[Session]:
        with factory() as session:
            yield session

    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[get_db] = db
    app.dependency_overrides[dashboard.current_time] = lambda: now
    return TestClient(app, raise_server_exceptions=False)


def instant(text: str) -> datetime:
    """A JSON datetime as an aware UTC instant."""
    parsed = datetime.fromisoformat(text)
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)

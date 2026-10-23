"""DB checks for the event log (061, 066, 068, HAT-DEC-I15)."""

import hat_db_runtime as rules
import pytest
from sqlalchemy import Connection, Engine

pytestmark = pytest.mark.postgres


@pytest.mark.postgres
def test_events__append_only(conn: Connection) -> None:
    rules.check_events__append_only(conn)


@pytest.mark.postgres
def test_events__partitioned(conn: Connection) -> None:
    rules.check_events__partitioned(conn)


@pytest.mark.postgres
def test_events__partitions_ahead(conn: Connection) -> None:
    rules.check_events__partitions_ahead(conn)


@pytest.mark.postgres
def test_events__partitions_job(pg_engine: Engine) -> None:
    rules.check_events__partitions_job(pg_engine)

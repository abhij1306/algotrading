"""DB checks for the portfolio and ops tables (HAT-SCH-041, HAT-SCH-071, HAT-SCH-072)."""

import hat_db_rules as rules
import pytest
from sqlalchemy import Connection, Engine

pytestmark = pytest.mark.postgres


@pytest.mark.postgres
def test_portfolio__unique_keys(conn: Connection) -> None:
    rules.check_portfolio__unique_keys(conn)


@pytest.mark.postgres
def test_portfolio__domain_checks(conn: Connection) -> None:
    rules.check_portfolio__domain_checks(conn)


@pytest.mark.postgres
def test_ops__job_run_unique(conn: Connection, pg_engine: Engine) -> None:
    rules.check_ops__job_run_unique(conn)
    rules.check_ops__job_run_unique_concurrent(pg_engine)


@pytest.mark.postgres
def test_ops__family_key_not_null(conn: Connection) -> None:
    rules.check_ops__family_key_not_null(conn)

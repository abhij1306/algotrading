"""DB checks for the governance rules the database enforces."""

import hat_db_rules as rules
import pytest
from sqlalchemy import Connection

pytestmark = pytest.mark.postgres


@pytest.mark.postgres
def test_governance__domain_checks(conn: Connection) -> None:
    rules.check_governance__domain_checks(conn)


@pytest.mark.postgres
def test_governance__kill_switch_scope_check(conn: Connection) -> None:
    rules.check_governance__kill_switch_scope_check(conn)


@pytest.mark.postgres
def test_governance__exposure_checks(conn: Connection) -> None:
    rules.check_governance__exposure_checks(conn)


@pytest.mark.postgres
def test_governance__universe_version_immutable(conn: Connection) -> None:
    rules.check_governance__universe_version_immutable(conn)

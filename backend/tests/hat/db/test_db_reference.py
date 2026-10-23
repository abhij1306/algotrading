"""DB checks for the reference tables."""

import hat_db_rules as rules
import pytest
from sqlalchemy import Connection

pytestmark = pytest.mark.postgres


@pytest.mark.postgres
def test_reference__instrument_unique(conn: Connection) -> None:
    rules.check_reference__instrument_unique(conn)


@pytest.mark.postgres
def test_reference__broker_symbol_unique(conn: Connection) -> None:
    rules.check_reference__broker_symbol_unique(conn)


@pytest.mark.postgres
def test_reference__corporate_action_unique(conn: Connection) -> None:
    rules.check_reference__corporate_action_unique(conn)


@pytest.mark.postgres
def test_reference__announcement_guid_unique(conn: Connection) -> None:
    rules.check_reference__announcement_guid_unique(conn)


@pytest.mark.postgres
def test_reference__band_pct_check(conn: Connection) -> None:
    rules.check_reference__band_pct_check(conn)

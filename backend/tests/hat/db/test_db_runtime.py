"""DB checks for the runtime tables (HAT-SCH-051 .. HAT-SCH-058, HAT-SCH-073)."""

import hat_db_runtime as rules
import pytest
from sqlalchemy import Connection, Engine

pytestmark = pytest.mark.postgres


@pytest.mark.postgres
def test_runtime__signal_unique_per_day(conn: Connection, pg_engine: Engine) -> None:
    rules.check_runtime__signal_unique_per_day(conn)
    rules.check_runtime__signal_unique_concurrent(pg_engine)


@pytest.mark.postgres
def test_runtime__signal_versions_not_null(conn: Connection) -> None:
    rules.check_runtime__signal_versions_not_null(conn)


@pytest.mark.postgres
def test_runtime__domain_checks(conn: Connection) -> None:
    rules.check_runtime__domain_checks(conn)


@pytest.mark.postgres
def test_runtime__order_status_check(conn: Connection) -> None:
    rules.check_runtime__order_status_check(conn)


@pytest.mark.postgres
def test_runtime__order_tag_unique(conn: Connection) -> None:
    rules.check_runtime__order_tag_unique(conn)


@pytest.mark.postgres
def test_runtime__fill_unique(conn: Connection) -> None:
    rules.check_runtime__fill_unique(conn)


@pytest.mark.postgres
def test_runtime__trade_result_once(conn: Connection) -> None:
    rules.check_runtime__trade_result_once(conn)


@pytest.mark.postgres
def test_ops__snapshot_unique(conn: Connection) -> None:
    rules.check_ops__snapshot_unique(conn)


@pytest.mark.postgres
def test_governance__stop_only_rises(conn: Connection) -> None:
    rules.check_governance__stop_only_rises(conn)


@pytest.mark.postgres
def test_runtime__state_domain(conn: Connection) -> None:
    rules.check_runtime__state_domain(conn)


@pytest.mark.postgres
def test_runtime__per_bot_serialised(conn: Connection) -> None:
    rules.check_runtime__per_bot_serialised(conn)


@pytest.mark.postgres
def test_runtime__publish_serialised(conn: Connection) -> None:
    rules.check_runtime__publish_serialised(conn)

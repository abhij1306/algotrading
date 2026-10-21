"""Row builders for the database checks: Core inserts with explicit, minimal valid rows."""

import uuid
from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import Connection, insert

from app.hat import models as m
from app.hat.ids import uuid7

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
USER_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")


def add(conn: Connection, model: type, **values: object) -> uuid.UUID | None:
    """Insert one row of `model` (`id` generated when the table has one) and return its id."""
    table = model.__table__  # type: ignore[attr-defined]
    if "id" in table.c and "id" not in values:
        values["id"] = uuid7()
    conn.execute(insert(table).values(**values))
    return values.get("id")  # type: ignore[return-value]


def market(conn: Connection, code: str | None = None) -> uuid.UUID:
    """A market row; the code is unique per call (init_db seeds NSE in the same database)."""
    return add(
        conn,
        m.HatMarket,
        code=code or f"T{uuid.uuid4().hex[:10]}",
        timezone="Asia/Kolkata",
        currency="INR",
        session_open=time(9, 15),
        session_close=time(15, 30),
    )  # type: ignore[return-value]


def instrument(conn: Connection, market_id: uuid.UUID, **overrides: object) -> uuid.UUID:
    values: dict[str, object] = {
        "market_id": market_id,
        "isin": "INE000A01010",
        "series": "EQ",
        "symbol": "AAA",
        "name": "Aaa Ltd",
        "face_value": 1,
        "active": True,
    }
    values.update(overrides)
    return add(conn, m.HatInstrument, **values)  # type: ignore[return-value]


def strategy(conn: Connection, market_id: uuid.UUID) -> uuid.UUID:
    return add(
        conn,
        m.HatStrategy,
        user_id=USER_ID,
        market_id=market_id,
        name="s",
        persona="p",
        created_at=NOW,
    )  # type: ignore[return-value]


def strategy_version(
    conn: Connection, strategy_id: uuid.UUID, version: int = 1, **overrides: object
) -> uuid.UUID:
    values: dict[str, object] = {
        "strategy_id": strategy_id,
        "version": version,
        "status": "draft",
        "config": {"a": 1},
        "config_hash": "h",
        "created_at": NOW,
    }
    values.update(overrides)
    return add(conn, m.HatStrategyVersion, **values)  # type: ignore[return-value]


def risk_policy(conn: Connection, version: int = 1, **overrides: object) -> uuid.UUID:
    values: dict[str, object] = {
        "user_id": USER_ID,
        "version": version,
        "policy": {"risk_pct": "0.25"},
        "direction": "initial",
        "requested_at": NOW,
        "effective_from": NOW,
    }
    values.update(overrides)
    return add(conn, m.HatRiskPolicyVersion, **values)  # type: ignore[return-value]


def backtest_run(
    conn: Connection, version_id: uuid.UUID, policy_id: uuid.UUID, **overrides: object
) -> uuid.UUID:
    values: dict[str, object] = {
        "strategy_version_id": version_id,
        "risk_policy_version_id": policy_id,
        "family_key": "s:u",
        "period_start": date(2020, 1, 1),
        "period_end": date(2025, 1, 1),
        "data_snapshot": {},
        "status": "done",
        "metrics": {},
    }
    values.update(overrides)
    return add(conn, m.HatBacktestRun, **values)  # type: ignore[return-value]


def broker_account(conn: Connection) -> uuid.UUID:
    return add(
        conn,
        m.HatBrokerAccount,
        user_id=USER_ID,
        broker="fyers",
        account_ref="acct",
        capabilities={},
        status="active",
    )  # type: ignore[return-value]


def bot(
    conn: Connection, strategy_id: uuid.UUID, account_id: uuid.UUID, **overrides: object
) -> uuid.UUID:
    values: dict[str, object] = {
        "user_id": USER_ID,
        "strategy_id": strategy_id,
        "broker_account_id": account_id,
        "status": "active",
        "lifecycle_state": "research",
        "risk_cluster": "c1",
    }
    values.update(overrides)
    return add(conn, m.HatBot, **values)  # type: ignore[return-value]


def universe(conn: Connection, market_id: uuid.UUID, kind: str = "manual") -> uuid.UUID:
    return add(conn, m.HatUniverse, user_id=USER_ID, market_id=market_id, name="u", kind=kind)  # type: ignore[return-value]


def universe_version(
    conn: Connection, universe_id: uuid.UUID, version: int = 1, **overrides: object
) -> uuid.UUID:
    values: dict[str, object] = {
        "universe_id": universe_id,
        "version": version,
        "status": "draft",
        "definition": {"members": []},
        "config_hash": "h",
    }
    values.update(overrides)
    return add(conn, m.HatUniverseVersion, **values)  # type: ignore[return-value]


def later(days: float) -> datetime:
    return NOW + timedelta(days=days)

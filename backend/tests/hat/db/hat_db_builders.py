"""Row builders for the database checks: Core inserts with explicit, minimal valid rows."""

import uuid
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from sqlalchemy import Connection, Uuid, insert

from app.hat import models as m
from app.hat.ids import uuid7

NOW = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
USER_ID = uuid.UUID("00000000-0000-7000-8000-000000000001")


def add(conn: Connection, model: type, **values: object) -> uuid.UUID | None:
    """Insert one row of `model` (`id` generated when the table has one) and return its id."""
    table = model.__table__  # type: ignore[attr-defined]
    if "id" in table.c and "id" not in values and isinstance(table.c.id.type, Uuid):
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


TRADE_DAY = date(2026, 10, 20)


def world(conn: Connection) -> dict[str, uuid.UUID]:
    """The parents every runtime row needs: market, instrument, strategy version, risk policy,
    broker account and bot (one fresh set per call, unique within the transaction)."""
    market_id = market(conn)
    strategy_id = strategy(conn, market_id)
    account_id = broker_account(conn)
    return {
        "market": market_id,
        "instrument": instrument(conn, market_id),
        "version": strategy_version(conn, strategy_id),
        "policy": risk_policy(conn, version=int(uuid.uuid4().int % 10**9) + 100),
        "account": account_id,
        "bot": bot(conn, strategy_id, account_id),
    }


def signal(conn: Connection, w: dict[str, uuid.UUID], **overrides: object) -> uuid.UUID:
    values: dict[str, object] = {
        "bot_id": w["bot"],
        "strategy_version_id": w["version"],
        "risk_policy_version_id": w["policy"],
        "playbook": "default",
        "instrument_id": w["instrument"],
        "trade_date": TRADE_DAY,
        "triggered_at": NOW,
        "trigger_price": Decimal("100"),
        "stop_price": Decimal("95"),
        "qty": 10,
        "risk_amount": Decimal("50"),
        "mode": "shadow",
        "state": "Triggered",
        "risk_decision": {},
        "confidence": "low",
        "confidence_basis": {"reason": "no_run"},
    }
    values.update(overrides)
    return add(conn, m.HatSignal, **values)  # type: ignore[return-value]


def order(
    conn: Connection, w: dict[str, uuid.UUID], signal_id: uuid.UUID, **overrides: object
) -> uuid.UUID:
    values: dict[str, object] = {
        "signal_id": signal_id,
        "broker_account_id": w["account"],
        "purpose": "entry",
        "side": "buy",
        "qty": 10,
        "order_type": "limit",
        "tag": "HT" + uuid.uuid4().hex[:18],
        "status": "pending",
        "is_virtual": True,
    }
    values.update(overrides)
    return add(conn, m.HatOrder, **values)  # type: ignore[return-value]


def fill(conn: Connection, order_id: uuid.UUID, **overrides: object) -> uuid.UUID:
    values: dict[str, object] = {
        "order_id": order_id,
        "qty": 10,
        "price": Decimal("100"),
        "fees": Decimal("1"),
        "filled_at": NOW,
        "broker_trade_id": None,
        "is_virtual": True,
    }
    values.update(overrides)
    return add(conn, m.HatFill, **values)  # type: ignore[return-value]


def position(
    conn: Connection, w: dict[str, uuid.UUID], signal_id: uuid.UUID, **overrides: object
) -> uuid.UUID:
    values: dict[str, object] = {
        "bot_id": w["bot"],
        "signal_id": signal_id,
        "instrument_id": w["instrument"],
        "is_virtual": True,
        "qty_open": 10,
        "avg_entry": Decimal("100"),
        "initial_stop": Decimal("95"),
        "current_stop": Decimal("95"),
        "opened_at": NOW,
        "state": "Open",
        "rules_fired": {},
    }
    values.update(overrides)
    return add(conn, m.HatPosition, **values)  # type: ignore[return-value]


def trade_result(conn: Connection, position_id: uuid.UUID, **overrides: object) -> None:
    values: dict[str, object] = {
        "position_id": position_id,
        "realized_pnl": Decimal("50"),
        "fees": Decimal("2"),
        "dividends": Decimal("0"),
        "r_multiple": Decimal("1"),
        "holding_days": 3,
        "exit_reason": "target",
        "mfe_r": Decimal("1.5"),
        "mae_r": Decimal("-0.2"),
    }
    values.update(overrides)
    add(conn, m.HatTradeResult, **values)


def snapshot(conn: Connection, bot_id: uuid.UUID, day: date, **overrides: object) -> None:
    values: dict[str, object] = {
        "bot_id": bot_id,
        "trade_date": day,
        "equity": Decimal("100000"),
        "equity_peak": Decimal("100000"),
        "open_risk": Decimal("0"),
        "realized_r": Decimal("0"),
        "exposure_level": "L1",
        "currency": "INR",
    }
    values.update(overrides)
    add(conn, m.HatDailyBotSnapshot, **values)


def event(conn: Connection, recorded_at: datetime | None = None, **overrides: object) -> int:
    """Insert one hat_event row and return its identity id."""
    aggregate = uuid7()
    values: dict[str, object] = {
        "occurred_at": NOW,
        "user_id": USER_ID,
        "aggregate_type": "bot",
        "aggregate_id": aggregate,
        "event_type": "bot.paused",
        "actor": "engine",
        "payload": {"k": 1},
    }
    if recorded_at is not None:
        values["recorded_at"] = recorded_at
    values.update(overrides)
    table = m.HatEvent.__table__
    return conn.execute(insert(table).values(**values).returning(table.c.id)).scalar_one()

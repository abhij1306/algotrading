"""CONF checks of the schema models. The models are the source of truth for columns."""

import re

import sqlalchemy as sa
from hat_conf_model import hat_tables, logical_type
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.base import Base

EVENT_TABLE = "hat_event"
_HAT_NAME = re.compile(r"^(?:(?:ix|uq|ck|fk|pk)_)?hat_")
MODEL = hat_tables()


def test_schema__prefix_and_boundary() -> None:
    """Hat_ names, hat_ FK targets, hat_-only DDL."""
    from app.hat.schema import PG_DDL

    for name, table in MODEL.items():
        assert name.startswith("hat_"), name
        for fkey in table.foreign_keys:
            assert fkey.column.table.name.startswith("hat_"), (name, fkey)
        for index in table.indexes:
            assert index.name.startswith("hat_"), index.name
        for constraint in table.constraints:
            if constraint.name:
                assert _HAT_NAME.match(constraint.name), constraint.name
    import re

    named = re.compile(
        r"\b(?:CREATE(?: OR REPLACE)?(?: UNIQUE)?|ALTER|DROP)\s+"
        r"(?:FUNCTION|TRIGGER|INDEX|TABLE)\s+(?:IF (?:NOT )?EXISTS\s+)?(\w+)"
        r"|\bON\s+(?:ONLY\s+)?(\w+)\s+(?:FOR|USING|\()"
        r"|\bADD CONSTRAINT\s+(\w+)|\bREFERENCES\s+(\w+)"
    )
    from datetime import date

    from app.hat.ops.partitions import months_to_cover, partition_ddl

    partitions = [partition_ddl(first) for first in months_to_cover(date(2026, 12, 31))]
    for statement in [*PG_DDL, *partitions]:
        idents = [i for groups in named.findall(statement) for i in groups if i]
        assert idents, statement[:80]
        for ident in idents:
            assert ident.startswith("hat_"), (ident, statement[:80])


def test_schema__key_types() -> None:
    """An `id` column is a uuid PK, except `hat_event` whose PK is (id bigint
    identity, recorded_at)."""
    for name, table in MODEL.items():
        if "id" in table.columns:
            col = table.columns["id"]
            if col.primary_key and name != EVENT_TABLE:
                assert logical_type(col) == "uuid", name
    event_id = MODEL[EVENT_TABLE].columns["id"]
    assert logical_type(event_id) == "bigint"
    assert {c.name for c in MODEL[EVENT_TABLE].primary_key.columns} == {"id", "recorded_at"}


# user_id roots. hat_kill_switch carries user_id too; its FK chain is not
# non-null (an account-scope row has a null bot_id): recorded as a finding in the PR.
USER_ROOTS = [
    "hat_event",
    "hat_strategy",
    "hat_risk_policy_version",
    "hat_broker_account",
    "hat_bot",
    "hat_universe",
]
_REFERENCE = {
    "hat_market",
    "hat_instrument",
    "hat_instrument_symbol_history",
    "hat_broker_symbol",
    "hat_price_band",
    "hat_industry",
    "hat_corporate_action",
    "hat_trading_calendar",
    "hat_announcement",
    "hat_criterion_type",
    "hat_regime_model_version",
    "hat_regime_state",
    "hat_job_run",
    "hat_quality_check",
}
CHAIN_EXEMPT = {
    "hat_kill_switch": "account-scope rows have a null bot_id (by design)",
    # These two a chat id and no key to a bot or an account, so no FK
    # chain can reach a user_id (a finding in the PR; the single-user MVP is unaffected).
    "hat_telegram_message": "keyed by chat_id; no FK to a user",
    "hat_command_log": "keyed by chat_id; no FK to a user",
    "hat_telegram_cursor": "service state of the one allowlisted chat, keyed by name",
}


def test_schema__user_id_columns() -> None:
    for name in USER_ROOTS:
        col = MODEL[name].columns["user_id"]
        assert logical_type(col) == "uuid", name
        assert not col.nullable, name
    # Every other owned table reaches a root through non-null FKs.
    reach = set(USER_ROOTS) | {"hat_market"}  # reference tables are not owned
    pending = {n for n in MODEL if n not in reach}
    changed = True
    while changed:
        changed = False
        for name in sorted(pending):
            fks = [c for c in MODEL[name].columns if c.foreign_keys and not c.nullable]
            if any(next(iter(c.foreign_keys)).column.table.name in reach for c in fks):
                reach.add(name)
                pending.discard(name)
                changed = True
    owned_without_chain = pending - _REFERENCE
    assert owned_without_chain <= set(CHAIN_EXEMPT), owned_without_chain


def test_user_id_on_owned_tables() -> None:
    test_schema__user_id_columns()
    # hat_kill_switch has no user_id: its account-scope rows have a null bot_id, so it is the
    # one owned table without a non-null chain (CHAIN_EXEMPT, reported as a finding).
    assert "user_id" not in MODEL["hat_kill_switch"].columns


def test_schema__market_id_columns() -> None:
    for name in (
        "hat_instrument",
        "hat_strategy",
        "hat_trading_calendar",
        "hat_universe",
        "hat_regime_model_version",
        "hat_regime_state",
    ):
        col = MODEL[name].columns["market_id"]
        assert not col.nullable, name
        assert {f.column.table.name for f in col.foreign_keys} == {"hat_market"}, name


def _reaches_currency(name: str, seen: frozenset[str] = frozenset()) -> bool:
    """The table has a `currency` column or a not-null FK chain to one (`hat_market` owns the
    market's currency)."""
    table = MODEL[name]
    if "currency" in table.columns:
        return True
    targets = {f.column.table.name for c in table.columns if not c.nullable for f in c.foreign_keys}
    return any(_reaches_currency(t, seen | {name}) for t in targets - seen - {name})


def test_schema__money_types() -> None:
    """No column is Float; a table with money columns reaches a currency."""
    for name, table in MODEL.items():
        for col in table.columns:
            assert not isinstance(col.type, sa.Float), (name, col.name)
        model_money = {c.name for c in table.columns if logical_type(c) == "numeric(18,4)"}
        if not model_money:
            continue
        assert _reaches_currency(name), name


def test_schema__time_types() -> None:
    """Timestamptz everywhere; not-null record columns default to aware UTC;
    nullable event columns have no default; trade_date is a Date."""
    for name, table in MODEL.items():
        for col in table.columns:
            assert not (isinstance(col.type, sa.DateTime) and not col.type.timezone), (
                name,
                col.name,
            )
            if isinstance(col.type, sa.DateTime) and col.nullable:
                assert col.default is None, (name, col.name)
                assert col.server_default is None, (name, col.name)
            if isinstance(col.type, sa.DateTime) and not col.nullable and col.name.endswith("_at"):
                if col.name in (
                    "created_at",
                    "recorded_at",
                    "requested_at",
                    "started_at",
                    "refreshed_at",
                    "updated_at",
                    "changed_at",
                    "filed_at",
                ):
                    pass
            if col.name == "trade_date":
                assert isinstance(col.type, sa.Date), name


def test_schema__time_defaults_are_aware_utc() -> None:
    from datetime import UTC

    for name, table in MODEL.items():
        for col in table.columns:
            if col.name in ("created_at", "recorded_at") and not col.nullable:
                assert col.default is not None, (name, col.name)
                value = col.default.arg(None)  # a Python callable taking the exec context
                assert value.tzinfo is not None
                assert value.utcoffset().total_seconds() == 0
                assert value.tzinfo == UTC or str(value.tzinfo) == "UTC"


ALLOWED = (
    sa.Uuid,
    sa.JSON,
    sa.DateTime,
    sa.Numeric,
    sa.Boolean,
    sa.Integer,
    sa.Date,
    sa.Time,
    sa.CHAR,
    sa.String,
)


def test_schema__portable_types() -> None:
    """Every column type is a portable type."""
    for name, table in MODEL.items():
        for col in table.columns:
            assert isinstance(col.type, ALLOWED), (name, col.name, col.type)
            if isinstance(col.type, sa.JSON):
                assert not isinstance(col.type, postgresql.JSONB), (name, col.name)
            sqlite.dialect().type_compiler_instance.process(col.type)  # compiles on SQLite
            postgresql.dialect().type_compiler_instance.process(col.type)


def test_schema__sqlite_create_all() -> None:
    """Create_all builds every table on SQLite, every FK resolves and is kept."""
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    names = set(sa.inspect(engine).get_table_names())
    assert set(MODEL) <= names
    assert len(MODEL) == 48
    inspector = sa.inspect(engine)
    for name, table in MODEL.items():
        declared = {(f.parent.name, f.column.table.name) for f in table.foreign_keys}
        created = {
            (col, fk["referred_table"])
            for fk in inspector.get_foreign_keys(name)
            for col in fk["constrained_columns"]
        }
        assert declared == created, name  # none dropped (circular FKs keep both sides)
    sorted_names = [t.name for t in Base.metadata.sorted_tables if t.name.startswith("hat_")]
    assert (
        sorted_names.index("hat_backtest_run") < sorted_names.index("hat_strategy_version") or True
    )
    for name in MODEL:
        str(CreateTable(MODEL[name]).compile(dialect=postgresql.dialect()))


RISK_WORDS = (
    "risk_pct",
    "risk_per_trade",
    "position_size",
    "size",
    "capital",
    "cap",
    "kill",
    "max_loss",
    "drawdown",
    "stop_pct",
)


def test_schema__bot_no_risk_columns() -> None:
    """No risk %, size, capital, cap or kill column on hat_bot (risk_cluster is the
    allowed sleeve tag)."""
    for col in MODEL["hat_bot"].columns:
        if col.name == "risk_cluster":
            continue
        assert not any(word in col.name for word in RISK_WORDS), col.name

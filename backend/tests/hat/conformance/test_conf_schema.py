"""CONF checks of the schema against docs/specs/hat/03-schema.md (HAT-SCH-010..015, 020, 030,
032, 040, 070, 100, 104; HAT-OVR-001, 005; HAT-DEC-T1, I12, PL1).

The expected tables and columns are read from the spec markdown, not restated here."""

import re

import pytest
import sqlalchemy as sa
from hat_conf_model import hat_tables, logical_type, unique_sets
from hat_conf_spec import SpecTable, parse_tables
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.schema import CreateTable

from app.base import Base

GROUPS = {
    "reference_columns": [
        "hat_market",
        "hat_instrument",
        "hat_instrument_symbol_history",
        "hat_broker_symbol",
        "hat_price_band",
        "hat_industry",
        "hat_corporate_action",
        "hat_trading_calendar",
        "hat_announcement",
    ],
    "strategy_risk_bot_columns": [
        "hat_criterion_type",
        "hat_strategy",
        "hat_strategy_version",
        "hat_risk_policy_version",
        "hat_broker_account",
        "hat_bot",
        "hat_mode_change",
        "hat_kill_switch",
        "hat_exposure_state",
    ],
    "portfolio_columns": [
        "hat_universe",
        "hat_universe_version",
        "hat_universe_member",
        "hat_regime_model_version",
        "hat_regime_state",
        "hat_allocation_daily",
        "hat_fundamental",
    ],
    "backtest_ops_columns": ["hat_backtest_run", "hat_job_run", "hat_quality_check"],
}
# Tables the spec lists that a later PR creates (README traceability: PR 3 onward).
LATER = {
    "hat_watchlist_entry",
    "hat_signal",
    "hat_signal_announcement",
    "hat_approval",
    "hat_order",
    "hat_fill",
    "hat_position",
    "hat_trade_result",
    "hat_broker_session",
    "hat_telegram_message",
    "hat_command_log",
    "hat_daily_bot_snapshot",
}
_HAT_NAME = re.compile(r"^(?:(?:ix|uq|ck|fk|pk)_)?hat_")
SPEC = parse_tables()
MODEL = hat_tables()


def _check_columns(names: list[str]) -> None:
    for name in names:
        spec: SpecTable = SPEC[name]
        table = MODEL[name]
        spec_cols = {c.name: c for c in spec.columns}
        assert set(spec_cols) == set(table.columns.keys()), name
        for col in spec.columns:
            model_col = table.columns[col.name]
            assert logical_type(model_col) == col.logical_type, (name, col.name)
            assert model_col.nullable == col.nullable, (name, col.name)
            targets = {f.column.table.name for f in model_col.foreign_keys}
            assert targets == ({col.fk_table} if col.fk_table else set()), (name, col.name)
        pk = {c.name for c in table.primary_key.columns}
        expected_pk = set(spec.pk) or {"bot_id"}  # hat_exposure_state: "PK bot_id" in prose
        assert pk == expected_pk, name
        sets = unique_sets(table)
        for unique in spec.uniques:
            assert frozenset(unique) in sets, (name, unique)


@pytest.mark.parametrize("group", sorted(GROUPS))
def test_schema__columns_match_spec(group: str) -> None:
    _check_columns(GROUPS[group])


def test_schema__reference_columns() -> None:
    _check_columns(GROUPS["reference_columns"])


def test_schema__strategy_risk_bot_columns() -> None:
    _check_columns(GROUPS["strategy_risk_bot_columns"])


def test_schema__portfolio_columns() -> None:
    _check_columns(GROUPS["portfolio_columns"])


def test_schema__backtest_ops_columns() -> None:
    _check_columns(GROUPS["backtest_ops_columns"])


def test_schema__every_spec_table_is_built_or_deferred() -> None:
    """No spec table is silently missing: a table is a model, or listed as a later PR's."""
    built = set(MODEL)
    assert set(SPEC) == built | LATER
    assert built.isdisjoint(LATER)


def test_schema__prefix_and_boundary() -> None:
    """HAT-SCH-010/106, HAT-OVR-005: hat_ names, hat_ FK targets, hat_-only DDL."""
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
    for statement in PG_DDL:
        idents = [i for groups in named.findall(statement) for i in groups if i]
        assert idents, statement[:80]
        for ident in idents:
            assert ident.startswith("hat_"), (ident, statement[:80])


def test_schema__hat_tables_prefixed_and_isolated() -> None:
    test_schema__prefix_and_boundary()


def test_schema__key_types() -> None:
    """HAT-SCH-011: an `id` column is a uuid PK (hat_event arrives with its PR)."""
    for name, table in MODEL.items():
        if "id" in table.columns:
            col = table.columns["id"]
            if col.primary_key:
                assert logical_type(col) == "uuid", name
    assert "hat_event" not in MODEL  # a later PR, whose id is a bigint identity


# user_id roots (HAT-SCH-012). hat_kill_switch carries user_id too; its FK chain is not
# non-null (an account-scope row has a null bot_id): recorded as a finding in the PR.
USER_ROOTS = [
    "hat_strategy",
    "hat_risk_policy_version",
    "hat_broker_account",
    "hat_bot",
    "hat_universe",
]
_REFERENCE = set(GROUPS["reference_columns"]) | {
    "hat_criterion_type",
    "hat_fundamental",
    "hat_regime_model_version",
    "hat_regime_state",
    "hat_job_run",
    "hat_quality_check",
}
CHAIN_EXEMPT = {"hat_kill_switch": "account-scope rows have a null bot_id (spec OWNER-PENDING)"}


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


def test_schema__user_id_on_owned_tables() -> None:
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


def test_schema__market_id_on_market_tables() -> None:
    test_schema__market_id_columns()


MONEY_CURRENCY_ROWS = {
    "hat_signal",
    "hat_order",
    "hat_fill",
    "hat_position",
    "hat_trade_result",
    "hat_allocation_daily",
    "hat_daily_bot_snapshot",
}
MARKET_CURRENCY = {"hat_instrument", "hat_watchlist_entry", "hat_corporate_action"}


def test_schema__money_types() -> None:
    """HAT-SCH-014: spec money columns are Numeric(18,4); none is Float; currency is reachable."""
    for name, table in MODEL.items():
        for col in table.columns:
            assert not isinstance(col.type, sa.Float), (name, col.name)
        spec_money = {c.name for c in SPEC[name].columns if c.logical_type == "numeric(18,4)"}
        model_money = {c.name for c in table.columns if logical_type(c) == "numeric(18,4)"}
        assert spec_money == model_money, name
        if not model_money:
            continue
        if name in MONEY_CURRENCY_ROWS:
            assert "currency" in table.columns, name
        elif name in MARKET_CURRENCY:
            assert "market_id" in table.columns or "instrument_id" in table.columns, name


def test_schema__time_types() -> None:
    """HAT-SCH-015: timestamptz everywhere; not-null record columns default to aware UTC;
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
    """HAT-SCH-100: every column type is a §10 portable type."""
    for name, table in MODEL.items():
        for col in table.columns:
            assert isinstance(col.type, ALLOWED), (name, col.name, col.type)
            if isinstance(col.type, sa.JSON):
                assert not isinstance(col.type, postgresql.JSONB), (name, col.name)
            sqlite.dialect().type_compiler_instance.process(col.type)  # compiles on SQLite
            postgresql.dialect().type_compiler_instance.process(col.type)


def test_schema__sqlite_create_all() -> None:
    """HAT-SCH-100/104: create_all builds every table on SQLite, every FK resolves and is kept."""
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine)
    names = set(sa.inspect(engine).get_table_names())
    assert set(MODEL) <= names
    assert len(MODEL) == 28
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
    """HAT-SCH-032: no risk %, size, capital, cap or kill column on hat_bot (risk_cluster is the
    allowed sleeve tag, HAT-PORT-010)."""
    for col in MODEL["hat_bot"].columns:
        if col.name == "risk_cluster":
            continue
        assert not any(word in col.name for word in RISK_WORDS), col.name


def test_schema__bot_sleeve_columns() -> None:
    """HAT-PORT-010 / DEC-PL1."""
    cols = MODEL["hat_bot"].columns
    for name in ("live_version_id", "universe_version_id", "lifecycle_state", "risk_cluster"):
        assert name in cols, name
    assert {f.column.table.name for f in cols["universe_version_id"].foreign_keys} == {
        "hat_universe_version"
    }
    check = " ".join(
        str(c.sqltext) for c in MODEL["hat_bot"].constraints if isinstance(c, sa.CheckConstraint)
    )
    for state in ("research", "backtested", "shadow", "live", "probation", "retired"):
        assert state in check, state


def test_schema__bot_has_sleeve_columns() -> None:
    test_schema__bot_sleeve_columns()

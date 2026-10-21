"""
Database engine, sessions and schema upkeep.

Models live in `app.models`; import them from there.
"""

import logging
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import (
    Connection,
    Engine,
    MetaData,
    create_engine,
    insert,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool
from sqlalchemy.schema import CreateColumn, CreateTable

from .base import Base
from .hat import models as hat_models  # noqa: F401  (registers the hat_* tables on Base)
from .hat.schema import ensure_hat_columns, ensure_hat_schema, seed_hat_reference
from .hat.settings import is_enabled as hat_is_enabled
from .models import (
    Company,
    CustomUniverse,
    CustomUniverseMember,
    LiveOrder,
)
from .models.live_order import CLIENT_ORDER_ID_INDEX
from .paths import ENV_FILE
from .utils.env_loader import env_flag, load_dotenv

logger = logging.getLogger(__name__)

# ============================================
# Environment & Path Discovery
# ============================================


def get_env_file() -> Path | str:
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(sys.executable)
        potential_paths = [
            os.path.join(exe_dir, ".env"),
            os.path.join(os.path.dirname(exe_dir), ".env"),
            os.path.join(os.getcwd(), ".env"),
        ]
        for p in potential_paths:
            if os.path.exists(p):
                return p
        return ".env"
    else:
        return ENV_FILE


env_path = get_env_file()
load_dotenv(env_path)

# ============================================
# Database Configuration
# ============================================

DB_CONNECT_TIMEOUT_S = 3
SQLITE_TEST_URL = "sqlite:///./test_quant.db"


def postgres_url_from_env() -> URL:
    """The Postgres URL from DB_* env vars; URL.create quotes any character in the password."""
    return URL.create(
        # Explicit driver: SQLAlchemy 2.1 made psycopg (v3) the default for postgresql://.
        drivername="postgresql+psycopg2",
        username=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=os.getenv("DB_NAME", "algotrading"),
    )


def create_engine_from_env() -> Engine:
    """SQLite when USE_SQLITE_TEST is a true flag, else pooled Postgres from DB_* env vars."""
    if env_flag("USE_SQLITE_TEST"):
        return create_engine(SQLITE_TEST_URL, connect_args={"check_same_thread": False})
    return create_engine(
        postgres_url_from_env(),
        echo=False,
        pool_pre_ping=True,
        poolclass=QueuePool,
        pool_size=20,
        max_overflow=40,
        pool_recycle=3600,  # Recycle connections after 1 hour
        pool_timeout=30,  # Do not wait forever for a pooled connection
        # A down server fails a request in seconds instead of hanging on the TCP connect.
        connect_args={"connect_timeout": DB_CONNECT_TIMEOUT_S},
    )


engine = create_engine_from_env()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def describe_database_target(url: URL | None = None) -> str:
    """Where the engine (or `url`) connects, for log lines: host:port/name, never the password."""
    url = url if url is not None else engine.url
    if url.get_backend_name() == "sqlite":
        return f"sqlite:{url.database}"
    return f"{url.host}:{url.port}/{url.database}"


# ============================================
# Database Helpers
# ============================================


def init_db() -> None:
    """Create all tables and backfill schema changes on existing databases."""
    Base.metadata.create_all(bind=engine)
    ensure_universe_schema(bind=engine)
    ensure_live_order_schema(bind=engine)
    ensure_hat_database(bind=engine)
    logger.info("Database initialized: %s", describe_database_target())


def ensure_hat_database(*, bind: Engine | None = None) -> None:
    """HAT's Postgres-only DDL and reference seed (HAT-SCH-101, HAT-SCH-021).

    A failure here must not take SmartTrader's own startup down while HAT is off: it is logged,
    and raised only when HAT_ENABLED is true.
    """
    db_engine = bind or engine
    try:
        ensure_hat_columns(db_engine)
        ensure_hat_schema(db_engine)
        seed_hat_reference(db_engine)
    except Exception:
        logger.exception("HAT schema upkeep failed")
        if hat_is_enabled():
            raise


def get_db() -> Generator[Session, None, None]:
    """FastAPI Dependency for database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session_scope() -> Generator[Session, None, None]:
    """Context-managed service session boundary with commit/rollback semantics."""
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_or_create_company(db: Session, symbol: str, **kwargs: object) -> Company:
    """Utility to get existing company or create new one"""
    company = db.query(Company).filter(Company.symbol == symbol).first()
    if not company:
        company = Company(symbol=symbol, **kwargs)
        db.add(company)
        db.commit()
        db.refresh(company)
    return company


def ensure_universe_schema(*, bind: Engine | None = None) -> None:
    """Bring universe tables on existing databases up to the current models.

    Idempotent, safe on every start and on a fresh database:
    - widens index_universe_definitions.index_code to VARCHAR(50) (Postgres only);
    - repoints custom_universe_members.universe_id at custom_universes.id (it used to
      reference index_universe_definitions.id), see _ensure_custom_member_fk.
    """
    db_engine = bind or engine
    _widen_index_code(db_engine)
    _ensure_custom_member_fk(db_engine)


def _widen_index_code(db_engine: Engine) -> None:
    """Widen legacy index_universe_definitions.index_code to support current index codes."""
    if db_engine.dialect.name != "postgresql":
        return

    inspector = inspect(db_engine)
    existing_tables = set(inspector.get_table_names())
    if "index_universe_definitions" not in existing_tables:
        return

    columns = {
        column["name"]: column for column in inspector.get_columns("index_universe_definitions")
    }
    index_code = columns.get("index_code")
    current_length = getattr(index_code.get("type"), "length", None) if index_code else None
    if current_length is not None and current_length >= 50:
        return

    with db_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE index_universe_definitions ALTER COLUMN index_code TYPE VARCHAR(50)")
        )
    logger.warning("Widened index_universe_definitions.index_code to VARCHAR(50)")


_CUSTOM_MEMBERS = "custom_universe_members"
_CUSTOM_MEMBER_FK = "custom_universe_members_universe_id_fkey"  # Postgres' default name
_CUSTOM_MEMBER_ORPHANS = (
    "FROM custom_universe_members WHERE universe_id NOT IN (SELECT id FROM custom_universes)"
)


def _custom_member_fks(connection: Connection) -> tuple[bool, list[str]]:
    """(has the correct FK, names of the other FKs on universe_id) for custom_universe_members."""
    fks = [
        fk
        for fk in inspect(connection).get_foreign_keys(_CUSTOM_MEMBERS)
        if fk["constrained_columns"] == ["universe_id"]
    ]
    correct = any(
        fk["referred_table"] == "custom_universes" and fk["referred_columns"] == ["id"]
        for fk in fks
    )
    wrong = [fk["name"] for fk in fks if fk["referred_table"] != "custom_universes" and fk["name"]]
    return correct, wrong


def _ensure_custom_member_fk(db_engine: Engine) -> None:
    """Point custom_universe_members.universe_id at custom_universes.id.

    Older databases have it referencing index_universe_definitions.id, so every insert
    from POST /api/universe/custom failed the FK check on Postgres. Member rows whose
    universe_id is not a custom_universes id cannot belong to any custom universe (the
    service only ever writes custom_universes ids), so they are counted, logged and
    deleted before the correct FK is added. index_universe_* tables are never touched.
    """
    if not {"custom_universes", _CUSTOM_MEMBERS} <= set(inspect(db_engine).get_table_names()):
        return
    with db_engine.connect() as connection:
        if _custom_member_fks(connection)[0]:
            return

    is_postgres = db_engine.dialect.name == "postgresql"
    with db_engine.begin() as connection:
        if is_postgres:
            # Serialise concurrent starts, then re-check under the lock.
            connection.execute(text(f"LOCK TABLE {_CUSTOM_MEMBERS} IN ACCESS EXCLUSIVE MODE"))
        correct, wrong_fk_names = _custom_member_fks(connection)
        if correct:
            return
        orphans = connection.execute(text(f"SELECT COUNT(*) {_CUSTOM_MEMBER_ORPHANS}")).scalar_one()
        if orphans:
            logger.warning(
                "Deleting %d custom_universe_members rows with no matching custom_universes id",
                orphans,
            )
        if is_postgres:
            for name in wrong_fk_names:
                connection.execute(text(f'ALTER TABLE {_CUSTOM_MEMBERS} DROP CONSTRAINT "{name}"'))
            connection.execute(text(f"DELETE {_CUSTOM_MEMBER_ORPHANS}"))
            connection.execute(
                text(
                    f"ALTER TABLE {_CUSTOM_MEMBERS} ADD CONSTRAINT {_CUSTOM_MEMBER_FK} "
                    "FOREIGN KEY (universe_id) REFERENCES custom_universes (id)"
                )
            )
        else:
            _rebuild_sqlite_custom_members(connection)
    logger.warning("Repointed custom_universe_members.universe_id FK at custom_universes.id")


def _rebuild_sqlite_custom_members(connection: Connection) -> None:
    """SQLite cannot alter constraints: copy the valid rows into a table built from the model."""
    model = CustomUniverseMember.__table__
    scratch = MetaData()
    CustomUniverse.__table__.to_metadata(scratch)
    rebuilt = model.to_metadata(scratch, name=f"{_CUSTOM_MEMBERS}__fkfix")
    existing = {column["name"] for column in inspect(connection).get_columns(_CUSTOM_MEMBERS)}
    columns = [column.name for column in model.columns if column.name in existing]

    connection.execute(CreateTable(rebuilt))
    # `model` names the old table here: same name, and only columns it already has.
    valid_rows = select(*(model.c[name] for name in columns)).where(
        model.c.universe_id.in_(select(CustomUniverse.__table__.c.id))
    )
    connection.execute(insert(rebuilt).from_select(columns, valid_rows))
    connection.execute(text(f"DROP TABLE {_CUSTOM_MEMBERS}"))
    connection.execute(text(f"ALTER TABLE {rebuilt.name} RENAME TO {_CUSTOM_MEMBERS}"))
    for index in model.indexes:
        index.create(connection)


def ensure_live_order_schema(*, bind: Engine | None = None) -> None:
    """Add live_orders.client_order_id and its unique index to existing databases.

    Idempotent: the column is added only when missing, and the index is created with
    checkfirst, so running it on every startup is safe. Existing rows keep NULL, which
    the unique index allows any number of times (Postgres and SQLite).
    """
    db_engine = bind or engine
    table = LiveOrder.__table__
    inspector = inspect(db_engine)
    if table.name not in set(inspector.get_table_names()):
        return

    existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
    # IF NOT EXISTS keeps a second process starting at the same time from failing on
    # Postgres; SQLite has no IF NOT EXISTS for columns (and no concurrent starts).
    if_not_exists = "IF NOT EXISTS " if db_engine.dialect.name == "postgresql" else ""
    with db_engine.begin() as connection:
        if "client_order_id" not in existing_columns:
            column_sql = str(
                CreateColumn(table.c.client_order_id).compile(dialect=db_engine.dialect)
            ).strip()
            connection.execute(
                text(f"ALTER TABLE {table.name} ADD COLUMN {if_not_exists}{column_sql}")
            )
            logger.warning("Backfilled missing column on %s: client_order_id", table.name)
        connection.execute(
            text(
                f"CREATE UNIQUE INDEX IF NOT EXISTS {CLIENT_ORDER_ID_INDEX} "
                f"ON {table.name} (client_order_id)"
            )
        )


# Avoid import-time schema creation; startup owns database validation/initialization.

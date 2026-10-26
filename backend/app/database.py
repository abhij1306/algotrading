"""
Database engines, sessions and schema creation (docs/architecture.md, Storage).

HAT keeps the stock data in hat_live (`app.base.Base`), the trading and runtime state in
hat_state (`app.base.StateBase`, same server and credentials, `DB_STATE_NAME`) and its configs as
JSON files under `HAT_DATA_ROOT/admin/config` (`app.hat.configs`). Every session binds both
databases and the config store (`app.hat.storage.HatSession`). `HatSessionLocal` reaches hat_live
read-only; only the data pipeline's jobs and the importers use `HatDataSessionLocal`, the one
that may write it.
"""

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker
from sqlalchemy.pool import NullPool, QueuePool

from .base import Base, StateBase
from .hat import models as hat_models  # noqa: F401  (registers the hat_* tables on both bases)
from .hat.configs.store import FileConfigStore, config_root
from .hat.configs.unit import RUNS_ROOT, STORE
from .hat.risk.policy_upkeep import ensure_hat_policies
from .hat.schema import (
    check_schema_drift,
    ensure_live_schema,
    ensure_state_schema,
    seed_hat_reference,
)
from .hat.settings import is_enabled as hat_is_enabled
from .hat.settings import load_settings
from .hat.storage import HatSession, live_connect_options, session_binds
from .paths import ENV_FILE
from .utils.env_loader import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(str(ENV_FILE))

# ============================================
# Database Configuration
# ============================================

DB_CONNECT_TIMEOUT_S = 3
DEFAULT_STATE_DB = "hat_state"


def postgres_url_from_env(database: str | None = None) -> URL:
    """The Postgres URL from DB_* env vars (hat_live's `DB_NAME`, or `database`); URL.create
    quotes any character in the password."""
    return URL.create(
        # Explicit driver: SQLAlchemy 2.1 made psycopg (v3) the default for postgresql://.
        drivername="postgresql+psycopg2",
        username=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", ""),
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        database=database or os.getenv("DB_NAME", "algotrading"),
    )


def state_database_name() -> str:
    """hat_state's database name (`DB_STATE_NAME`, default `hat_state`)."""
    return (os.getenv("DB_STATE_NAME") or "").strip() or DEFAULT_STATE_DB


def create_engine_from_env(
    database: str | None = None, *, options: str = "", pool_size: int = 20
) -> Engine:
    """A pooled Postgres engine from DB_* env vars (HAT refuses any other database)."""
    connect_args: dict[str, object] = {"connect_timeout": DB_CONNECT_TIMEOUT_S}
    if options:
        connect_args["options"] = options
    return create_engine(
        postgres_url_from_env(database),
        echo=False,
        pool_pre_ping=True,
        poolclass=QueuePool,
        pool_size=pool_size,
        max_overflow=2 * pool_size,
        pool_recycle=3600,  # Recycle connections after 1 hour
        pool_timeout=30,  # Do not wait forever for a pooled connection
        # A down server fails a request in seconds instead of hanging on the TCP connect.
        connect_args=connect_args,
    )


# hat_live as every process but the data jobs sees it: each transaction read-only.
engine = create_engine_from_env(options=live_connect_options(writer=False))
# hat_live for the data pipeline's jobs and the importers.
live_writer_engine = create_engine_from_env(options=live_connect_options(writer=True), pool_size=5)
state_engine = create_engine_from_env(state_database_name())

# The longest one statement may run (lock waits included). Every statement reads or writes a few
# thousand rows at most (bars and features live in Parquet), so a statement still running after
# two minutes is wedged: it fails, and the periodic loop, job, route or command line that sent it
# moves on instead of holding its thread and its locks.
HAT_STATEMENT_TIMEOUT_MS = 120_000


def bounded_sessionmaker(
    live: Engine, state: Engine, timeout_ms: int = HAT_STATEMENT_TIMEOUT_MS
) -> sessionmaker[Session]:
    """`HatSession`s over `live` and `state` and the config files, whose every transaction runs
    under `SET LOCAL statement_timeout` on each connection it opens: it ends with the
    transaction, so a pooled connection the routes reuse keeps the server default."""
    root = load_settings().data_root
    factory = sessionmaker(
        class_=HatSession,
        autocommit=False,
        autoflush=False,
        binds=session_binds(live, state),
        info={STORE: FileConfigStore(config_root(root)), RUNS_ROOT: root},
    )

    def bound(_session: Session, _transaction: SessionTransaction, connection: Connection) -> None:
        connection.exec_driver_sql(f"SET LOCAL statement_timeout = {int(timeout_ms)}")

    event.listen(factory, "after_begin", bound)
    return factory


# Every session but the data jobs': the HAT runtime (`HatRuntime`, its jobs, loops and feed
# calendar), the dashboard routes (`get_db`), the broker logins and the command lines.
HatSessionLocal = bounded_sessionmaker(engine, state_engine)
# The data pipeline's jobs and the importers: the only sessions that may write hat_live.
HatDataSessionLocal = bounded_sessionmaker(live_writer_engine, state_engine)


def describe_database_target(url: URL | None = None) -> str:
    """Where the engines (or `url`) connect, for log lines: host:port/name, never the password."""
    if url is not None:
        return f"{url.host}:{url.port}/{url.database}"
    live, state = engine.url, state_engine.url
    return f"{live.host}:{live.port}/{live.database}+{state.database}"


# ============================================
# Database Helpers
# ============================================


def state_database_exists(*, create: bool) -> bool:
    """Whether hat_state exists on the server, creating it first when missing and `create`
    (through the server's `postgres` database: CREATE DATABASE runs outside a transaction)."""
    name = state_database_name()
    maintenance = create_engine(postgres_url_from_env("postgres"), poolclass=NullPool)
    try:
        with maintenance.connect() as connection:
            found = connection.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": name}
            ).first()
        if found is None and create:
            with maintenance.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                quoted = conn.dialect.identifier_preparer.quote(name)  # an identifier, quoted
                conn.exec_driver_sql(f"CREATE DATABASE {quoted}")
            logger.info("Created the state database %s", name)
            return True
        return found is not None
    finally:
        maintenance.dispose()


def init_db() -> None:
    """Create hat_state when missing and the HAT tables of both databases, run HAT's upkeep, then
    refuse a schema that differs from the models (`SchemaDriftError`, HAT enabled or not; called
    by the lifespan at startup). hat_live's upkeep and its drift probe run on the data writer's
    connection: the probe creates temporary tables, which a read-only transaction refuses."""
    state_database_exists(create=True)
    Base.metadata.create_all(bind=live_writer_engine)
    StateBase.metadata.create_all(bind=state_engine)
    ensure_hat_database(live=live_writer_engine, state=state_engine)
    check_schema_drift(live_writer_engine, Base)
    check_schema_drift(state_engine, StateBase)
    logger.info("Database initialized: %s", describe_database_target())


def ensure_hat_database(*, live: Engine | None = None, state: Engine | None = None) -> None:
    """HAT's Postgres-only DDL of both databases, the reference seed and the policy upkeep
    (HAT-SCH-101, HAT-SCH-021, HAT-RISK-011).

    A failure here must not take the API down while HAT is off: it is logged, and raised only
    when HAT_ENABLED is true.
    """
    try:
        ensure_live_schema(live or live_writer_engine)
        ensure_state_schema(state or state_engine)
        seed_hat_reference(live or live_writer_engine)
        ensure_hat_policies(HatSessionLocal)
    except Exception:
        logger.exception("HAT schema upkeep failed")
        if hat_is_enabled():
            raise


def get_db() -> Generator[Session, None, None]:
    """FastAPI Dependency for database session"""
    db = HatSessionLocal()
    try:
        yield db
    finally:
        db.close()


@contextmanager
def db_session_scope(*, writes_data: bool = False) -> Generator[Session, None, None]:
    """Context-managed service session boundary with commit/rollback semantics; `writes_data`
    for an importer or data tool that writes hat_live."""
    db = (HatDataSessionLocal if writes_data else HatSessionLocal)()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

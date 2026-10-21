"""
Database engine, sessions and schema creation.

The models are HAT's (`app.hat.models`). There is no legacy schema: HAT uses a fresh Postgres
database, and `init_db` creates its tables and seeds its reference rows.
"""

import logging
import os
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import QueuePool

from .base import Base
from .hat import models as hat_models  # noqa: F401  (registers the hat_* tables on Base)
from .hat.schema import ensure_hat_columns, ensure_hat_schema, seed_hat_reference
from .hat.settings import is_enabled as hat_is_enabled
from .paths import ENV_FILE
from .utils.env_loader import load_dotenv

logger = logging.getLogger(__name__)

load_dotenv(str(ENV_FILE))

# ============================================
# Database Configuration
# ============================================

DB_CONNECT_TIMEOUT_S = 3


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
    """The pooled Postgres engine from DB_* env vars (HAT refuses any other database)."""
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
    return f"{url.host}:{url.port}/{url.database}"


# ============================================
# Database Helpers
# ============================================


def init_db() -> None:
    """Create the HAT tables and run HAT's upkeep (called by the lifespan at startup)."""
    Base.metadata.create_all(bind=engine)
    ensure_hat_database(bind=engine)
    logger.info("Database initialized: %s", describe_database_target())


def ensure_hat_database(*, bind: Engine | None = None) -> None:
    """HAT's Postgres-only DDL and reference seed (HAT-SCH-101, HAT-SCH-021).

    A failure here must not take the API down while HAT is off: it is logged, and raised only
    when HAT_ENABLED is true.
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

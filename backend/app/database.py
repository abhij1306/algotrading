"""
Database Configuration and Model Export
Unified entry point for database engine, session, and models.
"""

import logging
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Column, Engine, Table, create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.schema import CreateColumn

from . import models
from .base import Base
from .models import (
    BacktestDailyResult,
    BacktestRun,
    Company,
    DatasetArtifact,
    DatasetRun,
    DataUpdateLog,
    FinancialStatement,
    HistoricalPrice,
    IntradayCandle,
    QuarterlyResult,
    SnapshotIndexStock,
    SnapshotIndexUniverse,
    StrategyPosition,
    SystemConfig,
    VCPScanResult,
    Watchlist,
)
from .utils.env_loader import load_dotenv

logger = logging.getLogger(__name__)

MODELS_IMPORTED = models
MODEL_EXPORTS = (
    Company,
    DataUpdateLog,
    DatasetRun,
    DatasetArtifact,
    SnapshotIndexStock,
    SnapshotIndexUniverse,
    FinancialStatement,
    HistoricalPrice,
    IntradayCandle,
    QuarterlyResult,
    Watchlist,
)

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
        return Path(__file__).resolve().parent.parent.parent / ".env"


env_path = get_env_file()
load_dotenv(env_path)

# ============================================
# Database Configuration
# ============================================

DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "algotrading")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_CONNECT_TIMEOUT_S = 3

if os.getenv("USE_SQLITE_TEST", "False") == "True":
    DATABASE_URL = "sqlite:///./test_quant.db"
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    from sqlalchemy.pool import QueuePool

    # Explicit driver: SQLAlchemy 2.1 made psycopg (v3) the default for postgresql://.
    DATABASE_URL = f"postgresql+psycopg2://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        poolclass=QueuePool,
        pool_size=20,  # Increased from 10 for better concurrency
        max_overflow=40,  # Increased from 20 for peak load handling
        pool_recycle=3600,  # Recycle connections after 1 hour
        pool_timeout=30,  # Add timeout to prevent indefinite waiting
        # A down server fails a request in seconds instead of hanging on the TCP connect.
        connect_args={"connect_timeout": DB_CONNECT_TIMEOUT_S},
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def describe_database_target() -> str:
    """Where the engine connects, for log lines: host:port/name, never the password."""
    url = engine.url
    if url.get_backend_name() == "sqlite":
        return f"sqlite:{url.database}"
    return f"{url.host}:{url.port}/{url.database}"


# ============================================
# Database Helpers
# ============================================


def init_db() -> None:
    """Initialize database and create all tables"""
    Base.metadata.create_all(bind=engine)
    ensure_backtest_schema(bind=engine)
    ensure_universe_schema(bind=engine)
    ensure_strategy_schema(bind=engine)
    target = DATABASE_URL.split("@")[-1] if "@" in DATABASE_URL else DATABASE_URL
    logger.info("Database initialized: %s", target)


def get_db() -> Generator[Session, None, None]:
    """FastAPI Dependency for database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_session() -> Session:
    """Create a new database session for services"""
    return SessionLocal()


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


def ensure_backtest_schema(*, bind: Engine | None = None) -> None:
    """Backfill missing backtest table columns on existing databases."""
    db_engine = bind or engine
    for table in (BacktestRun.__table__, BacktestDailyResult.__table__):
        inspector = inspect(db_engine)
        existing_tables = set(inspector.get_table_names())
        if table.name not in existing_tables:
            continue

        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing_columns = [
            column for column in table.columns if column.name not in existing_columns
        ]
        if not missing_columns:
            continue

        added: list[str] = []
        with db_engine.begin() as connection:
            for column in missing_columns:
                column_sql = str(CreateColumn(column).compile(dialect=db_engine.dialect)).strip()
                connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column_sql}"))
                added.append(column.name)

        logger.warning(
            "Backfilled missing columns on %s: %s",
            table.name,
            ", ".join(added),
        )


def ensure_universe_schema(*, bind: Engine | None = None) -> None:
    """Widen legacy universe table columns to support current index codes."""
    db_engine = bind or engine
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


_COPY_LEGACY_PNL_SQL = "UPDATE positions SET pnl_inr = COALESCE(pnl_inr, pnl_rs)"
# vcp_scan_results column -> backfill statement run when that column is newly added
_VCP_SCAN_BACKFILLS = {
    "scan_id": ("UPDATE vcp_scan_results SET scan_id = COALESCE(scan_id, CAST(id AS VARCHAR(36)))"),
    "scan_timestamp": (
        "UPDATE vcp_scan_results SET scan_timestamp = COALESCE(scan_timestamp, created_at)"
    ),
}


def ensure_strategy_schema(*, bind: Engine | None = None) -> None:
    """Backfill missing strategy tables/columns on existing databases."""
    db_engine = bind or engine
    for table in (VCPScanResult.__table__, StrategyPosition.__table__, SystemConfig.__table__):
        inspector = inspect(db_engine)
        existing_tables = set(inspector.get_table_names())
        if table.name not in existing_tables:
            continue

        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing_columns = [
            column for column in table.columns if column.name not in existing_columns
        ]
        if missing_columns:
            _add_strategy_columns(db_engine, table, missing_columns, existing_columns)
        elif table.name == "positions" and {"pnl_rs", "pnl_inr"} <= existing_columns:
            with db_engine.begin() as connection:
                connection.execute(text(_COPY_LEGACY_PNL_SQL))
            logger.warning("Backfilled positions.pnl_inr from legacy pnl_rs")


def _add_strategy_columns(
    db_engine: Engine, table: Table, missing_columns: list[Column], existing_columns: set[str]
) -> None:
    """ALTER TABLE ADD each missing column, then backfill the new columns from legacy data."""
    added: list[str] = []
    with db_engine.begin() as connection:
        for column in missing_columns:
            column_sql = str(CreateColumn(column).compile(dialect=db_engine.dialect)).strip()
            connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column_sql}"))
            added.append(column.name)
        for statement in _strategy_backfills(table.name, added, existing_columns):
            connection.execute(text(statement))

    logger.warning("Backfilled missing columns on %s: %s", table.name, ", ".join(added))


def _strategy_backfills(table_name: str, added: list[str], existing_columns: set[str]) -> list[str]:
    """Backfill statements for the columns just added to a strategy table."""
    if table_name == "vcp_scan_results":
        return [_VCP_SCAN_BACKFILLS[name] for name in _VCP_SCAN_BACKFILLS if name in added]
    if table_name == "positions" and "pnl_rs" in existing_columns and "pnl_inr" in added:
        return [_COPY_LEGACY_PNL_SQL]
    return []


# Avoid import-time schema creation; startup owns database validation/initialization.

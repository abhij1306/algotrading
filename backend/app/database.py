"""
Database Configuration and Model Export
Unified entry point for database engine, session, and models.
"""

import logging
import os
import sys
from collections.abc import Generator
from pathlib import Path

from sqlalchemy import create_engine, inspect, text
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

if os.getenv("USE_SQLITE_TEST", "False") == "True":
    DATABASE_URL = "sqlite:///./test_quant.db"
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    from sqlalchemy.pool import QueuePool

    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_pre_ping=True,
        poolclass=QueuePool,
        pool_size=20,  # Increased from 10 for better concurrency
        max_overflow=40,  # Increased from 20 for peak load handling
        pool_recycle=3600,  # Recycle connections after 1 hour
        pool_timeout=30,  # Add timeout to prevent indefinite waiting
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

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


def get_or_create_company(db: Session, symbol: str, **kwargs: object) -> Company:
    """Utility to get existing company or create new one"""
    company = db.query(Company).filter(Company.symbol == symbol).first()
    if not company:
        company = Company(symbol=symbol, **kwargs)
        db.add(company)
        db.commit()
        db.refresh(company)
    return company


def ensure_backtest_schema(*, bind=None) -> None:
    """Backfill missing backtest table columns on existing databases."""
    db_engine = bind or engine
    for table in (BacktestRun.__table__, BacktestDailyResult.__table__):
        inspector = inspect(db_engine)
        existing_tables = set(inspector.get_table_names())
        if table.name not in existing_tables:
            continue

        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing_columns = [column for column in table.columns if column.name not in existing_columns]
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


def ensure_universe_schema(*, bind=None) -> None:
    """Widen legacy universe table columns to support current index codes."""
    db_engine = bind or engine
    if db_engine.dialect.name != "postgresql":
        return

    inspector = inspect(db_engine)
    existing_tables = set(inspector.get_table_names())
    if "index_universe_definitions" not in existing_tables:
        return

    columns = {column["name"]: column for column in inspector.get_columns("index_universe_definitions")}
    index_code = columns.get("index_code")
    current_length = getattr(index_code.get("type"), "length", None) if index_code else None
    if current_length is not None and current_length >= 50:
        return

    with db_engine.begin() as connection:
        connection.execute(
            text("ALTER TABLE index_universe_definitions ALTER COLUMN index_code TYPE VARCHAR(50)")
        )
    logger.warning("Widened index_universe_definitions.index_code to VARCHAR(50)")


def ensure_strategy_schema(*, bind=None) -> None:
    """Backfill missing strategy tables/columns on existing databases."""
    db_engine = bind or engine
    for table in (VCPScanResult.__table__, StrategyPosition.__table__, SystemConfig.__table__):
        inspector = inspect(db_engine)
        existing_tables = set(inspector.get_table_names())
        if table.name not in existing_tables:
            continue

        existing_columns = {column["name"] for column in inspector.get_columns(table.name)}
        missing_columns = [column for column in table.columns if column.name not in existing_columns]
        if not missing_columns:
            continue

        added: list[str] = []
        with db_engine.begin() as connection:
            for column in missing_columns:
                column_sql = str(CreateColumn(column).compile(dialect=db_engine.dialect)).strip()
                connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column_sql}"))
                added.append(column.name)

        logger.warning("Backfilled missing columns on %s: %s", table.name, ", ".join(added))


# Avoid import-time schema creation; startup owns database validation/initialization.

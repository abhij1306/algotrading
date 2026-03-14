"""
Database Configuration and Model Export
Unified entry point for database engine, session, and models.
"""

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from . import models
from .base import Base
from .models import (
    Company,
    DataUpdateLog,
    DatasetArtifact,
    DatasetRun,
    FinancialStatement,
    HistoricalPrice,
    IntradayCandle,
    QuarterlyResult,
    SnapshotIndexStock,
    SnapshotIndexUniverse,
    Watchlist,
)
from .utils.env_loader import load_dotenv

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


def get_env_file():
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


def init_db():
    """Initialize database and create all tables"""
    Base.metadata.create_all(bind=engine)
    print(
        f"Database initialized: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}"
    )


def get_db():
    """FastAPI Dependency for database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def get_db_session():
    """Create a new database session for services"""
    return SessionLocal()


def get_or_create_company(db, symbol: str, **kwargs):
    """Utility to get existing company or create new one"""
    company = db.query(Company).filter(Company.symbol == symbol).first()
    if not company:
        company = Company(symbol=symbol, **kwargs)
        db.add(company)
        db.commit()
        db.refresh(company)
    return company


# Avoid import-time schema creation; startup owns database validation/initialization.

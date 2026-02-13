"""
Database Configuration and Model Export
Unified entry point for database engine, session, and models.
"""
from sqlalchemy import create_engine
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from pathlib import Path
import os
import sys
from .utils.env_loader import load_dotenv

# ============================================
# Environment & Path Discovery
# ============================================

def get_env_file():
    if getattr(sys, 'frozen', False):
        exe_dir = os.path.dirname(sys.executable)
        potential_paths = [
            os.path.join(exe_dir, ".env"),
            os.path.join(os.path.dirname(exe_dir), ".env"),
            os.path.join(os.getcwd(), ".env")
        ]
        for p in potential_paths:
            if os.path.exists(p): return p
        return ".env"
    else:
        return Path(__file__).resolve().parent.parent.parent / '.env'

env_path = get_env_file()
load_dotenv(env_path)

# ============================================
# Database Configuration
# ============================================

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'algotrading')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')

if os.getenv("USE_SQLITE_TEST", "False") == "True":
    DATABASE_URL = "sqlite:///./test_quant.db"
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
else:
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    engine = create_engine(DATABASE_URL, echo=False, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
from .base import Base

# ============================================
# Model Imports (to ensure Base picks them up)
# ============================================

# Import models here so they are registered with Base.metadata
from .models import *

# ============================================
# Database Helpers
# ============================================

def init_db():
    """Initialize database and create all tables"""
    Base.metadata.create_all(bind=engine)
    print(f"Database initialized: {DATABASE_URL.split('@')[-1] if '@' in DATABASE_URL else DATABASE_URL}")

def get_db():
    """FastAPI Dependency for database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def get_or_create_company(db, symbol: str, **kwargs):
    """Utility to get existing company or create new one"""
    from .models.company import Company
    company = db.query(Company).filter(Company.symbol == symbol).first()
    if not company:
        company = Company(symbol=symbol, **kwargs)
        db.add(company)
        db.commit()
        db.refresh(company)
    return company

# Attempt to initialize DB (tables only created if they don't exist)
try:
    if os.getenv("INIT_DB_ON_IMPORT", "True") == "True":
        Base.metadata.create_all(bind=engine)
except Exception as e:
    # Silent fail for test/offline environments
    pass

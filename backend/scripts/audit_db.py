
import sys
import os
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Add backend to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.database import Base, Company, HistoricalPrice, get_env_file
from app.utils.env_loader import load_dotenv

# Load Env
env_path = get_env_file()
load_dotenv(env_path)

DB_HOST = os.getenv('DB_HOST', 'localhost')
DB_PORT = os.getenv('DB_PORT', '5432')
DB_NAME = os.getenv('DB_NAME', 'algotrading')
DB_USER = os.getenv('DB_USER', 'postgres')
DB_PASSWORD = os.getenv('DB_PASSWORD', '')

DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"

def audit_db():
    print(f"Auditing Database: {DB_NAME} at {DB_HOST}")
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # 1. Check Connection
        session.execute(text("SELECT 1"))
        print("[OK] Database Connection Successful")

        # 2. Count Companies
        company_count = session.query(Company).count()
        print(f"[INFO] Total Companies: {company_count}")
        
        # 3. Count Historical Data
        price_count = session.query(HistoricalPrice).count()
        print(f"[INFO] Total Historical Price Rows: {price_count}")

        # 4. Check NIFTY 50 Symbols
        nifty50_sample = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK"]
        print("\nChecking Key NIFTY50 Symbols:")
        for sym in nifty50_sample:
            co = session.query(Company).filter(Company.symbol == sym).first()
            if co:
                data_count = session.query(HistoricalPrice).filter(HistoricalPrice.company_id == co.id).count()
                print(f"  - {sym}: FOUND (ID: {co.id}), Rows: {data_count}")
            else:
                print(f"  - {sym}: [MISSING]")

    except Exception as e:
        print(f"[ERROR] Audit Failed: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    audit_db()

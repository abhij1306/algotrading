import sys
from pathlib import Path
from sqlalchemy import func
import datetime

# Add parent directory to path
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from backend.app.database import SessionLocal, DataUpdateLog, HistoricalPrice, Company

def check_status():
    db = SessionLocal()
    try:
        # 1. Check Update Logs
        print("📋 Checking Data Update Logs...")
        latest_log = db.query(DataUpdateLog).order_by(DataUpdateLog.last_update.desc()).first()
        if latest_log:
            print(f"   Last Run: {latest_log.last_update}")
            print(f"   Status: {latest_log.status}")
            print(f"   Records: {latest_log.records_updated}")
            print(f"   Type: {latest_log.data_type}")
        else:
            print("   No logs found.")

        # 2. Check Historical Prices for explicit latest date
        print("\n📊 Checking Historical Prices...")
        latest_date = db.query(func.max(HistoricalPrice.date)).scalar()
        print(f"   Latest Date in DB: {latest_date}")
        
        if latest_date:
            count = db.query(HistoricalPrice).filter(HistoricalPrice.date == latest_date).count()
            print(f"   Records for {latest_date}: {count}")
            
            # Check vs Active Companies
            active_count = db.query(Company).filter(Company.is_active == True).count()
            print(f"   Total Active Companies: {active_count}")
            
            if count >= (active_count * 0.9):
                print(f"   ✅ Data coverage is good ({count}/{active_count})")
            else:
                print(f"   ⚠️  Partial data ({count}/{active_count})")

    except Exception as e:
        print(f"Error: {e}")
    finally:
        db.close()

if __name__ == "__main__":
    check_status()


import sys
from pathlib import Path
from datetime import datetime
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Add backend to path
sys.path.append(str(Path(__file__).resolve().parent.parent))
from app.database import get_env_file, ResearchPortfolio, LivePortfolioState, Base
from app.utils.env_loader import load_dotenv
import os

def seed_data():
    env_path = get_env_file()
    load_dotenv(env_path)
    
    DB_HOST = os.getenv('DB_HOST', 'localhost')
    DB_PORT = os.getenv('DB_PORT', '5432')
    DB_NAME = os.getenv('DB_NAME', 'algotrading')
    DB_USER = os.getenv('DB_USER', 'postgres')
    DB_PASSWORD = os.getenv('DB_PASSWORD', '')
    
    DATABASE_URL = f"postgresql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    
    engine = create_engine(DATABASE_URL)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    
    try:
        # 1. Create Portfolio if not exists
        port = db.query(ResearchPortfolio).filter(ResearchPortfolio.name == "Alpha Prime Live").first()
        if not port:
            port = ResearchPortfolio(
                name="Alpha Prime Live",
                policy_id="default-policy", # Mock
                status="LIVE",
                composition=[{"strategy_id": "TREND_FOLLOWING_V1", "weight": 0.5}],
                created_at=datetime.utcnow()
            )
            db.add(port)
            db.commit()
            db.refresh(port)
            print(f"Created Portfolio: {port.id}")
        else:
            print(f"Using Portfolio: {port.id}")
            
        # 2. Add Live State
        state = LivePortfolioState(
            portfolio_id=port.id,
            timestamp=datetime.utcnow(),
            total_equity=1050000.0,
            cash_balance=200000.0,
            deployed_capital=850000.0,
            current_drawdown_pct=-1.2,
            is_breached=False,
            breach_details="All systems nominal",
            strategy_performance={
                "TREND_FOLLOWING_V1": {"pnl": 5000, "allocation": 0.5, "equity": 420000},
                "MEAN_REVERSION_RSI": {"pnl": -1200, "allocation": 0.3, "equity": 300000}
            }
        )
        db.add(state)
        db.commit()
        print("Seeded Live State.")
        
    except Exception as e:
        print(f"Error: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    seed_data()

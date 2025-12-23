
import sys
import os
 
# Add current directory to path so we can import app
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from app.database import Base, engine, PortfolioPolicy, ResearchPortfolio

def migrate():
    print("Migrating Quant Research Models...")
    try:
        # Create tables if they don't exist
        # This checks __tablename__ and creates missing ones
        Base.metadata.create_all(bind=engine)
        print("✅ Tables created/verified: portfolio_policies, research_portfolios")
    except Exception as e:
        print(f"❌ Migration failed: {e}")

if __name__ == "__main__":
    migrate()

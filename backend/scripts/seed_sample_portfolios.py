import os
import sys
import json
from sqlalchemy.orm import Session
from datetime import date

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from app.database import SessionLocal
from app.engines.universe_manager import UniverseManager

def seed_sample_portfolios():
    """
    Create sample custom stock portfolios for testing
    """
    db = SessionLocal()
    try:
        mgr = UniverseManager(db)
        
        # Sample Portfolio 1: Large Cap Tech (Top Indian Tech)
        tech_stocks = ["INFY", "TCS", "WIPRO", "HCLTECH", "TECHM", "LTI", "PERSISTENT"]
        mgr.create_custom_portfolio(
            portfolio_id="TECH_LARGECAP",
            name="Large Cap Tech",
            description="Top Indian IT Services companies",
            symbols=tech_stocks
        )
        
        # Sample Portfolio 2: Banking & Finance
        banking_stocks = ["HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK", "KOTAKBANK", "INDUSINDBK"]
        mgr.create_custom_portfolio(
            portfolio_id="BANKING_LEADERS",
            name="Banking Leaders",
            description="Top Indian Banking stocks",
            symbols=banking_stocks
        )
        
        # Sample Portfolio 3: High Momentum (Recent performers)
        momentum_stocks = ["RELIANCE", "ADANIENT", "ADANIPORTS", "TATAMOTORS", "M&M", "BAJFINANCE"]
        mgr.create_custom_portfolio(
            portfolio_id="MOMENTUM_PICKS",
            name="Momentum Picks",
            description="High momentum stocks",
            symbols=momentum_stocks
        )
        
        # Sample Portfolio 4: Index-Only (Nifty 50 + BankNifty)
        index_symbols = ["NIFTY50-INDEX", "BANKNIFTY-INDEX"]
        mgr.create_custom_portfolio(
            portfolio_id="INDICES_ONLY",
            name="Major Indices",
            description="Nifty 50 and Bank Nifty indices",
            symbols=index_symbols
        )
        
        print("✅ Successfully created 4 sample portfolios:")
        print("  1. TECH_LARGECAP (7 stocks)")
        print("  2. BANKING_LEADERS (6 stocks)")
        print("  3. MOMENTUM_PICKS (6 stocks)")
        print("  4. INDICES_ONLY (2 indices)")
        
    finally:
        db.close()

if __name__ == "__main__":
    seed_sample_portfolios()

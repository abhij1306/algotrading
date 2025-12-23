
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from ..database import StockUniverse, UserStockPortfolio, Company, HistoricalPrice, engine
import json

logger = logging.getLogger(__name__)

class UniverseManager:
    """
    Manages stock universes, handles historical membership, and derives universes.
    """
    
    def __init__(self, db: Session):
        self.db = db

    def get_universe_symbols(self, universe_id: str, target_date: date) -> List[str]:
        """
        Returns the list of symbols in a universe as of a specific date.
        """
        # First check User Portfolios
        user_portfolio = self.db.query(UserStockPortfolio).filter(
            UserStockPortfolio.portfolio_id == universe_id
        ).first()
        if user_portfolio:
            return user_portfolio.symbols

        # Then check System Universes
        universe = self.db.query(StockUniverse).filter(
            StockUniverse.id == universe_id
        ).first()
        
        if not universe:
            logger.error(f"Universe {universe_id} not found.")
            return []

        # Find the symbols_by_date entry that is <= target_date
        sorted_dates = sorted(universe.symbols_by_date.keys())
        active_date = None
        for d_str in sorted_dates:
            if d_str <= target_date.isoformat():
                active_date = d_str
            else:
                break
        
        if not active_date:
            # Fallback to the earliest available date
            active_date = sorted_dates[0] if sorted_dates else None
        
        return universe.symbols_by_date.get(active_date, []) if active_date else []

    def seed_default_universes(self, nifty50_symbols: List[str], nifty100_symbols: List[str]):
        """
        Seeds the initial system universes if they don't exist.
        """
        created = []
        # NIFTY100_CORE
        if not self.db.query(StockUniverse).filter(StockUniverse.id == "NIFTY100_CORE").first():
            core_100 = StockUniverse(
                id="NIFTY100_CORE",
                description="Historical NIFTY 100 constituents",
                symbols_by_date={date(2024, 1, 1).isoformat(): nifty100_symbols},
                rebalance_frequency="NONE",
                selection_rules="NSE Official Index List"
            )
            self.db.add(core_100)

        # NIFTY50_ONLY
        if not self.db.query(StockUniverse).filter(StockUniverse.id == "NIFTY50_ONLY").first():
            core_50 = StockUniverse(
                id="NIFTY50_ONLY",
                description="Historical NIFTY 50 constituents",
                symbols_by_date={date(2024, 1, 1).isoformat(): nifty50_symbols},
                rebalance_frequency="NONE",
                selection_rules="NSE Official Index List"
            )
            self.db.add(core_50)
            created.append("NIFTY50_ONLY")

        self.db.commit()        
        logger.info(f"Seeded default universes: {created}")
    
    def create_custom_portfolio(self, portfolio_id: str, name: str, description: str, symbols: list):
        """
        Create a custom user portfolio
        """
        from ..database import UserStockPortfolio
        
        # Check if portfolio already exists
        existing = self.db.query(UserStockPortfolio).filter(
            UserStockPortfolio.portfolio_id == portfolio_id
        ).first()
        
        if existing:
            logger.info(f"Portfolio {portfolio_id} already exists, updating...")
            existing.name = name
            existing.description = description
            existing.symbols = symbols
        else:
            portfolio = UserStockPortfolio(
                portfolio_id=portfolio_id,
                name=name,
                description=description,
                symbols=symbols
            )
            self.db.add(portfolio)
        
        self.db.commit()
        logger.info(f"Created/Updated custom portfolio: {portfolio_id} with {len(symbols)} symbols")

    def derive_liquid_50(self, target_date: date):
        """
        Derived from NIFTY100_CORE. Top 50 by 30-day avg traded value.
        """
        # Implementation of rolling volume logic would go here
        # For now, we will use a placeholder or the first 50 of Nifty 100
        pass

    def derive_mean_rev(self, target_date: date):
        """
        Derived from NIFTY100_CORE. Stable volume + lower trend persistence.
        """
        pass

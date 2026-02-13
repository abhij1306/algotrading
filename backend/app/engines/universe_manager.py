
import logging
from datetime import date, datetime, timedelta
from typing import List, Dict, Optional
from sqlalchemy.orm import Session
from sqlalchemy import and_, or_
from ..database import StockUniverse, UserStockPortfolio, Company, HistoricalPrice, engine
from ..models.index_membership import IndexMembership
import json

logger = logging.getLogger(__name__)

# Lazy import for index universe loader
_index_universe_loader = None

def _get_index_universe_loader():
    """Lazy load the index universe loader."""
    global _index_universe_loader
    if _index_universe_loader is None:
        from ..services.index_universe_loader import index_universe_loader
        _index_universe_loader = index_universe_loader
    return _index_universe_loader


class UniverseManager:
    """
    Manages stock universes, handles historical membership, and derives universes.
    
    Integrates with IndexUniverseLoader for accurate index constituent data.
    """
    
    # Static cache to avoid redundant DB queries across multiple instances (e.g. in backtest loops)
    _symbols_cache: Dict[str, List[str]] = {}

    def __init__(self, db: Session):
        self.db = db

    def get_universe_symbols(self, universe_id: str, target_date: date) -> List[str]:
        """
        Returns the list of symbols in a universe as of a specific date.
        
        Resolution Priority:
        1. User Portfolios (UserStockPortfolio)
        2. System Universes (StockUniverse)
        3. Index Membership History (IndexMembership)
        4. Current Index Files (IndexUniverseLoader fallback)
        """
        # Check cache first
        cache_key = f"{universe_id}_{target_date.isoformat()}"
        if cache_key in self._symbols_cache:
            return self._symbols_cache[cache_key]

        # 1. First check User Portfolios
        user_portfolio = self.db.query(UserStockPortfolio).filter(
            UserStockPortfolio.portfolio_id == universe_id
        ).first()
        if user_portfolio:
            self._symbols_cache[cache_key] = user_portfolio.symbols
            return user_portfolio.symbols

        # 2. Then check System Universes (JSON-based history)
        universe = self.db.query(StockUniverse).filter(
            StockUniverse.id == universe_id
        ).first()
        
        if universe:
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
            
            if active_date:
                symbols = universe.symbols_by_date.get(active_date, [])
                self._symbols_cache[cache_key] = symbols
                return symbols
        
        # 3. Then check IndexMembership for historical tracking
        # We look for records that were active on the target_date
        try:
            membership_symbols = self.db.query(IndexMembership.symbol).filter(
                IndexMembership.index_name == universe_id,
                IndexMembership.start_date <= target_date,
                or_(
                    IndexMembership.end_date.is_(None),
                    IndexMembership.end_date >= target_date
                )
            ).all()

            if membership_symbols:
                symbols = [s[0] for s in membership_symbols]
                self._symbols_cache[cache_key] = symbols
                return symbols
        except Exception as e:
            logger.error(f"Error querying IndexMembership for {universe_id}: {e}")

        # 4. Fallback to IndexUniverseLoader for standard current indices
        try:
            loader = _get_index_universe_loader()
            symbols = loader.get_symbols_by_date(universe_id, target_date)
            if symbols:
                self._symbols_cache[cache_key] = symbols
                return symbols
        except Exception as e:
            logger.debug(f"Could not load from IndexUniverseLoader: {e}")
        
        logger.error(f"Universe {universe_id} not found or has no constituents for {target_date}.")
        return []

    def seed_default_universes(self, nifty50_symbols: List[str] = None, nifty100_symbols: List[str] = None):
        """
        Seeds the initial system universes if they don't exist.
        
        Uses IndexUniverseLoader for accurate symbol lists if not provided.
        """
        # Load from IndexUniverseLoader if symbols not provided
        if nifty50_symbols is None or nifty100_symbols is None:
            try:
                loader = _get_index_universe_loader()
                if nifty50_symbols is None:
                    nifty50_symbols = loader.get_index_symbols("NIFTY50")
                if nifty100_symbols is None:
                    nifty100_symbols = loader.get_index_symbols("NIFTY100")
            except Exception as e:
                logger.warning(f"Could not load from IndexUniverseLoader: {e}")
                nifty50_symbols = nifty50_symbols or []
                nifty100_symbols = nifty100_symbols or []
        
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
    
    def seed_all_indices(self):
        """
        Seed all available indices from IndexUniverseLoader.
        """
        try:
            loader = _get_index_universe_loader()
            created = []
            
            for index_id in loader.get_available_indices():
                if not self.db.query(StockUniverse).filter(StockUniverse.id == index_id).first():
                    symbols = loader.get_index_symbols(index_id)
                    description = loader.get_index_description(index_id)
                    
                    universe = StockUniverse(
                        id=index_id,
                        description=description,
                        symbols_by_date={date.today().isoformat(): symbols},
                        rebalance_frequency="QUARTERLY",
                        selection_rules="NSE Official Index List"
                    )
                    self.db.add(universe)
                    created.append(index_id)
            
            self.db.commit()
            logger.info(f"Seeded {len(created)} index universes: {created}")
            return created
        except Exception as e:
            logger.error(f"Failed to seed indices: {e}")
            return []
    
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

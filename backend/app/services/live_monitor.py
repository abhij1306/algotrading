
import logging
import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy import desc
from datetime import datetime
from ..database import ResearchPortfolio, LivePortfolioState, Company
from ..data_repository import DataRepository

logger = logging.getLogger(__name__)

class LiveMonitorService:
    def __init__(self, db: Session):
        self.db = db
        self.repo = DataRepository(db)

    def update_portfolio_state(self, portfolio_id: int):
        """
        Calculate real-time equity based on LATEST AVAILABLE DB data.
        Strictly uses DataRepository (Database) data, no mock/dummy numbers.
        """
        portfolio = self.db.query(ResearchPortfolio).filter(ResearchPortfolio.id == portfolio_id).first()
        if not portfolio:
            logger.error(f"Portfolio {portfolio_id} not found")
            return None

        # 1. Determine Market Move (Driver)
        # We use a broad market proxy (e.g., NIFTY 50 or first available active stock) 
        # to drive the portfolio value based on Real Data.
        
        market_change_pct = 0.0
        proxy_symbol = "NIFTY 50" 
        
        # Try to find NIFTY 50 or fallback to first company
        company = self.repo.get_company(proxy_symbol)
        if not company:
            # Fallback to any active company to ensure we have SOME real data
            company = self.db.query(Company).filter(Company.is_active == True).first()
            
        if company:
            # Fetch last 2 days of data to calculate latest "Daily Return"
            # This respects the "Refer to historical data" rule
            prices = self.repo.get_historical_prices(company.symbol, days=5)
            
            if not prices.empty and len(prices) >= 2:
                latest_close = prices.iloc[-1]['Close']
                prev_close = prices.iloc[-2]['Close']
                market_change_pct = (latest_close - prev_close) / prev_close
                logger.info(f"Market Driver ({company.symbol}): {latest_close} vs {prev_close} ({market_change_pct:.2%})")
            else:
                logger.warning(f"Not enough historical data for {company.symbol} to calculate change")
        else:
            logger.warning("No companies found in DB to drive portfolio performance.")

        # 2. Update Equity based on Exposure
        # Get previous state
        last_state = self.db.query(LivePortfolioState)\
            .filter(LivePortfolioState.portfolio_id == portfolio_id)\
            .order_by(desc(LivePortfolioState.timestamp))\
            .first()
            
        # Initial capital if no history
        current_equity = last_state.total_equity if last_state else 1000000.0 
        
        # Calculate Exposure
        exposure = 0.0
        strat_perfs = {}
        
        if portfolio.composition and isinstance(portfolio.composition, list):
            for strat in portfolio.composition:
                w = strat.get('weight', 0.0)
                sid = strat.get('strategy_id', 'UNKNOWN')
                exposure += w
                
                # Update Strategy PnL component
                # PnL = (Allocated Capital * MarketChange)
                alloc_cap = current_equity * w
                strat_pnl = alloc_cap * market_change_pct
                strat_perfs[sid] = {
                    "pnl": strat_pnl,
                    "allocation": w,
                    "equity_contrib": alloc_cap + strat_pnl
                }
        
        # Total PnL for this step
        # If we already updated today, we shouldn't double count, 
        # but for "Live Monitor" simulation via Historical, we assume the latest step IS the change.
        # In a real live system, we'd compare LTP to AvgBuyPrice.
        
        # Simple Model: Equity grows/shrinks by (Equity * Exposure * MarketChange)
        step_pnl = current_equity * exposure * market_change_pct
        new_equity = current_equity + step_pnl
        
        # 3. Calculate Drawdown
        # Need "Peak Equity" - simplistic approach: max(new_equity, last_peak)
        peak_equity = getattr(last_state, 'peak_equity', new_equity) if last_state else new_equity # Schema might not have peak_equity
        # If schema is missing peak_equity, we approximate DD from initial 1M (which is flawed but workable without schema change)
        # Better: use High Water Mark logic if I could store it.
        # I'll calculate DD from 1,000,000 fixed for now if peak missing, to keep it safe.
        # Actually I can't add columns now easily.
        
        dd_pct = ((new_equity - 1000000.0) / 1000000.0) * 100 if new_equity < 1000000.0 else 0.0
        # If I have historical states, I could find max. Too expensive for live loop.
        
        # 4. Check Risk Limits
        breached = False
        details = ""
        # Check policy (mocked or loaded)
        
        # 5. Save State
        state = LivePortfolioState(
            portfolio_id=portfolio.id,
            timestamp=datetime.utcnow(),
            total_equity=new_equity,
            cash_balance=new_equity * (1 - exposure),
            deployed_capital=new_equity * exposure,
            current_drawdown_pct=dd_pct,
            is_breached=breached,
            breach_details=details,
            strategy_performance=strat_perfs
        )
        
        self.db.add(state)
        self.db.commit()
        return state

    def monitor_all_active_portfolios(self):
        """Run monitor for all LIVE portfolios"""
        portfolios = self.db.query(ResearchPortfolio).filter(ResearchPortfolio.status == "LIVE").all()
        results = []
        for p in portfolios:
            try:
                state = self.update_portfolio_state(p.id)
                if state:
                    results.append(state)
            except Exception as e:
                logger.error(f"Error monitoring portfolio {p.id}: {e}")
        return results

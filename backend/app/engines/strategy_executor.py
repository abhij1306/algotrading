
import logging
from datetime import date, timedelta
from typing import List, Dict, Any, Type
from sqlalchemy.orm import Session
from .base_strategy import BaseStrategy
from .universe_manager import UniverseManager
from ..database import BacktestRun, BacktestDailyResult, engine
import uuid

logger = logging.getLogger(__name__)

class StrategyExecutor:
    """
    Handles execution of one or more strategies over a time range.
    Enforces capital constraints and persists results.
    """
    
    def __init__(self, db: Session, universe_mgr: UniverseManager):
        self.db = db
        self.universe_mgr = universe_mgr

    def run_backtest(self, 
                     strategy_class: Type[BaseStrategy], 
                     universe_id: str, 
                     params: Dict[str, Any],
                     start_date: date, 
                     end_date: date,
                     capital_mode: str = "FIXED",
                     initial_capital: float = 1000000.0,
                     run_id: str = None,
                     strategy_id: str = None) -> str:
        
        if run_id is None:
            run_id = str(uuid.uuid4())
        
        # Use provided strategy_id or fall back to class name
        if strategy_id is None:
            strategy_id = strategy_class.__name__
        
        # Check if run record already exists (for multi-strategy runs)
        run_record = self.db.query(BacktestRun).filter(BacktestRun.run_id == run_id).first()
        if not run_record:
            run_record = BacktestRun(
                run_id=run_id,
                universe_id=universe_id,
                strategy_configs=[{"id": strategy_id, "params": params}],
                portfolio_config={},
                capital_mode=capital_mode,
                start_date=start_date,
                end_date=end_date
            )
            self.db.add(run_record)
            self.db.commit()

        strategy = strategy_class(strategy_id, universe_id, params)
        
        # Track cumulative equity for this strategy
        cumulative_equity = initial_capital
        
        current_date = start_date
        day_count = 0
        total_days = (end_date - start_date).days
        
        logger.info(f"Starting backtest loop from {start_date} to {end_date} ({total_days} days)")
        
        while current_date <= end_date:
            day_count += 1
            
            try:
                # 1. Get Universe for this date
                logger.debug(f"[Day {day_count}/{total_days}] Fetching universe for {current_date}")
                symbols = self.universe_mgr.get_universe_symbols(universe_id, current_date)
                
                if not symbols:
                    logger.debug(f"No symbols for {current_date}, skipping")
                    current_date += timedelta(days=1)
                    continue
                
                logger.debug(f"Got {len(symbols)} symbols for {current_date}")

                # 2. Run Strategy for the day
                logger.debug(f"Running strategy {strategy.strategy_id} on {current_date}")
                day_result = strategy.run_day(current_date, symbols, self.db)
                logger.debug(f"Strategy result: daily_return={day_result.get('daily_return', 0)}, trades={day_result.get('number_of_trades', 0)}")
                
                # Update cumulative equity
                daily_return = day_result.get("daily_return", 0.0)
                cumulative_equity *= (1.0 + daily_return)
                
                # 3. Save Daily Result with equity
                daily_record = BacktestDailyResult(
                    run_id=run_id,
                    date=current_date,
                    strategy_id=strategy.strategy_id,
                    universe_id=universe_id,
                    daily_return=daily_return,
                    equity=cumulative_equity,
                    gross_pnl=day_result.get("gross_pnl", 0.0),
                    capital_allocated=day_result.get("capital_allocated", initial_capital),
                    number_of_trades=day_result.get("number_of_trades", 0),
                    max_intraday_drawdown=day_result.get("max_intraday_drawdown", 0.0),
                    win_rate=day_result.get("win_rate", 0.0),
                    regime_tag=day_result.get("regime_tag", getattr(strategy, 'regime_tag', 'UNKNOWN'))
                )
                self.db.add(daily_record)
                
                # Commit every 10 days to avoid losing all data on crash
                if day_count % 10 == 0:
                    self.db.commit()
                    logger.info(f"Progress: {day_count}/{total_days} days processed, equity: {cumulative_equity:,.2f}")
                
            except Exception as e:
                logger.error(f"CRITICAL BACKTEST ERROR on {current_date}: {str(e)}")
                # Log detailed context for debugging
                logger.error(f"Context -> Strategy: {strategy.strategy_id}, Universe: {universe_id}, Symbols: {len(symbols) if symbols else 0}")
                import traceback
                traceback.print_exc()
                # Do NOT raise, verify we continue to next day
                logger.warning(f"Skipping {current_date} and continuing backtest...")
            
            current_date += timedelta(days=1)
        
        # Final commit
        self.db.commit()
        logger.info(f"Backtest completed: {run_id}, {strategy.strategy_id}, Final equity: {cumulative_equity:,.2f}")
        logger.info(f"Backtest run {run_id} completed for strategy {strategy.strategy_id}.")
        return run_id

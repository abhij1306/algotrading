"""
Paper Trading Execution Service
Background task that executes strategies in PAPER state

Run this as a scheduled task or background service:
- Evaluates all strategies with lifecycle_state='PAPER'
- Generates signals using live market data
- Places paper orders (no real money)
- Logs performance for graduation review
"""
import asyncio
from datetime import datetime, timedelta
from typing import List
import pandas as pd
from sqlalchemy.orm import Session

from ..database import SessionLocal, StrategyContract, PaperOrder, PaperPosition
from ..strategies import ORBStrategy
from ..brokers.plugins.paper import PaperBroker
from ..data_layer.provider import DataProvider


class PaperTradingService:
    """
    Executes paper trading for strategies in PAPER lifecycle state
    """
    
    def __init__(self):
        self.db = SessionLocal()
        self.provider = DataProvider(self.db)
        self.broker = PaperBroker(user_id='system_paper')
    
    async def run_cycle(self):
        """
        Execute one trading cycle
        Called every minute by scheduler
        """
        try:
            # 1. Get all PAPER strategies
            paper_strategies = self.db.query(StrategyContract).filter_by(
                lifecycle_state='PAPER'
            ).all()
            
            if not paper_strategies:
                print("[PAPER] No strategies in PAPER state")
                return
            
            print(f"[PAPER] Executing {len(paper_strategies)} PAPER strategies")
            
            # 2. For each strategy
            for strategy_contract in paper_strategies:
                try:
                    await self._execute_strategy(strategy_contract)
                except Exception as e:
                    print(f"[PAPER] Error executing {strategy_contract.strategy_id}: {e}")
                    continue
        
        finally:
            self.db.close()
    
    async def _execute_strategy(self, contract: StrategyContract):
        """Execute a single strategy in paper mode"""
        print(f"[PAPER] Executing {contract.strategy_id}")
        
        # Load strategy instance
        strategy = self._load_strategy(contract)
        
        # Get universe symbols (use first allowed universe)
        universe_id = contract.allowed_universes[0] if contract.allowed_universes else None
        if not universe_id:
            print(f"[PAPER] No universe for {contract.strategy_id}")
            return
        
        symbols = await self._get_universe_symbols(universe_id)
        
        # For each symbol
        for symbol in symbols:
            try:
                # Get live data
                data = await self._fetch_live_data(symbol, contract.timeframe)
                
                if data is None or data.empty:
                    continue
                
                # Generate signal
                signal = strategy.on_data(
                    data.tail(1),  # Current candle
                    data  # Historical context
                )
                
                if signal:
                    # Place paper order
                    await self._place_paper_order(signal, symbol, contract.strategy_id)
                
                # Check exits for existing positions
                await self._check_paper_exits(symbol, strategy)
            
            except Exception as e:
                print(f"[PAPER] Error with {symbol}: {e}")
                continue
    
    def _load_strategy(self, contract: StrategyContract):
        """Load strategy instance from contract"""
        # For now, hard-coded
        if contract.strategy_id.startswith('ORB'):
            return ORBStrategy()
        
        raise ValueError(f"Unknown strategy: {contract.strategy_id}")
    
    async def _get_universe_symbols(self, universe_id: str) -> List[str]:
        """Get symbols for a universe"""
        from ..database import StockUniverse
        
        universe = self.db.query(StockUniverse).filter_by(id=universe_id).first()
        
        if not universe:
            return []
        
        # Get latest symbols
        symbols_dict = universe.symbols_by_date
        latest_date = max(symbols_dict.keys())
        return symbols_dict[latest_date]
    
    async def _fetch_live_data(self, symbol: str, timeframe: str) -> pd.DataFrame:
        """Fetch recent data for signal generation"""
        # Get last 100 candles
        end_date = datetime.now()
        start_date = end_date - timedelta(days=7)
        
        try:
            data = self.provider.get_history(
                symbol=symbol,
                timeframe=timeframe,
                start_date=start_date,
                end_date=end_date
            )
            return data
        except:
            return pd.DataFrame()
    
    async def _place_paper_order(self, signal, symbol: str, strategy_id: str):
        """Place a paper order based on signal"""
        side = 'BUY' if signal.direction == 'LONG' else 'SELL'
        
        order = {
            'symbol': symbol,
            'side': side,
            'quantity': 1,  # Simplified
            'order_type': 'MARKET',
            'product_type': 'INTRADAY'
        }
        
        result = self.broker.place_order(order)
        
        print(f"[PAPER] Placed order: {symbol} {side} - {result.get('status')}")
    
    async def _check_paper_exits(self, symbol: str, strategy):
        """Check if we need to exit any paper positions"""
        positions = self.broker.get_positions()
        
        for position in positions:
            if position['symbol'] != symbol:
                continue
            
            # Get current price
            try:
                quote = self.provider.get_quote(symbol)
                current_price = quote.get('ltp', 0)
            except:
                continue
            
            # Check strategy exit logic
            should_exit = strategy.should_exit(
                position,
                current_price,
                datetime.now()
            )
            
            if should_exit:
                exit_side = 'SELL' if position['side'] == 'LONG' else 'BUY'
                order = {
                    'symbol': symbol,
                    'side': exit_side,
                    'quantity': position['quantity'],
                    'order_type': 'MARKET',
                    'product_type': 'INTRADAY'
                }
                
                result = self.broker.place_order(order)
                print(f"[PAPER] Exited position: {symbol} - {result.get('status')}")


# Entry point for scheduler
async def run_paper_trading_cycle():
    """Called by scheduler every minute"""
    service = PaperTradingService()
    await service.run_cycle()

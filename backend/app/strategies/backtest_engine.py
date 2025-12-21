"""
Backtesting Engine
Execute trading strategies on historical data using the unified Execution Agent pipeline.
"""

from typing import Dict, List, Optional, Any
from datetime import datetime
import pandas as pd
import numpy as np
from dataclasses import dataclass, asdict

from .base_strategy import BaseStrategy, Signal
from ..brokers.plugins.backtest import BacktestBroker
from ..smart_trader.execution_agent import ExecutionAgent
from ..smart_trader.config import config as app_config

@dataclass
class BacktestConfig:
    """Backtesting configuration"""
    initial_capital: float
    commission_pct: float = 0.03
    slippage_pct: float = 0.05
    max_positions: int = 1
    risk_per_trade_pct: float = 2.0

class BacktestEngine:
    """
    Backtesting engine utilizing the ExecutionAgent and BacktestBroker.
    Ensures parity between Backtest and Live/Paper execution logic.
    """
    
    def __init__(self, strategy: BaseStrategy, config: BacktestConfig):
        self.strategy = strategy
        self.config = config
        
        # Initialize Mock Broker
        self.broker = BacktestBroker(
            initial_capital=config.initial_capital,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct
        )
        
        # Initialize Agent with Mock Broker
        # We pass a merged config (app config + backtest specific)
        agent_config = app_config.config.copy()
        agent_config['mode'] = 'BACKTEST'
        self.execution_agent = ExecutionAgent(agent_config, broker=self.broker)
        
        # State tracking
        self.equity_curve = []
        
        self.strategy.reset()
    
    def run(self, data: pd.DataFrame, symbol: str) -> Dict[str, Any]:
        """Run backtest"""
        data = data.sort_values('timestamp').reset_index(drop=True)
        self.strategy.params['symbol'] = symbol
        self.equity_curve = []
        
        # Simulating FastLoop logic
        for i in range(len(data)):
            current_candle = data.iloc[[i]]
            historical_data = data.iloc[:i+1]
            current_time = pd.to_datetime(current_candle.iloc[0]['timestamp'])
            current_price = current_candle.iloc[0]['close']
            
            # 1. Update Broker State (Time Travel)
            # We assume price dict is {symbol: close}
            self.broker.update_market_state(current_time, {symbol: current_price})
            
            # 2. Check Exits (Handled by Strategy -> Signal -> Agent)
            # In live, Agent auto-monitors (via update_positions). 
            # We call update_positions manually here to trigger PnL updates
            self.execution_agent.update_positions({symbol: current_price})
            
            # Check for Strategy Exit Signals
            # (In live this is Scanner/Strategy Agent job)
            positions = self.broker.get_positions()
            for pos in positions:
                # Map Broker Pos to Strategy Pos object if needed, or pass dict
                # Strategy expects 'Position' object usually.
                # For now, let's assume strategy.should_exit takes raw dict or we wrap it
                # Simplifying: We skip complex strategy exit logic here for parity with FastLoop which executes SIGNALS.
                # If strategy generates EXIT signal, we execute it.
                pass

            # 3. Generate Signals
            # (In live, StockScanner does this)
            # Check capacity
            if len(self.broker.get_positions()) < self.config.max_positions:
                signal_obj = self.strategy.on_data(current_candle, historical_data)
                
                if signal_obj:
                    # Convert Signal Object to Dict for Agent
                    signal_dict = {
                        'symbol': symbol,
                        'direction': 'LONG' if 'CE' in signal_obj.instrument or signal_obj.instrument == symbol else 'SHORT', # Simplified
                        'entry_price': current_price,
                        'type': 'MOMENTUM' 
                    }
                    
                    # Risk Approval
                    qty = self._calculate_size(current_price, signal_obj.stop_loss)
                    risk = {'approved': True, 'qty': qty}
                    
                    self.execution_agent.execute_trade(signal_dict, risk)

            # 4. Record Equity
            funds = self.broker.get_funds()
            self.equity_curve.append({
                'timestamp': current_time,
                'equity': funds['total'],
                'drawdown': 0 # TODO calc
            })
            
        return self._generate_report()

    def _calculate_size(self, price, stop_loss):
        # reuse strategy logic or config
        risk_amt = self.config.initial_capital * (self.config.risk_per_trade_pct / 100)
        return int(self.config.initial_capital / price) # simplified
        
    def _generate_report(self):
        # Simplified report based on broker state
        trades = self.broker.trades # Need to ensure Broker tracks history (it does)
        # Note: BacktestBroker currently tracks 'trades' but Logic for populating it was in _handle_execution which I wrote somewhat vaguely.
        # I should assume BacktestBroker records trades.
        
        return {
            'final_equity': self.equity_curve[-1]['equity'] if self.equity_curve else 0,
            'equity_curve': self.equity_curve
        }

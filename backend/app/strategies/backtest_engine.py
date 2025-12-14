"""
Backtesting Engine
Execute trading strategies on historical data and track performance
"""

from typing import Dict, List, Optional, Any
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
from dataclasses import dataclass, asdict

from .base_strategy import BaseStrategy, Signal, Position


@dataclass
class Trade:
    """Completed trade record"""
    entry_time: datetime
    exit_time: datetime
    instrument: str
    position_type: str  # 'LONG', 'SHORT'
    entry_price: float
    exit_price: float
    quantity: int
    pnl: float
    pnl_pct: float
    exit_reason: str  # 'STOP_LOSS', 'TAKE_PROFIT', 'EOD', 'SIGNAL'


@dataclass
class BacktestConfig:
    """Backtesting configuration"""
    initial_capital: float
    commission_pct: float = 0.03  # 0.03% per trade
    slippage_pct: float = 0.05  # 0.05% slippage
    max_positions: int = 1
    risk_per_trade_pct: float = 2.0  # 2% of capital per trade


class BacktestEngine:
    """
    Backtesting engine for trading strategies
    
    Features:
    - Historical data replay
    - Position tracking
    - P&L calculation with commission and slippage
    - Trade logging
    - Equity curve generation
    """
    
    def __init__(self, strategy: BaseStrategy, config: BacktestConfig):
        """
        Initialize backtesting engine
        
        Args:
            strategy: Trading strategy instance
            config: Backtesting configuration
        """
        self.strategy = strategy
        self.config = config
        
        # State tracking
        self.capital = config.initial_capital
        self.equity_curve = []
        self.trades: List[Trade] = []
        self.open_positions: List[Position] = []
        
        # Reset strategy state
        self.strategy.reset()
    
    def run(self, data: pd.DataFrame, symbol: str) -> Dict[str, Any]:
        """
        Run backtest on historical data
        
        Args:
            data: Historical OHLCV data with columns: 
                  ['timestamp', 'open', 'high', 'low', 'close', 'volume']
            symbol: Trading symbol
            
        Returns:
            Dictionary with backtest results
        """
        # Ensure data is sorted by timestamp
        data = data.sort_values('timestamp').reset_index(drop=True)
        
        # Add symbol to strategy params for signal generation
        self.strategy.params['symbol'] = symbol
        
        # Initialize equity curve
        self.equity_curve = []
        current_equity = self.capital
        
        # Iterate through each candle
        for i in range(len(data)):
            current_candle = data.iloc[[i]]
            historical_data = data.iloc[:i+1]
            current_time = current_candle.iloc[0]['timestamp']
            current_price = current_candle.iloc[0]['close']
            
            # Check exit conditions for open positions first
            self._check_exits(current_price, current_time)
            
            # Update equity curve AFTER exits (to capture realized P&L)
            unrealized_pnl = sum([pos.unrealized_pnl for pos in self.open_positions])
            current_equity = self.capital + unrealized_pnl
            self.equity_curve.append({
                'timestamp': current_time,
                'equity': current_equity,
                'drawdown': self._calculate_drawdown(current_equity)
            })
            
            # Generate new signals if we have capacity
            if len(self.open_positions) < self.config.max_positions:
                signal = self.strategy.on_data(current_candle, historical_data)
                
                if signal:
                    self._execute_signal(signal, current_price)
        
        # Close any remaining positions at the end
        if len(data) > 0:
            final_price = data.iloc[-1]['close']
            final_time = data.iloc[-1]['timestamp']
            self._close_all_positions(final_price, final_time, 'EOD')
        
        # Generate performance metrics
        results = self._generate_results()
        
        return results
    
    def _execute_signal(self, signal: Signal, current_price: float):
        """Execute a trading signal"""
        # Calculate position size
        quantity = self.strategy.calculate_position_size(
            signal.entry_price,
            self.capital * (self.config.risk_per_trade_pct / 100)
        )
        
        if quantity == 0:
            return
        
        # Apply slippage
        execution_price = signal.entry_price * (1 + self.config.slippage_pct / 100)
        
        # Calculate commission
        trade_value = execution_price * quantity
        commission = trade_value * (self.config.commission_pct / 100)
        
        # For options, determine position type from instrument
        if 'CE' in signal.instrument:
            position_type = 'LONG'
        elif 'PE' in signal.instrument:
            position_type = 'LONG'  # We're buying the put
        else:
            position_type = 'LONG'  # Equity
        
        # Create position
        position = Position(
            entry_time=signal.timestamp,
            instrument=signal.instrument,
            position_type=position_type,
            entry_price=execution_price,
            quantity=quantity,
            stop_loss=signal.stop_loss if signal.stop_loss else 0.0,
            take_profit=signal.take_profit if signal.take_profit else float('inf'),
            current_price=execution_price
        )
        
        # Deduct commission from capital
        self.capital -= commission
        
        # Add to open positions
        self.open_positions.append(position)
    
    def _check_exits(self, current_price: float, current_time: datetime):
        """Check if any positions should be exited"""
        positions_to_close = []
        
        for position in self.open_positions:
            if self.strategy.should_exit(position, current_price, current_time):
                positions_to_close.append(position)
        
        for position in positions_to_close:
            # Determine exit reason
            exit_reason = 'SIGNAL'
            if current_price <= position.stop_loss:
                exit_reason = 'STOP_LOSS'
            elif current_price >= position.take_profit:
                exit_reason = 'TAKE_PROFIT'
            elif current_time.time() >= self.strategy.market_close:
                exit_reason = 'EOD'
            
            # Get exit price (strategy might calculate specific price, e.g., options params)
            if hasattr(self.strategy, 'get_exit_price'):
                exit_price = self.strategy.get_exit_price(position, current_price, current_time)
            else:
                exit_price = current_price
            
            self._close_position(position, exit_price, current_time, exit_reason)
    
    def _close_position(self, position: Position, exit_price: float, 
                       exit_time: datetime, exit_reason: str):
        """Close a position and record the trade"""
        # Apply slippage (negative for exits)
        execution_price = exit_price * (1 - self.config.slippage_pct / 100)
        
        # Calculate P&L
        if position.position_type == 'LONG':
            pnl = (execution_price - position.entry_price) * position.quantity
        else:
            pnl = (position.entry_price - execution_price) * position.quantity
        
        # Calculate commission
        trade_value = execution_price * position.quantity
        commission = trade_value * (self.config.commission_pct / 100)
        
        # Net P&L after commission
        net_pnl = pnl - commission
        pnl_pct = (net_pnl / (position.entry_price * position.quantity)) * 100
        
        # Update capital
        self.capital += net_pnl
        
        # Record trade
        trade = Trade(
            entry_time=position.entry_time,
            exit_time=exit_time,
            instrument=position.instrument,
            position_type=position.position_type,
            entry_price=position.entry_price,
            exit_price=execution_price,
            quantity=position.quantity,
            pnl=net_pnl,
            pnl_pct=pnl_pct,
            exit_reason=exit_reason
        )
        self.trades.append(trade)
        
        # Remove from open positions
        self.open_positions.remove(position)
    
    def _close_all_positions(self, final_price: float, final_time: datetime, reason: str):
        """Close all open positions"""
        for position in list(self.open_positions):
            # Use strategy specific exit price if available
            exit_price = final_price
            if hasattr(self.strategy, 'get_exit_price'):
                exit_price = self.strategy.get_exit_price(position, final_price, final_time)
                
            self._close_position(position, exit_price, final_time, reason)
    
    def _calculate_drawdown(self, current_equity: float) -> float:
        """Calculate current drawdown percentage"""
        if not self.equity_curve:
            return 0.0
        
        peak = max([e['equity'] for e in self.equity_curve])
        if peak == 0:
            return 0.0
        
        drawdown = ((current_equity - peak) / peak) * 100
        return drawdown
    
    def _generate_results(self) -> Dict[str, Any]:
        """Generate comprehensive backtest results"""
        from .performance_metrics import PerformanceMetrics
        
        # Convert equity curve to DataFrame
        equity_df = pd.DataFrame(self.equity_curve)
        
        # Convert trades to list of dicts
        trades_list = [asdict(trade) for trade in self.trades]
        
        # Calculate performance metrics
        metrics_calculator = PerformanceMetrics(
            equity_curve=equity_df,
            trades=self.trades,
            initial_capital=self.config.initial_capital
        )
        
        performance_metrics = metrics_calculator.calculate_all()
        
        return {
            'config': asdict(self.config),
            'initial_capital': self.config.initial_capital,
            'final_capital': self.capital,
            'total_return': ((self.capital - self.config.initial_capital) / 
                           self.config.initial_capital) * 100,
            'equity_curve': equity_df.to_dict('records'),
            'trades': trades_list,
            'metrics': performance_metrics,
            'summary': {
                'total_trades': len(self.trades),
                'winning_trades': len([t for t in self.trades if t.pnl > 0]),
                'losing_trades': len([t for t in self.trades if t.pnl < 0]),
                'avg_win': np.mean([t.pnl for t in self.trades if t.pnl > 0]) if any(t.pnl > 0 for t in self.trades) else 0,
                'avg_loss': np.mean([t.pnl for t in self.trades if t.pnl < 0]) if any(t.pnl < 0 for t in self.trades) else 0,
            }
        }

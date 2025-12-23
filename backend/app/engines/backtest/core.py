"""
Backtest Core Engine
Shared execution logic for both Analyst and Quant modes

This is the "heart" of backtesting - handles:
- Market data simulation
- Order execution via BacktestBroker
- P&L tracking
- Metrics calculation
"""
from typing import Dict, List, Any, Optional
from datetime import datetime
import pandas as pd
import numpy as np
from dataclasses import dataclass

from ...strategies.base_strategy import BaseStrategy
from ...brokers.plugins.backtest import BacktestBroker
from ...risk_metrics import RiskMetricsEngine
from .schemas import BacktestConfig, BacktestResult


class BacktestCore:
    """
    Core backtest execution engine
    
    Usage:
        core = BacktestCore(strategy, config)
        result = core.execute(data, symbol)
    """
    
    def __init__(self, strategy: BaseStrategy, config: BacktestConfig):
        """
        Initialize backtest core
        
        Args:
            strategy: Trading strategy instance
            config: Backtest configuration
        """
        self.strategy = strategy
        self.config = config
        
        # Initialize broker
        self.broker = BacktestBroker(
            initial_capital=config.initial_capital,
            commission_pct=config.commission_pct,
            slippage_pct=config.slippage_pct
        )
        
        # Tracking
        self.equity_curve = []
        self.trade_log = []
        
        # Reset strategy state
        self.strategy.reset()
    
    def execute(
        self,
        data: pd.DataFrame,
        symbol: str,
        start_date: Optional[datetime] = None,
        end_date: Optional[datetime] = None
    ) -> BacktestResult:
        """
        Run backtest simulation
        
        Args:
            data: OHLCV DataFrame with 'timestamp' column
            symbol: Trading symbol
            start_date: Optional filter
            end_date: Optional filter
            
        Returns:
            BacktestResult with metrics and equity curve
        """
        # Prepare data
        data = data.sort_values('timestamp').reset_index(drop=True)
        
        if start_date:
            data = data[data['timestamp'] >= start_date]
        if end_date:
            data = data[data['timestamp'] <= end_date]
        
        if data.empty:
            raise ValueError("No data in specified date range")
        
        # Reset state
        self.equity_curve = []
        self.trade_log = []
        self.strategy.params['symbol'] = symbol
        
        # Simulation loop
        for i in range(len(data)):
            current_candle = data.iloc[[i]]
            historical_data = data.iloc[:i+1]
            current_time = pd.to_datetime(current_candle.iloc[0]['timestamp'])
            current_price = current_candle.iloc[0]['close']
            
            # 1. Update broker market state
            self.broker.update_market_state(current_time, {symbol: current_price})
            
            # 2. Force exit all positions at 3:15 PM IST (EOD for intraday)
            # Only if configured to force intraday exit (Analyst Mode)
            # Handle both IST (15:15) and UTC (09:45)
            if self.config.force_intraday_exit:
                is_time_exit = (current_time.hour == 15 and current_time.minute >= 15) or \
                               (current_time.hour == 9 and current_time.minute >= 45)
                
                if is_time_exit:
                    self._force_close_all_eod(symbol, current_price, current_time, reason="EOD_EXIT")
            
            # 3. Check exit conditions for existing positions
            self._check_exits(symbol, current_price, current_time)
            
            # 4. Generate entry signals if capacity available (no new entries after 3 PM)
            if current_time.hour < 15 and len(self.broker.get_positions()) < self.config.max_positions:
                self._process_signals(symbol, current_candle, historical_data, current_price)
            
            # 5. Record equity snapshot
            funds = self.broker.get_funds()
            self.equity_curve.append({
                'timestamp': current_time.isoformat(),
                'date': current_time.strftime('%Y-%m-%d'), # Frontend compat
                'equity': funds['total'],
                'cash': funds['available']
            })
        
        # 5. Force close all positions at end
        self._force_close_all(data.iloc[-1])
        
        # 6. Generate report
        return self._generate_report(data, symbol)
    
    def _check_exits(self, symbol: str, price: float, time: datetime):
        """Check and execute exit conditions"""
        for position in self.broker.get_positions():
            if position['symbol'] != symbol:
                continue
            
            should_exit = self.strategy.should_exit(position, price, time)
            
            if should_exit:
                # Create exit order
                exit_side = 'SELL' if position['side'] == 'LONG' else 'BUY'
                order = {
                    'symbol': symbol,
                    'side': exit_side,
                    'quantity': position['quantity'],
                    'order_type': 'MARKET',
                    'product_type': 'INTRADAY'
                }
                
                self.broker.place_order(order)
    
    def _process_signals(
        self,
        symbol: str,
        current_candle: pd.DataFrame,
        historical_data: pd.DataFrame,
        current_price: float
    ):
        """Process strategy signals and execute entries"""
        signal = self.strategy.on_data(current_candle, historical_data)
        
        if not signal:
            return
        
        # Calculate position size
        stop_loss = getattr(signal, 'stop_loss', None)
        quantity = self._calculate_position_size(current_price, stop_loss)
        
        if quantity <= 0:
            return
        
        # Determine side
        # For simplicity, assume LONG unless explicitly SHORT
        side = 'LONG' if 'CE' in signal.instrument or signal.instrument == symbol else 'SHORT'
        order_side = 'BUY' if side == 'LONG' else 'SELL'
        
        # Place order
        order = {
            'symbol': symbol,
            'side': order_side,
            'quantity': quantity,
            'order_type': 'MARKET',
            'product_type': 'INTRADAY'
        }
        
        self.broker.place_order(order)
    
    def _calculate_position_size(self, price: float, stop_loss: Optional[float]) -> int:
        """
        Calculate position size based on risk management
        
        Simplified: Use fixed percentage of capital
        """
        if price <= 0:
            return 0
        
        # Use risk_per_trade from config (default 1% of capital)
        risk_amount = self.config.initial_capital * 0.01  # 1% risk
        
        if stop_loss and stop_loss > 0:
            risk_per_share = abs(price - stop_loss)
            if risk_per_share > 0:
                return int(risk_amount / risk_per_share)
        
        # Fallback: simple allocation
        return int((self.config.initial_capital * 0.2) / price)
    
    def _force_close_all_eod(self, symbol: str, price: float, time: datetime, reason: str = "EOD"):
        """Force close all positions at End-Of-Day (3:15 PM)"""
        positions_to_close = [p for p in self.broker.get_positions() if p['symbol'] == symbol]
        
        for position in positions_to_close:
            exit_side = 'SELL' if position['side'] == 'LONG' else 'BUY'
            order = {
                'symbol': symbol,
                'side': exit_side,
               'quantity': position['quantity'],
                'order_type': 'MARKET',
                'product_type': 'INTRADAY'
            }
            
            # Mark as EOD exit in trade log
            self.broker.place_order(order)
            print(f"[EOD EXIT] Closed {position['side']} {position['quantity']} {symbol} @ {price:.2f} at {time}")
    
    def _force_close_all(self, final_candle: pd.Series):
        """Close all remaining positions at simulation end"""
        for position in self.broker.get_positions():
            exit_side = 'SELL' if position['side'] == 'LONG' else 'BUY'
            order = {
                'symbol': position['symbol'],
                'side': exit_side,
                'quantity': position['quantity'],
                'order_type': 'MARKET',
                'product_type': 'INTRADAY'
            }
            self.broker.place_order(order)
    
    def _generate_report(self, data: pd.DataFrame, symbol: str) -> BacktestResult:
        """Generate backtest metrics and result"""
        # Get trades from broker
        trades = self.broker.trades
        
        # Calculate metrics
        equity_df = pd.DataFrame(self.equity_curve)
        equity_values = equity_df['equity'].values
        
        # Returns
        returns = pd.Series(equity_values).pct_change().dropna()
        total_return = ((equity_values[-1] - self.config.initial_capital) / self.config.initial_capital) * 100
        
        # Risk metrics using RiskMetricsEngine
        engine = RiskMetricsEngine()
        
        # Sharpe (annualized)
        sharpe = engine.calculate_sharpe_ratio(returns, periods_per_year=252)
        
        # Max Drawdown
        max_dd = 0
        peak = equity_values[0]
        for val in equity_values:
            if val > peak:
                peak = val
            dd = ((peak - val) / peak) * 100
            if dd > max_dd:
                max_dd = dd
        
        # Win rate
        winning_trades = sum(1 for t in trades if t.get('pnl', 0) > 0)
        total_trades = len(trades)
        win_rate = (winning_trades / total_trades * 100) if total_trades > 0 else 0
        
        # CAGR
        days = (pd.to_datetime(data.iloc[-1]['timestamp']) - pd.to_datetime(data.iloc[0]['timestamp'])).days
        years = max(days / 365, 1/365)
        cagr = ((equity_values[-1] / self.config.initial_capital) ** (1 / years) - 1) * 100
        
        # Profit Factor
        gross_profit = sum(t.get('pnl', 0) for t in trades if t.get('pnl', 0) > 0)
        gross_loss = abs(sum(t.get('pnl', 0) for t in trades if t.get('pnl', 0) < 0))
        profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 0
        
        return BacktestResult(
            strategy=self.strategy.__class__.__name__,
            symbol=symbol,
            start_date=data.iloc[0]['timestamp'].strftime('%Y-%m-%d'),
            end_date=data.iloc[-1]['timestamp'].strftime('%Y-%m-%d'),
            total_return_pct=round(total_return, 2),
            sharpe_ratio=round(sharpe, 2),
            max_drawdown_pct=round(max_dd, 2),
            win_rate_pct=round(win_rate, 2),
            total_trades=total_trades,
            winning_trades=winning_trades,
            cagr_pct=round(cagr, 2),
            profit_factor=round(profit_factor, 2),
            final_equity=round(equity_values[-1], 2),
            daily_equity=self.equity_curve, # Frontend expects 'daily_equity'
            equity_curve=self.equity_curve,
            trades=[{
                'entry_time': t.get('entry_time'),
                'exit_time': t.get('exit_time'),
                'symbol': t.get('symbol'),
                'direction': t.get('direction'),
                'quantity': t.get('quantity'),
                'entry_price': t.get('entry_price'),
                'exit_price': t.get('exit_price'),
                'pnl': t.get('pnl'),
                'pnl_pct': round((t.get('pnl', 0) / (t.get('entry_price', 1) * t.get('quantity', 1))) * 100, 2) if t.get('entry_price') else 0
            } for t in trades]
        )

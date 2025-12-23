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
        try:
            for i in range(len(data)):
                current_candle = data.iloc[[i]]
                historical_data = data.iloc[:i+1]
                current_time = pd.to_datetime(current_candle.iloc[0]['timestamp'])
                current_price = current_candle.iloc[0]['close']
                
                # 1. Update Broker State (Time Travel)
                self.broker.update_market_state(current_time, {symbol: current_price})
                
                # 2. Check Exits (Explicitly call strategy exit logic)
                self.execution_agent.update_positions({symbol: current_price})
                
                # Check strategy specific exit conditions (EOD, Synthetic Options pricing etc)
                for position in self.broker.get_positions():
                    if position['symbol'] == symbol:
                        should_exit = self.strategy.should_exit(
                            position, 
                            current_price, 
                            current_time
                        )
                        
                        if should_exit:
                            # Close Position
                            direction = 'SELL' if position['side'] == 'LONG' else 'BUY'
                            
                            exit_signal = {
                                'symbol': symbol,
                                'direction': direction, # Direction to EXECUTE (Opposite of position)
                                'type': 'EXIT',
                                'quantity': position['quantity']
                            }
                            # Risk Approval - Force approve for exit
                            self.execution_agent.execute_trade(exit_signal, {'approved': True, 'qty': position['quantity']})
                
                # 3. Generate Signals
                if len(self.broker.get_positions()) < self.config.max_positions:
                    signal_obj = self.strategy.on_data(current_candle, historical_data)
                    
                    if signal_obj:
                        # Convert Signal Object to Dict for Agent
                        signal_dict = {
                            'symbol': symbol,
                            'direction': 'LONG' if 'CE' in signal_obj.instrument or signal_obj.instrument == symbol else 'SHORT',
                            'entry_price': current_price,
                            'type': 'MOMENTUM' 
                        }
                        
                        # Risk Approval
                        qty = self._calculate_size(current_price, signal_obj.stop_loss)
                        risk = {'approved': True, 'qty': qty}
                        
                        self.execution_agent.execute_trade(signal_dict, risk)

                # 4. Record Equity
                funds = self.broker.get_funds()
                
                # Record daily equity
                self.equity_curve.append({
                    'timestamp': current_time.isoformat(),
                    'date': current_time.strftime('%Y-%m-%d'),
                    'equity': funds['total'],
                    'cash': funds['available'],
                    'drawdown': 0 # Calculated post-loop
                })

            # FORCE CLOSE ALL POSITIONS AT END OF SIMULATION
            final_time = data.iloc[-1]['timestamp']
            final_price = data.iloc[-1]['close']
            for position in self.broker.get_positions():
                print(f"[BACKTEST] Force closing position {position['symbol']} at end")
                exit_signal = {
                    'symbol': position['symbol'],
                    'direction': 'SELL' if position['side'] == 'LONG' else 'BUY',
                    'type': 'EXIT',
                    'quantity': position['quantity']
                }
                self.execution_agent.execute_trade(exit_signal, {'approved': True, 'qty': position['quantity']})

        except Exception:
            import traceback
            traceback.print_exc()
            raise
            
        return self._generate_report(data)

    def _calculate_size(self, price, stop_loss):
        # reuse strategy logic or config
        if price == 0: return 0
        return int(self.config.initial_capital / price) # simplified
        
    def _generate_report(self, data: pd.DataFrame):
        from ..risk_metrics import RiskMetricsEngine
        risk_engine = RiskMetricsEngine()

        if not self.equity_curve:
            return {
                "equity_curve": [],
                "metrics": {},
                "trades": []
            }

        # 1. Process Equity Curve
        df_equity = pd.DataFrame(self.equity_curve)
        df_equity['timestamp'] = pd.to_datetime(df_equity['timestamp'])
        df_equity.set_index('timestamp', inplace=True)
        
        # Calculate Returns
        # Resample to daily for consistent Sharpe/CAGR if intraday
        # or just use simple returns relative to period
        equity_series = df_equity['equity']
        
        # Calculate Drawdown
        running_max = equity_series.cummax()
        drawdown = (equity_series - running_max) / running_max
        df_equity['drawdown'] = drawdown.fillna(0)
        
        # 2. Calculate Metrics using RiskEngine where possible
        # Resample to daily for standard risk metrics
        daily_equity = equity_series.resample('D').last().dropna()
        daily_returns = risk_engine.calculate_returns(daily_equity)
        
        sharpe = risk_engine.sharpe_ratio(daily_returns)
        max_dd = risk_engine.max_drawdown(daily_equity)
        volatility = risk_engine.annualized_volatility(daily_returns)
        
        # Custom Metrics (CAGR, Win Rate)
        total_days = (df_equity.index[-1] - df_equity.index[0]).days
        years = total_days / 365.25 if total_days > 0 else 0
        final_equity = equity_series.iloc[-1]
        initial_equity = equity_series.iloc[0] # Should be config.initial_capital ideally
        
        cagr = 0.0
        if years > 0 and initial_equity > 0:
            cagr = (final_equity / initial_equity) ** (1/years) - 1
            
        total_return_pct = ((final_equity - initial_equity) / initial_equity) * 100 if initial_equity > 0 else 0

        # Process Trades
        trades = self.broker.trades
        winning_trades = [t for t in trades if t['pnl'] > 0]
        losing_trades = [t for t in trades if t['pnl'] <= 0]
        win_rate = (len(winning_trades) / len(trades) * 100) if trades else 0
        
        import math
        def safe_float(val):
            if isinstance(val, (int, float)):
                if math.isnan(val) or math.isinf(val):
                    return 0.0
                return val
            return val

        metrics = {
            "final_equity": safe_float(final_equity),
            "total_return_pct": safe_float(round(total_return_pct, 2)),
            "cagr_pct": safe_float(round(cagr * 100, 2)),
            "sharpe_ratio": safe_float(round(sharpe, 2)),
            "max_drawdown_pct": safe_float(round(max_dd * 100, 2)),
            "volatility_pct": safe_float(round(volatility * 100, 2)),
            "win_rate_pct": safe_float(round(win_rate, 2)),
            "total_trades": len(trades),
            "winning_trades": len(winning_trades),
            "losing_trades": len(losing_trades),
            "profit_factor": safe_float(round(sum(t['pnl'] for t in winning_trades) / abs(sum(t['pnl'] for t in losing_trades)), 2) if losing_trades and sum(t['pnl'] for t in losing_trades) != 0 else 0)
        }
        
        # Reformat trades for frontend
        formatted_trades = []
        for t in trades:
            formatted_trades.append({
                "entry_time": t.get('entry_time'),
                "exit_time": t.get('exit_time'),
                "symbol": t['symbol'],
                "direction": t['direction'],
                "entry_price": safe_float(t['entry_price']),
                "exit_price": safe_float(t.get('exit_price')),
                "quantity": t['quantity'],
                "pnl": safe_float(round(t['pnl'], 2)),
                "pnl_pct": safe_float(round((t['pnl'] / (t['entry_price'] * t['quantity'])) * 100, 2)) if t['quantity'] else 0,
                "status": "CLOSED" 
            })
            
        # Sanitize Equity Curve
        safe_equity_curve = []
        for point in df_equity.reset_index().to_dict(orient='records'):
            safe_point = point.copy()
            safe_point['timestamp'] = point['timestamp'].strftime('%Y-%m-%d %H:%M')
            safe_point['equity'] = safe_float(point['equity'])
            safe_point['cash'] = safe_float(point['cash'])
            safe_point['drawdown'] = safe_float(point['drawdown'])
            safe_equity_curve.append(safe_point)

        return {
            'final_equity': safe_float(final_equity),
            'daily_equity': safe_equity_curve,
            'equity_curve': safe_equity_curve,
            'metrics': metrics,
            'trades': formatted_trades
        }

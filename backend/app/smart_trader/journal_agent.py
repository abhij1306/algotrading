"""
P&L and Journal Agent - Trade tracking and performance metrics
"""
from typing import Dict, Any, List, Optional
from datetime import datetime, date
import json
from pathlib import Path


class JournalAgent:
    """Tracks all trades, calculates P&L, and provides performance metrics"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.initial_capital = config.get('paper_trading', {}).get('initial_capital', 1000000)
        self.current_capital = self.initial_capital
        
        # Trade storage
        self.all_trades = []
        self.open_trades = {}
        
        # Daily tracking
        self.daily_pnl = {}
        self.daily_trades = {}
        
        # Persistence - use absolute path
        import os
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
        self.trades_file = Path(os.path.join(base_dir, 'data', 'paper_trades.json'))
        print(f"[JOURNAL] Trades file path: {self.trades_file}")
        self._load_trades()
    
    def record_trade(self, trade: Dict[str, Any]):
        """Record a new trade"""
        trade_id = trade['trade_id']
        self.open_trades[trade_id] = trade.copy()
        
        # Track daily
        today = date.today().isoformat()
        if today not in self.daily_trades:
            self.daily_trades[today] = []
        self.daily_trades[today].append(trade_id)
        
        # Persist to file
        self._save_trades()
    
    def update_trade(self, trade: Dict[str, Any]):
        """Update trade when closed"""
        trade_id = trade['trade_id']
        
        # Remove from open trades
        if trade_id in self.open_trades:
            del self.open_trades[trade_id]
        
        # Add to all trades
        self.all_trades.append(trade.copy())
        
        # Update capital
        self.current_capital += trade.get('pnl', 0)
        
        # Update daily P&L
        today = date.today().isoformat()
        if today not in self.daily_pnl:
            self.daily_pnl[today] = 0
        self.daily_pnl[today] += trade.get('pnl', 0)
        
        # Persist to file
        self._save_trades()
    
    def get_today_pnl(self) -> float:
        """Get today's total P&L"""
        today = date.today().isoformat()
        return self.daily_pnl.get(today, 0)
    
    def get_today_trades_count(self) -> int:
        """Get count of trades today"""
        today = date.today().isoformat()
        return len(self.daily_trades.get(today, []))
    
    def get_tradebook(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """Get all closed trades"""
        trades = sorted(self.all_trades, key=lambda x: x['exit_time'], reverse=True)
        
        if limit:
            return trades[:limit]
        return trades
    
    def get_pnl_summary(self) -> Dict[str, Any]:
        """Calculate comprehensive P&L and performance metrics"""
        if not self.all_trades:
            return self._empty_summary()
        
        # Basic metrics
        total_trades = len(self.all_trades)
        winning_trades = [t for t in self.all_trades if t['pnl'] > 0]
        losing_trades = [t for t in self.all_trades if t['pnl'] < 0]
        
        total_pnl = sum(t['pnl'] for t in self.all_trades)
        total_wins = sum(t['pnl'] for t in winning_trades)
        total_losses = abs(sum(t['pnl'] for t in losing_trades))
        
        # Win rate
        win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0
        
        # Profit factor
        profit_factor = (total_wins / total_losses) if total_losses > 0 else 0
        
        # Average win/loss
        avg_win = (total_wins / len(winning_trades)) if winning_trades else 0
        avg_loss = (total_losses / len(losing_trades)) if losing_trades else 0
        
        # Max drawdown
        max_dd, dd_start, dd_end = self._calculate_max_drawdown()
        
        # Return on capital
        return_pct = (total_pnl / self.initial_capital) * 100
        
        return {
            'initial_capital': self.initial_capital,
            'current_capital': round(self.current_capital, 2),
            'total_pnl': round(total_pnl, 2),
            'return_pct': round(return_pct, 2),
            'total_trades': total_trades,
            'winning_trades': len(winning_trades),
            'losing_trades': len(losing_trades),
            'win_rate': round(win_rate, 2),
            'profit_factor': round(profit_factor, 2),
            'avg_win': round(avg_win, 2),
            'avg_loss': round(avg_loss, 2),
            'max_drawdown': round(max_dd, 2),
            'max_drawdown_pct': round((max_dd / self.initial_capital) * 100, 2) if self.initial_capital > 0 else 0,
            'drawdown_period': {
                'start': dd_start,
                'end': dd_end
            } if dd_start and dd_end else None
        }
    
    def _calculate_max_drawdown(self) -> tuple[float, Optional[str], Optional[str]]:
        """Calculate maximum drawdown"""
        if not self.all_trades:
            return 0, None, None
        
        # Build equity curve
        equity = self.initial_capital
        peak = equity
        max_dd = 0
        dd_start = None
        dd_end = None
        current_dd_start = None
        
        for trade in self.all_trades:
            equity += trade['pnl']
            
            if equity > peak:
                peak = equity
                current_dd_start = None
            else:
                drawdown = peak - equity
                
                if drawdown > max_dd:
                    max_dd = drawdown
                    dd_end = trade['exit_time']
                    dd_start = current_dd_start or trade['entry_time']
                
                if current_dd_start is None:
                    current_dd_start = trade['entry_time']
        
        return max_dd, dd_start, dd_end
    
    def _empty_summary(self) -> Dict[str, Any]:
        """Return empty summary when no trades"""
        return {
            'initial_capital': self.initial_capital,
            'current_capital': self.initial_capital,
            'total_pnl': 0,
            'return_pct': 0,
            'total_trades': 0,
            'winning_trades': 0,
            'losing_trades': 0,
            'win_rate': 0,
            'profit_factor': 0,
            'avg_win': 0,
            'avg_loss': 0,
            'max_drawdown': 0,
            'max_drawdown_pct': 0,
            'drawdown_period': None
        }
    
    def export_to_csv(self, filepath: str):
        """Export tradebook to CSV"""
        import csv
        
        if not self.all_trades:
            return
        
        with open(filepath, 'w', newline='') as f:
            fieldnames = [
                'trade_id', 'symbol', 'instrument_type', 'direction',
                'quantity', 'entry_price', 'entry_time', 'exit_price',
                'exit_time', 'exit_reason', 'pnl', 'commission'
            ]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            
            for trade in self.all_trades:
                writer.writerow({k: trade.get(k, '') for k in fieldnames})
    
    def get_equity_curve(self) -> List[Dict[str, Any]]:
        """Get equity curve data for charting"""
        equity = self.initial_capital
        curve = [{'timestamp': datetime.now().isoformat(), 'equity': equity}]
        
        for trade in self.all_trades:
            equity += trade['pnl']
            curve.append({
                'timestamp': trade['exit_time'],
                'equity': round(equity, 2),
                'pnl': round(trade['pnl'], 2),
                'symbol': trade['symbol']
            })
        
        return curve
    
    def reset(self):
        """Reset journal (for new trading day)"""
        # Don't reset all_trades, just reset daily tracking
        today = date.today().isoformat()
        self.daily_pnl[today] = 0
        self.daily_trades[today] = []
    
    def _load_trades(self):
        """Load trades from JSON file"""
        try:
            if self.trades_file.exists():
                with open(self.trades_file, 'r') as f:
                    data = json.load(f)
                    self.all_trades = data.get('all_trades', [])
                    self.open_trades = data.get('open_trades', {})
                    self.current_capital = data.get('current_capital', self.initial_capital)
                    self.daily_pnl = data.get('daily_pnl', {})
                    self.daily_trades = data.get('daily_trades', {})
                    print(f"[JOURNAL] Loaded {len(self.all_trades)} closed trades and {len(self.open_trades)} open trades")
        except Exception as e:
            print(f"[JOURNAL] Error loading trades: {e}")
    
    def _save_trades(self):
        """Save trades to JSON file"""
        try:
            # Ensure directory exists
            self.trades_file.parent.mkdir(parents=True, exist_ok=True)
            
            data = {
                'all_trades': self.all_trades,
                'open_trades': self.open_trades,
                'current_capital': self.current_capital,
                'daily_pnl': self.daily_pnl,
                'daily_trades': self.daily_trades,
                'last_updated': datetime.now().isoformat()
            }
            
            with open(self.trades_file, 'w') as f:
                json.dump(data, f, indent=2)
            
            print(f"[JOURNAL] Saved {len(self.all_trades)} closed trades and {len(self.open_trades)} open trades")
        except Exception as e:
            print(f"[JOURNAL] Error saving trades: {e}")

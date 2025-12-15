"""
Risk Management Agent - Validates trades against risk rules
"""
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from .utils import calculate_position_size, round_to_lot_size, get_lot_size


class RiskAgent:
    """Validates trades against risk management rules"""
    
    def __init__(self, config: Dict[str, Any], journal_agent=None):
        self.config = config
        self.journal_agent = journal_agent
        self.risk_config = config.get('risk', {})
        
        # Risk limits
        self.max_trades_per_day = self.risk_config.get('max_trades_per_day', 5)
        self.max_risk_per_trade_pct = self.risk_config.get('max_risk_per_trade_pct', 2)
        self.max_daily_loss_pct = self.risk_config.get('max_daily_loss_pct', 5)
        self.symbol_cooldown_minutes = self.risk_config.get('symbol_cooldown_minutes', 30)
        self.min_risk_reward_ratio = self.risk_config.get('min_risk_reward_ratio', 1.5)
        
        # Track recent trades for cooldown
        self.recent_trades = {}
    
    def validate_trade(
        self, 
        signal: Dict[str, Any], 
        capital: float
    ) -> Dict[str, Any]:
        """
        Validate trade against all risk rules
        
        Args:
            signal: Trading signal
            capital: Current available capital
            
        Returns:
            Validation result with approval status and position details
        """
        # 1. Check daily trade limit
        if self.journal_agent:
            today_trades = self._get_today_trades_count()
            if today_trades >= self.max_trades_per_day:
                return {
                    'approved': False,
                    'rejection_reason': f'Daily trade limit reached ({self.max_trades_per_day} trades)',
                    'qty': 0,
                    'risk_amount': 0
                }
        
        # 2. Check daily loss limit
        if self.journal_agent:
            today_pnl = self.journal_agent.get_today_pnl()
            daily_loss_limit = capital * (self.max_daily_loss_pct / 100)
            
            if today_pnl < -daily_loss_limit:
                return {
                    'approved': False,
                    'rejection_reason': f'Daily loss limit exceeded (₹{abs(today_pnl):.2f} / ₹{daily_loss_limit:.2f})',
                    'qty': 0,
                    'risk_amount': 0
                }
        
        # 3. Check symbol cooldown
        symbol = signal['symbol']
        if not self._check_symbol_cooldown(symbol):
            return {
                'approved': False,
                'rejection_reason': f'Symbol in cooldown period ({self.symbol_cooldown_minutes} minutes)',
                'qty': 0,
                'risk_amount': 0
            }
        
        # 4. Check risk/reward ratio
        entry = signal['entry_price']
        stop_loss = signal['stop_loss']
        target = signal['target']
        
        risk = abs(entry - stop_loss)
        reward = abs(target - entry)
        rr_ratio = reward / risk if risk > 0 else 0
        
        # Use small epsilon for floating point comparison
        if rr_ratio < self.min_risk_reward_ratio - 0.01:
            return {
                'approved': False,
                'rejection_reason': f'R:R ratio too low ({rr_ratio:.2f} < {self.min_risk_reward_ratio})',
                'qty': 0,
                'risk_amount': 0,
                'risk_reward_ratio': round(rr_ratio, 2)
            }
        
        # 5. Calculate position size based on risk
        max_risk_amount = capital * (self.max_risk_per_trade_pct / 100)
        
        instrument_type = signal.get('instrument_type', 'STOCK')
        
        if instrument_type in ['OPTION', 'FUTURE', 'FUTURES']:
            # For F&O, use lot size
            symbol = signal.get('symbol', signal.get('index', ''))
            lot_size = signal.get('lot_size', get_lot_size(symbol))
            quantity = lot_size
            risk_amount = risk * quantity
            
            # Check if risk exceeds limit
            if risk_amount > max_risk_amount:
                return {
                    'approved': False,
                    'rejection_reason': f'Risk amount too high (₹{risk_amount:.2f} > ₹{max_risk_amount:.2f})',
                    'qty': 0,
                    'risk_amount': risk_amount,
                    'lot_size': lot_size
                }
        else:
            # For stocks, calculate quantity based on risk
            quantity = calculate_position_size(capital, self.max_risk_per_trade_pct, entry, stop_loss)
            
            if quantity <= 0:
                return {
                    'approved': False,
                    'rejection_reason': 'Calculated quantity is zero or negative',
                    'qty': 0,
                    'risk_amount': 0
                }
            
            risk_amount = risk * quantity
        
        # 6. Check if capital is sufficient
        required_capital = entry * quantity
        if required_capital > capital:
            return {
                'approved': False,
                'rejection_reason': f'Insufficient capital (Need: ₹{required_capital:.2f}, Available: ₹{capital:.2f})',
                'qty': 0,
                'risk_amount': 0
            }
        
        # All checks passed - Approve trade
        return {
            'approved': True,
            'rejection_reason': None,
            'qty': int(quantity),
            'stop_loss': stop_loss,
            'target': target,
            'risk_amount': round(risk_amount, 2),
            'required_capital': round(required_capital, 2),
            'risk_reward_ratio': round(rr_ratio, 2)
        }
    
    def _get_today_trades_count(self) -> int:
        """Get count of trades executed today"""
        if not self.journal_agent:
            return 0
        
        return self.journal_agent.get_today_trades_count()
    
    def _check_symbol_cooldown(self, symbol: str) -> bool:
        """Check if symbol is in cooldown period"""
        if symbol not in self.recent_trades:
            return True
        
        last_trade_time = self.recent_trades[symbol]
        cooldown_end = last_trade_time + timedelta(minutes=self.symbol_cooldown_minutes)
        
        return datetime.now() >= cooldown_end
    
    def record_trade(self, symbol: str):
        """Record trade execution for cooldown tracking"""
        self.recent_trades[symbol] = datetime.now()
    
    def reset_daily_limits(self):
        """Reset daily limits (call at market close)"""
        self.recent_trades = {}

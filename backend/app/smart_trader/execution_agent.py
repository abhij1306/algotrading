"""
Execution Agent - Simulates paper trading execution
"""
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from enum import Enum
from .utils import calculate_slippage, calculate_pnl


class TradeStatus(Enum):
    """Trade lifecycle states"""
    PENDING = "PENDING"
    FILLED = "FILLED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class ExecutionAgent:
    """Simulates paper trading execution with realistic slippage"""
    
    def __init__(self, config: Dict[str, Any], journal_agent=None):
        self.config = config
        self.journal_agent = journal_agent
        
        paper_config = config.get('paper_trading', {})
        self.slippage_pct = paper_config.get('slippage_pct', 0.05)
        self.commission_per_trade = paper_config.get('commission_per_trade', 20)
        
        # Track open positions
        self.open_positions = {}
        self.position_counter = 0
        
        # Load open positions from journal
        self._load_open_positions()
    
    def execute_trade(
        self, 
        signal: Dict[str, Any], 
        risk_approval: Dict[str, Any],
        user_quantity: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Execute paper trade (simulate market order)
        
        Args:
            signal: Trading signal
            risk_approval: Risk validation result
            user_quantity: Optional user override for quantity
            
        Returns:
            Trade execution result
        """
        if not risk_approval.get('approved', False):
            return {
                'status': TradeStatus.REJECTED.value,
                'trade_id': None,
                'message': risk_approval.get('rejection_reason', 'Trade rejected by risk agent')
            }
        
        # Generate trade ID
        self.position_counter += 1
        trade_id = f"ST{datetime.now().strftime('%Y%m%d')}{self.position_counter:04d}"
        
        
        # Get execution details
        entry_price = signal['entry_price']
        direction = signal['direction']
        quantity = user_quantity or risk_approval['qty']
        
        # For OPTIONS, fetch live premium from Fyers API
        if signal.get('instrument_type') == 'OPTION':
            try:
                from ..fyers_direct import get_option_premium
                
                # Get option parameters
                symbol = signal['symbol']
                option_type = signal.get('option_type', 'CE' if direction == 'LONG' else 'PE')
                strike = signal.get('strike')
                expiry_date = signal.get('expiry_date')
                
                # If strike not provided, calculate ATM strike
                if not strike:
                    # Round to nearest 50 for stocks, 100 for NIFTY/BANKNIFTY
                    strike_interval = 100 if symbol in ['NIFTY', 'BANKNIFTY'] else 50
                    strike = round(entry_price / strike_interval) * strike_interval
                
                print(f"[EXECUTION] Fetching live option premium: {symbol} {strike} {option_type}")
                
                # Fetch real option premium from Fyers
                premium = get_option_premium(symbol, strike, option_type, expiry_date)
                
                if premium and premium > 0:
                    entry_price = premium
                    print(f"[EXECUTION] Using live option premium: ₹{premium:.2f}")
                else:
                    # Fallback to rough estimate if Fyers fetch fails
                    from ..fyers_direct import get_fyers_quotes
                    quotes = get_fyers_quotes([symbol])
                    
                    if quotes and symbol in quotes:
                        underlying_price = quotes[symbol].get('ltp', entry_price)
                        # Rough premium estimation (2-3% of underlying for ATM options)
                        premium_pct = 0.03 if abs(strike - underlying_price) / underlying_price < 0.02 else 0.02
                        entry_price = underlying_price * premium_pct
                        print(f"[EXECUTION] Fyers fetch failed, using estimate: ₹{entry_price:.2f}")
                    
            except Exception as e:
                print(f"[EXECUTION] Error fetching option price: {e}. Using signal price.")
        
        # Apply slippage
        filled_price = calculate_slippage(entry_price, self.slippage_pct, direction)
        
        # Calculate total cost
        total_cost = filled_price * quantity + self.commission_per_trade
        
        # Create position
        position = {
            'trade_id': trade_id,
            'symbol': signal['symbol'],
            'instrument_type': signal['instrument_type'],
            'direction': direction,
            'quantity': quantity,
            'entry_price': round(filled_price, 2),
            'entry_time': datetime.now().isoformat(),
            'stop_loss': signal['stop_loss'],
            'target': signal['target'],
            'status': TradeStatus.OPEN.value,
            'pnl': 0,
            'exit_price': None,
            'exit_time': None,
            'commission': self.commission_per_trade,
            'signal_score': signal.get('momentum_score', 0),
            'reasons': signal.get('reasons', [])
        }
        
        # Add option-specific fields if this is an option trade
        if signal.get('instrument_type') == 'OPTION':
            position['strike'] = signal.get('strike')
            position['option_type'] = signal.get('option_type', 'CE' if direction == 'LONG' else 'PE')
            position['expiry_date'] = signal.get('expiry_date')
        
        # Store position
        self.open_positions[trade_id] = position
        
        # Record in journal
        if self.journal_agent:
            self.journal_agent.record_trade(position)
        
        return {
            'status': TradeStatus.FILLED.value,
            'trade_id': trade_id,
            'message': f'Order filled at ₹{filled_price:.2f} (slippage: {self.slippage_pct}%)',
            'position': position
        }
    
    def update_positions(self, current_prices: Dict[str, float]):
        """
        Update all open positions with current prices and check exit conditions
        
        Args:
            current_prices: Dict mapping symbols to current prices
        """
        positions_to_close = []
        
        for trade_id, position in self.open_positions.items():
            symbol = position['symbol']
            current_price = current_prices.get(symbol)
            
            if not current_price:
                continue
            
            # Check stop loss and target
            should_exit, exit_reason = self._check_exit_conditions(position, current_price)
            
            if should_exit:
                positions_to_close.append((trade_id, current_price, exit_reason))
            else:
                # Update unrealized P&L
                position['pnl'] = calculate_pnl(
                    position['entry_price'],
                    current_price,
                    position['quantity'],
                    position['direction'],
                    0  # Don't include commission yet
                )
        
        # Close positions that hit exit conditions
        for trade_id, exit_price, exit_reason in positions_to_close:
            self.close_position(trade_id, exit_price, exit_reason)
    
    def _check_exit_conditions(self, position: Dict[str, Any], current_price: float) -> tuple[bool, str]:
        """Check if position should be exited"""
        direction = position['direction']
        stop_loss = position['stop_loss']
        target = position['target']
        
        if direction == 'LONG':
            if current_price <= stop_loss:
                return True, 'Stop Loss Hit'
            elif current_price >= target:
                return True, 'Target Reached'
        else:  # SHORT
            if current_price >= stop_loss:
                return True, 'Stop Loss Hit'
            elif current_price <= target:
                return True, 'Target Reached'
        
        return False, ''
    
    def close_position(self, trade_id: str, exit_price: float = None, exit_reason: str = 'Manual Exit'):
        """
        Close an open position
        
        Args:
            trade_id: Trade ID
            exit_price: Exit price (if None, use current entry price)
            exit_reason: Reason for exit
            
        Returns:
            Result dict with success status
        """
        if trade_id not in self.open_positions:
            return {'success': False, 'error': 'Position not found'}
        
        position = self.open_positions[trade_id]
        
        # Use entry price if exit price not provided
        if exit_price is None:
            exit_price = position['entry_price']
        
        # Apply slippage to exit
        direction = position['direction']
        # Reverse direction for exit slippage
        exit_direction = 'SHORT' if direction == 'LONG' else 'LONG'
        filled_exit_price = calculate_slippage(exit_price, self.slippage_pct, exit_direction)
        
        # Calculate final P&L
        pnl = calculate_pnl(
            position['entry_price'],
            filled_exit_price,
            position['quantity'],
            direction,
            self.commission_per_trade * 2  # Entry + Exit commission
        )
        
        # Update position
        position['exit_price'] = round(filled_exit_price, 2)
        position['exit_time'] = datetime.now().isoformat()
        position['exit_reason'] = exit_reason
        position['pnl'] = round(pnl, 2)
        position['status'] = TradeStatus.CLOSED.value
        
        # Record in journal
        if self.journal_agent:
            self.journal_agent.update_trade(position)
        
        # Remove from open positions
        del self.open_positions[trade_id]
        
        return {
            'success': True,
            'trade_id': trade_id,
            'pnl': round(pnl, 2),
            'exit_price': round(filled_exit_price, 2)
        }
    
    def get_open_positions(self) -> list:
        """Get all open positions formatted for Terminal"""
        positions = []
        for trade_id, pos in self.open_positions.items():
            positions.append({
                'trade_id': trade_id,
                'symbol': pos['symbol'],
                'side': pos['direction'],
                'quantity': pos['quantity'],
                'entry_price': pos['entry_price'],
                'current_price': pos.get('current_price', pos['entry_price']),
                'unrealized_pnl': pos.get('pnl', 0),
                'status': pos['status']
            })
        return positions
    
    def get_pnl_summary(self) -> dict:
        """Get P&L summary for Terminal"""
        total_pnl = sum(pos.get('pnl', 0) for pos in self.open_positions.values())
        return {
            'total_pnl': total_pnl,
            'open_positions': len(self.open_positions),
            'realized_pnl': 0  # Would need to track closed positions
        }
    
    def execute_paper_trade(self, trade_setup) -> dict:
        """Execute paper trade from TradeSetup object"""
        # Convert TradeSetup to signal format
        signal = {
            'symbol': trade_setup.symbol,
            'direction': trade_setup.direction.value,
            'entry_price': trade_setup.entry_price,
            'stop_loss': trade_setup.stop_loss,
            'target': trade_setup.target,
            'instrument_type': 'EQ',
            'reasons': []
        }
        
        # Create risk approval
        risk_approval = {
            'approved': True,
            'qty': trade_setup.quantity
        }
        
        return self.execute_trade(signal, risk_approval)
    
    def close_all_positions(self, current_prices: Dict[str, float], reason: str = 'Market Close'):
        """Close all open positions (e.g., at market close)"""
        for trade_id, position in list(self.open_positions.items()):
            symbol = position['symbol']
            current_price = current_prices.get(symbol, position['entry_price'])
            result = self.close_position(trade_id, current_price, reason)
    
    def _load_open_positions(self):
        """Load open positions from journal agent"""
        if self.journal_agent:
            self.open_positions = self.journal_agent.open_trades.copy()
            print(f"[EXECUTION] Loaded {len(self.open_positions)} open positions from journal")


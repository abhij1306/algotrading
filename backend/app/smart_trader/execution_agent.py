"""
Execution Agent - Wraps Broker Adapter.
Delegates actual execution to the active Broker (Paper or Live).
"""
from typing import Dict, Any, Optional, List
from datetime import datetime, timedelta
from enum import Enum
from .utils import calculate_slippage, calculate_pnl
from .smart_orders import SmartOrderManager
from ..brokers.plugins.paper import PaperBroker
from ..brokers.plugins.fyers import FyersBroker
from ..utils.audit_logger import AuditLogger

class TradeStatus(Enum):
    """Trade lifecycle states"""
    PENDING = "PENDING"
    FILLED = "FILLED"
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    REJECTED = "REJECTED"


class ExecutionAgent:
    """
    Execution Agent - Wraps Broker Adapter.
    Stateless: Relies on Broker (DB) for state.
    """
    
    def __init__(self, config: Dict[str, Any], journal_agent=None, broker: Optional[Any] = None):
        self.config = config
        self.journal_agent = journal_agent
        
        # Initialize Brokers
        self.paper_broker = PaperBroker(config)
        self.live_broker = FyersBroker() 
        
        if broker:
            self.broker = broker
            self.active_mode = "BACKTEST"
        else:
            # Determine Mode from Config
            self.active_mode = config.get('mode', 'PAPER')
            
            if self.active_mode == "LIVE":
                 self.broker = self.live_broker
            else:
                 self.broker = self.paper_broker
        
        # Initialize Smart Order Manager
        self.smart_manager = SmartOrderManager(self)
        
    def set_mode(self, mode: str):
        """Switch between PAPER and LIVE"""
        if mode.upper() == "LIVE":
            self.active_mode = "LIVE"
            self.broker = self.live_broker
            print("[EXECUTION] Switched to LIVE Trading Mode")
        else:
            self.active_mode = "PAPER"
            self.broker = self.paper_broker
            print("[EXECUTION] Switched to PAPER Trading Mode")



    def execute_trade(
        self, 
        signal: Dict[str, Any], 
        risk_approval: Dict[str, Any],
        user_quantity: Optional[int] = None
    ) -> Dict[str, Any]:
        """
        Execute trade via active broker
        """
        # Log Attempt
        AuditLogger.log_decision(
            agent_name="ExecutionAgent",
            action_type="TRADE_ATTEMPT",
            symbol=signal.get('symbol'),
            input_snapshot={"signal": signal, "risk": risk_approval},
            decision={"broker": self.active_mode},
            reasoning="Received execution request",
            confidence=1.0 # Execution has no confidence, it's an order
        )

        if not risk_approval.get('approved', False):
            reason = risk_approval.get('rejection_reason', 'Trade rejected by risk agent')
            AuditLogger.log_decision(
                agent_name="ExecutionAgent",
                action_type="TRADE_REJECTED",
                symbol=signal.get('symbol'),
                input_snapshot={},
                decision={"approved": False},
                reasoning=reason,
                confidence=1.0,
                status="FAILURE"
            )
            return {
                'status': TradeStatus.REJECTED.value,
                'message': reason
            }
        
        # Prepare Order
        quantity = user_quantity or risk_approval['qty']
        order = {
            "symbol": signal['symbol'],
            "direction": signal['direction'], # LONG/SHORT
            "side": "BUY" if signal['direction'] == "LONG" else "SELL",
            "quantity": quantity,
            "type": "MARKET", 
            "price": signal.get('entry_price', 0),
            "product": "MIS"
        }
        
        # Delegate to Broker
        try:
             result = self.broker.place_order(order)
             # Result is OrderResponse TypedDict
             
             status_map = {
                 "FILLED": TradeStatus.FILLED.value,
                 "SUBMITTED": TradeStatus.PENDING.value,
                 "REJECTED": TradeStatus.REJECTED.value,
                 "ERROR": TradeStatus.REJECTED.value
             }
             
             final_status = status_map.get(result.get('status'), TradeStatus.PENDING.value)
             
             # Log Result
             AuditLogger.log_decision(
                agent_name="ExecutionAgent",
                action_type="TRADE_EXECUTED",
                symbol=signal['symbol'],
                input_snapshot={"order": order},
                decision={"result": result},
                reasoning=result.get('message'),
                confidence=1.0,
                status="SUCCESS" if final_status in [TradeStatus.FILLED.value, TradeStatus.PENDING.value] else "FAILURE"
             )
             
             return {
                 'status': final_status,
                 'trade_id': result.get('order_id'),
                 'message': result.get('message', 'Order Placed'),
                 'details': result
             }
        except Exception as e:
            print(f"[EXECUTION] Error placing order: {e}")
            AuditLogger.log_decision(
                agent_name="ExecutionAgent",
                action_type="TRADE_ERROR",
                symbol=signal['symbol'],
                input_snapshot={"order": order},
                decision={},
                reasoning=str(e),
                confidence=1.0,
                status="FAILURE"
            )
            return {
                'status': TradeStatus.REJECTED.value, 
                'message': str(e)
            }
    
    def update_positions(self, current_prices: Dict[str, float]):
        """
        Update positions and check exits.
        Delegates PnL calculation to broker first.
        """
        # 1. Update Broker PnL (Persists to DB if Paper)
        if hasattr(self.broker, 'update_pnl'):
            self.broker.update_pnl(current_prices)
            
        # 2. Get Updated Positions
        open_positions = self.broker.get_positions()
        
        # 3. Check Exit Conditions
        for position in open_positions:
            symbol = position['symbol']
            current_price = current_prices.get(symbol)
            
            if not current_price:
                continue
            
            # Reconstruct stop/target logic (DB doesn't store SL/Target yet on Position level)
            # Todo Phase 3: Move SL/Target to DB Position table
            # For now, we skip auto-exit based on SL/Target unless we fetch original signal order parameters.
            # But the user Requirement says "Eliminate in-memory critical state".
            # For now, assume manual exit or strategy loop handles exit signals.
            pass
            
            # NOTE: We removed the auto-exit logic here because 'stop_loss' and 'target' 
            # are not yet columns in PaperPosition. 
            # In live systems, these should be Limit/Stop orders on the exchange.
            # We will rely on FastLoop or Strategy Agent to send exit signals.

    def close_position(self, trade_id: str, exit_price: float = None, exit_reason: str = 'Manual Exit'):
        """
        Close a position by placing an opposing order.
        Note: trade_id here acts as a reference, but we really need Symbol.
        """
        # We need the symbol to close. 
        # If trade_id comes from UI, we might need lookup.
        # But UI usually sends "symbol" for closing.
        # Assuming trade_id MIGHT be a symbol or we look it up.
        
        # Retrieve position by trade_id? 
        # Broker.get_positions returns list.
        # For simplify, assume frontend passes Symbol.
        
        # IF trade_id is actually Symbol (common simplification in this codebase history)
        symbol = trade_id 
        
        # Find position to get Quantity and Side
        positions = self.broker.get_positions()
        target_pos = next((p for p in positions if p['symbol'] == symbol or p.get('trade_id') == trade_id), None)
        
        if not target_pos:
            return {'success': False, 'error': 'Position not found'}
        
        side = target_pos['side']  # LONG or SHORT
        quantity = target_pos['quantity']
        
        # Opposing Side
        exit_side = 'SELL' if side == 'LONG' else 'BUY'
        exit_direction = 'SHORT' if side == 'LONG' else 'LONG'
        
        order = {
            "symbol": target_pos['symbol'],
            "direction": exit_direction,
            "side": exit_side,
            "quantity": quantity,
            "type": "MARKET",
            "price": exit_price or target_pos.get('ltp', 0)
        }
        
        result = self.broker.place_order(order)
        
        if result['status'] == 'FILLED':
             return {
                'success': True,
                'trade_id': result['order_id'],
                'pnl': 0, # PnL is tracked in DB now
                'exit_price': result['average_price']
            }
        else:
             return {'success': False, 'error': result['message']}
    
    def get_open_positions(self) -> list:
        """Get positions from Broker"""
        # Map Broker Format to Terminal Format
        raw_positions = self.broker.get_positions()
        terminal_positions = []
        
        for pos in raw_positions:
            terminal_positions.append({
                'trade_id': pos.get('symbol'), # Use symbol as ID for now
                'symbol': pos['symbol'],
                'side': pos['side'],
                'quantity': pos['quantity'],
                'entry_price': pos['entry_price'],
                'current_price': pos.get('ltp', pos['entry_price']),
                'unrealized_pnl': pos.get('pnl', 0),
                'status': 'OPEN'
            })
        return terminal_positions
    
    def get_pnl_summary(self) -> dict:
        """Get P&L from Broker"""
        positions = self.broker.get_positions()
        funds = self.broker.get_funds()
        
        total_pnl = sum(p.get('pnl', 0) for p in positions)
        
        return {
            'total_pnl': total_pnl,
            'open_positions': len(positions),
            'realized_pnl': 0 # Needs Trade Table query
        }
    
    def execute_paper_trade(self, trade_setup) -> dict:
        """Execute paper trade from TradeSetup object"""
        signal = {
            'symbol': trade_setup.symbol,
            'direction': trade_setup.direction.value,
            'entry_price': trade_setup.entry_price
        }
        risk_approval = {'approved': True, 'qty': trade_setup.quantity}
        return self.execute_trade(signal, risk_approval)
    
    def close_all_positions(self, current_prices: Dict[str, float], reason: str = 'Market Close'):
        positions = self.broker.get_positions()
        for pos in positions:
            self.close_position(pos['symbol'], reason=reason)
    
    def execute_manual_trade(self, order_details: Dict[str, Any]) -> Dict[str, Any]:
        """Execute manual trade"""
        signal = {
            'symbol': order_details['symbol'],
            'direction': 'LONG' if order_details['type'] == 'BUY' else 'SHORT',
            'entry_price': order_details['price']
        }
        risk_approval = {'approved': True, 'qty': order_details['quantity']}
        return self.execute_trade(signal, risk_approval)

    # ==================== SMART ORDER CAPABILITIES ====================
    def place_basket_trade(self, orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        return self.smart_manager.place_basket_order(orders)
        
    def place_split_trade(self, symbol: str, action: str, total_quantity: int, split_size: int, price: float = 0) -> Dict[str, Any]:
        return self.smart_manager.place_split_order(symbol, action, total_quantity, split_size, price)
        
    def place_smart_trade(self, symbol: str, action: str, target_value: float, current_price: float) -> Dict[str, Any]:
        return self.smart_manager.place_smart_order(symbol, action, target_value, current_price)


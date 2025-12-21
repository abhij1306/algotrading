"""
Smart Order Logic extracted from OpenAlgo MCP
Implements Basket, Split, and Position-Sizing logic.
"""
from typing import List, Dict, Any, Optional
import math

class SmartOrderManager:
    """
    Manages advanced order types: Basket, Split, and Smart Sizing.
    Designed to work with any execution backend (Paper or Live).
    """
    
    def __init__(self, execution_backend):
        """
        Args:
            execution_backend: Object with a .execute_trade(signal, ...) method
        """
        self.execution_backend = execution_backend

    def place_basket_order(self, orders: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Place multiple orders sequentially.
        """
        results = []
        for order in orders:
            # Convert simple order dict to signal format expected by ExecutionAgent
            signal = {
                'symbol': order['symbol'],
                'direction': order['action'], # BUY/SELL
                'entry_price': order.get('price', 0),
                'instrument_type': order.get('product', 'MIS'), # mapping product to type roughly
                'quantity': order['quantity'],
                # Defaults
                'stop_loss': 0,
                'target': 0,
                'reasons': ['Basket Order']
            }
            
            # Auto-approve risk for now as it represents user intent
            risk_approval = {'approved': True, 'qty': order['quantity']}
            
            res = self.execution_backend.execute_trade(signal, risk_approval)
            results.append(res)
            
        return {
            "status": "COMPLETED",
            "total_orders": len(orders),
            "results": results
        }

    def place_split_order(self, 
                          symbol: str, 
                          action: str, 
                          total_quantity: int, 
                          split_size: int, 
                          price: float = 0) -> Dict[str, Any]:
        """
        Split a large order into smaller chunks.
        """
        if split_size <= 0:
            return {"status": "ERROR", "message": "Split size must be > 0"}
            
        num_splits = math.ceil(total_quantity / split_size)
        orders = []
        
        remaining = total_quantity
        for i in range(num_splits):
            qty = min(remaining, split_size)
            if qty > 0:
                orders.append({
                    "symbol": symbol,
                    "action": action,
                    "quantity": qty,
                    "price": price
                })
                remaining -= qty
                
        # Execute as basket
        return self.place_basket_order(orders)

    def place_smart_order(self, 
                          symbol: str, 
                          action: str, 
                          target_value: float, 
                          current_price: float, 
                          max_slippage_pct: float = 0.5) -> Dict[str, Any]:
        """
        Smart order that calculates quantity based on target value and constraints.
        """
        if current_price <= 0:
             return {"status": "ERROR", "message": "Invalid current price"}
             
        quantity = int(target_value / current_price)
        
        if quantity <= 0:
            return {"status": "ERROR", "message": "Calculated quantity is 0"}
            
        # Create order
        order = {
            "symbol": symbol,
            "action": action,
            "quantity": quantity,
            "price": current_price
        }
        
        return self.place_basket_order([order])

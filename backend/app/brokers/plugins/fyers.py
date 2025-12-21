import sys
import os
from ..base import IBroker, OrderResponse, Position, BrokerFunds

# Add project root to sys.path to access 'fyers' module
# Current: backend/app/brokers/plugins/fyers.py
# Root: c:/AlgoTrading
# Path: ../../../../..
BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
if BASE_DIR not in sys.path:
    sys.path.append(BASE_DIR)

try:
    from fyers.fyers_client import load_fyers
except ImportError:
    load_fyers = None

class FyersBroker(IBroker):
    """
    Fyers Implementation of IBroker.
    Wraps actual API calls for Live Trading and standardizes output.
    """
    def __init__(self):
        self.client = None
        self.connect()
        
    def connect(self):
        """Attempt to load authenticated client"""
        try:
            if load_fyers:
                self.client = load_fyers()
        except Exception as e:
            print(f"Fyers Broker: Not connected ({e})")
            self.client = None

    def place_order(self, order: Dict[str, Any]) -> OrderResponse:
        """
        Execute order on Fyers.
        """
        if not self.client:
             self.connect() # Try one last time
             if not self.client:
                 return OrderResponse(order_id="", status="REJECTED", message="Fyers Client not initialized. Please Login.", details=None)
             
        # Map generic order dict to Fyers parameters
        # order keys: symbol, quantity, side (BUY/SELL), type (MARKET/LIMIT), product (MIS/CNC/INTRADAY)
        
        product_map = {
            "MIS": "INTRADAY",
            "INTRADAY": "INTRADAY",
            "CNC": "CNC",
            "MARGIN": "MARGIN"
        }
        
        data = {
            "symbol": order["symbol"], 
            "qty": order["quantity"],
            "type": 2 if order.get("type", "MARKET") == "MARKET" else 1, # 1=Limit, 2=Market
            "side": 1 if order.get("side") == "BUY" else -1,
            "productType": product_map.get(order.get("product", "INTRADAY").upper(), "INTRADAY"), 
            "limitPrice": order.get("price", 0) if order.get("type") != "MARKET" else 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }
        
        try:
            response = self.client.place_order(data=data)
            # Response format: {'s': 'ok', 'code': 1101, 'message': 'Order submitted successfully', 'id': '12345'}
            
            if response.get("s") == "ok":
                return OrderResponse(
                    order_id=response.get("id", ""),
                    status="SUBMITTED",
                    message=response.get("message", "Order Placed"),
                    details=response
                )
            else:
                return OrderResponse(
                    order_id="",
                    status="REJECTED",
                    message=response.get("message", "Fyers rejected order"),
                    details=response
                )
                
        except Exception as e:
            return OrderResponse(
                order_id="", 
                status="ERROR", 
                message=str(e), 
                details=None
            )

    def cancel_order(self, order_id: str) -> Dict[str, Any]:
        if not self.client: return {"status": "ERROR", "message": "Client not initialized"}
        data = {"id": order_id}
        return self.client.cancel_order(data=data)

    def modify_order(self, order_id: str, params: Dict[str, Any]) -> Dict[str, Any]:
         # Todo: Implement robust modify 
         return {"status": "SKIPPED", "message": "Modify not implemented in plugin yet"}

    def get_positions(self) -> List[Position]:
        if not self.client: return []
        
        response = self.client.positions()
        # Response: {"s": "ok", "netPositions": [...]}
        
        positions = []
        if response.get("s") == "ok":
            net_positions = response.get("netPositions", [])
            for p in net_positions:
                # Fyers side: 1 (Long), -1 (Short)
                side = "LONG" if p.get("netQty", 0) > 0 else "SHORT"
                if p.get("netQty", 0) == 0: continue # Skip closed
                
                positions.append(Position(
                    symbol=p.get("symbol"),
                    side=side,
                    quantity=abs(p.get("netQty", 0)),
                    entry_price=p.get("buyAvg", 0) if side == "LONG" else p.get("sellAvg", 0),
                    current_price=p.get("ltp", 0),
                    pnl=p.get("pl", 0),
                    product_type=p.get("productType", "INTRADAY")
                ))
        return positions

    def get_orders(self) -> List[Dict[str, Any]]:
        if not self.client: return []
        response = self.client.orderbook()
        return response.get("orderBook", [])

    def get_holdings(self) -> List[Dict[str, Any]]:
        if not self.client: return []
        response = self.client.holdings()
        return response.get("holdings", [])
        
    def get_funds(self) -> BrokerFunds:
        if not self.client: 
            return BrokerFunds(available=0, used=0, total=0)
            
        response = self.client.funds()
        # Need to parse Fyers fund structure
        # Assuming response['fund_limit'] list
        
        available = 0
        used = 0
        total = 0
        
        # Simple extraction (needs verification with actual API response structure)
        # Usually list of dicts: source="limit", etc.
        # Fallback to 0 if parsing fails
        
        return BrokerFunds(available=available, used=used, total=total)

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        return {}

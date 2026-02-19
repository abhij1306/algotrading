from typing import Any

from ...services.fyers_client import get_fyers_client
from ..base import BrokerFunds, IBroker, OrderResponse, Position


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
            fyers_service = get_fyers_client()
            self.client = fyers_service.fyers
        except Exception as e:
            print(f"Fyers Broker: Not connected ({e})")
            self.client = None

    def place_order(self, order: dict[str, Any]) -> OrderResponse:
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

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self.client:
            return {"status": "ERROR", "message": "Client not initialized"}
        data = {"id": order_id}
        return self.client.cancel_order(data=data)

    def modify_order(self, order_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Modify pending order.
        params: {type, limitPrice, qty}
        """
        if not self.client:
            return {"status": "ERROR", "message": "Client not initialized"}

        data = {
            "id": order_id,
            "type": 1 if params.get("order_type") != "MARKET" else 2
        }

        if "quantity" in params:
            data["qty"] = params["quantity"]
        if "price" in params:
            data["limitPrice"] = params["price"]
            data["type"] = 1 # Force Limit

        # If changing to MARKET, set type=2 and limitPrice=0
        if params.get("order_type") == "MARKET":
            data["type"] = 2
            data["limitPrice"] = 0

        return self.client.modify_order(data=data)

    def get_positions(self) -> list[Position]:
        if not self.client:
            return []

        response = self.client.positions()
        # Response: {"s": "ok", "netPositions": [...]}

        positions = []
        if response.get("s") == "ok":
            net_positions = response.get("netPositions", [])
            for p in net_positions:
                # Fyers side: 1 (Long), -1 (Short)
                qty = p.get("netQty", 0)
                if qty == 0:
                    continue

                side = "LONG" if qty > 0 else "SHORT"

                positions.append(Position(
                    symbol=p.get("symbol"),
                    side=side,
                    quantity=abs(qty),
                    entry_price=float(p.get("buyAvg", 0)) if side == "LONG" else float(p.get("sellAvg", 0)),
                    current_price=float(p.get("ltp", 0)),
                    pnl=float(p.get("pl", 0)),
                    product_type=p.get("productType", "INTRADAY")
                ))
        return positions

    def get_orders(self) -> list[dict[str, Any]]:
        if not self.client:
            return []
        response = self.client.orderbook()
        return response.get("orderBook", [])

    def get_holdings(self) -> list[dict[str, Any]]:
        if not self.client:
            return []
        response = self.client.holdings()
        return response.get("holdings", [])

    def get_funds(self) -> BrokerFunds:
        if not self.client:
            return BrokerFunds(available=0, used=0, total=0)

        response = self.client.funds()

        available: float = 0
        used: float = 0
        total: float = 0

        if isinstance(response, dict):
            available = float(response.get("available", 0) or 0)
            used = float(response.get("used", 0) or 0)
            total = float(response.get("total", 0) or (available + used))
             # Handle complex fund_limit structure if needed, but simple dict check is usually enough
             # or verify against previous implementation logic
             # The previous logic had a complex check for 'fund_limit' list. Retaining it.
            funds = response.get("fund_limit", [])
            if isinstance(funds, list):
                for f in funds:
                    if f.get("title") == "Total Balance":
                        total = float(f.get("equityAmount", 0))
                    if f.get("title") == "Available Balance":
                        available = float(f.get("equityAmount", 0))
                    if f.get("title") == "Utilized Amount":
                        used = float(f.get("equityAmount", 0))

        return BrokerFunds(available=available, used=used, total=total)

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get live quote."""
        if not self.client:
            return {}
        data = {"symbols": symbol} # "NSE:SBIN-EQ"
        response = self.client.quotes(data=data)
        if response.get("s") == "ok":
            d = response.get("d", [])
            if d:
                return d[0]
        return {}

    def get_tradebook(self) -> list[dict[str, Any]]:
        """Get executed trades."""
        if not self.client:
            return []
        response = self.client.tradebook()
        # Returns list of trades
        return response.get("tradeBook", [])

    def exit_position(self, symbol: str) -> dict[str, Any]:
        """Exit position for a specific symbol."""
        if not self.client:
            return {"status": "ERROR", "message": "Client not initialized"}

        # Strategy: Get positions, find matches, close them.
        positions = self.get_positions()
        res = {"status": "OK", "message": "No positions found"}

        for p in positions:
            if p["symbol"] == symbol:
                # Place a counter order (market) to close it.
                order_data = {
                    "symbol": symbol,
                    "quantity": p["quantity"],
                    "side": "SELL" if p["side"] == "LONG" else "BUY",
                    "type": "MARKET",
                    "product": p["product_type"]
                }
                res = self.place_order(order_data)

        return res

    def exit_all_positions(self) -> dict[str, Any]:
        """Panic button: Exit all positions."""
        if not self.client:
            return {"status": "ERROR", "message": "Client not initialized"}

        # Fyers API supports exiting all via exit_positions
        return self.client.exit_positions(data={})

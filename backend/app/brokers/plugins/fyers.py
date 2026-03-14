import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from typing import Any

from ...services.fyers_client import get_fyers_client
from ..base import BrokerFunds, IBroker, OrderResponse, Position

CLIENT_NOT_INITIALIZED = "Client not initialized"


class FyersBroker(IBroker):
    """
    Fyers Implementation of IBroker.
    Wraps actual API calls for Live Trading and standardizes output.
    """

    _executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="fyers-broker")
    _circuit_failures = 0
    _circuit_open_until = 0.0
    _circuit_failure_threshold = 3
    _circuit_recovery_sec = 20.0

    def __init__(self):
        self.client = None
        self.connect()

    @classmethod
    def _circuit_open(cls) -> bool:
        return time.monotonic() < cls._circuit_open_until

    @classmethod
    def _record_success(cls) -> None:
        cls._circuit_failures = 0
        cls._circuit_open_until = 0.0

    @classmethod
    def _record_failure(cls) -> None:
        cls._circuit_failures += 1
        if cls._circuit_failures >= cls._circuit_failure_threshold:
            cls._circuit_open_until = time.monotonic() + cls._circuit_recovery_sec

    def _call_with_guard(
        self,
        fn,
        *,
        timeout_sec: float = 2.0,
        fallback: Any,
    ) -> Any:
        if self._circuit_open():
            return fallback

        future = self._executor.submit(fn)
        try:
            result = future.result(timeout=timeout_sec)
            self._record_success()
            return result
        except FutureTimeoutError:
            self._record_failure()
            return fallback
        except Exception:
            self._record_failure()
            return fallback

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
            self.connect()  # Try one last time
            if not self.client:
                return OrderResponse(
                    order_id="",
                    status="REJECTED",
                    message=f"Fyers {CLIENT_NOT_INITIALIZED}. Please Login.",
                    details=None,
                )

        # Map generic order dict to Fyers parameters
        # order keys: symbol, quantity, side (BUY/SELL), type (MARKET/LIMIT), product (MIS/CNC/INTRADAY)

        product_map = {"MIS": "INTRADAY", "INTRADAY": "INTRADAY", "CNC": "CNC", "MARGIN": "MARGIN"}

        data = {
            "symbol": order["symbol"],
            "qty": order["quantity"],
            "type": 2 if order.get("type", "MARKET") == "MARKET" else 1,  # 1=Limit, 2=Market
            "side": 1 if order.get("side") == "BUY" else -1,
            "productType": product_map.get(order.get("product", "INTRADAY").upper(), "INTRADAY"),
            "limitPrice": order.get("price", 0) if order.get("type") != "MARKET" else 0,
            "stopPrice": 0,
            "validity": "DAY",
            "disclosedQty": 0,
            "offlineOrder": False,
        }

        try:
            response = self._call_with_guard(
                lambda: self.client.place_order(data=data),
                timeout_sec=1.5,
                fallback={"s": "error", "message": "Fyers order timeout/unavailable"},
            )

            if response.get("s") == "ok":
                return OrderResponse(
                    order_id=response.get("id", ""),
                    status="SUBMITTED",
                    message=response.get("message", "Order Placed"),
                    details=response,
                )
            else:
                return OrderResponse(
                    order_id="",
                    status="REJECTED",
                    message=response.get("message", "Fyers rejected order"),
                    details=response,
                )

        except Exception as e:
            return OrderResponse(order_id="", status="ERROR", message=str(e), details=None)

    def cancel_order(self, order_id: str) -> dict[str, Any]:
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}
        data = {"id": order_id}
        return self._call_with_guard(
            lambda: self.client.cancel_order(data=data),
            timeout_sec=1.5,
            fallback={"s": "error", "message": "Fyers cancel timeout/unavailable"},
        )

    def modify_order(self, order_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """
        Modify pending order.
        params: {type, limitPrice, qty}
        """
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}

        data = {"id": order_id, "type": 1 if params.get("order_type") != "MARKET" else 2}

        if "quantity" in params:
            data["qty"] = params["quantity"]
        if "price" in params:
            data["limitPrice"] = params["price"]
            data["type"] = 1  # Force Limit

        # If changing to MARKET, set type=2 and limitPrice=0
        if params.get("order_type") == "MARKET":
            data["type"] = 2
            data["limitPrice"] = 0

        return self._call_with_guard(
            lambda: self.client.modify_order(data=data),
            timeout_sec=1.5,
            fallback={"s": "error", "message": "Fyers modify timeout/unavailable"},
        )

    def get_positions(self) -> list[Position]:
        if not self.client:
            return []

        response = self._call_with_guard(
            lambda: self.client.positions(),
            timeout_sec=2.0,
            fallback={"s": "error", "netPositions": []},
        )
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

                positions.append(
                    Position(
                        symbol=p.get("symbol"),
                        side=side,
                        quantity=abs(qty),
                        entry_price=float(p.get("buyAvg", 0))
                        if side == "LONG"
                        else float(p.get("sellAvg", 0)),
                        current_price=float(p.get("ltp", 0)),
                        pnl=float(p.get("pl", 0)),
                        product_type=p.get("productType", "INTRADAY"),
                    )
                )
        return positions

    def get_orders(self) -> list[dict[str, Any]]:
        if not self.client:
            return []
        response = self._call_with_guard(
            lambda: self.client.orderbook(),
            timeout_sec=2.0,
            fallback={"orderBook": []},
        )
        return response.get("orderBook", [])

    def get_holdings(self) -> list[dict[str, Any]]:
        if not self.client:
            return []
        response = self._call_with_guard(
            lambda: self.client.holdings(),
            timeout_sec=2.0,
            fallback={"holdings": []},
        )
        return response.get("holdings", [])

    def get_funds(self) -> BrokerFunds:
        if not self.client:
            return BrokerFunds(available=0, used=0, total=0)

        response = self._call_with_guard(
            lambda: self.client.funds(),
            timeout_sec=2.0,
            fallback={},
        )

        available: float = 0
        used: float = 0
        total: float = 0

        if isinstance(response, dict):
            available = float(response.get("available", 0) or 0)
            used = float(response.get("used", 0) or 0)
            total = float(response.get("total", 0) or (available + used))
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
        data = {"symbols": symbol}  # "NSE:SBIN-EQ"
        response = self._call_with_guard(
            lambda: self.client.quotes(data=data),
            timeout_sec=1.5,
            fallback={},
        )
        if response.get("s") == "ok":
            d = response.get("d", [])
            if d:
                return d[0]
        return {}

    def get_tradebook(self) -> list[dict[str, Any]]:
        """Get executed trades."""
        if not self.client:
            return []
        response = self._call_with_guard(
            lambda: self.client.tradebook(),
            timeout_sec=2.0,
            fallback={"tradeBook": []},
        )
        return response.get("tradeBook", [])

    @staticmethod
    def _build_exit_order(position: Position) -> dict[str, Any]:
        return {
            "symbol": position["symbol"],
            "quantity": position["quantity"],
            "side": "SELL" if position["side"] == "LONG" else "BUY",
            "type": "MARKET",
            "product": position["product_type"],
        }

    def exit_position(self, symbol: str) -> dict[str, Any]:
        """Exit position for a specific symbol."""
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}

        matching_positions = [position for position in self.get_positions() if position["symbol"] == symbol]
        if not matching_positions:
            return {"status": "OK", "message": "No positions found"}

        result: dict[str, Any] = {"status": "OK", "message": "No positions found"}
        for position in matching_positions:
            result = self.place_order(self._build_exit_order(position))
        return result

    def exit_all_positions(self) -> dict[str, Any]:
        """Panic button: Exit all positions."""
        if not self.client:
            return {"status": "ERROR", "message": CLIENT_NOT_INITIALIZED}

        # Fyers API supports exiting all via exit_positions
        return self._call_with_guard(
            lambda: self.client.exit_positions(data={}),
            timeout_sec=1.5,
            fallback={"s": "error", "message": "Fyers exit timeout/unavailable"},
        )

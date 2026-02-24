from abc import ABC, abstractmethod
from typing import Any, TypedDict

# --- Standardized Schemas ---


class OrderResponse(TypedDict):
    order_id: str
    status: str
    message: str
    details: dict[str, Any] | None


class Position(TypedDict):
    symbol: str
    side: str  # LONG/SHORT
    quantity: int
    entry_price: float
    current_price: float
    pnl: float
    product_type: str


class BrokerFunds(TypedDict):
    available: float
    used: float
    total: float


class IBroker(ABC):
    """
    Interface for Broker Adapters (IBroker).
    Enforces strict standard for all broker plugins.
    """

    @abstractmethod
    def place_order(self, order: dict[str, Any]) -> OrderResponse:
        """
        Place an order.
        Args:
            order: dict with keys {symbol, side, quantity, type, price(opt), product(opt)}
        """
        pass

    @abstractmethod
    def modify_order(self, order_id: str, params: dict[str, Any]) -> dict[str, Any]:
        """Modify an existing order."""
        pass

    @abstractmethod
    def cancel_order(self, order_id: str) -> dict[str, Any]:
        """Cancel an order."""
        pass

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Get standardized list of current open positions."""
        pass

    @abstractmethod
    def get_orders(self) -> list[dict[str, Any]]:
        """Get list of orders (today's)."""
        pass

    @abstractmethod
    def get_holdings(self) -> list[dict[str, Any]]:
        """Get demat holdings."""
        pass

    @abstractmethod
    def get_funds(self) -> BrokerFunds:
        """Get funds details."""
        pass

    @abstractmethod
    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Get live quote (LTP, etc)."""
        pass

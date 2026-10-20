"""
Risk Manager Service
====================
Pre-trade and post-trade risk checks for live trading.
Implements circuit breakers, position limits, and margin checks.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import func, or_
from sqlalchemy.orm import Query, Session

from ..database import SessionLocal
from ..models.live_order import LiveOrder
from ..models.live_position import LivePosition
from ..utils.market_hours import DEFAULT_POLICY, SESSION_CLOSE, SESSION_OPEN, MarketHoursPolicy
from .symbol_master import SymbolFormat, symbol_master

if TYPE_CHECKING:
    from ..brokers.plugins.fyers import FyersBroker

logger = logging.getLogger(__name__)


class RiskStatus(Enum):
    """Risk check status"""

    PASS = "PASS"  # noqa: S105 - risk status value and not a password
    FAIL = "FAIL"
    WARNING = "WARNING"


@dataclass
class RiskCheckResult:
    """Result of a risk check"""

    status: RiskStatus
    message: str
    code: str
    details: dict | None = None


INVALID_QUANTITY_MESSAGE = "Quantity must be a positive integer"


class InvalidQuantityError(ValueError):
    """An order quantity that is not a positive whole number. The message is user-facing."""

    def __init__(self) -> None:
        super().__init__(INVALID_QUANTITY_MESSAGE)


def coerce_quantity(value: object) -> int:
    """An order quantity as a positive int.

    Accepts an int, an integral float or Decimal (5.0) and a string of digits ("5").
    Rejects booleans, fractions (2.5, "2.5"), anything else non-numeric, and values <= 0,
    raising InvalidQuantityError. Never truncates: 2.9 is rejected, not read as 2.
    """
    if isinstance(value, bool):
        raise InvalidQuantityError()
    if isinstance(value, int):
        quantity = value
    elif isinstance(value, float):
        if not value.is_integer():
            raise InvalidQuantityError()
        quantity = int(value)
    elif isinstance(value, Decimal):
        if not value.is_finite() or value != value.to_integral_value():
            raise InvalidQuantityError()
        quantity = int(value)
    elif isinstance(value, str):
        try:
            quantity = int(value.strip())
        except ValueError:
            raise InvalidQuantityError() from None
    else:
        raise InvalidQuantityError()
    if quantity <= 0:
        raise InvalidQuantityError()
    return quantity


def _invalid_quantity_result() -> RiskCheckResult:
    return RiskCheckResult(
        status=RiskStatus.FAIL, message=INVALID_QUANTITY_MESSAGE, code="INVALID_QUANTITY"
    )


def _live_orders(query: Query) -> Query:
    """Only LIVE orders. A row with no mode flag (legacy) counts as LIVE: fail safe."""
    return query.filter(or_(LiveOrder.is_paper == 0, LiveOrder.is_paper.is_(None)))


def _with_warnings(checks: list[Callable[[], RiskCheckResult]], passed: str) -> RiskCheckResult:
    """Run checks in order. The first FAIL is returned as is. Otherwise the result is a
    WARNING when any check warned (the first warning's message and code), else a PASS;
    either way details["warnings"] lists every warning message."""
    warnings: list[RiskCheckResult] = []
    for check in checks:
        result = check()
        if result.status == RiskStatus.FAIL:
            return result
        if result.status == RiskStatus.WARNING:
            warnings.append(result)
    messages = [w.message for w in warnings]
    if warnings:
        first = warnings[0]
        return RiskCheckResult(
            status=RiskStatus.WARNING,
            message=first.message,
            code=first.code,
            details={**(first.details or {}), "warnings": messages},
        )
    return RiskCheckResult(
        status=RiskStatus.PASS, message=passed, code="RISK_PASS", details={"warnings": messages}
    )


@dataclass
class RiskConfig:
    """Risk configuration"""

    # Position limits
    max_position_size: int = 1000  # Max quantity per symbol
    max_total_exposure: float = 500000  # Max total position value in INR
    max_positions_count: int = 20  # Max number of open positions

    # Daily limits
    max_daily_loss: float = 5000  # Circuit breaker - max daily loss in INR
    max_orders_per_day: int = 100  # Max orders per day

    # Options specific
    max_naked_short_quantity: int = 500  # Max naked short option quantity

    # Order confirmation threshold
    large_order_notional: float = 10000  # Orders above this require confirmation

    # Price sanity: a modified limit or trigger price may sit at most this far from the LTP.
    # 20% is the widest NSE circuit band, so a price outside it cannot be a real intent.
    max_price_deviation_pct: float = 20.0


class RiskManager:
    """
    Risk Manager for pre-trade and post-trade risk validation.
    """

    def __init__(
        self,
        config: RiskConfig | None = None,
        market_hours: MarketHoursPolicy = DEFAULT_POLICY,
    ) -> None:
        self.config = config or RiskConfig()
        # The one owner of NSE session times; tests inject a policy with a FixedClock.
        self.market_hours = market_hours
        self._broker = None

    def _get_broker(self) -> "FyersBroker":
        """Lazy load Fyers broker"""
        if self._broker is None:
            from ..brokers.plugins.fyers import FyersBroker

            self._broker = FyersBroker()
        return self._broker

    @staticmethod
    def _quote_symbol(symbol: str, fyers_symbol: str | None) -> str:
        """Fyers symbol to quote: the explicit fyers_symbol, else symbol converted."""
        if fyers_symbol:
            return fyers_symbol
        if symbol_master.is_valid(symbol, SymbolFormat.FYERS_FORMAT):
            return symbol
        return symbol_master.to_fyers(symbol)

    def _get_estimated_price(self, symbol: str, fyers_symbol: str | None = None) -> float:
        """
        Get estimated price for a symbol.
        Tries to fetch from broker quote first, falls back to config default.

        Args:
            symbol: Symbol to get price for, in DB ("SBIN") or Fyers format
            fyers_symbol: Fyers symbol to quote, when the caller already has it

        Returns:
            Estimated price or 0 if unavailable
        """
        if not symbol and not fyers_symbol:
            return 0

        try:
            from ..brokers.plugins.fyers import quote_last_price

            quote_symbol = self._quote_symbol(symbol, fyers_symbol)
            price = quote_last_price(self._get_broker().get_quote(quote_symbol))
            if price > 0:
                return price
        except Exception as e:
            logger.warning(f"Could not fetch quote for {symbol}: {e}")

        # Fallback to default from config or env
        import os

        default_price = float(os.getenv("RISK_DEFAULT_PRICE", "0"))
        if default_price > 0:
            logger.info(f"Using configured default price {default_price} for {symbol}")
        return default_price

    def pre_trade_check(
        self, order_params: dict, db: Session | None = None, *, user_id: str
    ) -> RiskCheckResult:
        """
        Perform pre-trade risk checks for a new LIVE order.

        Args:
            order_params: Order details
            db: Database session
            user_id: whose daily loss to check (the owner of the synced LIVE positions)

        Returns:
            The first FAIL. Otherwise a WARNING when any check warned, else a PASS. A
            WARNING or PASS lists every warning message in details["warnings"].
        """
        try:
            quantity = coerce_quantity(order_params.get("quantity"))
        except InvalidQuantityError:
            return _invalid_quantity_result()
        order = {**order_params, "quantity": quantity}

        checks = [
            self._check_market_hours,
            # Circuit breaker
            lambda: self._check_daily_loss_limit(db, user_id),
            lambda: self._check_position_size(order, db),
            lambda: self._check_total_exposure(order, db),
            lambda: self._check_available_margin(order),
            lambda: self._check_order_frequency(db),
        ]
        if order.get("instrument_type") in ["CE", "PE"]:
            checks.append(lambda: self._check_naked_short_option(order, db))
        return _with_warnings(checks, "All risk checks passed")

    @staticmethod
    def _effective_price(order: dict, ltp: float) -> float:
        """Price an order is valued at: its limit for LIMIT/SL, else the live price."""
        if str(order.get("type") or "MARKET").upper() in {"LIMIT", "SL"}:
            return float(order.get("price") or 0)
        return ltp

    def pre_modify_check(
        self,
        order_params: dict,
        current: dict,
        ltp: float,
        db: Session | None = None,
        *,
        user_id: str,
    ) -> RiskCheckResult:
        """Checks for changing a working LIVE order.

        Args:
            order_params: the order as it will be after the change (symbol, side, quantity,
                type, price, trigger_price, product, instrument_type)
            current: the order as it is now (quantity, type, price)
            ltp: last traded price from a fresh quote; 0 when none was available
            db: Database session
            user_id: whose daily loss to check (the owner of the synced LIVE positions)

        Every modify must pass market hours, the per-order size limit and the price band.
        A modify that raises the order's notional (more quantity, a higher limit, or a
        switch to a dearer type) runs the exposure and margin checks on the notional
        increase; one that adds quantity also runs the daily-loss and naked-short checks.
        Fails closed when either notional cannot be priced. Returns the first FAIL, else
        a WARNING when any check warned, else a PASS; both list every warning message in
        details["warnings"].
        """
        try:
            new_qty = coerce_quantity(order_params.get("quantity"))
            old_qty = coerce_quantity(current.get("quantity"))
        except InvalidQuantityError:
            return _invalid_quantity_result()
        order_params = {**order_params, "quantity": new_qty}
        checks = [
            self._check_market_hours,
            lambda: self._check_position_size(order_params, db),
            lambda: self._check_price_band(order_params, ltp),
        ]
        new_price = self._effective_price(order_params, ltp)
        old_price = self._effective_price(current, ltp)
        if new_price <= 0 or old_price <= 0:
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message=(
                    f"Cannot price the order for {order_params.get('symbol', '')}; modify rejected."
                ),
                code="PRICE_ESTIMATE_FAILED",
            )
        notional_increase = new_qty * new_price - old_qty * old_price
        if notional_increase > 0:
            # Valued as one unit at the notional increase: exposure and margin both
            # multiply quantity by price.
            added_value = {**order_params, "quantity": 1, "price": notional_increase}
            checks += [
                lambda: self._check_total_exposure(added_value, db),
                lambda: self._check_available_margin(added_value),
            ]
        quantity_increase = new_qty - old_qty
        if quantity_increase > 0:
            added_qty = {**order_params, "quantity": quantity_increase}
            checks.append(lambda: self._check_daily_loss_limit(db, user_id))
            if order_params.get("instrument_type") in ["CE", "PE"]:
                checks.append(lambda: self._check_naked_short_option(added_qty, db))
        return _with_warnings(checks, "All modify risk checks passed")

    def _check_price_band(self, order_params: dict, ltp: float) -> RiskCheckResult:
        """A limit or trigger price must lie within max_price_deviation_pct of the LTP.

        Fails closed when there is no live price to compare against.
        """
        symbol = order_params.get("symbol", "")
        if ltp <= 0:
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message=f"No live price for {symbol}; cannot verify the order price.",
                code="PRICE_UNAVAILABLE",
            )
        order_type = str(order_params.get("type") or "MARKET").upper()
        prices = []
        if order_type in {"LIMIT", "SL"}:
            prices.append(("limit", float(order_params.get("price") or 0)))
        if order_type in {"SL", "SL-M"}:
            prices.append(("trigger", float(order_params.get("trigger_price") or 0)))
        band = self.config.max_price_deviation_pct
        for label, price in prices:
            deviation_pct = abs(price - ltp) / ltp * 100
            if deviation_pct > band:
                return RiskCheckResult(
                    status=RiskStatus.FAIL,
                    message=(
                        f"The {label} price {price:.2f} is more than {band:.0f}% "
                        f"from the last price {ltp:.2f}."
                    ),
                    code="PRICE_OUT_OF_BAND",
                    details={"price": price, "ltp": ltp, "band_pct": band},
                )
        return RiskCheckResult(
            status=RiskStatus.PASS, message="Price within band", code="PRICE_BAND_OK"
        )

    def pre_exit_check(self) -> RiskCheckResult:
        """Checks before any LIVE exit or square-off: the market must be open.

        Exits only reduce exposure, so the loss, exposure, margin and order-count limits do
        not apply: they must never trap an open position. Each exit order is then checked
        against its position with exit_order_check.
        """
        return self._check_market_hours()

    @staticmethod
    def exit_order_check(net_qty: int, side: str, quantity: int) -> RiskCheckResult:
        """An exit order may only reduce an open position: the opposite side, and no more
        than the open quantity. It must never flip or add to a position."""
        if net_qty == 0:
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message="No open position to exit.",
                code="NO_OPEN_POSITION",
            )
        expected_side = "SELL" if net_qty > 0 else "BUY"
        if side != expected_side:
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message=f"An exit of a {'long' if net_qty > 0 else 'short'} position "
                f"must {expected_side}.",
                code="EXIT_WRONG_SIDE",
            )
        if quantity <= 0 or quantity > abs(net_qty):
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message=f"Exit quantity {quantity} exceeds the open quantity {abs(net_qty)}.",
                code="EXIT_EXCEEDS_POSITION",
            )
        return RiskCheckResult(
            status=RiskStatus.PASS, message="Exit reduces the position", code="EXIT_OK"
        )

    def _check_market_hours(self) -> RiskCheckResult:
        """Check if market is open, by the shared MarketHoursPolicy."""
        if not self.market_hours.is_open():
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message=(
                    "Market is closed. Orders can only be placed during market hours "
                    f"({SESSION_OPEN:%H:%M} - {SESSION_CLOSE:%H:%M} IST)."
                ),
                code="MARKET_CLOSED",
            )

        return RiskCheckResult(status=RiskStatus.PASS, message="Market is open", code="MARKET_OPEN")

    def _check_daily_loss_limit(self, db: Session | None, user_id: str) -> RiskCheckResult:
        """Check if this user's daily loss limit has been reached"""
        owns_session = db is None
        db = db or SessionLocal()
        try:
            today = date.today()

            # Calculate realized P&L from today's trades
            # This is a simplified calculation - in production you'd use actual tradebook
            result = (
                db.query(func.sum(LivePosition.realized_pl))
                .filter(
                    LivePosition.user_id == user_id,
                    func.date(LivePosition.last_synced_at) == today,
                )
                .scalar()
            )

            realized_pnl = result or 0

            # Also check unrealized P&L
            result = (
                db.query(func.sum(LivePosition.unrealized_pl))
                .filter(LivePosition.user_id == user_id, LivePosition.net_qty != 0)
                .scalar()
            )

            unrealized_pnl = result or 0
            total_pnl = realized_pnl + unrealized_pnl

            if total_pnl < -self.config.max_daily_loss:
                return RiskCheckResult(
                    status=RiskStatus.FAIL,
                    message=(
                        f"Daily loss limit reached. Current P&L: ₹{total_pnl:.2f}, "
                        f"Limit: ₹{-self.config.max_daily_loss:.2f}"
                    ),
                    code="DAILY_LOSS_LIMIT",
                    details={"current_pnl": total_pnl, "limit": -self.config.max_daily_loss},
                )

            # Warning at 80% of limit
            if total_pnl < -self.config.max_daily_loss * 0.8:
                return RiskCheckResult(
                    status=RiskStatus.WARNING,
                    message=f"Approaching daily loss limit. Current P&L: ₹{total_pnl:.2f}",
                    code="DAILY_LOSS_WARNING",
                    details={"current_pnl": total_pnl, "limit": -self.config.max_daily_loss},
                )

            return RiskCheckResult(
                status=RiskStatus.PASS,
                message="Daily loss within limits",
                code="DAILY_LOSS_OK",
                details={"current_pnl": total_pnl},
            )
        finally:
            if owns_session and db:
                db.close()

    def _check_position_size(self, order_params: dict, _db: Session | None) -> RiskCheckResult:
        """Check if order size exceeds limits"""
        try:
            quantity = coerce_quantity(order_params.get("quantity"))
        except InvalidQuantityError:
            return _invalid_quantity_result()
        symbol = order_params.get("symbol", "")

        if quantity > self.config.max_position_size:
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message=(
                    f"Order size {quantity} exceeds maximum "
                    f"{self.config.max_position_size} for {symbol}"
                ),
                code="POSITION_SIZE_LIMIT",
                details={"quantity": quantity, "limit": self.config.max_position_size},
            )

        return RiskCheckResult(
            status=RiskStatus.PASS, message="Position size within limits", code="POSITION_SIZE_OK"
        )

    def _check_total_exposure(self, order_params: dict, db: Session | None) -> RiskCheckResult:
        """Check if total exposure exceeds limits"""
        owns_session = db is None
        db = db or SessionLocal()
        try:
            # Get current positions value
            positions = db.query(LivePosition).filter(LivePosition.net_qty != 0).all()
            missing_ltp_positions = [p for p in positions if p.ltp is None or p.ltp == 0]
            if missing_ltp_positions:
                symbols = [
                    getattr(position, "symbol", None) or getattr(position, "id", "unknown")
                    for position in missing_ltp_positions
                ]
                logger.warning(
                    "Exposure check aborted because positions are missing LTP: %s",
                    symbols,
                )
                return RiskCheckResult(
                    status=RiskStatus.FAIL,
                    message=(
                        "Cannot verify exposure because one or more open positions "
                        "are missing live prices."
                    ),
                    code="EXPOSURE_LTP_MISSING",
                    details={"symbols": symbols},
                )

            current_exposure = sum(abs(p.net_qty) * p.ltp for p in positions)
            # Add new order value
            quantity = order_params.get("quantity", 0)
            price = order_params.get("price", 0)

            # For market orders, try to get actual quote or use configured default
            if price == 0:
                symbol = order_params.get("symbol", "")
                price = self._get_estimated_price(symbol, order_params.get("fyers_symbol"))
                if price == 0:
                    return RiskCheckResult(
                        status=RiskStatus.FAIL,
                        message=f"Cannot estimate price for {symbol}. Please provide limit price.",
                        code="PRICE_ESTIMATE_FAILED",
                    )

            new_exposure = quantity * price
            total_exposure = current_exposure + new_exposure

            if total_exposure > self.config.max_total_exposure:
                return RiskCheckResult(
                    status=RiskStatus.FAIL,
                    message=(
                        f"Total exposure ₹{total_exposure:.2f} exceeds limit "
                        f"₹{self.config.max_total_exposure:.2f}"
                    ),
                    code="EXPOSURE_LIMIT",
                    details={
                        "current_exposure": current_exposure,
                        "new_exposure": new_exposure,
                        "total": total_exposure,
                        "limit": self.config.max_total_exposure,
                    },
                )

            return RiskCheckResult(
                status=RiskStatus.PASS, message="Exposure within limits", code="EXPOSURE_OK"
            )
        finally:
            if owns_session and db:
                db.close()

    def _check_available_margin(self, order_params: dict) -> RiskCheckResult:
        """Check if sufficient margin is available"""
        try:
            broker = self._get_broker()
            funds = broker.get_funds()

            # Estimate margin required (simplified - would need actual margin calculator)
            quantity = order_params.get("quantity", 0)
            price = order_params.get("price", 0)

            # For market orders, try to get actual quote or use configured default
            if price == 0:
                symbol = order_params.get("symbol", "")
                price = self._get_estimated_price(symbol, order_params.get("fyers_symbol"))

            # Rough estimate: 20% margin for intraday, 100% for CNC
            product = order_params.get("product", "INTRADAY")
            margin_pct = 0.2 if product == "INTRADAY" else 1.0

            estimated_margin = quantity * price * margin_pct
            # BrokerFunds is a TypedDict: read it by key, not attribute.
            available = float(funds["available"])

            if available < estimated_margin:
                return RiskCheckResult(
                    status=RiskStatus.FAIL,
                    message=(
                        f"Insufficient margin. Available: ₹{available:.2f}, "
                        f"Required: ₹{estimated_margin:.2f}"
                    ),
                    code="INSUFFICIENT_MARGIN",
                    details={
                        "available": available,
                        "required": estimated_margin,
                        "shortfall": estimated_margin - available,
                    },
                )

            return RiskCheckResult(
                status=RiskStatus.PASS,
                message="Sufficient margin available",
                code="MARGIN_OK",
                details={"available": available, "required": estimated_margin},
            )
        except Exception:
            logger.exception("Error checking margin")
            # DENY trade if margin check fails (fail-closed). The error stays in the log.
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message="Could not verify margin. Trade denied for safety.",
                code="MARGIN_CHECK_ERROR",
            )

    def _check_order_frequency(self, db: Session | None) -> RiskCheckResult:
        """Check today's LIVE order count. PAPER orders never use up the LIVE limit.

        Counted across the broker account, not per user: every LIVE order lands on the one
        Fyers account, and the routes still tag orders with different user ids.
        """
        owns_session = db is None
        db = db or SessionLocal()
        try:
            order_count = self._live_orders_today(db)

            if order_count >= self.config.max_orders_per_day:
                return RiskCheckResult(
                    status=RiskStatus.FAIL,
                    message=f"Daily order limit reached ({self.config.max_orders_per_day})",
                    code="ORDER_FREQUENCY_LIMIT",
                    details={"orders_today": order_count, "limit": self.config.max_orders_per_day},
                )

            return RiskCheckResult(
                status=RiskStatus.PASS,
                message="Order frequency within limits",
                code="ORDER_FREQUENCY_OK",
            )
        finally:
            if owns_session and db:
                db.close()

    def _check_naked_short_option(self, order_params: dict, db: Session | None) -> RiskCheckResult:
        """Check for naked short option limits"""
        side = order_params.get("side", "BUY")

        if side == "BUY":
            return RiskCheckResult(
                status=RiskStatus.PASS,
                message="Buy option order - no naked short risk",
                code="NAKED_SHORT_OK",
            )

        # It's a sell order - check current short position
        quantity = order_params.get("quantity", 0)
        symbol = order_params.get("symbol", "")

        owns_session = db is None
        db = db or SessionLocal()
        try:
            # Get current short position for this option
            position = (
                db.query(LivePosition)
                .filter(LivePosition.symbol == symbol, LivePosition.net_qty < 0)
                .first()
            )

            current_short = abs(position.net_qty) if position else 0
            total_short = current_short + quantity

            if total_short > self.config.max_naked_short_quantity:
                return RiskCheckResult(
                    status=RiskStatus.FAIL,
                    message=(
                        f"Naked short option limit exceeded. Current: {current_short}, "
                        f"Adding: {quantity}, Limit: {self.config.max_naked_short_quantity}"
                    ),
                    code="NAKED_SHORT_LIMIT",
                    details={
                        "current_short": current_short,
                        "adding": quantity,
                        "total": total_short,
                        "limit": self.config.max_naked_short_quantity,
                    },
                )

            return RiskCheckResult(
                status=RiskStatus.PASS, message="Naked short within limits", code="NAKED_SHORT_OK"
            )
        finally:
            if owns_session and db:
                db.close()

    @staticmethod
    def _live_orders_today(db: Session) -> int:
        today = date.today()
        query = _live_orders(db.query(LiveOrder))
        return query.filter(func.date(LiveOrder.created_at) == today).count()

    def get_exposure_summary(self, user_id: str) -> dict:
        """Get current exposure summary"""
        db = SessionLocal()
        try:
            positions = (
                db.query(LivePosition)
                .filter(LivePosition.user_id == user_id, LivePosition.net_qty != 0)
                .all()
            )

            long_exposure = sum(p.ltp * p.net_qty for p in positions if p.net_qty > 0)
            short_exposure = sum(p.ltp * abs(p.net_qty) for p in positions if p.net_qty < 0)
            total_exposure = long_exposure + short_exposure

            # Get today's P&L
            today = date.today()
            result = (
                db.query(func.sum(LivePosition.realized_pl))
                .filter(
                    LivePosition.user_id == user_id,
                    func.date(LivePosition.last_synced_at) == today,
                )
                .scalar()
            )
            today_pnl = result or 0

            order_count = self._live_orders_today(db)

            return {
                "long_exposure": round(long_exposure, 2),
                "short_exposure": round(short_exposure, 2),
                "total_exposure": round(total_exposure, 2),
                "exposure_limit": self.config.max_total_exposure,
                "exposure_utilization": round(
                    (total_exposure / self.config.max_total_exposure) * 100, 2
                )
                if self.config.max_total_exposure > 0
                else 0,
                "today_pnl": round(today_pnl, 2),
                "daily_loss_limit": -self.config.max_daily_loss,
                "orders_today": order_count,
                "orders_limit": self.config.max_orders_per_day,
                "position_count": len(positions),
                "positions_limit": self.config.max_positions_count,
            }
        finally:
            db.close()

    def check_large_order(self, order_params: dict) -> RiskCheckResult:
        """
        Check if order is large and requires confirmation.

        Returns:
            RiskCheckResult with WARNING status for large orders
        """
        quantity = order_params.get("quantity", 0)
        price = order_params.get("price", 0)

        # For market orders, get current price
        if price == 0:
            symbol = order_params.get("symbol", "")
            price = self._get_estimated_price(symbol, order_params.get("fyers_symbol"))
            if price == 0:
                return RiskCheckResult(
                    status=RiskStatus.WARNING,
                    message=f"Cannot estimate price for {symbol}. Please confirm the order value.",
                    code="PRICE_ESTIMATE_FAILED",
                )

        notional = quantity * price

        if notional > self.config.large_order_notional:
            return RiskCheckResult(
                status=RiskStatus.WARNING,
                message=f"Large order detected. Notional value: ₹{notional:.2f}. Please confirm.",
                code="LARGE_ORDER_CONFIRMATION",
                details={"notional": notional, "threshold": self.config.large_order_notional},
            )

        return RiskCheckResult(
            status=RiskStatus.PASS, message="Order size normal", code="ORDER_SIZE_NORMAL"
        )


# Singleton instance with default config
risk_manager = RiskManager()

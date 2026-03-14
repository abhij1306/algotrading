"""
Risk Manager Service
====================
Pre-trade and post-trade risk checks for live trading.
Implements circuit breakers, position limits, and margin checks.
"""

import logging
from dataclasses import dataclass
from datetime import date, time
from enum import Enum

from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import SessionLocal
from ..models.live_order import LiveOrder
from ..models.live_position import LivePosition

logger = logging.getLogger(__name__)


class RiskStatus(Enum):
    """Risk check status"""

    PASS = "PASS"
    FAIL = "FAIL"
    WARNING = "WARNING"


@dataclass
class RiskCheckResult:
    """Result of a risk check"""

    status: RiskStatus
    message: str
    code: str
    details: dict | None = None


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

    # Market hours (IST)
    market_open_time: time = time(9, 15)
    market_close_time: time = time(15, 30)

    # Order confirmation threshold
    large_order_notional: float = 10000  # Orders above this require confirmation


class RiskManager:
    """
    Risk Manager for pre-trade and post-trade risk validation.
    """

    def __init__(self, config: RiskConfig | None = None):
        self.config = config or RiskConfig()
        self._broker = None

    def _get_broker(self):
        """Lazy load Fyers broker"""
        if self._broker is None:
            from ..brokers.plugins.fyers import FyersBroker

            self._broker = FyersBroker()
        return self._broker

    def _get_estimated_price(self, symbol: str) -> float:
        """
        Get estimated price for a symbol.
        Tries to fetch from broker quote first, falls back to config default.

        Args:
            symbol: Symbol to get price for

        Returns:
            Estimated price or 0 if unavailable
        """
        if not symbol:
            return 0

        try:
            broker = self._get_broker()
            quote = broker.get_quote(symbol)
            if quote and "ltp" in quote:
                return float(quote["ltp"])
            if quote and "last_price" in quote:
                return float(quote["last_price"])
        except Exception as e:
            logger.warning(f"Could not fetch quote for {symbol}: {e}")

        # Fallback to default from config or env
        import os

        default_price = float(os.getenv("RISK_DEFAULT_PRICE", "0"))
        if default_price > 0:
            logger.info(f"Using configured default price {default_price} for {symbol}")
        return default_price

    def pre_trade_check(self, order_params: dict, db: Session | None = None) -> RiskCheckResult:
        """
        Perform pre-trade risk checks.

        Args:
            order_params: Order details
            db: Database session

        Returns:
            RiskCheckResult with status and message
        """
        # Check market hours
        market_check = self._check_market_hours()
        if market_check.status == RiskStatus.FAIL:
            return market_check

        # Check daily loss limit (circuit breaker)
        loss_check = self._check_daily_loss_limit(db)
        if loss_check.status == RiskStatus.FAIL:
            return loss_check

        # Check position size
        size_check = self._check_position_size(order_params, db)
        if size_check.status == RiskStatus.FAIL:
            return size_check

        # Check total exposure
        exposure_check = self._check_total_exposure(order_params, db)
        if exposure_check.status == RiskStatus.FAIL:
            return exposure_check

        # Check available margin (for live orders)
        margin_check = self._check_available_margin(order_params)
        if margin_check.status == RiskStatus.FAIL:
            return margin_check

        # Check order frequency
        frequency_check = self._check_order_frequency(db)
        if frequency_check.status == RiskStatus.FAIL:
            return frequency_check

        # Check for naked short options
        if order_params.get("instrument_type") in ["CE", "PE"]:
            naked_check = self._check_naked_short_option(order_params, db)
            if naked_check.status == RiskStatus.FAIL:
                return naked_check

        return RiskCheckResult(
            status=RiskStatus.PASS, message="All risk checks passed", code="RISK_PASS"
        )

    def _check_market_hours(self) -> RiskCheckResult:
        """Check if market is open"""
        from ..utils.market_hours import is_market_hours

        if not is_market_hours():
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message="Market is closed. Orders can only be placed during market hours (9:15 AM - 3:30 PM IST).",
                code="MARKET_CLOSED",
            )

        return RiskCheckResult(status=RiskStatus.PASS, message="Market is open", code="MARKET_OPEN")

    def _check_daily_loss_limit(self, db: Session | None) -> RiskCheckResult:
        """Check if daily loss limit has been reached"""
        owns_session = db is None
        db = db or SessionLocal()
        try:
            today = date.today()

            # Calculate realized P&L from today's trades
            # This is a simplified calculation - in production you'd use actual tradebook
            result = (
                db.query(func.sum(LivePosition.realized_pl))
                .filter(func.date(LivePosition.last_synced_at) == today)
                .scalar()
            )

            realized_pnl = result or 0

            # Also check unrealized P&L
            result = (
                db.query(func.sum(LivePosition.unrealized_pl))
                .filter(LivePosition.net_qty != 0)
                .scalar()
            )

            unrealized_pnl = result or 0
            total_pnl = realized_pnl + unrealized_pnl

            if total_pnl < -self.config.max_daily_loss:
                return RiskCheckResult(
                    status=RiskStatus.FAIL,
                    message=f"Daily loss limit reached. Current P&L: ₹{total_pnl:.2f}, Limit: ₹{-self.config.max_daily_loss:.2f}",
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
        quantity = order_params.get("quantity", 0)
        symbol = order_params.get("symbol", "")

        if quantity > self.config.max_position_size:
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message=f"Order size {quantity} exceeds maximum {self.config.max_position_size} for {symbol}",
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
            current_exposure = sum(abs(p.net_qty) * (p.ltp or 0) for p in positions)
            # Add new order value
            quantity = order_params.get("quantity", 0)
            price = order_params.get("price", 0)

            # For market orders, try to get actual quote or use configured default
            if price == 0:
                symbol = order_params.get("symbol", "")
                price = self._get_estimated_price(symbol)
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
                    message=f"Total exposure ₹{total_exposure:.2f} exceeds limit ₹{self.config.max_total_exposure:.2f}",
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
                price = self._get_estimated_price(symbol)

            # Rough estimate: 20% margin for intraday, 100% for CNC
            product = order_params.get("product", "INTRADAY")
            margin_pct = 0.2 if product == "INTRADAY" else 1.0

            estimated_margin = quantity * price * margin_pct

            if funds.available < estimated_margin:
                return RiskCheckResult(
                    status=RiskStatus.FAIL,
                    message=f"Insufficient margin. Available: ₹{funds.available:.2f}, Required: ₹{estimated_margin:.2f}",
                    code="INSUFFICIENT_MARGIN",
                    details={
                        "available": funds.available,
                        "required": estimated_margin,
                        "shortfall": estimated_margin - funds.available,
                    },
                )

            return RiskCheckResult(
                status=RiskStatus.PASS,
                message="Sufficient margin available",
                code="MARGIN_OK",
                details={"available": funds.available, "required": estimated_margin},
            )
        except Exception as e:
            logger.error(f"Error checking margin: {e}")
            # DENY trade if margin check fails (fail-closed for security)
            return RiskCheckResult(
                status=RiskStatus.FAIL,
                message=f"Could not verify margin: {str(e)}. Trade denied for safety.",
                code="MARGIN_CHECK_ERROR",
            )

    def _check_order_frequency(self, db: Session | None) -> RiskCheckResult:
        """Check if order frequency exceeds limits"""
        owns_session = db is None
        db = db or SessionLocal()
        try:
            today = date.today()
            order_count = (
                db.query(LiveOrder).filter(func.date(LiveOrder.created_at) == today).count()
            )

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
                    message=f"Naked short option limit exceeded. Current: {current_short}, Adding: {quantity}, Limit: {self.config.max_naked_short_quantity}",
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

    def get_exposure_summary(self, user_id: str = "default_user") -> dict:
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
                .filter(func.date(LivePosition.last_synced_at) == today)
                .scalar()
            )
            today_pnl = result or 0

            # Get order count
            order_count = (
                db.query(LiveOrder).filter(func.date(LiveOrder.created_at) == today).count()
            )

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
            try:
                broker = self._get_broker()
                symbol = order_params.get("symbol", "")
                quote = broker.get_quote(symbol)
                price = quote.get("lp", 100)  # Last traded price
            except Exception:
                price = 100  # Fallback

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

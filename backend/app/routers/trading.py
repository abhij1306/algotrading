"""
Trading Router
==============
API endpoints for order placement, position management, and trading operations.
"""

import logging
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import UUID4, BaseModel, ValidationInfo, field_validator
from sqlalchemy.orm import Session

from ..database import get_db
from ..exceptions import BrokerUnavailableApiError
from ..models.live_order import LiveOrder
from ..models.live_position import LIVE_ACCOUNT_USER_ID
from ..services.execution import paper_book
from ..services.execution.intent import (
    Choice,
    InstrumentType,
    Mode,
    OrderType,
    Product,
    Side,
    parse_choice,
)
from ..services.live_exit_service import live_exit_service
from ..services.live_market_service import live_market
from ..services.order_execution_service import (
    ClientOrderIdField,
    order_execution_service,
)
from ..services.position_sync_service import position_sync_service
from ..services.risk_manager import RiskStatus, risk_manager
from ..utils.db_errors import is_db_unavailable
from ..utils.logger import safe_log_value

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trading", tags=["Trading"])

SINGLE_USER_ID = "default_user"
DBSession = Annotated[Session, Depends(get_db)]
BAD_REQUEST_RESPONSE = {400: {"description": "Invalid request"}}
AUTH_RESPONSE = {401: {"description": "Authentication required"}}
SERVER_ERROR_RESPONSE = {500: {"description": "Server error"}}
BROKER_UNAVAILABLE_RESPONSE = {
    503: {"description": "Positions could not be refreshed from the broker (BROKER_UNAVAILABLE)"}
}
LIVE_POSITIONS_UNAVAILABLE_MESSAGE = "Live positions could not be refreshed from the broker"
SQUARE_MODE_MESSAGE = "mode must be PAPER or LIVE"

# ============== Pydantic Models ==============


_REQUEST_CHOICES: dict[str, type[Choice]] = {
    "mode": Mode,
    "side": Side,
    "type": OrderType,
    "new_type": OrderType,
    "product": Product,
    "instrument_type": InstrumentType,
}


def _request_choice(value: object, info: ValidationInfo, message: str | None = None) -> object:
    """A request's choice field as its enum member, in any letter case.

    An invalid value answers 400 with the enum's fixed message (the code and body these
    fields gave before they were enums), not FastAPI's 422. None passes through for the
    optional fields; a required one then fails as a missing value would.
    """
    if value is None:
        return None
    try:
        return parse_choice(_REQUEST_CHOICES[info.field_name], value)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=message or str(exc)) from None


class OrderRequest(ClientOrderIdField):
    mode: Mode = Mode.PAPER
    symbol: str
    side: Side
    quantity: int
    product: Product
    type: OrderType
    price: float | None = 0.0
    trigger_price: float | None = 0.0
    tag: str | None = "manual"
    instrument_type: InstrumentType | None = InstrumentType.EQ
    strike_price: float | None = None
    expiry_date: str | None = None  # YYYY-MM-DD
    option_type: str | None = None  # CE/PE
    is_live_confirmation_ack: bool = False
    risk_override_reason: str | None = None

    _choices = field_validator("mode", "side", "product", "type", "instrument_type", mode="before")(
        _request_choice
    )


class ModifyOrderRequest(BaseModel):
    order_id: str
    new_quantity: int | None = None
    new_price: float | None = None
    new_type: OrderType | None = None
    new_trigger_price: float | None = None

    _choices = field_validator("new_type", mode="before")(_request_choice)


class ExitPositionRequest(BaseModel):
    """LIVE exit. client_order_id is the idempotency key: one per click, reused on retry."""

    symbol: str | None = None  # If None, exit all positions
    product_type: str | None = None
    client_order_id: UUID4


class SquarePositionRequest(ClientOrderIdField):
    """Square-off; a LIVE request needs a client_order_id (one per click, reused on retry)."""

    symbol: str | None = None
    mode: Mode = Mode.PAPER
    product_type: str | None = None  # LIVE only: limit the exit to this product

    @field_validator("mode", mode="before")
    @classmethod
    def _mode(cls, value: object, info: ValidationInfo) -> object:
        return _request_choice(value, info, SQUARE_MODE_MESSAGE)


class RiskCheckRequest(BaseModel):
    symbol: str
    side: Side
    quantity: int
    product: Product = Product.INTRADAY
    type: OrderType = OrderType.MARKET
    price: float = 0.0
    instrument_type: InstrumentType = InstrumentType.EQ

    _choices = field_validator("side", "product", "type", "instrument_type", mode="before")(
        _request_choice
    )


class RiskCheckResponse(BaseModel):
    can_trade: bool
    status: str
    message: str
    code: str
    details: dict | None = None


class PositionResponse(BaseModel):
    id: str
    symbol: str
    fyers_symbol: str
    side: str
    quantity: int
    net_qty: int
    entry_price: float
    current_price: float
    unrealized_pnl: float
    product_type: str
    instrument_type: str | None = None
    strike_price: float | None = None
    expiry_date: str | None = None
    last_synced: str | None = None


class FundsResponse(BaseModel):
    available: float
    used: float
    total: float


class TradeResponse(BaseModel):
    id: str
    order_id: str
    symbol: str
    side: str
    quantity: int
    price: float
    value: float
    time: str


class ExposureSummaryResponse(BaseModel):
    long_exposure: float
    short_exposure: float
    total_exposure: float
    exposure_limit: float
    exposure_utilization: float
    today_pnl: float
    daily_loss_limit: float
    orders_today: int
    orders_limit: int
    position_count: int
    positions_limit: int


def _to_float(value: object, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _latest_price_for_symbol(symbol: str) -> Decimal:
    """Last tick price from the market-data cache (never the broker), or 0 if none."""
    tick = live_market.get_latest_tick(symbol)
    if tick and isinstance(tick, dict):
        ltp = tick.get("ltp")
        if isinstance(ltp, (int, float)) and not isinstance(ltp, bool):
            return paper_book.to_decimal(ltp)
    return paper_book.ZERO


@dataclass
class _PaperBookEntry:
    product_type: str
    instrument_type: str | None
    position: paper_book.PaperPosition = field(default_factory=paper_book.PaperPosition)


def _resolve_fill_price(order: LiveOrder, position: paper_book.PaperPosition) -> Decimal:
    px = paper_book.to_decimal(order.average_price)
    if px <= 0:
        px = paper_book.to_decimal(order.price)
    if px > 0:
        return px
    return _latest_price_for_symbol(order.symbol) or position.entry_price


def _apply_paper_fill(position: paper_book.PaperPosition, order: LiveOrder) -> None:
    qty = int(order.filled_qty or order.quantity or 0)
    if qty <= 0:
        return

    side = str(order.side or "").upper()
    if side not in {"BUY", "SELL"}:
        logger.warning(
            "Skipping malformed paper fill for order %s with invalid side %r",
            order.internal_id or order.id,
            order.side,
        )
        return

    position.apply_fill(side, qty, _resolve_fill_price(order, position))


def _serialize_paper_position(symbol: str, entry: _PaperBookEntry) -> dict[str, Any] | None:
    """API shape of an open paper position. Money is quantized to paise, sent as floats."""
    position = entry.position
    net_qty = position.net_qty
    if net_qty == 0:
        return None

    entry_price = paper_book.to_money(position.entry_price)
    current_price = paper_book.to_money(_latest_price_for_symbol(symbol) or entry_price)
    realized_pnl = paper_book.to_money(position.realized_pnl)
    unrealized = paper_book.to_money(position.unrealized_pnl(current_price))
    return {
        "id": f"PAPER-{symbol}",
        "symbol": symbol,
        "fyers_symbol": None,
        "side": "LONG" if net_qty > 0 else "SHORT",
        "quantity": abs(net_qty),
        "net_qty": net_qty,
        "entry_price": float(entry_price),
        "current_price": float(current_price),
        "unrealized_pnl": float(unrealized),
        "realized_pnl": float(realized_pnl),
        "net_pnl": float(unrealized + realized_pnl),
        "product_type": entry.product_type,
        "instrument_type": entry.instrument_type,
        "mode": "PAPER",
    }


def _build_paper_positions(db: Session, user_id: str = SINGLE_USER_ID) -> list[dict[str, Any]]:
    orders = (
        db.query(LiveOrder)
        .filter(
            LiveOrder.user_id == user_id,
            LiveOrder.is_paper == 1,
            LiveOrder.status == "FILLED",
            LiveOrder.filled_qty > 0,
        )
        .order_by(LiveOrder.created_at.asc())
        .all()
    )

    book: dict[str, _PaperBookEntry] = {}
    for order in orders:
        entry = book.get(order.symbol)
        if entry is None:
            entry = _PaperBookEntry(order.product_type or "INTRADAY", order.instrument_type)
            book[order.symbol] = entry
        _apply_paper_fill(entry.position, order)

    positions = []
    for symbol, entry in book.items():
        position = _serialize_paper_position(symbol, entry)
        if position:
            positions.append(position)
    return positions


def _create_square_order(pos: dict[str, Any], timestamp: datetime) -> LiveOrder | None:
    qty = int(abs(_to_float(pos.get("net_qty"), 0)))
    if qty <= 0:
        return None

    symbol = str(pos.get("symbol"))
    px = (
        _latest_price_for_symbol(symbol)
        or paper_book.to_decimal(pos.get("current_price"))
        or paper_book.to_decimal(pos.get("entry_price"))
    )
    internal_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
    return LiveOrder(
        id=internal_id,
        internal_id=internal_id,
        user_id=SINGLE_USER_ID,
        symbol=symbol,
        fyers_symbol=symbol,
        side="SELL" if _to_float(pos.get("net_qty"), 0) > 0 else "BUY",
        quantity=qty,
        order_type="MARKET",
        product_type=pos.get("product_type") or "INTRADAY",
        price=0.0,
        trigger_price=0.0,
        status="FILLED",
        filled_qty=qty,
        average_price=paper_book.money_float(px),
        instrument_type=pos.get("instrument_type") or "EQ",
        order_tag="square-off-paper",
        source="MANUAL",
        is_paper=1,
        broker_message="Paper square-off",
        created_at=timestamp,
        updated_at=timestamp,
    )


def _square_paper_positions(req: SquarePositionRequest, db: Session) -> dict[str, Any]:
    paper_positions = _build_paper_positions(db, SINGLE_USER_ID)
    targets = [p for p in paper_positions if not req.symbol or p["symbol"] == req.symbol]
    if not targets:
        return {"status": "SUCCESS", "mode": "PAPER", "message": "No paper position to square"}

    now = datetime.now(UTC)
    squared = 0
    for pos in targets:
        square_order = _create_square_order(pos, now)
        if square_order is None:
            continue
        squared += 1
        db.add(square_order)

    db.commit()
    return {
        "status": "SUCCESS",
        "mode": "PAPER",
        "message": f"Squared {squared} paper position(s)",
    }


# ============== Endpoints ==============


@router.get("/mode")
def get_trading_mode() -> dict[str, str]:
    """Deprecated compatibility endpoint. Orders must now supply mode explicitly."""
    return {
        "mode": "PAPER",
        "message": "Deprecated endpoint. Supply mode on each order request.",
    }


@router.post("/mode")
def set_trading_mode() -> dict[str, str]:
    """Deprecated compatibility endpoint. No shared mode is stored server-side."""
    return {
        "status": "IGNORED",
        "message": "Shared trading mode has been removed. Supply mode on each order request.",
    }


@router.post(
    "/order",
    responses={
        400: {"description": "Invalid request"},
        401: {"description": "Authentication required"},
        500: {"description": "Server error"},
    },
)
def place_order(
    order: OrderRequest,
    db: DBSession,
    x_user_id: Annotated[
        str | None, Header(description="User ID header for authentication")
    ] = None,
    x_user_id_query: Annotated[
        str | None, Query(alias="x_user_id", description="Legacy user ID query parameter")
    ] = None,
) -> dict[str, Any]:
    """Place a new order (PAPER or LIVE based on current mode)"""
    try:
        # Get user_id from header or fallback to DEV_MODE default
        if x_user_id:
            user_id = x_user_id
        elif x_user_id_query:
            user_id = x_user_id_query
        elif os.getenv("DEV_MODE", "false").lower() == "true":
            user_id = "dev_user"
        else:
            raise HTTPException(
                status_code=401, detail="Authentication required. Provide x-user-id header."
            )

        # Convert Request to Dict
        params = order.model_dump()
        params["user_id"] = user_id

        result = order_execution_service.place_order(params, db, mode=order.mode)

        # A duplicate answers 200 with the existing order's state, whatever that state is.
        if result.get("status") == "ERROR" and not result.get("duplicate"):
            raise HTTPException(status_code=400, detail=result.get("message"))

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error placing order: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.put("/order", responses=BAD_REQUEST_RESPONSE)
def modify_order(req: ModifyOrderRequest, db: DBSession) -> dict[str, Any]:
    """Modify an existing order"""
    params = {}
    if req.new_quantity is not None:
        params["quantity"] = req.new_quantity
    if req.new_price is not None:
        params["price"] = req.new_price
    if req.new_type is not None:
        params["order_type"] = req.new_type.value
    if req.new_trigger_price is not None:
        params["trigger_price"] = req.new_trigger_price

    result = order_execution_service.modify_order(req.order_id, params, db)

    if result.get("status") == "ERROR":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.delete("/order/{order_id}", responses=BAD_REQUEST_RESPONSE)
def cancel_order(order_id: str, db: DBSession) -> dict[str, Any]:
    """Cancel an order"""
    result = order_execution_service.cancel_order(order_id, db)
    if result.get("status") == "ERROR":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/orders", response_model=None)
def get_orders(db: DBSession, limit: int = 50) -> list[LiveOrder]:
    """Get today's orders"""
    return order_execution_service.get_todays_orders(db, limit)


# ============== Position Endpoints ==============


def _sync_live_positions() -> bool:
    """Refresh LIVE positions from the broker. False when the sync failed, in which case
    the stored rows are whatever the last successful sync left."""
    try:
        result = position_sync_service.sync_positions()
    except Exception as exc:
        if is_db_unavailable(exc):
            raise  # 503 DATABASE_UNAVAILABLE from the app handler
        # A broker plugin should raise BrokerUnavailableError, but any failure means the
        # stored rows were not refreshed. The error text stays in the server log.
        logger.exception("Live position sync raised")
        return False
    if result.get("status") == "success":
        return True
    logger.warning(
        "Live position sync failed: %s", safe_log_value(result.get("code") or "SYNC_FAILED")
    )
    return False


@router.get("/positions", responses={**SERVER_ERROR_RESPONSE, **BROKER_UNAVAILABLE_RESPONSE})
def get_positions(
    db: DBSession,
    user_id: Annotated[str, Query(description="User ID")] = LIVE_ACCOUNT_USER_ID,
) -> list[PositionResponse]:
    """Current open LIVE positions, freshly synced from the broker.

    Fails closed: when the sync fails this answers 503 BROKER_UNAVAILABLE rather than the
    stored rows, which could be stale and would read as the current book.
    """
    if not _sync_live_positions():
        raise BrokerUnavailableApiError(LIVE_POSITIONS_UNAVAILABLE_MESSAGE)
    try:
        return position_sync_service.get_positions(user_id)
    except Exception as e:
        logger.exception("Error fetching positions")
        raise HTTPException(status_code=500, detail="Error fetching positions") from e


@router.get("/positions/book", responses=SERVER_ERROR_RESPONSE)
def get_positions_book(
    db: DBSession,
) -> dict[str, Any]:
    """Unified positions view for Terminal: live + paper + net P&L.

    Paper positions are local, so they are always current. When the LIVE sync fails the
    live rows are the last synced ones, each marked `stale: true`, with `live_stale: true`
    and `live_as_of` (the last successful sync, or null). A LIVE exit never trusts these
    rows: it re-reads the broker and fails closed itself.
    """
    try:
        live_fresh = _sync_live_positions()
        live_positions = position_sync_service.get_positions(SINGLE_USER_ID)
        for pos in live_positions:
            unrealized = _to_float(pos.get("unrealized_pnl"), 0.0)
            pos["realized_pnl"] = 0.0
            pos["net_pnl"] = round(unrealized, 2)
            pos["mode"] = "LIVE"
            pos["stale"] = not live_fresh

        paper_positions = _build_paper_positions(db, SINGLE_USER_ID)
        live_net = round(sum(_to_float(p.get("net_pnl"), 0.0) for p in live_positions), 2)
        paper_net = round(sum(_to_float(p.get("net_pnl"), 0.0) for p in paper_positions), 2)

        last_sync = position_sync_service.last_sync
        return {
            "live_positions": live_positions,
            "paper_positions": paper_positions,
            "live_stale": not live_fresh,
            "live_as_of": last_sync.isoformat() if last_sync else None,
            "net_pnl_live": live_net,
            "net_pnl_paper": paper_net,
            "net_pnl_total": round(live_net + paper_net, 2),
        }
    except Exception as e:
        logger.exception("Error building positions book")
        raise HTTPException(
            status_code=500,
            detail="Error building positions book",
        ) from e


@router.get("/positions/summary", responses=SERVER_ERROR_RESPONSE)
def get_position_summary(
    user_id: Annotated[str, Query(description="User ID")] = "default_user",
) -> dict[str, Any]:
    """Get position summary (P&L, exposure, etc.)"""
    try:
        summary = position_sync_service.get_position_summary(user_id)
        return summary
    except Exception as e:
        logger.exception("Error fetching position summary")
        raise HTTPException(
            status_code=500,
            detail="Error fetching position summary",
        ) from e


@router.post(
    "/positions/square",
    responses={
        400: {"description": "Invalid request"},
        500: {"description": "Server error"},
    },
)
def square_position(req: SquarePositionRequest, db: DBSession) -> dict[str, Any]:
    """Square open position(s) in PAPER or LIVE mode."""
    try:
        if req.mode is Mode.LIVE:
            return live_exit_service.exit_positions(
                symbol=req.symbol,
                product_type=req.product_type,
                client_order_id=req.client_order_id,
                user_id=SINGLE_USER_ID,
                db=db,
            )
        return _square_paper_positions(req, db)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        logger.exception("Square position failed for symbol=%s", safe_log_value(req.symbol))
        raise HTTPException(status_code=500, detail="Error squaring position") from e


@router.post(
    "/positions/exit",
    responses={
        400: {"description": "Invalid request"},
        500: {"description": "Server error"},
    },
)
def exit_position(req: ExitPositionRequest, db: DBSession) -> dict[str, Any]:
    """Exit a specific LIVE position, or all LIVE positions when no symbol is given."""
    try:
        return live_exit_service.exit_positions(
            symbol=req.symbol,
            product_type=req.product_type,
            client_order_id=req.client_order_id,
            user_id=SINGLE_USER_ID,
            db=db,
        )
    except Exception as e:
        db.rollback()
        logger.exception("Error exiting position for symbol=%s", safe_log_value(req.symbol))
        raise HTTPException(status_code=500, detail="Error exiting position") from e


# ============== Funds Endpoints ==============


@router.get("/funds", responses=SERVER_ERROR_RESPONSE)
def get_funds() -> FundsResponse:
    """Get available funds/margin from broker"""
    try:
        from ..brokers.plugins.fyers import FyersBroker

        broker = FyersBroker()
        funds = broker.get_funds()

        return FundsResponse(available=funds.available, used=funds.used, total=funds.total)
    except Exception as e:
        logger.exception("Error fetching funds")
        raise HTTPException(status_code=500, detail="Error fetching funds") from e


# ============== Tradebook Endpoints ==============


@router.get("/tradebook", responses=SERVER_ERROR_RESPONSE)
def get_tradebook() -> list[dict[str, Any]]:
    """Get executed trades from broker"""
    try:
        from ..brokers.plugins.fyers import FyersBroker

        broker = FyersBroker()
        trades = broker.get_tradebook()
        return trades
    except Exception as e:
        logger.exception("Error fetching tradebook")
        raise HTTPException(status_code=500, detail="Error fetching tradebook") from e


# ============== Risk Management Endpoints ==============


@router.post("/risk-check", responses=SERVER_ERROR_RESPONSE)
def check_risk(req: RiskCheckRequest, db: DBSession) -> RiskCheckResponse:
    """
    Perform pre-trade risk check without placing order.
    Use this to validate if an order would pass risk checks.
    """
    try:
        order_params = req.model_dump()
        result = risk_manager.pre_trade_check(order_params, db, user_id=LIVE_ACCOUNT_USER_ID)

        # A WARNING can still trade, with a risk_override_reason on the order.
        return RiskCheckResponse(
            can_trade=result.status != RiskStatus.FAIL,
            status=result.status.value,
            message=result.message,
            code=result.code,
            details=result.details,
        )
    except Exception as e:
        logger.exception("Risk check error")
        raise HTTPException(status_code=500, detail="Risk check error") from e


@router.get(
    "/risk/exposure",
    responses=SERVER_ERROR_RESPONSE,
)
def get_exposure_summary() -> ExposureSummaryResponse:
    """Get current exposure and risk summary"""
    try:
        summary = risk_manager.get_exposure_summary(LIVE_ACCOUNT_USER_ID)
        return ExposureSummaryResponse(**summary)
    except Exception as e:
        logger.exception("Error fetching exposure")
        raise HTTPException(status_code=500, detail="Error fetching exposure") from e


@router.post("/risk/large-order-check", responses=SERVER_ERROR_RESPONSE)
def check_large_order(order: OrderRequest) -> dict[str, Any]:
    """Check if order requires confirmation due to size"""
    try:
        params = order.model_dump()
        result = risk_manager.check_large_order(params)

        return {
            "requires_confirmation": result.status == RiskStatus.WARNING,
            "status": result.status.value,
            "message": result.message,
            "code": result.code,
            "details": result.details,
        }
    except Exception as e:
        logger.exception("Large order check error")
        raise HTTPException(status_code=500, detail="Large order check error") from e

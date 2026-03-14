"""
Trading Router
==============
API endpoints for order placement, position management, and trading operations.
"""

import logging
import os
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.live_order import LiveOrder
from ..services.live_market_service import live_market
from ..services.order_execution_service import order_execution_service
from ..services.position_sync_service import position_sync_service
from ..services.risk_manager import RiskStatus, risk_manager
from ..services.symbol_master import symbol_master

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/trading", tags=["Trading"])

SINGLE_USER_ID = "default_user"
DBSession = Annotated[Session, Depends(get_db)]
BAD_REQUEST_RESPONSE = {400: {"description": "Invalid request"}}
AUTH_RESPONSE = {401: {"description": "Authentication required"}}
SERVER_ERROR_RESPONSE = {500: {"description": "Server error"}}

# ============== Pydantic Models ==============


class OrderRequest(BaseModel):
    mode: str = "PAPER"  # PAPER / LIVE
    symbol: str
    side: str  # BUY / SELL
    quantity: int
    product: str  # INTRADAY / CNC / MARGIN
    type: str  # MARKET / LIMIT / SL / SL-M
    price: float | None = 0.0
    trigger_price: float | None = 0.0
    tag: str | None = "manual"
    instrument_type: str | None = "EQ"  # EQ, FUT, CE, PE
    strike_price: float | None = None
    expiry_date: str | None = None  # YYYY-MM-DD
    option_type: str | None = None  # CE/PE
    is_live_confirmation_ack: bool = False
    risk_override_reason: str | None = None


class ModifyOrderRequest(BaseModel):
    order_id: str
    new_quantity: int | None = None
    new_price: float | None = None
    new_type: str | None = None


class ExitPositionRequest(BaseModel):
    symbol: str | None = None  # If None, exit all positions
    product_type: str | None = None


class SquarePositionRequest(BaseModel):
    symbol: str | None = None
    mode: str = "PAPER"  # PAPER / LIVE


class RiskCheckRequest(BaseModel):
    symbol: str
    side: str
    quantity: int
    product: str = "INTRADAY"
    type: str = "MARKET"
    price: float = 0.0
    instrument_type: str = "EQ"


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


def _latest_price_for_symbol(symbol: str) -> float:
    tick = live_market.get_latest_tick(symbol)
    if tick and isinstance(tick, dict):
        ltp = tick.get("ltp")
        if isinstance(ltp, (int, float)):
            return float(ltp)
    return 0.0


def _initial_paper_state(order: LiveOrder) -> dict[str, Any]:
    return {
        "symbol": order.symbol,
        "product_type": order.product_type or "INTRADAY",
        "instrument_type": order.instrument_type,
        "net_qty": 0,
        "entry_price": 0.0,
        "realized_pnl": 0.0,
    }


def _resolve_fill_price(order: LiveOrder, state: dict[str, Any]) -> float:
    px = _to_float(order.average_price, _to_float(order.price, 0.0))
    if px > 0:
        return px
    return _latest_price_for_symbol(order.symbol) or _to_float(state["entry_price"], 0.0)


def _apply_fill_to_flat_position(state: dict[str, Any], delta: int, px: float) -> bool:
    if int(state["net_qty"]) != 0:
        return False
    state["net_qty"] = delta
    state["entry_price"] = px
    return True


def _apply_fill_to_same_direction(
    state: dict[str, Any], delta: int, px: float, net: int, entry: float
) -> bool:
    if not ((net > 0 and delta > 0) or (net < 0 and delta < 0)):
        return False
    total_qty = abs(net) + abs(delta)
    state["entry_price"] = ((entry * abs(net)) + (px * abs(delta))) / max(total_qty, 1)
    state["net_qty"] = net + delta
    return True


def _apply_fill_to_reversal(
    state: dict[str, Any], delta: int, px: float, net: int, entry: float
) -> None:
    closing_qty = min(abs(net), abs(delta))
    state["realized_pnl"] += (px - entry) * closing_qty if net > 0 else (entry - px) * closing_qty
    net_after = net + delta
    state["net_qty"] = net_after
    if net_after == 0:
        state["entry_price"] = 0.0
    elif (net_after > 0 and net < 0) or (net_after < 0 and net > 0):
        state["entry_price"] = px


def _apply_paper_fill(state: dict[str, Any], order: LiveOrder) -> None:
    qty = int(order.filled_qty or order.quantity or 0)
    if qty <= 0:
        return

    px = _resolve_fill_price(order, state)
    side = str(order.side or "").upper()
    if side not in {"BUY", "SELL"}:
        logger.warning(
            "Skipping malformed paper fill for order %s with invalid side %r",
            order.internal_id or order.id,
            order.side,
        )
        return

    delta = qty if side == "BUY" else -qty
    net = int(state["net_qty"])
    entry = _to_float(state["entry_price"], 0.0)

    if _apply_fill_to_flat_position(state, delta, px):
        return

    if _apply_fill_to_same_direction(state, delta, px, net, entry):
        return

    _apply_fill_to_reversal(state, delta, px, net, entry)


def _serialize_paper_position(symbol: str, state: dict[str, Any]) -> dict[str, Any] | None:
    net_qty = int(state["net_qty"])
    if net_qty == 0:
        return None

    entry_price = round(_to_float(state["entry_price"], 0.0), 2)
    current_price = round(_latest_price_for_symbol(symbol) or entry_price, 2)
    realized_pnl = round(_to_float(state["realized_pnl"], 0.0), 2)
    unrealized = round((current_price - entry_price) * net_qty, 2)
    return {
        "id": f"PAPER-{symbol}",
        "symbol": symbol,
        "fyers_symbol": None,
        "side": "LONG" if net_qty > 0 else "SHORT",
        "quantity": abs(net_qty),
        "net_qty": net_qty,
        "entry_price": entry_price,
        "current_price": current_price,
        "unrealized_pnl": unrealized,
        "realized_pnl": realized_pnl,
        "net_pnl": round(unrealized + realized_pnl, 2),
        "product_type": state["product_type"],
        "instrument_type": state["instrument_type"],
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

    book: dict[str, dict[str, Any]] = {}
    for order in orders:
        state = book.setdefault(order.symbol, _initial_paper_state(order))
        _apply_paper_fill(state, order)

    positions = []
    for symbol, state in book.items():
        position = _serialize_paper_position(symbol, state)
        if position:
            positions.append(position)
    return positions


def _live_square_response(symbol: str | None, result: dict[str, Any]) -> dict[str, Any]:
    ok = result.get("status") in {"SUCCESS", "OK", "SUBMITTED"} or result.get("s") == "ok"
    response = {
        "status": "SUCCESS" if ok else "ERROR",
        "mode": "LIVE",
        "symbol": symbol,
        "message": result.get("message", "Live square sent" if symbol else "Live exit-all sent"),
    }
    if not ok:
        response["details"] = result
    return response


def _square_live_positions(req: SquarePositionRequest) -> dict[str, Any]:
    from ..brokers.plugins.fyers import FyersBroker

    broker = FyersBroker()
    if not req.symbol:
        return _live_square_response(None, broker.exit_all_positions())

    result = broker.exit_position(req.symbol)
    if result.get("status") in {"ERROR", "REJECTED"} or result.get("s") == "error":
        try:
            fyers_symbol = symbol_master.to_fyers(req.symbol)
            result = broker.exit_position(fyers_symbol)
        except Exception as exc:
            fyers_symbol = req.symbol
            logger.exception(
                "Live square fallback failed for symbol=%s fyers_symbol=%s",
                req.symbol,
                fyers_symbol,
                exc_info=exc,
            )
            result = {
                **result,
                "status": "ERROR",
                "message": result.get("message", "Live square-off failed"),
                "fallback_error": str(exc),
                "fyers_symbol": fyers_symbol,
            }
    return _live_square_response(req.symbol, result)


def _create_square_order(pos: dict[str, Any], timestamp: datetime) -> LiveOrder | None:
    qty = int(abs(_to_float(pos.get("net_qty"), 0)))
    if qty <= 0:
        return None

    symbol = str(pos.get("symbol"))
    px = (
        _latest_price_for_symbol(symbol)
        or _to_float(pos.get("current_price"), 0.0)
        or _to_float(pos.get("entry_price"), 0.0)
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
        average_price=round(px, 2),
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
    x_user_id: Annotated[str | None, Query(description="User ID header for authentication")] = None,
) -> dict[str, Any]:
    """Place a new order (PAPER or LIVE based on current mode)"""
    try:
        # Get user_id from header or fallback to DEV_MODE default
        if x_user_id:
            user_id = x_user_id
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

        if result.get("status") == "ERROR":
            raise HTTPException(status_code=400, detail=result.get("message"))

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error placing order: {e}")
        raise HTTPException(status_code=500, detail="Internal server error") from e


@router.put("/order", responses=BAD_REQUEST_RESPONSE)
def modify_order(req: ModifyOrderRequest, db: DBSession) -> dict[str, Any]:
    """Modify an existing order"""
    params = {}
    if req.new_quantity:
        params["quantity"] = req.new_quantity
    if req.new_price:
        params["price"] = req.new_price
    if req.new_type:
        params["order_type"] = req.new_type

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


@router.get("/positions", responses=SERVER_ERROR_RESPONSE)
def get_positions(
    db: DBSession,
    user_id: Annotated[str, Query(description="User ID")] = "default_user",
) -> list[PositionResponse]:
    """Get current open positions (synced from broker in LIVE mode)"""
    try:
        # Sync positions first to ensure fresh data
        position_sync_service.sync_positions()

        positions = position_sync_service.get_positions(user_id)
        return positions
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching positions: {str(e)}") from e


@router.get("/positions/book", responses=SERVER_ERROR_RESPONSE)
def get_positions_book(
    db: DBSession,
) -> dict[str, Any]:
    """Unified positions view for Terminal: live + paper + net P&L."""
    try:
        position_sync_service.sync_positions()
        live_positions = position_sync_service.get_positions(SINGLE_USER_ID)
        for pos in live_positions:
            unrealized = _to_float(pos.get("unrealized_pnl"), 0.0)
            pos["realized_pnl"] = 0.0
            pos["net_pnl"] = round(unrealized, 2)
            pos["mode"] = "LIVE"

        paper_positions = _build_paper_positions(db, SINGLE_USER_ID)
        live_net = round(sum(_to_float(p.get("net_pnl"), 0.0) for p in live_positions), 2)
        paper_net = round(sum(_to_float(p.get("net_pnl"), 0.0) for p in paper_positions), 2)

        return {
            "live_positions": live_positions,
            "paper_positions": paper_positions,
            "net_pnl_live": live_net,
            "net_pnl_paper": paper_net,
            "net_pnl_total": round(live_net + paper_net, 2),
        }
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error building positions book: {str(e)}",
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
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching position summary: {str(e)}",
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
        mode = req.mode.strip().upper()
        if mode not in {"PAPER", "LIVE"}:
            raise HTTPException(status_code=400, detail="mode must be PAPER or LIVE")

        if mode == "LIVE":
            return _square_live_positions(req)
        return _square_paper_positions(req, db)
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error squaring position: {str(e)}") from e


@router.post(
    "/positions/exit",
    responses={
        400: {"description": "Invalid request"},
        500: {"description": "Server error"},
    },
)
def exit_position(req: ExitPositionRequest) -> dict[str, Any]:
    """Exit a specific position or all positions"""
    try:
        from ..brokers.plugins.fyers import FyersBroker

        broker = FyersBroker()

        if req.symbol:
            # Exit specific position
            result = broker.exit_position(req.symbol)
            result_ok = result.get("status") in {"OK", "SUCCESS", "SUBMITTED"} or (
                result.get("s") == "ok"
            )
            return {
                "status": "SUCCESS" if result_ok else "ERROR",
                "message": result.get("message", "Exit order placed"),
                "symbol": req.symbol,
                "results": result.get("results"),
            }
        else:
            # Exit all positions (panic button)
            result = broker.exit_all_positions()
            return {
                "status": "SUCCESS" if result.get("s") == "ok" else "ERROR",
                "message": result.get("message", "All positions exited"),
                "action": "EXIT_ALL",
            }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error exiting position: {str(e)}") from e


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
        raise HTTPException(status_code=500, detail=f"Error fetching funds: {str(e)}") from e


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
        raise HTTPException(status_code=500, detail=f"Error fetching tradebook: {str(e)}") from e


# ============== Risk Management Endpoints ==============


@router.post("/risk-check", responses=SERVER_ERROR_RESPONSE)
def check_risk(req: RiskCheckRequest, db: DBSession) -> RiskCheckResponse:
    """
    Perform pre-trade risk check without placing order.
    Use this to validate if an order would pass risk checks.
    """
    try:
        order_params = req.dict()
        result = risk_manager.pre_trade_check(order_params, db)

        return RiskCheckResponse(
            can_trade=result.status == RiskStatus.PASS,
            status=result.status.value,
            message=result.message,
            code=result.code,
            details=result.details,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Risk check error: {str(e)}") from e


@router.get(
    "/risk/exposure",
    responses=SERVER_ERROR_RESPONSE,
)
def get_exposure_summary() -> ExposureSummaryResponse:
    """Get current exposure and risk summary"""
    try:
        summary = risk_manager.get_exposure_summary()
        return ExposureSummaryResponse(**summary)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching exposure: {str(e)}") from e


@router.post("/risk/large-order-check", responses=SERVER_ERROR_RESPONSE)
def check_large_order(order: OrderRequest, db: DBSession) -> dict[str, Any]:
    """Check if order requires confirmation due to size"""
    try:
        params = order.dict()
        result = risk_manager.check_large_order(params)

        return {
            "requires_confirmation": result.status == RiskStatus.WARNING,
            "status": result.status.value,
            "message": result.message,
            "code": result.code,
            "details": result.details,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Large order check error: {str(e)}") from e

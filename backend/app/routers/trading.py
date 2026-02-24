"""
Trading Router
==============
API endpoints for order placement, position management, and trading operations.
"""

import logging
import os
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

logger = logging.getLogger(__name__)
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..models.live_order import LiveOrder
from ..services.live_market_service import live_market
from ..services.order_execution_service import order_execution_service
from ..services.position_sync_service import position_sync_service
from ..services.risk_manager import RiskStatus, risk_manager
from ..services.symbol_master import symbol_master

router = APIRouter(prefix="/api/trading", tags=["Trading"])

SINGLE_USER_ID = "default_user"

# ============== Pydantic Models ==============


class OrderRequest(BaseModel):
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


class ModeRequest(BaseModel):
    mode: str  # PAPER / LIVE


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
    instrument_type: str | None
    strike_price: float | None
    expiry_date: str | None
    last_synced: str | None


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


def _to_float(value: Any, default: float = 0.0) -> float:
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
        symbol = order.symbol
        state = book.setdefault(
            symbol,
            {
                "symbol": symbol,
                "product_type": order.product_type or "INTRADAY",
                "instrument_type": order.instrument_type,
                "net_qty": 0,
                "entry_price": 0.0,
                "realized_pnl": 0.0,
            },
        )

        qty = int(order.filled_qty or order.quantity or 0)
        if qty <= 0:
            continue

        px = _to_float(order.average_price, _to_float(order.price, 0.0))
        if px <= 0:
            px = _latest_price_for_symbol(symbol) or state["entry_price"]

        delta = qty if str(order.side).upper() == "BUY" else -qty
        net = int(state["net_qty"])
        entry = _to_float(state["entry_price"], 0.0)

        if net == 0:
            state["net_qty"] = delta
            state["entry_price"] = px
            continue

        same_side = (net > 0 and delta > 0) or (net < 0 and delta < 0)
        if same_side:
            total_qty = abs(net) + abs(delta)
            state["entry_price"] = ((entry * abs(net)) + (px * abs(delta))) / max(total_qty, 1)
            state["net_qty"] = net + delta
            continue

        closing_qty = min(abs(net), abs(delta))
        if net > 0:
            state["realized_pnl"] += (px - entry) * closing_qty
        else:
            state["realized_pnl"] += (entry - px) * closing_qty

        net_after = net + delta
        state["net_qty"] = net_after
        if net_after == 0:
            state["entry_price"] = 0.0
        elif (net_after > 0 and net < 0) or (net_after < 0 and net > 0):
            state["entry_price"] = px

    positions: list[dict[str, Any]] = []
    for symbol, state in book.items():
        net_qty = int(state["net_qty"])
        if net_qty == 0:
            continue
        current_price = _latest_price_for_symbol(symbol) or _to_float(state["entry_price"], 0.0)
        unrealized = (current_price - _to_float(state["entry_price"], 0.0)) * net_qty
        positions.append(
            {
                "id": f"PAPER-{symbol}",
                "symbol": symbol,
                "fyers_symbol": None,
                "side": "LONG" if net_qty > 0 else "SHORT",
                "quantity": abs(net_qty),
                "net_qty": net_qty,
                "entry_price": round(_to_float(state["entry_price"], 0.0), 2),
                "current_price": round(current_price, 2),
                "unrealized_pnl": round(unrealized, 2),
                "realized_pnl": round(_to_float(state["realized_pnl"], 0.0), 2),
                "net_pnl": round(unrealized + _to_float(state["realized_pnl"], 0.0), 2),
                "product_type": state["product_type"],
                "instrument_type": state["instrument_type"],
                "mode": "PAPER",
            }
        )

    return positions


# ============== Endpoints ==============


@router.get("/mode")
def get_trading_mode():
    """Get current trading mode (PAPER or LIVE)"""
    return {"mode": order_execution_service.get_mode()}


@router.post("/mode")
def set_trading_mode(req: ModeRequest):
    """Set trading mode (PAPER or LIVE)"""
    try:
        order_execution_service.set_mode(req.mode)
        return {"status": "SUCCESS", "mode": req.mode}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/order")
def place_order(
    order: OrderRequest,
    db: Session = Depends(get_db),
    x_user_id: str = Query(None, description="User ID header for authentication"),
):
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
        params = order.dict()
        params["user_id"] = user_id

        result = order_execution_service.place_order(params, db)

        if result.get("status") == "ERROR":
            raise HTTPException(status_code=400, detail=result.get("message"))

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error placing order: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.put("/order")
def modify_order(req: ModifyOrderRequest, db: Session = Depends(get_db)):
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


@router.delete("/order/{order_id}")
def cancel_order(order_id: str, db: Session = Depends(get_db)):
    """Cancel an order"""
    result = order_execution_service.cancel_order(order_id, db)
    if result.get("status") == "ERROR":
        raise HTTPException(status_code=400, detail=result.get("message"))
    return result


@router.get("/orders")
def get_orders(limit: int = 50, db: Session = Depends(get_db)):
    """Get today's orders"""
    return order_execution_service.get_todays_orders(db, limit)


# ============== Position Endpoints ==============


@router.get("/positions", response_model=list[PositionResponse])
def get_positions(
    user_id: str = Query("default_user", description="User ID"), db: Session = Depends(get_db)
):
    """Get current open positions (synced from broker in LIVE mode)"""
    try:
        # Sync positions first to ensure fresh data
        position_sync_service.sync_positions()

        positions = position_sync_service.get_positions(user_id)
        return positions
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching positions: {str(e)}")


@router.get("/positions/book")
def get_positions_book(
    db: Session = Depends(get_db),
):
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
        raise HTTPException(status_code=500, detail=f"Error building positions book: {str(e)}")


@router.get("/positions/summary")
def get_position_summary(user_id: str = Query("default_user")):
    """Get position summary (P&L, exposure, etc.)"""
    try:
        summary = position_sync_service.get_position_summary(user_id)
        return summary
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching position summary: {str(e)}")


@router.post("/positions/square")
def square_position(req: SquarePositionRequest, db: Session = Depends(get_db)):
    """Square open position(s) in PAPER or LIVE mode."""
    try:
        mode = req.mode.strip().upper()
        if mode not in {"PAPER", "LIVE"}:
            raise HTTPException(status_code=400, detail="mode must be PAPER or LIVE")

        if mode == "LIVE":
            from ..brokers.plugins.fyers import FyersBroker

            broker = FyersBroker()
            if req.symbol:
                symbol = req.symbol
                result = broker.exit_position(symbol)
                if result.get("status") in {"ERROR", "REJECTED"} or result.get("s") == "error":
                    try:
                        result = broker.exit_position(symbol_master.to_fyers(symbol))
                    except Exception:
                        pass
                ok = result.get("status") in {"SUCCESS", "OK"} or result.get("s") == "ok"
                return {
                    "status": "SUCCESS" if ok else "ERROR",
                    "mode": "LIVE",
                    "symbol": req.symbol,
                    "message": result.get("message", "Live square sent"),
                }
            result = broker.exit_all_positions()
            ok = result.get("status") in {"SUCCESS", "OK"} or result.get("s") == "ok"
            return {
                "status": "SUCCESS" if ok else "ERROR",
                "mode": "LIVE",
                "message": result.get("message", "Live exit-all sent"),
            }

        paper_positions = _build_paper_positions(db, req.user_id)
        targets = [p for p in paper_positions if not req.symbol or p["symbol"] == req.symbol]
        if not targets:
            return {"status": "SUCCESS", "mode": "PAPER", "message": "No paper position to square"}

        now = datetime.utcnow()
        for pos in targets:
            qty = int(abs(_to_float(pos.get("net_qty"), 0)))
            if qty <= 0:
                continue
            symbol = str(pos.get("symbol"))
            side = "SELL" if _to_float(pos.get("net_qty"), 0) > 0 else "BUY"
            px = (
                _latest_price_for_symbol(symbol)
                or _to_float(pos.get("current_price"), 0.0)
                or _to_float(pos.get("entry_price"), 0.0)
            )
            internal_id = f"ORD-{uuid.uuid4().hex[:8].upper()}"
            square_order = LiveOrder(
                id=internal_id,
                internal_id=internal_id,
                user_id=SINGLE_USER_ID,
                symbol=symbol,
                fyers_symbol=symbol,
                side=side,
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
                created_at=now,
                updated_at=now,
            )
            db.add(square_order)

        db.commit()
        return {
            "status": "SUCCESS",
            "mode": "PAPER",
            "message": f"Squared {len(targets)} paper position(s)",
        }
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Error squaring position: {str(e)}")


@router.post("/positions/exit")
def exit_position(req: ExitPositionRequest):
    """Exit a specific position or all positions"""
    try:
        from ..brokers.plugins.fyers import FyersBroker

        broker = FyersBroker()

        if req.symbol:
            # Exit specific position
            result = broker.exit_position(req.symbol)
            return {
                "status": "SUCCESS" if result.get("s") == "ok" else "ERROR",
                "message": result.get("message", "Exit order placed"),
                "symbol": req.symbol,
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
        raise HTTPException(status_code=500, detail=f"Error exiting position: {str(e)}")


# ============== Funds Endpoints ==============


@router.get("/funds", response_model=FundsResponse)
def get_funds():
    """Get available funds/margin from broker"""
    try:
        from ..brokers.plugins.fyers import FyersBroker

        broker = FyersBroker()
        funds = broker.get_funds()

        return FundsResponse(available=funds.available, used=funds.used, total=funds.total)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching funds: {str(e)}")


# ============== Tradebook Endpoints ==============


@router.get("/tradebook", response_model=list[dict[str, Any]])
def get_tradebook():
    """Get executed trades from broker"""
    try:
        from ..brokers.plugins.fyers import FyersBroker

        broker = FyersBroker()
        trades = broker.get_tradebook()
        return trades
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching tradebook: {str(e)}")


# ============== Risk Management Endpoints ==============


@router.post("/risk-check", response_model=RiskCheckResponse)
def check_risk(req: RiskCheckRequest, db: Session = Depends(get_db)):
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
        raise HTTPException(status_code=500, detail=f"Risk check error: {str(e)}")


@router.get("/risk/exposure", response_model=ExposureSummaryResponse)
def get_exposure_summary():
    """Get current exposure and risk summary"""
    try:
        summary = risk_manager.get_exposure_summary()
        return ExposureSummaryResponse(**summary)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching exposure: {str(e)}")


@router.post("/risk/large-order-check")
def check_large_order(order: OrderRequest, db: Session = Depends(get_db)):
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
        raise HTTPException(status_code=500, detail=f"Large order check error: {str(e)}")

import logging
import uuid
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import yfinance as yf
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_
from sqlalchemy.orm import Session

from ..data_repository import DataRepository
from ..database import SessionLocal, get_db
from ..models import Company, HistoricalPrice, LiveOrder
from ..services.fyers_client import get_fyers_client
from ..services.option_chain_service import option_chain_service
from ..services.order_execution_service import order_execution_service
from ..services.risk_manager import risk_manager
from ..services.symbol_master import symbol_master

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/terminal", tags=["Terminal"])

INTRADAY_TIMEFRAME_MAP = {
    "1m": 1,
    "5m": 5,
    "15m": 15,
    "30m": 30,
    "1h": 60,
}
INDEX_YF_MAP = {
    "NIFTY50": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
}

class PaperOrderRequest(BaseModel):
    symbol: str
    side: str
    quantity: int
    order_type: str = "MARKET"
    product_type: str = "INTRADAY"
    price: float = 0.0
    trigger_price: float = 0.0
    tag: str | None = "terminal-paper"


class TerminalOptionsPreviewRequest(BaseModel):
    symbol: str | None = None
    underlying: str | None = None
    expiry: str | None = None  # YYYY-MM-DD
    strike: float | None = None
    option_type: str | None = None  # CE/PE
    side: str
    quantity: int
    order_type: str = "MARKET"
    product: str = "INTRADAY"
    price: float = 0.0
    trigger_price: float = 0.0
    mode: str = "PAPER"


class TerminalOptionsOrderRequest(TerminalOptionsPreviewRequest):
    tag: str | None = "terminal-options"
    is_live_confirmation_ack: bool = False
    risk_override_reason: str | None = None


def _prepare_candle_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize candle frame with EMA20/EMA50 columns."""
    if df.empty:
        return df

    required_cols = {"open", "high", "low", "close", "volume"}
    if not required_cols.issubset(set(df.columns.str.lower())):
        renamed = {col: col.lower() for col in df.columns}
        df = df.rename(columns=renamed)

    df["close"] = pd.to_numeric(df["close"], errors="coerce").fillna(0.0)
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["ema50"] = df["close"].ewm(span=50, adjust=False).mean()
    return df


def _load_index_yfinance_candles(symbol: str, timeframe: str, limit: int) -> pd.DataFrame:
    yf_symbol = INDEX_YF_MAP.get(symbol)
    if not yf_symbol:
        return pd.DataFrame()

    tf = timeframe.strip().lower()
    interval = "1d"
    period = "1y"
    if tf == "1m":
        interval, period = "1m", "7d"
    elif tf == "5m":
        interval, period = "5m", "60d"
    elif tf == "15m":
        interval, period = "15m", "60d"
    elif tf == "30m":
        interval, period = "30m", "60d"
    elif tf == "1h":
        interval, period = "60m", "730d"
    elif tf == "w":
        interval, period = "1wk", "5y"
    elif tf == "m":
        interval, period = "1mo", "10y"

    try:
        data = yf.download(
            yf_symbol,
            period=period,
            interval=interval,
            progress=False,
            auto_adjust=False,
        )
        if data is None or data.empty:
            return pd.DataFrame()

        # yfinance can return MultiIndex columns (field, ticker) even for one symbol.
        if isinstance(data.columns, pd.MultiIndex):
            data.columns = data.columns.get_level_values(0)

        data = data.reset_index()
        time_col = "Datetime" if "Datetime" in data.columns else "Date"
        candles = pd.DataFrame(
            {
                "ts": pd.to_datetime(data[time_col]),
                "open": pd.to_numeric(data["Open"], errors="coerce").fillna(0.0),
                "high": pd.to_numeric(data["High"], errors="coerce").fillna(0.0),
                "low": pd.to_numeric(data["Low"], errors="coerce").fillna(0.0),
                "close": pd.to_numeric(data["Close"], errors="coerce").fillna(0.0),
                "volume": (
                    pd.to_numeric(data.get("Volume", 0), errors="coerce")
                    .fillna(0.0)
                    .astype(int)
                ),
            }
        )
        return candles.tail(limit).reset_index(drop=True)
    except Exception as exc:
        logger.warning("Index yfinance fallback failed for %s: %s", symbol, exc)
        return pd.DataFrame()


def _build_option_symbols(
    symbol: str | None,
    underlying: str | None,
    expiry: str | None,
    strike: float | None,
    option_type: str | None,
) -> tuple[str, str]:
    if symbol:
        db_symbol = symbol_master.to_db(symbol)
        return db_symbol, symbol_master.to_fyers(db_symbol)

    if not (underlying and expiry and strike and option_type):
        raise ValueError("Provide symbol, or (underlying + expiry + strike + option_type)")

    expiry_date = date.fromisoformat(expiry)
    # Prefer symbol resolution from live chain to avoid format mismatches.
    chain = option_chain_service.get_option_chain(
        underlying=underlying.upper(),
        expiry=expiry_date,
        strike_count=100,
    )
    if chain:
        strike_row = chain.get_strike(float(strike))
        if strike_row:
            leg = strike_row.call if option_type.upper() == "CE" else strike_row.put
            if leg and leg.fyers_symbol:
                db_symbol = symbol_master.to_db(leg.fyers_symbol)
                return db_symbol, leg.fyers_symbol

    fyers_symbol = symbol_master.to_fyers_option(
        underlying=underlying.upper(),
        expiry=expiry_date,
        strike=float(strike),
        opt_type=option_type.upper(),
    )
    db_symbol = symbol_master.to_db(fyers_symbol)
    return db_symbol, fyers_symbol


def _quote_ltp(fyers_symbol: str) -> float:
    try:
        client = get_fyers_client()
        if not client or not client.fyers:
            return 0.0
        response = client.fyers.quotes({"symbols": fyers_symbol})
        if response.get("s") != "ok":
            return 0.0
        rows = response.get("d", [])
        if not rows:
            return 0.0
        return float(rows[0].get("v", {}).get("lp", 0) or 0)
    except Exception:
        return 0.0


@router.get("/chart")
def get_chart_data(
    symbol: str = Query(..., min_length=1),
    timeframe: str = Query("D", min_length=1),
    limit: int = Query(200, ge=20, le=1000),
) -> dict[str, Any]:
    """Return OHLCV + EMA20/EMA50 for terminal chart."""
    db = None
    try:
        db = SessionLocal()
        repo = DataRepository(db)

        db_symbol = symbol_master.to_db(symbol)
        company = (
            db.query(Company)
            .filter(and_(Company.symbol == db_symbol, Company.is_active.is_(True)))
            .first()
        )

        normalized_tf = timeframe.strip().lower()
        candles_df = pd.DataFrame()
        source = "historical_prices"

        if normalized_tf in INTRADAY_TIMEFRAME_MAP:
            tf_minutes = INTRADAY_TIMEFRAME_MAP[normalized_tf]
            candles_df = repo.get_intraday_candles(db_symbol, timeframe=tf_minutes, days=15)
            source = "intraday_candles"

            if not candles_df.empty:
                candles_df = candles_df.rename(columns={"timestamp": "ts"})
                candles_df = candles_df.sort_values("ts")
                candles_df = candles_df.tail(limit).reset_index(drop=True)
            else:
                # Fallback to daily data if intraday candles not available
                normalized_tf = "d"

        if normalized_tf in {"d", "w", "m"} and company:
            rows = (
                db.query(HistoricalPrice)
                .filter(HistoricalPrice.company_id == company.id)
                .order_by(HistoricalPrice.date.asc())
                .all()
            )

            if not rows:
                return {
                    "symbol": db_symbol,
                    "timeframe": timeframe,
                    "source": source,
                    "candles": [],
                }

            daily_df = pd.DataFrame(
                {
                    "ts": [datetime.combine(row.date, datetime.min.time()) for row in rows],
                    "open": [float(row.open or 0) for row in rows],
                    "high": [float(row.high or 0) for row in rows],
                    "low": [float(row.low or 0) for row in rows],
                    "close": [float(row.close or 0) for row in rows],
                    "volume": [int(row.volume or 0) for row in rows],
                    "ema20": [float(row.ema_20 or 0) for row in rows],
                    "ema50": [float(row.ema_50 or 0) for row in rows],
                }
            )

            if normalized_tf in {"w", "m"}:
                resample_rule = "W" if normalized_tf == "w" else "ME"
                daily_df = daily_df.set_index("ts")
                candles_df = (
                    daily_df.resample(resample_rule)
                    .agg(
                        {
                            "open": "first",
                            "high": "max",
                            "low": "min",
                            "close": "last",
                            "volume": "sum",
                        }
                    )
                    .dropna(subset=["open", "high", "low", "close"])
                    .reset_index()
                )
                source = "historical_prices_resampled"
            else:
                candles_df = daily_df.copy()
                source = "historical_prices"

            candles_df = candles_df.tail(limit).reset_index(drop=True)

        if candles_df.empty and not company:
            candles_df = _load_index_yfinance_candles(db_symbol, normalized_tf, limit)
            source = "yfinance_index_fallback"

        if candles_df.empty:
            return {
                "symbol": db_symbol,
                "timeframe": timeframe,
                "source": source,
                "candles": [],
            }

        candles_df = _prepare_candle_frame(candles_df)

        candles = [
            {
                "ts": row.ts.isoformat() if hasattr(row.ts, "isoformat") else str(row.ts),
                "open": round(float(row.open), 2),
                "high": round(float(row.high), 2),
                "low": round(float(row.low), 2),
                "close": round(float(row.close), 2),
                "volume": int(row.volume),
                "ema20": round(float(row.ema20), 2),
                "ema50": round(float(row.ema50), 2),
            }
            for row in candles_df.itertuples(index=False)
        ]

        return {
            "symbol": db_symbol,
            "timeframe": timeframe,
            "source": source,
            "candles": candles,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Terminal chart fetch failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load chart data") from exc
    finally:
        if db:
            db.close()


@router.get("/options/board")
def get_options_board(
    underlying: str = Query("NIFTY", min_length=2),
    expiry: date | None = Query(None),
    strike_count: int = Query(15, ge=5, le=50),
) -> dict[str, Any]:
    """Options board snapshot for terminal options-first view."""
    try:
        chain = option_chain_service.get_option_chain(
            underlying=underlying.upper(),
            expiry=expiry,
            strike_count=strike_count,
        )
        if not chain:
            raise HTTPException(status_code=404, detail="Option board unavailable for selected underlying/expiry")

        rows: list[dict[str, Any]] = []
        for strike in chain.strikes:
            ce = strike.call
            pe = strike.put
            rows.append(
                {
                    "strike": strike.strike_price,
                    "ce": {
                        "symbol": ce.symbol if ce else None,
                        "fyers_symbol": ce.fyers_symbol if ce else None,
                        "ltp": ce.ltp if ce else None,
                        "oi": ce.oi if ce else None,
                        "volume": ce.volume if ce else None,
                        "iv": ce.iv if ce else None,
                        "change_pct": ce.change_pct if ce else None,
                    } if ce else None,
                    "pe": {
                        "symbol": pe.symbol if pe else None,
                        "fyers_symbol": pe.fyers_symbol if pe else None,
                        "ltp": pe.ltp if pe else None,
                        "oi": pe.oi if pe else None,
                        "volume": pe.volume if pe else None,
                        "iv": pe.iv if pe else None,
                        "change_pct": pe.change_pct if pe else None,
                    } if pe else None,
                }
            )

        return {
            "underlying": chain.underlying,
            "spot_price": chain.spot_price,
            "expiry": chain.expiry.isoformat(),
            "atm_strike": chain.get_atm_strike(),
            "strikes": rows,
            "timestamp": chain.timestamp.isoformat(),
            "stale_after_sec": 3,
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Terminal options board failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load options board") from exc


@router.get("/options/depth")
def get_options_depth(symbol: str = Query(..., min_length=2)) -> dict[str, Any]:
    """Depth snapshot for selected option contract or underlying."""
    try:
        db_symbol = symbol_master.to_db(symbol)
        fyers_symbol = symbol if ":" in symbol else symbol_master.to_fyers(db_symbol)

        client = get_fyers_client()
        if not client or not client.fyers:
            raise HTTPException(status_code=503, detail="Fyers client unavailable")

        response = client.fyers.depth({"symbol": fyers_symbol})
        if response.get("s") != "ok":
            return {
                "symbol": db_symbol,
                "fyers_symbol": fyers_symbol,
                "depth": None,
                "error": response.get("message", "Depth unavailable"),
            }

        return {
            "symbol": db_symbol,
            "fyers_symbol": fyers_symbol,
            "depth": response.get("d", response),
            "timestamp": datetime.now(UTC).isoformat(),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Terminal options depth failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load options depth") from exc


@router.get("/options/orderflow")
def get_options_orderflow(
    underlying: str = Query("NIFTY", min_length=2),
    expiry: date | None = Query(None),
    strike_count: int = Query(15, ge=5, le=50),
) -> dict[str, Any]:
    """Derived orderflow metrics from option chain snapshot (Phase-1)."""
    try:
        chain = option_chain_service.get_option_chain(
            underlying=underlying.upper(),
            expiry=expiry,
            strike_count=strike_count,
        )
        if not chain:
            raise HTTPException(status_code=404, detail="Orderflow unavailable for selected underlying/expiry")

        ce_oi = 0
        pe_oi = 0
        ce_volume = 0
        pe_volume = 0
        for strike in chain.strikes:
            if strike.call:
                ce_oi += int(strike.call.oi or 0)
                ce_volume += int(strike.call.volume or 0)
            if strike.put:
                pe_oi += int(strike.put.oi or 0)
                pe_volume += int(strike.put.volume or 0)

        pcr_oi = round(pe_oi / ce_oi, 4) if ce_oi > 0 else None
        pcr_volume = round(pe_volume / ce_volume, 4) if ce_volume > 0 else None

        return {
            "underlying": chain.underlying,
            "expiry": chain.expiry.isoformat(),
            "spot_price": chain.spot_price,
            "pcr_oi": pcr_oi,
            "pcr_volume": pcr_volume,
            "ce_oi": ce_oi,
            "pe_oi": pe_oi,
            "ce_volume": ce_volume,
            "pe_volume": pe_volume,
            "timestamp": chain.timestamp.isoformat(),
            "definition": "Derived from option chain OI/volume; not true tape analytics",
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Terminal options orderflow failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to load options orderflow") from exc


@router.post("/options/preview-order")
def preview_options_order(
    req: TerminalOptionsPreviewRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Validate and preview options order without placement."""
    try:
        db_symbol, fyers_symbol = _build_option_symbols(
            req.symbol, req.underlying, req.expiry, req.strike, req.option_type
        )
        ltp = _quote_ltp(fyers_symbol)
        side = req.side.upper()
        quantity = int(req.quantity)
        order_type = req.order_type.upper()
        mode = req.mode.upper()
        price = float(req.price or 0)

        reference_price = ltp if ltp > 0 else price
        effective_price = reference_price if order_type in {"MARKET", "SL-M"} else price
        notional = round(max(0.0, effective_price) * max(0, quantity), 2)
        estimated_charges = round(notional * 0.00035, 2)

        risk = None
        if mode == "LIVE":
            risk_result = risk_manager.pre_trade_check(
                {
                    "symbol": db_symbol,
                    "side": side,
                    "quantity": quantity,
                    "product": req.product.upper(),
                    "type": order_type,
                    "price": price,
                    "instrument_type": req.option_type.upper() if req.option_type else "CE",
                },
                db,
            )
            risk = {
                "status": risk_result.status.value,
                "code": risk_result.code,
                "message": risk_result.message,
                "details": risk_result.details,
            }

        return {
            "symbol": db_symbol,
            "fyers_symbol": fyers_symbol,
            "mode": mode,
            "side": side,
            "order_type": order_type,
            "quantity": quantity,
            "ltp": ltp,
            "reference_price": reference_price,
            "estimated_notional": notional,
            "estimated_charges": estimated_charges,
            "risk": risk,
        }
    except Exception as exc:
        logger.error("Terminal options preview failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/options/order")
def place_options_order(
    req: TerminalOptionsOrderRequest,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Terminal options order alias on top of unified trading execution service."""
    try:
        db_symbol, _fyers_symbol = _build_option_symbols(
            req.symbol, req.underlying, req.expiry, req.strike, req.option_type
        )

        mode = req.mode.upper()
        payload = {
            "symbol": db_symbol,
            "side": req.side.upper(),
            "quantity": int(req.quantity),
            "product": req.product.upper(),
            "type": req.order_type.upper(),
            "price": float(req.price or 0),
            "trigger_price": float(req.trigger_price or 0),
            "tag": req.tag or "terminal-options",
            "instrument_type": (req.option_type or "CE").upper(),
            "strike_price": req.strike,
            "expiry_date": req.expiry,
            "option_type": (req.option_type or "CE").upper(),
            "source": "TERMINAL_OPTIONS",
            "user_id": "terminal_user",
            "is_live_confirmation_ack": bool(req.is_live_confirmation_ack),
            "risk_override_reason": req.risk_override_reason,
        }

        result = order_execution_service.place_order(payload, db, mode=mode)
        if result.get("status") == "ERROR":
            raise HTTPException(status_code=400, detail=result.get("message", "Order failed"))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Terminal options order failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to place options order") from exc


@router.post("/paper/order")
def place_paper_order(order: PaperOrderRequest) -> dict[str, Any]:
    """
    Place a paper order without touching broker execution path.
    Used by Terminal paper mode to guarantee strict isolation from live broker APIs.
    Deprecated: migrate to unified `/api/trading/order` with mode=`PAPER`.
    """
    db = None
    try:
        db = SessionLocal()

        if order.quantity <= 0:
            raise HTTPException(status_code=400, detail="Quantity must be positive")

        db_symbol = symbol_master.to_db(order.symbol)
        fyers_symbol = symbol_master.to_fyers(db_symbol)
        internal_id = f"PAPER-{uuid.uuid4().hex[:10].upper()}"

        paper_order = LiveOrder(
            id=internal_id,
            internal_id=internal_id,
            user_id="terminal_user",
            symbol=db_symbol,
            fyers_symbol=fyers_symbol,
            side=order.side.upper(),
            quantity=order.quantity,
            order_type=order.order_type.upper(),
            product_type=order.product_type.upper(),
            price=float(order.price),
            trigger_price=float(order.trigger_price),
            status="SUBMITTED",
            broker_message="Paper order placed (broker path bypassed)",
            instrument_type="EQ",
            order_tag=order.tag,
            source="TERMINAL",
            is_paper=1,
        )

        db.add(paper_order)
        db.commit()

        return {
            "status": "SUBMITTED",
            "order_id": internal_id,
            "mode": "PAPER",
            "message": "Paper order placed successfully",
        }
    except HTTPException:
        raise
    except Exception as exc:
        if db:
            db.rollback()
        logger.error("Paper order placement failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to place paper order") from exc
    finally:
        if db:
            db.close()

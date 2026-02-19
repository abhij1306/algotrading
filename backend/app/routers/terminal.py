import logging
import uuid
from datetime import datetime
from typing import Any

import pandas as pd
import yfinance as yf
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_

from ..data_repository import DataRepository
from ..database import SessionLocal
from ..models import Company, HistoricalPrice, LiveOrder
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


@router.post("/paper/order")
def place_paper_order(order: PaperOrderRequest) -> dict[str, Any]:
    """
    Place a paper order without touching broker execution path.
    Used by Terminal paper mode to guarantee strict isolation from live broker APIs.
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

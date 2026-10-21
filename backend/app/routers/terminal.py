import logging
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any

import pandas as pd
import yfinance as yf
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_
from sqlalchemy.orm import Session

from ..data_repository import DataRepository
from ..database import get_db
from ..models import Company, HistoricalPrice, LiveOrder
from ..services.fyers_client import get_fyers_client
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


def _empty_chart_response(symbol: str, timeframe: str, source: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "timeframe": timeframe,
        "source": source,
        "candles": [],
    }


def _build_company_daily_frame(rows: list[HistoricalPrice]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "ts": [datetime.combine(row.date, datetime.min.time(), tzinfo=UTC) for row in rows],
            "open": [float(row.open or 0) for row in rows],
            "high": [float(row.high or 0) for row in rows],
            "low": [float(row.low or 0) for row in rows],
            "close": [float(row.close or 0) for row in rows],
            "volume": [int(row.volume or 0) for row in rows],
            "ema20": [float(row.ema_20 or 0) for row in rows],
            "ema50": [float(row.ema_50 or 0) for row in rows],
        }
    )


def _load_intraday_chart_frame(
    repo: DataRepository,
    db_symbol: str,
    normalized_tf: str,
    limit: int,
) -> tuple[pd.DataFrame, str, str]:
    if normalized_tf not in INTRADAY_TIMEFRAME_MAP:
        return pd.DataFrame(), "historical_prices", normalized_tf

    tf_minutes = INTRADAY_TIMEFRAME_MAP[normalized_tf]
    candles_df = repo.get_intraday_candles(db_symbol, timeframe=tf_minutes, days=15)
    if candles_df.empty:
        return pd.DataFrame(), "intraday_candles", "d"

    candles_df = candles_df.rename(columns={"timestamp": "ts"})
    candles_df = candles_df.sort_values("ts")
    candles_df = candles_df.tail(limit).reset_index(drop=True)
    return candles_df, "intraday_candles", normalized_tf


def _load_company_chart_frame(
    db: Session,
    company: Company | None,
    normalized_tf: str,
    limit: int,
) -> tuple[pd.DataFrame, str]:
    if normalized_tf not in {"d", "w", "m"} or not company:
        return pd.DataFrame(), "historical_prices"

    rows = (
        db.query(HistoricalPrice)
        .filter(HistoricalPrice.company_id == company.id)
        .order_by(HistoricalPrice.date.asc())
        .all()
    )
    if not rows:
        return pd.DataFrame(), "historical_prices"

    daily_df = _build_company_daily_frame(rows)
    if normalized_tf not in {"w", "m"}:
        return daily_df.tail(limit).reset_index(drop=True), "historical_prices"

    resample_rule = "W" if normalized_tf == "w" else "ME"
    candles_df = (
        daily_df.set_index("ts")
        .resample(resample_rule)
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
    return candles_df.tail(limit).reset_index(drop=True), "historical_prices_resampled"


def _load_chart_dataset(
    db: Session,
    repo: DataRepository,
    db_symbol: str,
    timeframe: str,
    limit: int,
) -> tuple[pd.DataFrame, str]:
    company = (
        db.query(Company)
        .filter(and_(Company.symbol == db_symbol, Company.is_active.is_(True)))
        .first()
    )
    normalized_tf = timeframe.strip().lower()
    candles_df, source, normalized_tf = _load_intraday_chart_frame(
        repo, db_symbol, normalized_tf, limit
    )
    if candles_df.empty:
        candles_df, source = _load_company_chart_frame(db, company, normalized_tf, limit)
    if candles_df.empty and not company:
        candles_df = _load_index_yfinance_candles(db_symbol, normalized_tf, limit)
        source = "yfinance_index_fallback"
    return candles_df, source


def _serialize_chart_candles(candles_df: pd.DataFrame) -> list[dict[str, Any]]:
    return [
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
                    pd.to_numeric(data.get("Volume", 0), errors="coerce").fillna(0.0).astype(int)
                ),
            }
        )
        return candles.tail(limit).reset_index(drop=True)
    except Exception as exc:
        logger.warning("Index yfinance fallback failed for %s: %s", symbol, exc)
        return pd.DataFrame()


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


@router.get("/chart", responses={500: {"description": "Internal server error"}})
def get_chart_data(
    symbol: Annotated[str, Query(..., min_length=1)],
    db: Annotated[Session, Depends(get_db)],
    timeframe: Annotated[str, Query(min_length=1)] = "D",
    limit: Annotated[int, Query(ge=20, le=1000)] = 200,
) -> dict[str, Any]:
    """Return OHLCV + EMA20/EMA50 for terminal chart."""
    try:
        repo = DataRepository(db)

        db_symbol = symbol_master.to_db(symbol)
        candles_df, source = _load_chart_dataset(db, repo, db_symbol, timeframe, limit)

        if candles_df.empty:
            return _empty_chart_response(db_symbol, timeframe, source)

        candles_df = _prepare_candle_frame(candles_df)

        return {
            "symbol": db_symbol,
            "timeframe": timeframe,
            "source": source,
            "candles": _serialize_chart_candles(candles_df),
        }
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Terminal chart fetch failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to load chart data") from exc


@router.post(
    "/paper/order",
    responses={
        400: {"description": "Invalid request"},
        500: {"description": "Internal server error"},
    },
)
def place_paper_order(
    order: PaperOrderRequest, db: Annotated[Session, Depends(get_db)]
) -> dict[str, Any]:
    """
    Place a paper order without touching broker execution path.
    Used by Terminal paper mode to guarantee strict isolation from live broker APIs.
    Deprecated: migrate to unified `/api/trading/order` with mode=`PAPER`.
    """
    try:
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
        db.rollback()
        logger.exception("Paper order placement failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to place paper order") from exc

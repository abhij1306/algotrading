"""
Options Router
==============
API endpoints for option chain data and Greeks.
"""

import logging
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..services.option_chain_service import OptionChainData, OptionLeg, option_chain_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/options", tags=["Options"])


# ============== Pydantic Models ==============


class OptionLegResponse(BaseModel):
    """Response model for option leg (Call or Put)"""

    symbol: str
    fyers_symbol: str
    strike: float
    expiry: date
    option_type: str
    ltp: float
    bid: float
    ask: float
    volume: int
    oi: int
    iv: float
    greeks: dict = Field(default_factory=dict)
    change: float
    change_pct: float
    prev_close: float


class OptionStrikeResponse(BaseModel):
    """Response model for a strike price with both legs"""

    strike_price: float
    ce: OptionLegResponse | None = None
    pe: OptionLegResponse | None = None


class OptionChainResponse(BaseModel):
    """Response model for complete option chain"""

    underlying: str
    spot_price: float
    expiry: date
    strikes: list[OptionStrikeResponse]
    atm_strike: float
    timestamp: str


class ExpiriesResponse(BaseModel):
    """Response model for expiry dates"""

    underlying: str
    expiries: list[date]


class ATMStrikeResponse(BaseModel):
    """Response model for ATM strike"""

    underlying: str
    spot_price: float
    atm_strike: float


class GreeksResponse(BaseModel):
    """Response model for option Greeks"""

    underlying: str
    strike: float
    option_type: str
    expiry: date
    spot_price: float
    greeks: dict


# ============== Endpoints ==============


def _to_leg_response(leg: OptionLeg | None) -> OptionLegResponse | None:
    if not leg:
        return None

    return OptionLegResponse(
        symbol=leg.symbol,
        fyers_symbol=leg.fyers_symbol,
        strike=leg.strike,
        expiry=leg.expiry,
        option_type=leg.option_type,
        ltp=leg.ltp,
        bid=leg.bid,
        ask=leg.ask,
        volume=leg.volume,
        oi=leg.oi,
        iv=leg.iv,
        greeks=leg.greeks,
        change=leg.change,
        change_pct=leg.change_pct,
        prev_close=leg.prev_close,
    )


def _to_chain_response(chain: OptionChainData) -> OptionChainResponse:
    strikes_response = [
        OptionStrikeResponse(
            strike_price=strike.strike_price,
            ce=_to_leg_response(strike.call),
            pe=_to_leg_response(strike.put),
        )
        for strike in chain.strikes
    ]
    return OptionChainResponse(
        underlying=chain.underlying,
        spot_price=chain.spot_price,
        expiry=chain.expiry,
        strikes=strikes_response,
        atm_strike=chain.get_atm_strike(),
        timestamp=chain.timestamp.isoformat(),
    )


@router.get(
    "/chain",
    responses={
        404: {"description": "Resource not found"},
        500: {"description": "Internal server error"},
    },
)
async def get_option_chain(
    symbol: Annotated[
        str,
        Query(..., description="Underlying symbol (e.g., NIFTY, BANKNIFTY, RELIANCE)"),
    ],
    expiry: Annotated[
        date | None,
        Query(None, description="Expiry date (YYYY-MM-DD). Defaults to nearest expiry."),
    ],
    strike_count: Annotated[
        int,
        Query(15, ge=5, le=50, description="Number of strikes to return (centered around ATM)"),
    ],
) -> OptionChainResponse:
    """
    Get option chain data for an underlying.

    Returns:
        Option chain with spot price, strikes, and option data (LTP, OI, IV, Greeks)
    """
    try:
        chain = option_chain_service.get_option_chain(
            underlying=symbol.upper(), expiry=expiry, strike_count=strike_count
        )

        if not chain:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Option chain not available for {symbol}. "
                    "Market may be closed or symbol invalid."
                ),
            )

        return _to_chain_response(chain)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching option chain for %s", symbol)
        raise HTTPException(status_code=500, detail=f"Error fetching option chain: {e}") from e


@router.get(
    "/expiries",
    responses={
        404: {"description": "Resource not found"},
        500: {"description": "Internal server error"},
    },
)
async def get_expiries(
    symbol: Annotated[
        str,
        Query(..., description="Underlying symbol (e.g., NIFTY, BANKNIFTY)"),
    ],
) -> ExpiriesResponse:
    """
    Get available expiry dates for an underlying.

    Returns:
        List of available expiry dates in ascending order
    """
    try:
        expiries = option_chain_service.get_expiries(symbol.upper())

        if not expiries:
            raise HTTPException(status_code=404, detail=f"No expiries found for {symbol}")

        return ExpiriesResponse(underlying=symbol.upper(), expiries=expiries)

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching expiries for %s", symbol)
        raise HTTPException(status_code=500, detail=f"Error fetching expiries: {e}") from e


@router.get(
    "/atm",
    responses={
        404: {"description": "Resource not found"},
        500: {"description": "Internal server error"},
    },
)
async def get_atm_strike(
    symbol: Annotated[
        str,
        Query(..., description="Underlying symbol (e.g., NIFTY, BANKNIFTY)"),
    ],
    expiry: Annotated[
        date | None,
        Query(None, description="Expiry date (YYYY-MM-DD). Defaults to nearest expiry."),
    ],
) -> ATMStrikeResponse:
    """
    Get the at-the-money (ATM) strike for an underlying.

    Returns:
        ATM strike price and current spot price
    """
    try:
        chain = option_chain_service.get_option_chain(
            underlying=symbol.upper(), expiry=expiry, strike_count=5
        )

        if not chain:
            raise HTTPException(
                status_code=404, detail=f"Could not determine ATM strike for {symbol}"
            )

        return ATMStrikeResponse(
            underlying=chain.underlying,
            spot_price=chain.spot_price,
            atm_strike=chain.get_atm_strike(),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching ATM strike for %s", symbol)
        raise HTTPException(status_code=500, detail=f"Error fetching ATM strike: {e}") from e


@router.get(
    "/greeks",
    responses={
        404: {"description": "Resource not found"},
        500: {"description": "Internal server error"},
    },
)
async def get_greeks(
    symbol: Annotated[str, Query(..., description="Underlying symbol (e.g., NIFTY)")],
    strike: Annotated[float, Query(..., description="Strike price")],
    option_type: Annotated[
        str,
        Query(..., pattern="^(CE|PE)$", description="Option type: CE or PE"),
    ],
    expiry: Annotated[
        date | None,
        Query(None, description="Expiry date (YYYY-MM-DD). Defaults to nearest expiry."),
    ],
) -> GreeksResponse:
    """
    Get Greeks (Delta, Gamma, Theta, Vega, Rho) for a specific option.

    Returns:
        Greeks calculated using Black-Scholes model
    """
    try:
        greeks = option_chain_service.get_greeks(
            underlying=symbol.upper(), strike=strike, option_type=option_type.upper(), expiry=expiry
        )

        if not greeks:
            raise HTTPException(
                status_code=404,
                detail=f"Could not calculate Greeks for {symbol} {strike} {option_type}",
            )

        # Get spot price from chain
        chain = option_chain_service.get_option_chain(symbol.upper(), expiry, strike_count=5)
        spot_price = chain.spot_price if chain else 0

        return GreeksResponse(
            underlying=symbol.upper(),
            strike=strike,
            option_type=option_type.upper(),
            expiry=expiry or (chain.expiry if chain else date.today()),
            spot_price=spot_price,
            greeks=greeks,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error fetching Greeks for %s %s %s", symbol, strike, option_type)
        raise HTTPException(status_code=500, detail=f"Error fetching Greeks: {e}") from e


@router.post("/cache/clear")
async def clear_cache() -> dict[str, str]:
    """
    Clear the option chain cache.
    Use this if you suspect stale data.
    """
    option_chain_service.clear_cache()
    return {"status": "success", "message": "Option chain cache cleared"}

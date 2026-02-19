"""
Options Router
==============
API endpoints for option chain data and Greeks.
"""

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..services.option_chain_service import option_chain_service

router = APIRouter(
    prefix="/api/options",
    tags=["Options"]
)


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

@router.get("/chain", response_model=OptionChainResponse)
async def get_option_chain(
    symbol: str = Query(..., description="Underlying symbol (e.g., NIFTY, BANKNIFTY, RELIANCE)"),
    expiry: date | None = Query(None, description="Expiry date (YYYY-MM-DD). Defaults to nearest expiry."),
    strike_count: int = Query(15, ge=5, le=50, description="Number of strikes to return (centered around ATM)"),
    db: Session = Depends(get_db)
):
    """
    Get option chain data for an underlying.

    Returns:
        Option chain with spot price, strikes, and option data (LTP, OI, IV, Greeks)
    """
    try:
        chain = option_chain_service.get_option_chain(
            underlying=symbol.upper(),
            expiry=expiry,
            strike_count=strike_count
        )

        if not chain:
            raise HTTPException(
                status_code=404,
                detail=f"Option chain not available for {symbol}. Market may be closed or symbol invalid."
            )

        # Convert to response model
        strikes_response = []
        for strike in chain.strikes:
            ce_response = None
            pe_response = None

            if strike.call:
                ce_response = OptionLegResponse(
                    symbol=strike.call.symbol,
                    fyers_symbol=strike.call.fyers_symbol,
                    strike=strike.call.strike,
                    expiry=strike.call.expiry,
                    option_type=strike.call.option_type,
                    ltp=strike.call.ltp,
                    bid=strike.call.bid,
                    ask=strike.call.ask,
                    volume=strike.call.volume,
                    oi=strike.call.oi,
                    iv=strike.call.iv,
                    greeks=strike.call.greeks,
                    change=strike.call.change,
                    change_pct=strike.call.change_pct,
                    prev_close=strike.call.prev_close
                )

            if strike.put:
                pe_response = OptionLegResponse(
                    symbol=strike.put.symbol,
                    fyers_symbol=strike.put.fyers_symbol,
                    strike=strike.put.strike,
                    expiry=strike.put.expiry,
                    option_type=strike.put.option_type,
                    ltp=strike.put.ltp,
                    bid=strike.put.bid,
                    ask=strike.put.ask,
                    volume=strike.put.volume,
                    oi=strike.put.oi,
                    iv=strike.put.iv,
                    greeks=strike.put.greeks,
                    change=strike.put.change,
                    change_pct=strike.put.change_pct,
                    prev_close=strike.put.prev_close
                )

            strikes_response.append(OptionStrikeResponse(
                strike_price=strike.strike_price,
                ce=ce_response,
                pe=pe_response
            ))

        return OptionChainResponse(
            underlying=chain.underlying,
            spot_price=chain.spot_price,
            expiry=chain.expiry,
            strikes=strikes_response,
            atm_strike=chain.get_atm_strike(),
            timestamp=chain.timestamp.isoformat()
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching option chain: {str(e)}"
        )


@router.get("/expiries", response_model=ExpiriesResponse)
async def get_expiries(
    symbol: str = Query(..., description="Underlying symbol (e.g., NIFTY, BANKNIFTY)"),
    db: Session = Depends(get_db)
):
    """
    Get available expiry dates for an underlying.

    Returns:
        List of available expiry dates in ascending order
    """
    try:
        expiries = option_chain_service.get_expiries(symbol.upper())

        if not expiries:
            raise HTTPException(
                status_code=404,
                detail=f"No expiries found for {symbol}"
            )

        return ExpiriesResponse(
            underlying=symbol.upper(),
            expiries=expiries
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching expiries: {str(e)}"
        )


@router.get("/atm", response_model=ATMStrikeResponse)
async def get_atm_strike(
    symbol: str = Query(..., description="Underlying symbol (e.g., NIFTY, BANKNIFTY)"),
    expiry: date | None = Query(None, description="Expiry date (YYYY-MM-DD)"),
    db: Session = Depends(get_db)
):
    """
    Get the at-the-money (ATM) strike for an underlying.

    Returns:
        ATM strike price and current spot price
    """
    try:
        chain = option_chain_service.get_option_chain(
            underlying=symbol.upper(),
            expiry=expiry,
            strike_count=5
        )

        if not chain:
            raise HTTPException(
                status_code=404,
                detail=f"Could not determine ATM strike for {symbol}"
            )

        return ATMStrikeResponse(
            underlying=chain.underlying,
            spot_price=chain.spot_price,
            atm_strike=chain.get_atm_strike()
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching ATM strike: {str(e)}"
        )


@router.get("/greeks", response_model=GreeksResponse)
async def get_greeks(
    symbol: str = Query(..., description="Underlying symbol (e.g., NIFTY)"),
    strike: float = Query(..., description="Strike price"),
    option_type: str = Query(..., pattern="^(CE|PE)$", description="Option type: CE or PE"),
    expiry: date | None = Query(None, description="Expiry date (YYYY-MM-DD)"),
    db: Session = Depends(get_db)
):
    """
    Get Greeks (Delta, Gamma, Theta, Vega, Rho) for a specific option.

    Returns:
        Greeks calculated using Black-Scholes model
    """
    try:
        greeks = option_chain_service.get_greeks(
            underlying=symbol.upper(),
            strike=strike,
            option_type=option_type.upper(),
            expiry=expiry
        )

        if not greeks:
            raise HTTPException(
                status_code=404,
                detail=f"Could not calculate Greeks for {symbol} {strike} {option_type}"
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
            greeks=greeks
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error fetching Greeks: {str(e)}"
        )


@router.post("/cache/clear")
async def clear_cache(db: Session = Depends(get_db)):
    """
    Clear the option chain cache.
    Use this if you suspect stale data.
    """
    option_chain_service.clear_cache()
    return {"status": "success", "message": "Option chain cache cleared"}

"""
Universe API
============
API endpoints for managing index universes with historical and live modes.
"""

import logging
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..exceptions import InvalidDateError
from ..services.universe import (
    UniverseConstituent,
    UniverseMode,
    get_universe_service,
)
from ..services.universe import custom as custom_universes
from ..utils.dates import parse_iso_date, parse_optional_iso_date
from ..utils.errors import handle_api_error

router = APIRouter()
logger = logging.getLogger(__name__)
DB_DEPENDENCY = Annotated[Session, Depends(get_db)]
BAD_REQUEST_RESPONSE = {400: {"description": "Invalid universe input"}}
NOT_IMPLEMENTED_RESPONSE = {501: {"description": "Historical universe mode not implemented"}}
NOT_FOUND_RESPONSE = {404: {"description": "Universe not found"}}


# Request/Response Models
class UniverseConstituentResponse(BaseModel):
    """Response model for a universe constituent"""

    symbol: str
    company_name: str
    weight: float | None = None
    isin: str | None = None
    sector: str | None = None
    industry: str | None = None


class UniverseResponse(BaseModel):
    """Response model for universe lookup"""

    index_code: str
    lookup_date: str
    constituents: list[UniverseConstituentResponse]
    source: str
    is_historical: bool
    count: int


class UniverseListItem(BaseModel):
    """Item in universe list"""

    index_code: str
    name: str
    description: str


class UniverseChange(BaseModel):
    """Universe change record"""

    date: str
    symbol: str
    change_type: str  # "addition", "removal", "weight_change"
    old_weight: float | None = None
    new_weight: float | None = None


class UniverseChangesResponse(BaseModel):
    """Response for universe changes"""

    index_code: str
    start_date: str
    end_date: str
    additions: list[UniverseChange]
    removals: list[UniverseChange]
    weight_changes: list[UniverseChange]


def constituent_to_response(c: UniverseConstituent) -> UniverseConstituentResponse:
    """Convert UniverseConstituent to response model"""
    return UniverseConstituentResponse(
        symbol=c.symbol,
        company_name=c.company_name,
        weight=c.weight,
        isin=c.isin,
        sector=c.sector,
        industry=c.industry,
    )


def _parse_universe_mode(mode: str) -> UniverseMode:
    normalized_mode = (mode or "").strip().lower()
    if normalized_mode == "historical":
        return UniverseMode.HISTORICAL
    if normalized_mode == "live":
        return UniverseMode.LIVE
    raise ValueError(f"Invalid mode: {mode}. Use 'historical' or 'live'.")


@router.get("/list")
def list_universes() -> dict[str, Any]:
    """
    List all available index universes.
    """
    try:
        service = get_universe_service()
        universes = service.list_available_indices()

        return {
            "count": len(universes),
            "universes": [
                UniverseListItem(
                    index_code=u.get("index_code", ""),
                    name=u.get("name", ""),
                    description=u.get("description", ""),
                )
                for u in universes
            ],
        }

    except Exception as e:
        logger.exception(f"Error listing universes: {e}")
        raise handle_api_error(e, "Failed to list universes") from e


@router.get(
    "/constituents/{index_code}",
    responses={
        400: {"description": "Invalid universe input"},
        501: {"description": "Historical universe mode not implemented"},
    },
)
def get_constituents(
    index_code: str,
    target_date: Annotated[str | None, Query(description="Date in YYYY-MM-DD format")] = None,
    mode: Annotated[str, Query(description="Mode: 'historical' or 'live'")] = "live",
) -> UniverseResponse:
    """
    Get constituents for an index on a specific date.

    - **index_code**: Index code (e.g., 'NIFTY50', 'NIFTY200')
    - **target_date**: Date for historical lookup (YYYY-MM-DD format)
    - **mode**: 'historical' for date-based lookup, 'live' for current constituents
    """
    try:
        service = get_universe_service()

        # Parse mode
        universe_mode = _parse_universe_mode(mode)
        target_date_obj = parse_optional_iso_date(target_date, field="target_date")

        # Get constituents
        result = service.get_constituents(
            index_code=index_code.upper(), target_date=target_date_obj, mode=universe_mode
        )

        return UniverseResponse(
            index_code=result.index_code,
            lookup_date=result.lookup_date.isoformat()
            if isinstance(result.lookup_date, date)
            else str(result.lookup_date),
            constituents=[constituent_to_response(c) for c in result.constituents],
            source=result.source,
            is_historical=result.is_historical,
            count=len(result.constituents),
        )

    except (HTTPException, InvalidDateError):
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"Error getting constituents: {e}")
        raise handle_api_error(e, "Failed to get constituents") from e


@router.get(
    "/symbols/{index_code}",
    responses={
        400: {"description": "Invalid universe input"},
        501: {"description": "Historical universe mode not implemented"},
    },
)
def get_symbols(
    index_code: str,
    target_date: Annotated[str | None, Query(description="Date in YYYY-MM-DD format")] = None,
    mode: Annotated[str, Query(description="Mode: 'historical' or 'live'")] = "live",
) -> dict[str, Any]:
    """
    Get just the symbols for an index (no metadata).
    """
    try:
        service = get_universe_service()

        # Parse mode
        universe_mode = _parse_universe_mode(mode)
        target_date_obj = parse_optional_iso_date(target_date, field="target_date")

        # Get symbols
        symbols = service.get_symbols(
            index_code=index_code.upper(), target_date=target_date_obj, mode=universe_mode
        )

        return {
            "index_code": index_code.upper(),
            "count": len(symbols),
            "symbols": symbols,
            "mode": mode,
            "target_date": target_date,
        }

    except (HTTPException, InvalidDateError):
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"Error getting symbols: {e}")
        raise handle_api_error(e, "Failed to get symbols") from e


@router.get(
    "/changes/{index_code}",
    responses={
        400: {"description": "Invalid universe input"},
        501: {"description": "Historical universe mode not implemented"},
    },
)
def get_universe_changes(
    index_code: str,
    start_date: Annotated[str, Query(description="Start date in YYYY-MM-DD format")],
    end_date: Annotated[str, Query(description="End date in YYYY-MM-DD format")],
) -> UniverseChangesResponse:
    """
    Get changes to an index between two dates.

    Returns additions, removals, and weight changes.
    """
    try:
        start = parse_iso_date(start_date, field="start_date")
        end = parse_iso_date(end_date, field="end_date")

        service = get_universe_service()
        changes = service.get_universe_changes(
            index_code=index_code.upper(), start_date=start, end_date=end
        )

        return UniverseChangesResponse(
            index_code=index_code.upper(),
            start_date=start_date,
            end_date=end_date,
            additions=[UniverseChange(**c) for c in changes.get("additions", [])],
            removals=[UniverseChange(**c) for c in changes.get("removals", [])],
            weight_changes=[UniverseChange(**c) for c in changes.get("weight_changes", [])],
        )

    except (HTTPException, InvalidDateError):
        raise
    except NotImplementedError as e:
        raise HTTPException(status_code=501, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"Error getting universe changes: {e}")
        raise handle_api_error(e, "Failed to get universe changes") from e


class CreateCustomUniverseRequest(BaseModel):
    """Request model for creating a custom universe"""

    universe_code: str
    name: str
    symbols: list[str]


@router.post("/custom")
def create_custom_universe(req: CreateCustomUniverseRequest, db: DB_DEPENDENCY) -> dict[str, Any]:
    """
    Create a custom universe with specified symbols.
    """
    try:
        custom_universe = custom_universes.create_custom_universe(
            db=db, universe_code=req.universe_code, universe_name=req.name, symbols=req.symbols
        )
        db.commit()

        return {
            "status": "success",
            "message": f"Custom universe '{custom_universe.universe_code}' created",
            "universe_code": custom_universe.universe_code,
            "name": custom_universe.universe_name,
            "symbol_count": len(req.symbols),
        }

    except custom_universes.DuplicateCustomUniverseError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        logger.exception(f"Error creating custom universe: {e}")
        raise handle_api_error(e, "Failed to create custom universe") from e


@router.get("/custom/{universe_code}", responses=NOT_FOUND_RESPONSE)
def get_custom_universe(universe_code: str, db: DB_DEPENDENCY) -> dict[str, Any]:
    """
    Get a custom universe by code.
    """
    try:
        symbols = custom_universes.get_custom_universe_symbols(db, universe_code)
        if symbols is None:
            raise HTTPException(
                status_code=404, detail=f"Custom universe '{universe_code}' not found"
            )

        return {
            "universe_code": universe_code,
            "count": len(symbols),
            "constituents": symbols,
            "source": "custom_universe",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"Error getting custom universe: {e}")
        raise handle_api_error(e, "Failed to get custom universe") from e

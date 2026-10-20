"""Screener HTTP mapping; the use case lives in `services/screener/service.py`."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from ..constants.indices import DEFAULT_SCREENER_UNIVERSE
from ..database import get_db
from ..services.screener import (
    InvalidSortFieldError,
    QuoteSource,
    ScreenerRequest,
    ScreenerService,
    SqlScreenerRepository,
    TTLCache,
    UniverseDirectory,
    UniversesUnavailableError,
    UnknownUniverseError,
    get_quote_source,
    get_screener_cache,
)
from ..services.universe import get_universe_service

router = APIRouter()

RESULTS_ERROR_RESPONSES = {
    400: {"description": "Invalid screener input"},
    500: {"description": "Failed to load screener results"},
}
INDICES_ERROR_RESPONSES = {500: {"description": "Failed to load screener universes"}}


def get_screener_service(
    db: Annotated[Session, Depends(get_db)],
    cache: Annotated[TTLCache[dict[str, Any]], Depends(get_screener_cache)],
    quotes: Annotated[QuoteSource, Depends(get_quote_source)],
    universes: Annotated[UniverseDirectory, Depends(get_universe_service)],
) -> ScreenerService:
    return ScreenerService(SqlScreenerRepository(db), universes, quotes, cache)


Screener = Annotated[ScreenerService, Depends(get_screener_service)]


@router.get("/indices", responses=INDICES_ERROR_RESPONSES)
def get_indices(response: Response, service: Screener) -> dict[str, Any]:
    """Return available screener universes from the canonical universe service."""
    response.headers["Cache-Control"] = "public, max-age=3600"
    try:
        return service.indices()
    except UniversesUnavailableError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@router.get("/results", responses=RESULTS_ERROR_RESPONSES)
def get_screener_results(
    service: Screener,
    universe: Annotated[str, Query(min_length=1)] = DEFAULT_SCREENER_UNIVERSE,
    query: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    full: Annotated[bool, Query()] = False,
    sort_by: Annotated[str, Query()] = "symbol",
    sort_order: Annotated[str, Query()] = "asc",
) -> dict[str, Any]:
    """
    Return screener rows using DB technicals with initial live quote hydration.
    Canonical Phase-1 endpoint contract.
    """
    request = ScreenerRequest.from_params(universe, query, page, limit, full, sort_by, sort_order)
    try:
        return service.results(request)
    except (UnknownUniverseError, InvalidSortFieldError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

"""
Pre-flight health check endpoint for Screener
Verifies Fyers connectivity and data freshness before UI loads
"""

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from ..data_layer.provider import DataProvider
from ..database import get_db
from ..exceptions import DatabaseUnavailableError
from ..services.deferred_startup import DeferredStartupStatus, startup_status

router = APIRouter(prefix="/api/system", tags=["System Health"])
DB_DEPENDENCY = Depends(get_db)


@router.get(
    "/health",
    response_model=None,
    responses={503: {"description": "Database unavailable (body still carries the report)"}},
)
def system_health_check(
    request: Request, db: Session = DB_DEPENDENCY
) -> dict[str, Any] | JSONResponse:
    """
    Comprehensive system health check

    Returns:
        {
            "status": "READY" | "DEGRADED" | "DOWN",
            "postgresql": "OK" | "DOWN" | "ERROR",
            "fyers": "OK" | "TOKEN_EXPIRED" | "DOWN",
            "last_data_update": ISO timestamp,
            "total_symbols": int,
            "recommendations": [str],
            "startup": "COMPLETE" | "DEFERRED" | "FAILED"
        }

    startup is DEFERRED while the database work skipped at startup (the server started with
    the DB down) is still being retried in the background; status is then at best DEGRADED.
    FAILED means that work hit a non-connection error after the DB came back.

    postgresql DOWN means the database is unreachable: the reply is HTTP 503, the same
    report plus the standard error envelope (code DATABASE_UNAVAILABLE). ERROR means it
    answered but the probe query failed (schema drift, SQL bug): status DEGRADED, HTTP 200,
    details in the server log.
    """
    provider = DataProvider(db)
    health = provider.health_check()

    # Determine overall status
    status = "READY"
    recommendations = []

    db_down = health["postgresql"] == "DOWN"
    db_error = health["postgresql"] == "ERROR"

    if health["fyers"] == "TOKEN_EXPIRED":
        status = "DEGRADED"
        recommendations.append(
            "Fyers access token expired. Re-authenticate at /api/hat/auth/fyers/url"
        )
    elif health["fyers"] != "OK":
        status = "DEGRADED"
        recommendations.append("Fyers API unavailable. Screener will use cached data only.")

    if db_down:
        # A down database outranks a degraded broker.
        status = "DOWN"
        recommendations.insert(
            0, "PostgreSQL database is unavailable. Start it: net start postgresql-x64-18"
        )
    elif db_error:
        status = "DEGRADED"
        recommendations.insert(
            0, "PostgreSQL query failed (schema out of date?). Check the server log."
        )
    elif health["last_update"] is None:
        recommendations.append(
            "No historical data found. Run canonical bhavcopy + phase1 pipeline."
        )

    startup = startup_status(request.app.state)
    if startup is not DeferredStartupStatus.COMPLETE:
        if status == "READY":
            status = "DEGRADED"
        recommendations.append(
            "Database setup deferred at startup (database was down); it completes automatically."
            if startup is DeferredStartupStatus.DEFERRED
            else "Deferred database setup failed after the database came back. Check the log."
        )

    report = {
        "status": status,
        **health,
        "recommendations": recommendations,
        "startup": startup.value,
    }
    if db_down:
        error = DatabaseUnavailableError()
        return JSONResponse(status_code=error.status_code, content={**report, **error.to_dict()})
    return report

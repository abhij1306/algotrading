"""
SmartTrader 3.0 API - FastAPI Application
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse, Response
from sqlalchemy.exc import DBAPIError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .utils.db_errors import db_error_summary, find_db_unavailable_error, is_db_unavailable
from .utils.env_loader import load_dotenv
from .utils.logger import get_logger, log_warning_throttled, safe_log_value, setup_logging

# Setup logging
setup_logging()
logger = get_logger("main")

# Load environment variables
load_dotenv()

# Import routers - these may have heavy dependencies
# E402 is suppressed below: routers read config at import, so load_dotenv() must run first.
from .config import config  # noqa: E402
from .exceptions import DatabaseUnavailableError, SmartTraderException  # noqa: E402
from .hat.api import fyers_auth as hat_fyers_auth  # noqa: E402
from .routers import (  # noqa: E402
    activity,
    data_snapshot,
    market,
    market_dashboard,
    portfolio,
    system_health,
    terminal,
    trading,
    unified,
    universe,
    upload,
    websocket,
)
from .services.fyers.auth_flow import install_legacy_hooks  # noqa: E402
from .utils.origins import FrontendCORSMiddleware  # noqa: E402


def _install_windows_reset_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Suppress noisy expected WinError 10054 transport disconnect callbacks on Windows."""
    default_handler = loop.default_exception_handler

    def _handler(event_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        message = context.get("message", "")
        handle = context.get("handle")
        if (
            isinstance(exc, ConnectionResetError)
            and getattr(exc, "winerror", None) == 10054
            and "_ProactorBasePipeTransport._call_connection_lost" in str(handle)
        ):
            logger.debug("Ignored expected Windows connection reset during transport shutdown")
            return
        if (
            isinstance(exc, ConnectionResetError)
            and getattr(exc, "winerror", None) == 10054
            and "_call_connection_lost" in message
        ):
            logger.debug("Ignored expected Windows connection reset callback")
            return
        default_handler(context)

    loop.set_exception_handler(_handler)


# Backoff for retrying the database startup work after starting with the DB down.
DB_RETRY_INITIAL_DELAY_S = 5.0
DB_RETRY_MAX_DELAY_S = 60.0


async def _step_get_event_loop(app: FastAPI) -> None:
    loop = asyncio.get_running_loop()
    app.state.loop = loop
    _install_windows_reset_handler(loop)
    logger.info(f"[OK] Event loop acquired: {loop}")
    await asyncio.sleep(0)


async def _step_validate_symbol_master() -> None:
    from .services.symbol_master import symbol_master

    test_symbol = "SBIN"
    fyers_format = symbol_master.to_fyers(test_symbol)
    db_format = symbol_master.to_db(fyers_format)
    if db_format != test_symbol:
        raise ValueError(f"Symbol master validation failed: {test_symbol} != {db_format}")
    logger.info("[OK] Symbol master validated")
    await asyncio.sleep(0)


def _ensure_database_schema() -> None:
    """Create/upgrade the schema and probe the DB; raise DatabaseUnavailableError if it is down."""
    from sqlalchemy import text

    from . import database

    try:
        database.init_db()
        with database.engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception as exc:
        if not is_db_unavailable(exc):
            raise
        raise DatabaseUnavailableError() from None


async def _step_validate_database() -> None:
    from .database import describe_database_target

    try:
        _ensure_database_schema()
    except DatabaseUnavailableError:
        # One clear line, no traceback and no password; requests answer 503 until it is up.
        logger.error(
            "[ERROR] Database unreachable at %s (start Postgres: net start postgresql-x64-18)",
            describe_database_target(),
        )
        raise
    logger.info("[OK] Database validated")
    await asyncio.sleep(0)


async def _step_validate_fyers_token(app: FastAPI) -> None:
    from .services.fyers_client import get_fyers_client

    fyers = get_fyers_client()
    is_valid = False
    if fyers:
        try:
            is_valid = await asyncio.wait_for(asyncio.to_thread(fyers.validate_token), timeout=5.0)
        except TimeoutError:
            logger.warning("[WARN] Fyers token validation timed out after 5s")
        except Exception as exc:
            logger.warning(f"[WARN] Fyers token validation errored: {exc}")

    app.state.fyers_token_valid = bool(is_valid)
    if app.state.fyers_token_valid:
        logger.info("[OK] Fyers token validated")
    else:
        logger.warning(
            "[WARN] Fyers token is invalid/expired. Startup will continue in degraded mode."
        )


async def _step_load_index_universe() -> None:
    from .services.index_universe_loader import index_universe_loader

    if index_universe_loader.data_path.exists():
        logger.info("[OK] Index universe data path available; loading deferred until first use")
    else:
        logger.warning(
            "[WARN] Index universe data path missing; "
            "universe features will degrade until data is provided"
        )
    await asyncio.sleep(0)


def _log_live_market_status(connect_status: str) -> None:
    from .services.live_market_service import connect_status_message

    message = f"Live market: {connect_status_message(connect_status)} ({connect_status})"
    if connect_status in {"started", "already_connected"}:
        logger.info(f"[OK] {message}")
    elif connect_status == "market_closed":
        logger.info(f"[INFO] {message}")
    else:
        logger.warning(f"[WARN] {message}")


async def _step_connect_live_market(app: FastAPI) -> None:
    from .services.live_market_service import live_market

    connect_status = live_market.connect(
        loop=app.state.loop,
        prevalidated_token=app.state.fyers_token_valid,
    )
    _log_live_market_status(connect_status)
    await asyncio.sleep(0)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """
    Application lifespan manager with structured startup sequence.

    Startup sequence:
    1. Get event loop
    2. Validate symbol_master
    3. Validate database (deferred, not fatal, when the DB is down)
    4. Validate Fyers token (warn-only)
    5. Load index universe (warn-only)
    6. Connect live_market with event loop

    Steps 4-6 do not use the database, so they run even when step 3 finds it down. The
    deferred schema work is retried in the background until the database answers.
    """
    from .services.deferred_startup import DeferredStartup
    from .services.startup_validator import StartupSequence, StartupStep

    sequence = StartupSequence()
    deferred_startup = DeferredStartup(
        _ensure_database_schema,
        initial_delay=DB_RETRY_INITIAL_DELAY_S,
        max_delay=DB_RETRY_MAX_DELAY_S,
    )
    app.state.deferred_startup = deferred_startup

    try:
        await sequence.execute_step(
            StartupStep.SET_EVENT_LOOP,
            lambda: _step_get_event_loop(app),
            required=True,
        )
        await sequence.execute_step(
            StartupStep.VALIDATE_SYMBOL_MASTER, _step_validate_symbol_master, required=True
        )
        await sequence.execute_step(
            StartupStep.VALIDATE_DATABASE, _step_validate_database, required=True
        )
        await sequence.execute_step(
            StartupStep.VALIDATE_FYERS_TOKEN,
            lambda: _step_validate_fyers_token(app),
            required=False,
        )
        await sequence.execute_step(
            StartupStep.LOAD_INDEX_UNIVERSE,
            _step_load_index_universe,
            required=False,
        )
        await sequence.execute_step(
            StartupStep.CONNECT_LIVE_MARKET,
            lambda: _step_connect_live_market(app),
            required=False,
        )
        if StartupStep.VALIDATE_DATABASE in sequence.deferred_steps:
            # Keep serving: health reports the DB as down and DB routes answer 503 meanwhile.
            deferred_startup.start()
        sequence.log_summary()
        if sequence.is_complete():
            logger.info("[OK] WebSocket services initialized successfully")

    except Exception as e:
        logger.exception(f"[ERROR] Startup sequence failed: {e}")
        sequence.log_summary()

    try:
        yield
    finally:
        await _shutdown(app)


async def _stop_deferred_startup(app: FastAPI) -> None:
    deferred_startup = app.state.deferred_startup
    del app.state.deferred_startup
    await deferred_startup.stop()


async def _stop_live_market() -> None:
    from .services import live_market_service

    await live_market_service.live_market.shutdown()


async def _close_websocket_clients() -> None:
    from .utils import ws_manager

    await ws_manager.manager.close_all()


async def _dispose_engine() -> None:
    from . import database

    database.engine.dispose()
    await asyncio.sleep(0)


async def _run_shutdown_step(name: str, step: Callable[[], Awaitable[None]]) -> bool:
    """Run one shutdown step; a failure is one warning line (no traceback), never raised."""
    try:
        await step()
    except Exception as exc:
        logger.warning("[WARN] Shutdown step %s failed: %s", name, exc)
        return False
    return True


async def _shutdown(app: FastAPI) -> None:
    """Stop background work, close sockets and release the DB pool; each step runs even if
    an earlier one failed."""
    steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("deferred_startup", lambda: _stop_deferred_startup(app)),
        ("live_market", _stop_live_market),
        ("websocket_clients", _close_websocket_clients),
        ("database_engine", _dispose_engine),
    ]
    results = [await _run_shutdown_step(name, step) for name, step in steps]
    if all(results):
        logger.info("Shutdown complete")
    else:
        logger.info("Shutdown complete (%d step(s) failed)", results.count(False))


app = FastAPI(
    title="SmartTrader 3.0 API",
    version="3.0.0",
    description="Algorithmic Trading Platform",
    lifespan=lifespan,
)


# Exception handlers
#
# Handlers registered for a specific exception class run in Starlette's
# ExceptionMiddleware, which returns the response without re-raising. Only the
# catch-all Exception handler runs in ServerErrorMiddleware, which re-raises after
# responding (uvicorn then prints the traceback). A down database therefore gets its
# own handlers so each request costs one throttled warning line, not a traceback.
INTERNAL_ERROR_BODY = {"error": {"code": "INTERNAL_ERROR", "message": "Server error"}}


def _db_unavailable_response(request: Request, exc: BaseException) -> JSONResponse:
    db_exc = find_db_unavailable_error(exc)
    log_warning_throttled(
        logger,
        "http.database_unavailable",
        "Database unavailable: %s %s -> 503 (%s)",
        request.method,
        safe_log_value(request.url.path),
        safe_log_value(db_error_summary(db_exc)) if db_exc is not None else "unreachable",
    )
    error = DatabaseUnavailableError()
    return JSONResponse(status_code=error.status_code, content=error.to_dict())


@app.exception_handler(SmartTraderException)
async def smarttrader_exception_handler(
    request: Request, exc: SmartTraderException
) -> JSONResponse:
    if isinstance(exc, DatabaseUnavailableError) or is_db_unavailable(exc):
        return _db_unavailable_response(request, exc)
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


@app.exception_handler(DBAPIError)
async def dbapi_exception_handler(request: Request, exc: DBAPIError) -> JSONResponse:
    """503 when the database is unreachable. Any other driver error (a SQL bug such as
    ProgrammingError or IntegrityError) stays a 500, with its traceback logged once."""
    if is_db_unavailable(exc):
        return _db_unavailable_response(request, exc)
    logger.error(
        "Unhandled database error on %s %s",
        request.method,
        safe_log_value(request.url.path),
        exc_info=exc,
    )
    return JSONResponse(status_code=500, content=INTERNAL_ERROR_BODY)


@app.exception_handler(StarletteHTTPException)
async def http_exception_with_db_check(request: Request, exc: StarletteHTTPException) -> Response:
    """Routers often wrap failures as `raise HTTPException(...) from exc`; when the
    wrapped error is a down database, answer 503 DATABASE_UNAVAILABLE instead."""
    if is_db_unavailable(exc):
        return _db_unavailable_response(request, exc)
    return await http_exception_handler(request, exc)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if is_db_unavailable(exc):
        return _db_unavailable_response(request, exc)
    logger.error(f"Unhandled: {str(exc)}", exc_info=True)
    return JSONResponse(status_code=500, content=INTERNAL_ERROR_BODY)


# CORS: the configured origins plus any http loopback origin (utils/origins.py), the same
# rule as the Origin check on WS /stream.
app.add_middleware(
    FrontendCORSMiddleware,
    allow_origins=list(config.ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(system_health.router)
app.include_router(unified.router)
app.include_router(market.router, prefix="/api/market", tags=["Market Data"])
app.include_router(market_dashboard.router, prefix="/api")
app.include_router(hat_fyers_auth.router)
install_legacy_hooks()
app.include_router(websocket.router, prefix="/api/websocket", tags=["WebSocket"])
app.include_router(universe.router, prefix="/api/universe", tags=["Universe"])
app.include_router(upload.router, prefix="/api/upload", tags=["Upload"])
app.include_router(data_snapshot.router)
app.include_router(terminal.router)
app.include_router(trading.router)
app.include_router(portfolio.router)
app.include_router(activity.router)

logger.info("[OK] All routers registered")


@app.get("/", response_model=None)
def read_root() -> dict[str, Any]:
    return {"status": "ok", "message": "SmartTrader 3.0 API Running", "version": "3.0.0"}


@app.get("/ping", response_model=None)
def ping() -> dict[str, Any]:
    return {"ok": True, "message": "Backend is alive"}

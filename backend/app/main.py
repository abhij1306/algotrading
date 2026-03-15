"""
SmartTrader 3.0 API - FastAPI Application
"""

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .utils.env_loader import load_dotenv
from .utils.logger import get_logger, setup_logging

# Setup logging
setup_logging()
logger = get_logger("main")

# Load environment variables
load_dotenv()

# Import routers - these may have heavy dependencies
from .exceptions import SmartTraderException
from .routers import (
    activity,
    auth,
    backtest,
    data_snapshot,
    market,
    market_dashboard,
    options,
    portfolio,
    screener,
    system_health,
    terminal,
    trading,
    unified,
    universe,
    upload,
    websocket,
)


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


async def _step_get_event_loop(app: FastAPI) -> None:
    loop = asyncio.get_running_loop()
    app.state.loop = loop
    _install_windows_reset_handler(loop)
    logger.info(f"[OK] Event loop acquired: {loop}")
    await asyncio.sleep(0)


async def _step_set_ws_manager_loop(app: FastAPI) -> None:
    from .utils.ws_manager import manager

    manager.set_loop(app.state.loop)
    logger.info("[OK] WebSocket manager loop set")
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


async def _step_validate_database() -> None:
    from sqlalchemy import text

    from .database import Base, SessionLocal, engine, ensure_backtest_schema, ensure_universe_schema

    Base.metadata.create_all(bind=engine)
    ensure_backtest_schema(bind=engine)
    ensure_universe_schema(bind=engine)
    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        logger.info("[OK] Database validated")
    finally:
        db.close()
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
        logger.warning("[WARN] Fyers token is invalid/expired. Startup will continue in degraded mode.")


async def _step_load_index_universe() -> None:
    from .services.index_universe_loader import index_universe_loader

    if index_universe_loader.data_path.exists():
        logger.info("[OK] Index universe data path available; loading deferred until first use")
    else:
        logger.warning(
            "[WARN] Index universe data path missing; universe features will degrade until data is provided"
        )
    await asyncio.sleep(0)


def _log_live_market_status(connect_status: str) -> None:
    if connect_status in {"started", "already_connected"}:
        logger.info("[OK] Live market service connection initialized")
    elif connect_status == "market_closed":
        logger.info("[INFO] Live market connection deferred: market closed")
    elif connect_status == "token_invalid":
        logger.warning("[WARN] Live market connection deferred: Fyers token invalid/expired")
    else:
        logger.warning(f"[WARN] Live market connection deferred: {connect_status}")


async def _step_connect_live_market(app: FastAPI) -> None:
    from .services.live_market_service import live_market

    connect_status = live_market.connect(
        loop=app.state.loop,
        prevalidated_token=app.state.fyers_token_valid,
    )
    _log_live_market_status(connect_status)
    await asyncio.sleep(0)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager with structured startup sequence.

    Startup sequence (from Websocket.md v2.2):
    1. Get event loop
    2. Set loop for WebSocket manager
    3. Validate symbol_master
    4. Validate database
    5. Validate Fyers token (warn-only)
    6. Load index universe (warn-only)
    7. Connect live_market with event loop
    """
    from .services.startup_validator import StartupSequence, StartupStep

    sequence = StartupSequence()

    try:
        await sequence.execute_step(
            StartupStep.SET_EVENT_LOOP,
            lambda: _step_get_event_loop(app),
            required=True,
        )
        await sequence.execute_step(
            StartupStep.SET_WS_MANAGER_LOOP,
            lambda: _step_set_ws_manager_loop(app),
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
        sequence.log_summary()
        if sequence.is_complete():
            logger.info("[OK] WebSocket services initialized successfully")

    except Exception as e:
        logger.error(f"[ERROR] Startup sequence failed: {e}", exc_info=True)
        sequence.log_summary()

    yield

    # Shutdown
    logger.info("Shutdown complete")


app = FastAPI(
    title="SmartTrader 3.0 API",
    version="3.0.0",
    description="Algorithmic Trading Platform",
    lifespan=lifespan,
)


# Exception handlers
@app.exception_handler(SmartTraderException)
async def smarttrader_exception_handler(request: Request, exc: SmartTraderException):
    return JSONResponse(status_code=exc.status_code, content=exc.to_dict())


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500, content={"error": {"code": "INTERNAL_ERROR", "message": "Server error"}}
    )


# CORS
# Allow frontend origins from environment variable or default to localhost
allowed_origins = os.getenv(
    "CORS_ORIGINS",
    "http://localhost:3000,http://localhost:3001,http://127.0.0.1:3000,http://127.0.0.1:3001",
).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(system_health.router)
app.include_router(unified.router)
app.include_router(screener.router, prefix="/api/screener", tags=["Screener"])
app.include_router(market.router, prefix="/api/market", tags=["Market Data"])
app.include_router(market_dashboard.router, prefix="/api")
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(websocket.router, prefix="/api/websocket", tags=["WebSocket"])
app.include_router(universe.router, prefix="/api/universe", tags=["Universe"])
app.include_router(upload.router, prefix="/api/upload", tags=["Upload"])
app.include_router(backtest.router, prefix="/api/backtest", tags=["Backtest"])
app.include_router(data_snapshot.router)
app.include_router(terminal.router)
app.include_router(trading.router)
app.include_router(portfolio.router)
app.include_router(activity.router)
app.include_router(options.router)

logger.info("[OK] All routers registered")


@app.get("/")
def read_root():
    return {"status": "ok", "message": "SmartTrader 3.0 API Running", "version": "3.0.0"}


@app.get("/ping")
def ping():
    return {"ok": True, "message": "Backend is alive"}

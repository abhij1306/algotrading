"""
Hire a Trader API: the FastAPI app, its routes and its startup.

Startup (the lifespan): connect the event loop, create the database schema, then, only when
`HAT_ENABLED` is true, start HAT's scheduler and its live feed (the one Fyers data socket,
HAT-OVR-006). With HAT disabled nothing of HAT runs, and the app serves only the Fyers login
routes. HAT's Telegram bot (`app/hat/telegram`, PR 16) is started here by PR 17.
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

setup_logging()
logger = get_logger("main")

load_dotenv()

# E402 is suppressed below: the modules read the environment at import, so load_dotenv() runs first.
from .config import config  # noqa: E402
from .exceptions import DatabaseUnavailableError  # noqa: E402
from .hat.api import fyers_auth as hat_fyers_auth  # noqa: E402
from .hat.settings import is_enabled  # noqa: E402
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


def _ensure_database_schema() -> None:
    """Create the schema and probe the DB; raise DatabaseUnavailableError if it is down."""
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


async def _init_database() -> None:
    """Create the schema. With the database down: one clear log line, no traceback and no
    password, and the app keeps serving (requests answer 503) unless HAT is enabled, which
    refuses to start without its database."""
    from .database import describe_database_target

    try:
        await asyncio.to_thread(_ensure_database_schema)
    except DatabaseUnavailableError:
        logger.error(
            "[ERROR] Database unreachable at %s (start Postgres)", describe_database_target()
        )
        if is_enabled():
            raise
        return
    logger.info("[OK] Database ready")


async def _validate_fyers_token(app: FastAPI) -> None:
    """Warn-only: the feed starts blocked, and the login route fixes it, when the token is bad."""
    from .hat.execution.fyers_session import get_fyers_session

    is_valid = False
    try:
        fyers = get_fyers_session()
        is_valid = await asyncio.wait_for(asyncio.to_thread(fyers.validate_token), timeout=5.0)
    except TimeoutError:
        logger.warning("[WARN] Fyers token validation timed out after 5s")
    except Exception as exc:
        logger.warning("[WARN] Fyers token validation errored: %s", type(exc).__name__)

    app.state.fyers_token_valid = bool(is_valid)
    if app.state.fyers_token_valid:
        logger.info("[OK] Fyers token validated")
    else:
        logger.warning("[WARN] Fyers token is invalid or expired; log in through the Fyers route.")


def _start_hat_scheduler(app: FastAPI) -> None:
    """The in-process job scheduler (HAT-OPS-013). The daily-cycle jobs are registered by PR 17."""
    from . import database
    from .hat.ops.scheduler import start_scheduler
    from .hat.settings import load_settings

    app.state.hat_scheduler = start_scheduler(
        load_settings(), dialect_name=database.engine.dialect.name
    )


def _start_hat_feed(app: FastAPI) -> None:
    """Build HAT's live feed, hang its login hooks on the Fyers auth service and start it."""
    from .database import SessionLocal
    from .hat.data.feed import LiveFeed, calendar_policy_source
    from .hat.execution.fyers_auth import get_fyers_auth

    feed = LiveFeed(policy_source=calendar_policy_source(SessionLocal))
    feed.install_auth_hooks(get_fyers_auth())
    app.state.hat_feed = feed
    feed.start(app.state.loop, prevalidated_token=app.state.fyers_token_valid)


async def _start_hat(app: FastAPI) -> None:
    """The scheduler, then the feed, only when HAT_ENABLED. A failure stops what already
    started, then raises: HAT enabled means HAT runs or the app does not start."""
    if is_enabled():
        try:
            _start_hat_scheduler(app)
            await _validate_fyers_token(app)
            _start_hat_feed(app)
        except Exception:
            await _shutdown(app)
            raise
        logger.info("[OK] HAT started")
    else:
        logger.info("HAT_ENABLED is false: the scheduler and the live feed are not started")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: event loop, database schema, then HAT (scheduler, feed) when HAT_ENABLED.
    Shutdown: the feed, the scheduler and the database engine, each even if another failed."""
    loop = asyncio.get_running_loop()
    app.state.loop = loop
    _install_windows_reset_handler(loop)
    await _init_database()
    await _start_hat(app)
    try:
        yield
    finally:
        await _shutdown(app)


async def _stop_hat_feed(app: FastAPI) -> None:
    feed = getattr(app.state, "hat_feed", None)
    if feed is None:
        return
    del app.state.hat_feed
    await feed.stop()


async def _stop_hat_scheduler(app: FastAPI) -> None:
    scheduler = getattr(app.state, "hat_scheduler", None)
    if scheduler is None:
        return
    del app.state.hat_scheduler
    await scheduler.stop()


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
    """Stop HAT's background work and release the DB pool; each step runs even if an earlier
    one failed."""
    steps: list[tuple[str, Callable[[], Awaitable[None]]]] = [
        ("hat_feed", lambda: _stop_hat_feed(app)),
        ("hat_scheduler", lambda: _stop_hat_scheduler(app)),
        ("database_engine", _dispose_engine),
    ]
    results = [await _run_shutdown_step(name, step) for name, step in steps]
    if all(results):
        logger.info("Shutdown complete")
    else:
        logger.info("Shutdown complete (%d step(s) failed)", results.count(False))


app = FastAPI(
    title="Hire a Trader API",
    version="3.0.0",
    description="Hire a Trader (HAT): rules-based NSE swing trading",
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


@app.exception_handler(DatabaseUnavailableError)
async def database_unavailable_handler(
    request: Request, exc: DatabaseUnavailableError
) -> JSONResponse:
    return _db_unavailable_response(request, exc)


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
    logger.error("Unhandled: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content=INTERNAL_ERROR_BODY)


# CORS: the configured origins plus any http loopback origin (utils/origins.py).
app.add_middleware(
    FrontendCORSMiddleware,
    allow_origins=list(config.ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# HAT routes. PR 15 adds the rest of `/api/hat/*`.
app.include_router(hat_fyers_auth.router)


@app.get("/", response_model=None)
def read_root() -> dict[str, Any]:
    return {"status": "ok", "message": "Hire a Trader API running", "version": "3.0.0"}


@app.get("/ping", response_model=None)
def ping() -> dict[str, Any]:
    return {"ok": True, "message": "Backend is alive"}

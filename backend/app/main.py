"""
Hire a Trader API: the FastAPI app, its routes and its startup.

Startup (the lifespan): connect the event loop, create the database schema, then, only when
`HAT_ENABLED` is true, start the HAT runtime (`app/hat/daily/runtime.py`): the broker desks,
the scheduler with the daily cycle, the live feed (the one Fyers data socket, HAT-OVR-006) and
the Telegram bot. With HAT disabled nothing of HAT runs, and the app serves only its routes.
"""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .utils.env_loader import load_dotenv
from .utils.logger import get_logger, setup_logging

setup_logging()
logger = get_logger("main")

load_dotenv()

# E402 is suppressed below: the modules read the environment at import, so load_dotenv() runs first.
from .hat.api import backtests as hat_backtests  # noqa: E402
from .hat.api import dashboard as hat_dashboard  # noqa: E402
from .hat.api import fyers_auth as hat_fyers_auth  # noqa: E402
from .hat.api import kite_auth as hat_kite_auth  # noqa: E402
from .hat.settings import is_enabled  # noqa: E402
from .utils.origins import ALLOWED_ORIGINS, FrontendCORSMiddleware, is_frontend_origin  # noqa: E402


def _ensure_database_schema() -> None:
    """Create the schema and probe the database."""
    from sqlalchemy import text

    from . import database

    database.init_db()
    with database.engine.connect() as connection:
        connection.execute(text("SELECT 1"))


async def _init_database() -> None:
    """Create the schema. Without its database the app does not start: an unreachable database
    is one clear log line, no driver text and no password, then the startup fails; so is a schema
    that differs from the models, with the DDL to run. Any other error (a schema bug, a refused
    permission) propagates with its traceback."""
    from sqlalchemy.exc import InterfaceError, OperationalError

    from .database import describe_database_target
    from .hat.schema import SchemaDriftError

    try:
        await asyncio.to_thread(_ensure_database_schema)
    except (OperationalError, InterfaceError) as exc:
        logger.error(
            "[ERROR] Database unreachable at %s (%s; start Postgres)",
            describe_database_target(),
            type(exc).__name__,
        )
        raise RuntimeError("database unavailable") from None
    except SchemaDriftError as exc:
        logger.error("[ERROR] Schema drift at %s: %s", describe_database_target(), exc)
        raise RuntimeError("database schema differs from the models") from None
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


# Returns a LiveFeed, imported only when HAT is enabled (hence `Any`).
def _build_hat_feed() -> Any:  # noqa: ANN401
    """HAT's live feed (the one Fyers data socket of the process, HAT-OVR-006), its login hooks
    hung on the Fyers auth service. The runtime starts it."""
    from .database import HatSessionLocal
    from .hat.data.feed import LiveFeed, calendar_source
    from .hat.execution.fyers_auth import get_fyers_auth
    from .hat.execution.fyers_session import fyers_token, session_token_valid

    feed = LiveFeed(
        calendar_source=calendar_source(HatSessionLocal),
        token_source=fyers_token,  # read on every connect, so a re-login reaches the next socket
        validate_token=session_token_valid,
    )
    feed.install_auth_hooks(get_fyers_auth())
    return feed


async def _start_hat_runtime(app: FastAPI, feed: Any) -> None:  # noqa: ANN401 - LiveFeed
    """Check the Fyers token (warn only), then start the HAT runtime (`app/hat/daily/runtime.py`:
    desks, scheduler with the daily cycle, the feed and the Telegram bot)."""
    from . import database
    from .hat.daily.production import production_parts
    from .hat.daily.runtime import HatRuntime
    from .hat.ops.runtime_lock import RuntimeLock
    from .hat.settings import load_settings

    settings = load_settings()
    await _validate_fyers_token(app)
    runtime = HatRuntime(
        settings,
        database.HatSessionLocal,
        dialect_name=database.engine.dialect.name,
        parts=production_parts(settings, feed, prevalidated_token=app.state.fyers_token_valid),
        runtime_lock=lambda: RuntimeLock.acquire(database.engine),
    )
    app.state.hat_runtime = runtime
    await runtime.start()


async def _start_hat(app: FastAPI) -> None:
    """The HAT runtime, only when HAT_ENABLED. A failure stops what already started, then
    raises: HAT enabled means HAT runs or the app does not start."""
    if is_enabled():
        try:
            await _start_hat_runtime(app, _build_hat_feed())
        except Exception:
            await _shutdown(app)
            raise
        logger.info("[OK] HAT started")
    else:
        logger.info("HAT_ENABLED is false: no HAT task is started")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: event loop, database schema, then the HAT runtime when HAT_ENABLED.
    Shutdown: the HAT runtime, then the database engine, each even if the other failed."""
    loop = asyncio.get_running_loop()
    app.state.loop = loop
    await _init_database()
    await _start_hat(app)
    try:
        yield
    finally:
        await _shutdown(app)


async def _stop_hat(app: FastAPI) -> None:
    runtime = getattr(app.state, "hat_runtime", None)
    if runtime is None:
        return
    del app.state.hat_runtime
    await runtime.stop()


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
        ("hat", lambda: _stop_hat(app)),
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


# The catch-all handler: a fixed body, the error only in the server log. Errors never carry
# broker or database text.
INTERNAL_ERROR_BODY = {"error": {"code": "INTERNAL_ERROR", "message": "Server error"}}


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("Unhandled: %s", exc, exc_info=True)
    return JSONResponse(status_code=500, content=INTERNAL_ERROR_BODY)


# CORS: the configured origins plus any http loopback origin (utils/origins.py).
app.add_middleware(
    FrontendCORSMiddleware,
    allow_origins=list(ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# The frontend-origin rule the HAT write guard checks (app/hat/api/guard.py, HAT-DEC-BD5): the
# same rule as CORS. Unset, the guard refuses every write that carries an Origin.
app.state.frontend_origin = is_frontend_origin

# HAT routes: the Fyers and Kite logins, the read-only dashboard and backtests (`/api/hat/*`).
app.include_router(hat_fyers_auth.router)
app.include_router(hat_kite_auth.router)
app.include_router(hat_dashboard.router)
app.include_router(hat_backtests.router)


@app.get("/", response_model=None)
def read_root() -> dict[str, Any]:
    return {"status": "ok", "message": "Hire a Trader API running", "version": "3.0.0"}


@app.get("/ping", response_model=None)
def ping() -> dict[str, Any]:
    return {"ok": True, "message": "Backend is alive"}

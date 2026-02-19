"""Backend startup script with Python 3.14 compatibility and env-driven config."""

import asyncio
import os
import sys

import uvicorn

from app.main import app

# Force WindowsSelectorEventLoopPolicy for Python 3.14+ compatibility
if sys.platform == "win32" and sys.version_info >= (3, 14):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    print("[INFO] Set WindowsSelectorEventLoopPolicy for Python 3.14 compatibility")

os.environ.setdefault("DEV_MODE", "true")


def _env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


if __name__ == "__main__":
    host = os.getenv("BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("BACKEND_PORT", "8000"))
    reload_enabled = _env_bool("BACKEND_RELOAD", True)

    if reload_enabled:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            log_level="info",
            loop="asyncio",
            reload=True,
        )
    else:
        uvicorn.run(app, host=host, port=port, log_level="info", loop="asyncio")

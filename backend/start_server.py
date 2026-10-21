"""Backend startup script (run from repo root: python backend/start_server.py).

Env: BACKEND_HOST, BACKEND_PORT, BACKEND_RELOAD, BACKEND_ACCESS_LOG, BACKEND_LOG_LEVEL.
"""

import os

import uvicorn

from app.main import app


def _env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_level(key: str, default: str) -> str:
    value = (os.getenv(key) or default).strip().upper()
    valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
    return value if value in valid else default.upper()


if __name__ == "__main__":
    host = os.getenv("BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("BACKEND_PORT", "8000"))
    reload_enabled = _env_bool("BACKEND_RELOAD", False)
    access_log_enabled = _env_bool("BACKEND_ACCESS_LOG", False)
    backend_log_level = _env_level("BACKEND_LOG_LEVEL", "INFO").lower()

    if reload_enabled:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            log_level=backend_log_level,
            loop="asyncio",
            reload=True,
            access_log=access_log_enabled,
            reload_dirs=["backend/app"],
            reload_excludes=["data_system/*", "frontend/*", "venv/*"],
        )
    else:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=backend_log_level,
            loop="asyncio",
            access_log=access_log_enabled,
        )

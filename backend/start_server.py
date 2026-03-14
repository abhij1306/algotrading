"""Backend startup script with env-driven config."""

import copy
import os

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from app.main import app

os.environ.setdefault("DEV_MODE", "false")


def _env_bool(key: str, default: bool) -> bool:
    value = os.getenv(key)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_level(key: str, default: str) -> str:
    value = (os.getenv(key) or default).strip().upper()
    valid = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}
    return value if value in valid else default.upper()


def _build_uvicorn_log_config(framework_level: str) -> dict:
    config = copy.deepcopy(LOGGING_CONFIG)
    config.setdefault("loggers", {})
    config["loggers"].setdefault(
        "websockets.server",
        {"handlers": ["default"], "level": framework_level, "propagate": False},
    )
    config["loggers"].setdefault(
        "websockets.protocol",
        {"handlers": ["default"], "level": framework_level, "propagate": False},
    )
    config["loggers"].setdefault(
        "websockets.client",
        {"handlers": ["default"], "level": framework_level, "propagate": False},
    )
    config["loggers"]["websockets.server"]["level"] = framework_level
    config["loggers"]["websockets.protocol"]["level"] = framework_level
    config["loggers"]["websockets.client"]["level"] = framework_level
    return config


if __name__ == "__main__":
    host = os.getenv("BACKEND_HOST", "127.0.0.1")
    port = int(os.getenv("BACKEND_PORT", "8000"))
    reload_enabled = _env_bool("BACKEND_RELOAD", False)
    access_log_enabled = _env_bool("BACKEND_ACCESS_LOG", False)
    backend_log_level = _env_level("BACKEND_LOG_LEVEL", "INFO").lower()
    framework_log_level = _env_level("FRAMEWORK_LOG_LEVEL", "WARNING")
    framework_log_level = _env_level("FRAMEWORK_LOG_LEVEL", "WARNING")
    uvicorn_log_config = _build_uvicorn_log_config(framework_log_level)

    if reload_enabled:
        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            log_level=backend_log_level,
            loop="asyncio",
            reload=True,
            access_log=access_log_enabled,
            log_config=uvicorn_log_config,
            reload_dirs=["backend/app"],
            reload_excludes=[
                "data_system/*",
                "nse_data/*",
                "archive/*",
                "frontend/*",
                ".next/*",
                "venv/*",
            ],
        )
    else:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=backend_log_level,
            loop="asyncio",
            access_log=access_log_enabled,
            log_config=uvicorn_log_config,
        )

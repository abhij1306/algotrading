import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from ..paths import LOG_DIR

# Logging configuration
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_env_level = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = _env_level if _env_level in _VALID_LEVELS else "INFO"

# Standard format for all logs
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Modules log with logging.getLogger(__name__), i.e. under the "app" package
# namespace; it gets the same handlers and level as the base logger.
APP_LOGGER_NAMESPACE = "app"


def setup_logging(name: str = "hat") -> logging.Logger:
    """
    Set up centralized logging with rotation and console output.
    """
    logger = logging.getLogger(name)
    logger.setLevel(LOG_LEVEL)

    # Prevent double logging if setup called multiple times
    if logger.handlers:
        _share_handlers(logger)
        return logger

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File Handler (Rotating: 10MB per file, keep 5 backups)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"{name}.log"
        file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning("Could not create file handler: %s. Using console logging only.", e)

    # Reduce framework transport/access noise unless explicitly overridden.
    framework_level = os.getenv("FRAMEWORK_LOG_LEVEL", "WARNING").upper()
    if framework_level not in _VALID_LEVELS:
        framework_level = "WARNING"
    logging.getLogger("uvicorn.access").setLevel(framework_level)

    _share_handlers(logger)
    return logger


def _share_handlers(base: logging.Logger) -> None:
    """Attach the base logger's handlers and level to the app.* namespace (idempotent)."""
    app_logger = logging.getLogger(APP_LOGGER_NAMESPACE)
    app_logger.setLevel(base.level)
    for handler in base.handlers:
        if handler not in app_logger.handlers:
            app_logger.addHandler(handler)


# Global default logger
logger = setup_logging()


def get_logger(module_name: str) -> logging.Logger:
    """
    Get a logger for a specific module, inheriting from the base configuration.
    """
    return logging.getLogger(f"hat.{module_name}")

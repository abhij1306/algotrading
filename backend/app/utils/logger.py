import logging
import os
import sys
import threading
import time
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

# Longest user-supplied value written to a log line; longer input is truncated.
_MAX_LOG_VALUE_LENGTH = 200


def safe_log_value(value: object) -> str:
    """Make a user-supplied value safe to write to a log line.

    Request input must not be able to forge log records, so CR and LF (and any other
    control characters) are replaced and the value is truncated.
    """
    text = str(value).replace("\r", "?").replace("\n", "?")
    return "".join(ch if ch.isprintable() else "?" for ch in text)[:_MAX_LOG_VALUE_LENGTH]


# A line that can repeat per request is logged at most once per site per interval.
TRACEBACK_INTERVAL_S = 60.0
_traceback_lock = threading.Lock()
_last_warning_at: dict[str, float] = {}


def log_warning_throttled(logger: logging.Logger, site: str, msg: str, *args: object) -> bool:
    """Log one warning line (no traceback) at most once per TRACEBACK_INTERVAL_S for this
    site; in between, log at debug.

    Returns True when the warning was written.
    """
    now = time.monotonic()
    with _traceback_lock:
        last = _last_warning_at.get(site)
        full = last is None or now - last >= TRACEBACK_INTERVAL_S
        if full:
            _last_warning_at[site] = now
    if full:
        logger.warning(msg, *args)
        return True
    logger.debug(msg, *args)
    return False


class DatabaseUnavailableLogFilter(logging.Filter):
    """Keep a down database from flooding the log.

    While Postgres is down every DB-backed request fails, and many routers log the
    failure with logger.exception: about 100 lines of traceback per request. For a record
    whose exception is a DB-unavailable error (see utils.db_errors) this filter drops the
    traceback, keeps only the first line of the message, and lets one such record through
    per logger and call site every TRACEBACK_INTERVAL_S. Other records pass untouched.
    """

    def __init__(self) -> None:
        super().__init__()
        self._last_at: dict[str, float] = {}
        self._lock = threading.Lock()

    def filter(self, record: logging.LogRecord) -> bool:
        exc = record.exc_info[1] if record.exc_info else None
        if exc is None:
            return True
        from .db_errors import is_db_unavailable

        if not is_db_unavailable(exc):
            return True

        site = f"{record.name}:{record.funcName}:{record.lineno}"
        now = time.monotonic()
        with self._lock:
            last = self._last_at.get(site)
            if last is not None and now - last < TRACEBACK_INTERVAL_S:
                return False
            self._last_at[site] = now

        first_line = (record.getMessage().strip().splitlines() or [""])[0]
        record.msg = f"{first_line} [database unavailable; traceback suppressed]"
        record.args = None
        record.exc_info = None
        record.exc_text = None
        return True


_db_unavailable_filter = DatabaseUnavailableLogFilter()


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
    console_handler.addFilter(_db_unavailable_filter)
    logger.addHandler(console_handler)

    # File Handler (Rotating: 10MB per file, keep 5 backups)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"{name}.log"
        file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        file_handler.setFormatter(formatter)
        file_handler.addFilter(_db_unavailable_filter)
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

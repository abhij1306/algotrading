import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Logging configuration
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_env_level = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = _env_level if _env_level in _VALID_LEVELS else "INFO"
PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOG_DIR = PROJECT_ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Standard format for all logs
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def setup_logging(name: str = "smarttrader"):
    """
    Set up centralized logging with rotation and console output.
    """
    logger = logging.getLogger(name)
    logger.setLevel(LOG_LEVEL)

    # Prevent double logging if setup called multiple times
    if logger.handlers:
        return logger

    formatter = logging.Formatter(LOG_FORMAT, DATE_FORMAT)

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # File Handler (Rotating: 10MB per file, keep 5 backups)
    try:
        log_file = LOG_DIR / f"{name}.log"
        file_handler = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as e:
        logger.warning(f"Could not create file handler: {e}. Using console logging only.")

    # Reduce framework transport/access noise unless explicitly overridden.
    framework_level = os.getenv("FRAMEWORK_LOG_LEVEL", "WARNING").upper()
    if framework_level not in _VALID_LEVELS:
        framework_level = "WARNING"
    logging.getLogger("uvicorn.access").setLevel(framework_level)
    logging.getLogger("websockets.server").setLevel(framework_level)
    logging.getLogger("websockets.protocol").setLevel(framework_level)

    return logger


# Global default logger
logger = setup_logging()


def get_logger(module_name: str):
    """
    Get a logger for a specific module, inheriting from the base configuration.
    """
    return logging.getLogger(f"smarttrader.{module_name}")

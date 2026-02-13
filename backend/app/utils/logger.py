import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from datetime import datetime

# Logging configuration
_VALID_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_env_level = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = _env_level if _env_level in _VALID_LEVELS else "INFO"
LOG_DIR = os.path.join(os.getcwd(), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

# Standard format for all logs
LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

def setup_logging(name: str = "smarttrader"):
    """
    Configure and return a logger that writes to stdout and to a rotating file.
    
    Parameters:
        name (str): Base logger name and the filename (without extension) used for the rotating log file.
    
    Returns:
        logging.Logger: A logger configured with a console StreamHandler and a RotatingFileHandler writing to LOG_DIR/<name>.log. If the logger already has handlers configured, the existing logger is returned unchanged.
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
    log_file = os.path.join(LOG_DIR, f"{name}.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10*1024*1024,
        backupCount=5
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger

# Global default logger
logger = setup_logging()

def get_logger(module_name: str):
    """
    Return a module-specific logger that uses the "smarttrader.<module_name>" logger namespace.
    
    Parameters:
        module_name (str): Module identifier appended to the "smarttrader" namespace.
    
    Returns:
        logging.Logger: Logger named "smarttrader.<module_name>".
    """
    return logging.getLogger(f"smarttrader.{module_name}")
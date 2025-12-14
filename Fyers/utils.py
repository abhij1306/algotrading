#!/usr/bin/env python3
"""
Utility functions for the AlgoTrading project.
"""

import os
import json
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, Optional, List
import threading


class RateLimiter:
    """Simple rate limiter to prevent API abuse."""
    
    def __init__(self, calls_per_second: float = 1.0):
        self.calls_per_second = calls_per_second
        self.min_interval = 1.0 / calls_per_second
        self.last_call = 0.0
        self.lock = threading.Lock()
    
    def wait(self):
        """Wait if necessary to respect rate limits."""
        with self.lock:
            current_time = time.time()
            time_since_last = current_time - self.last_call
            
            if time_since_last < self.min_interval:
                sleep_time = self.min_interval - time_since_last
                time.sleep(sleep_time)
            
            self.last_call = time.time()


def setup_directories():
    """Create necessary directories for the project."""
    directories = [
        "logs",
        "data",
        "config"
    ]
    
    for directory in directories:
        Path(directory).mkdir(parents=True, exist_ok=True)


def load_json_file(filepath: str) -> Optional[Dict[str, Any]]:
    """Load JSON file with error handling."""
    try:
        if not os.path.exists(filepath):
            logging.warning(f"File not found: {filepath}")
            return None
        
        with open(filepath, 'r') as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        logging.error(f"Invalid JSON in {filepath}: {e}")
        return None
    except Exception as e:
        logging.error(f"Error reading {filepath}: {e}")
        return None


def save_json_file(data: Dict[str, Any], filepath: str, indent: int = 4) -> bool:
    """Save data to JSON file with error handling."""
    try:
        # Create directory if it doesn't exist
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        
        with open(filepath, 'w') as f:
            json.dump(data, f, indent=indent, default=str)
        
        return True
    except Exception as e:
        logging.error(f"Error saving to {filepath}: {e}")
        return False


def format_time(timestamp: float) -> str:
    """Format timestamp to readable string."""
    return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def get_timestamp_ms() -> int:
    """Get current timestamp in milliseconds."""
    return int(time.time() * 1000)


def validate_symbol(symbol: str) -> bool:
    """Basic validation for trading symbols."""
    if not symbol or not isinstance(symbol, str):
        return False
    
    # Basic format check (should contain exchange:symbol)
    if ':' not in symbol:
        return False
    
    exchange, symbol_name = symbol.split(':', 1)
    
    # Check if exchange and symbol are not empty
    if not exchange or not symbol_name:
        return False
    
    # Check for valid characters (alphanumeric, hyphens, underscores)
    import re
    pattern = r'^[A-Za-z0-9\-_]+$'
    return re.match(pattern, exchange) is not None and re.match(pattern, symbol_name) is not None


def calculate_percentage_change(old_value: float, new_value: float) -> float:
    """Calculate percentage change between two values."""
    if old_value == 0:
        return 0.0
    return ((new_value - old_value) / old_value) * 100


def safe_divide(numerator: float, denominator: float, default: float = 0.0) -> float:
    """Safe division that returns default value when denominator is zero."""
    try:
        if denominator == 0:
            return default
        return numerator / denominator
    except (TypeError, ValueError):
        return default


def get_date_range(days: int = 30) -> tuple[str, str]:
    """Get date range for historical data."""
    end_date = datetime.now()
    start_date = end_date - timedelta(days=days)
    return start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d")


def format_number(value: float, decimals: int = 2) -> str:
    """Format number with specified decimal places."""
    if value is None:
        return "N/A"
    return f"{value:.{decimals}f}"


def is_market_open() -> bool:
    """Basic check if market is open (9:15 AM to 3:30 PM IST, Monday to Friday)."""
    now = datetime.now()
    
    # Check if weekend
    if now.weekday() >= 5:  # Saturday = 5, Sunday = 6
        return False
    
    # Check time (9:15 AM to 3:30 PM IST)
    market_open = now.replace(hour=9, minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=15, minute=30, second=0, microsecond=0)
    
    return market_open <= now <= market_close


def create_logger(name: str, log_file: str = None, level: int = logging.INFO) -> logging.Logger:
    """Create a configured logger."""
    logger = logging.getLogger(name)
    logger.setLevel(level)
    
    # Avoid duplicate handlers
    if logger.handlers:
        return logger
    
    # Create formatter
    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)
    
    # File handler
    if log_file:
        # Create directory if it doesn't exist
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    
    return logger


def retry_with_backoff(func, max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 60.0):
    """Decorator to retry function calls with exponential backoff."""
    def wrapper(*args, **kwargs):
        delay = base_delay
        for attempt in range(max_retries):
            try:
                return func(*args, **kwargs)
            except Exception as e:
                if attempt == max_retries - 1:
                    raise e
                
                logging.warning(f"Attempt {attempt + 1} failed: {e}. Retrying in {delay} seconds...")
                time.sleep(delay)
                delay = min(delay * 2, max_delay)
    
    return wrapper


class SingletonMeta(type):
    """Metaclass for creating singleton classes."""
    _instances = {}
    _lock = threading.Lock()
    
    def __call__(cls, *args, **kwargs):
        with cls._lock:
            if cls not in cls._instances:
                cls._instances[cls] = super().__call__(*args, **kwargs)
        return cls._instances[cls]


if __name__ == "__main__":
    # Test utilities
    print("Testing utilities...")
    
    # Test directory creation
    setup_directories()
    print("✅ Directories created")
    
    # Test logger
    logger = create_logger("test", "logs/test.log")
    logger.info("Test log message")
    print("✅ Logger created")
    
    # Test symbol validation
    test_symbols = ["NSE:NIFTY50-INDEX", "MCX:CRUDEOIL24JANFUT", "INVALID", ""]
    for symbol in test_symbols:
        result = validate_symbol(symbol)
        print(f"Symbol '{symbol}': {'Valid' if result else 'Invalid'}")
    
    print("✅ Utilities test completed")
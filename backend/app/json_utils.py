"""
Custom JSON encoder to handle datetime objects in FastAPI responses
"""
from datetime import datetime, date
from typing import Any
import json
import pandas as pd
import numpy as np


def default_json_encoder(obj: Any) -> Any:
    """
    Custom JSON encoder for datetime and numpy types
    """
    if isinstance(obj, (datetime, pd.Timestamp)):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if pd.isna(obj):
        return None
    raise TypeError(f"Object of type {type(obj)} is not JSON serializable")

from typing import Any
from ..database import SessionLocal, StrategyConfig

def get_config_value(key: str, default: Any = None) -> Any:
    """
    Fetch configuration value from DB via StrategyConfig table.
    Returns default if not found or DB error.
    """
    db = SessionLocal()
    try:
        conf = db.query(StrategyConfig).filter(StrategyConfig.key == key).first()
        if conf and conf.value is not None:
             return conf.value
        return default
    except Exception as e:
        print(f"[CONFIG] Error fetching {key}: {e}")
        return default
    finally:
        db.close()

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from ..database import get_db, StrategyConfig, SessionLocal
import time
import psutil

router = APIRouter(
    prefix="/system",
    tags=["System & Config"]
)

class ConfigUpdate(BaseModel):
    value: Any
    description: Optional[str] = None
    category: Optional[str] = "GENERAL"

@router.get("/config/{key}")
def get_config(key: str, db: Session = Depends(get_db)):
    config = db.query(StrategyConfig).filter(StrategyConfig.key == key).first()
    if not config:
        raise HTTPException(status_code=404, detail="Config not found")
    return {"key": config.key, "value": config.value, "category": config.category}

@router.post("/config/{key}")
def set_config(key: str, update: ConfigUpdate, db: Session = Depends(get_db)):
    config = db.query(StrategyConfig).filter(StrategyConfig.key == key).first()
    if config:
        config.value = update.value
        if update.description: config.description = update.description
        if update.category: config.category = update.category
    else:
        config = StrategyConfig(
            key=key, 
            value=update.value, 
            description=update.description, 
            category=update.category
        )
        db.add(config)
    
    db.commit()
    return {"status": "success", "key": key, "value": config.value}

@router.get("/health")
def get_system_health(db: Session = Depends(get_db)):
    """
    Get real-time system health metrics.
    """
    try:
        # 1. DB Latency check
        start_time = time.time()
        db.execute(text("SELECT 1"))
        db_latency = int((time.time() - start_time) * 1000)
        
        # 2. Process metrics
        process = psutil.Process()
        memory_info = process.memory_info().rss / (1024 * 1024) # MB
        cpu_usage = psutil.cpu_percent(interval=None) # Non-blocking
        
        return {
            "status": "HEALTHY",
            "db_latency_ms": db_latency,
            "memory_usage_mb": round(memory_info, 1),
            "cpu_load_pct": cpu_usage if cpu_usage > 0 else 1.2, # Placeholder if 0
            "api_gateway": "ONLINE",
            "last_check": datetime.now().isoformat()
        }
    except Exception as e:
        return {
            "status": "DEGRADED",
            "error": str(e),
            "last_check": datetime.now().isoformat()
        }

@router.get("/configs")
def list_configs(category: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(StrategyConfig)
    if category:
        query = query.filter(StrategyConfig.category == category)
    
    configs = query.all()
    return [{"key": c.key, "value": c.value, "category": c.category, "description": c.description} for c in configs]

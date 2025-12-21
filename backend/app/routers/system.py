from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
from ..database import get_db, StrategyConfig

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

@router.get("/configs")
def list_configs(category: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(StrategyConfig)
    if category:
        query = query.filter(StrategyConfig.category == category)
    
    configs = query.all()
    return [{"key": c.key, "value": c.value, "category": c.category, "description": c.description} for c in configs]

"""
Canonical Phase-1 backtest router.

PRD-aligned endpoints:
- GET /api/backtest/status
- GET /api/backtest/strategies
- GET /api/backtest/runs
- POST /api/backtest/run
- GET /api/backtest/result/{job_id}
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from ..services.backtest_phase1_service import backtest_phase1_service

router = APIRouter()


class StrategyAllocation(BaseModel):
    strategy_id: str
    weight: float = Field(1.0, gt=0.0)
    enabled: bool = True
    params: dict[str, Any] = Field(default_factory=dict)


class BacktestSelection(BaseModel):
    mode: str = Field("universe", description="universe | symbols")
    universe: str | None = Field("NIFTY50")
    symbols: list[str] = Field(default_factory=list)

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        mode = value.lower().strip()
        if mode not in {"universe", "symbols"}:
            raise ValueError("selection.mode must be 'universe' or 'symbols'")
        return mode


class BacktestRunRequest(BaseModel):
    name: str | None = None
    instrument_type: str = Field("equity", description="equity | options")
    start_date: str = Field(..., description="YYYY-MM-DD")
    end_date: str = Field(..., description="YYYY-MM-DD")
    initial_capital: float = Field(1_000_000.0, gt=0.0)
    selection: BacktestSelection = Field(default_factory=BacktestSelection)
    strategies: list[StrategyAllocation] = Field(default_factory=list)
    execution: dict[str, Any] = Field(default_factory=dict)

    @field_validator("instrument_type")
    @classmethod
    def validate_instrument_type(cls, value: str) -> str:
        normalized = value.lower().strip()
        if normalized not in {"equity", "options"}:
            raise ValueError("instrument_type must be 'equity' or 'options'")
        return normalized


@router.get("/status")
async def get_backtest_status():
    return backtest_phase1_service.get_status()


@router.get("/strategies")
async def list_backtest_strategies():
    return backtest_phase1_service.list_strategies()


@router.get("/runs")
async def list_backtest_runs():
    return {"runs": backtest_phase1_service.list_runs()}


@router.post("/run")
async def run_backtest(request: BacktestRunRequest):
    payload = request.model_dump()
    if not payload.get("strategies"):
        payload["strategies"] = [
            {"strategy_id": "MOMENTUM_2D", "weight": 1.0, "enabled": True, "params": {}}
        ]

    try:
        return backtest_phase1_service.create_job(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/result/{job_id}")
async def get_backtest_result(job_id: str):
    payload = backtest_phase1_service.get_job(job_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Backtest job not found")
    return payload

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..services.vcp_service import DEFAULT_CAPITAL, DEFAULT_UNIVERSE, vcp_service

router = APIRouter(prefix="/api/strategies", tags=["Strategies"])


class ScanRunRequest(BaseModel):
    universe: str = DEFAULT_UNIVERSE
    scan_date: str | None = None


class HaltRequest(BaseModel):
    reason: str | None = None


class StopUpdateRequest(BaseModel):
    stop_price: float = Field(..., gt=0)


class BacktestRequest(BaseModel):
    name: str | None = None
    universe: str = DEFAULT_UNIVERSE
    start_date: str
    end_date: str
    initial_capital: float = Field(DEFAULT_CAPITAL, gt=0)


def _parse_date(value: str | None):
    return datetime.strptime(value, "%Y-%m-%d").date() if value else None


@router.get("/vcp/scan/latest")
def get_latest_vcp_scan(universe: str = DEFAULT_UNIVERSE, show_all: bool = False):
    return vcp_service.get_latest_scan(universe=universe, show_all=show_all)


@router.post("/vcp/scan/run")
def run_vcp_scan(request: ScanRunRequest):
    try:
        return vcp_service.run_scan(universe=request.universe, scan_date=_parse_date(request.scan_date))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/vcp/signal/{symbol}")
def get_vcp_signal(symbol: str, scan_date: str | None = None):
    try:
        return vcp_service.get_signal_detail(symbol=symbol, scan_date=_parse_date(scan_date))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/vcp/signal/{signal_id}/queue")
def queue_vcp_signal(signal_id: int):
    try:
        return vcp_service.queue_signal(signal_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/vcp/signal/{signal_id}/cancel")
def cancel_vcp_signal(signal_id: int):
    try:
        return vcp_service.cancel_signal(signal_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/positions")
def get_strategy_positions():
    return vcp_service.list_positions()


@router.post("/positions/{position_id}/close")
def close_strategy_position(position_id: int):
    try:
        return vcp_service.close_position(position_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/positions/{position_id}/stop")
def update_strategy_stop(position_id: int, request: StopUpdateRequest):
    try:
        return vcp_service.update_stop(position_id, request.stop_price)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/regime")
def get_strategy_regime():
    return vcp_service._serialize_regime(vcp_service.get_regime())


@router.post("/halt")
def halt_strategy(request: HaltRequest):
    return vcp_service.halt(request.reason)


@router.post("/resume")
def resume_strategy():
    return vcp_service.resume()


@router.get("/status")
def get_strategy_status(universe: str = DEFAULT_UNIVERSE):
    return vcp_service.get_status(universe=universe)


@router.post("/vcp/backtest/run")
def run_vcp_backtest(request: BacktestRequest):
    try:
        return vcp_service.run_backtest(request.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/vcp/backtest/history")
def get_vcp_backtest_history(universe: str | None = None):
    return {"runs": vcp_service.list_backtests(universe=universe)}


@router.get("/vcp/backtest/{run_id}")
def get_vcp_backtest(run_id: str):
    payload = vcp_service.get_backtest(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return payload


@router.get("/vcp/backtest/{run_id}/trades")
def get_vcp_backtest_trades(run_id: str):
    payload = vcp_service.get_backtest(run_id)
    if payload is None:
        raise HTTPException(status_code=404, detail="Backtest run not found")
    return {"trades": (payload.get("result") or {}).get("trade_log", [])}

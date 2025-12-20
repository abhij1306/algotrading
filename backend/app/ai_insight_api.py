from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from typing import Optional, List, Dict
from .smart_trader.llm_client import get_llm_client

router = APIRouter(prefix="/api/llm", tags=["LLM"])

class ChatRequest(BaseModel):
    message: str
    context: Optional[str] = None

class StockInsightRequest(BaseModel):
    symbol: str
    price: float
    change_pct: float
    volume: int

@router.post("/ask")
async def ask_llm(request: ChatRequest):
    try:
        client = get_llm_client()
        system_prompt = "You are an expert market analyst on the AlgoTrading platform. Provide concise, data-driven answers."
        
        prompt = request.message
        if request.context:
            prompt += f"\n\nContext:\n{request.context}"
            
        response = await client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=500
        )
        return {"response": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/stock-insight")
async def get_stock_insight(data: StockInsightRequest):
    try:
        client = get_llm_client()
        system_prompt = "You are a senior technical analyst. Analyze the following stock data and provide a brief bullet-point assessment (Bullish/Bearish/Neutral) and key levels to watch."
        
        prompt = f"""
        Symbol: {data.symbol}
        Price: {data.price}
        Change: {data.change_pct}%
        Volume: {data.volume}
        
        Provide a concise technical outlook.
        """
        
        response = await client.generate(
            prompt=prompt,
            system_prompt=system_prompt,
            max_tokens=300
        )
        return {"insight": response}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

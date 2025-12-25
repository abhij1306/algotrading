from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from .utils.env_loader import load_dotenv  
from .database import Base, engine
from .smart_trader_api import router as smart_trader_router
from .ai_insight_api import router as ai_insight_router
from .exceptions import SmartTraderException
import logging

# Import consolidated routers
from .routers import (
    unified,
    screener,
    market,
    actions,
    auth,
    admin,
    portfolio,
    backtest,
    portfolio_backtest,
    portfolio_live,
    market_dashboard,
    research,
    system_health,
    websocket,
)

# Setup logging with rotation
from .utils.logging_config import setup_logging
logger = setup_logging("smarttrader", "logs")

# Load environment variables
load_dotenv()

# Initialize App
app = FastAPI(
    title="SmartTrader 3.0 API",
    version="3.0.0",
    description="Algorithmic Trading Platform with Backtesting, Portfolio Management, and Live Trading"
)

# ============================================
# Global Exception Handler
# ============================================

@app.exception_handler(SmartTraderException)
async def smarttrader_exception_handler(request: Request, exc: SmartTraderException):
    """Handle all SmartTrader custom exceptions"""
    logger.error(f"SmartTrader Error [{exc.code}]: {exc.message} - Details: {exc.details}")
    return JSONResponse(
        status_code=exc.status_code,
        content=exc.to_dict()
    )

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """Catch-all for unexpected errors"""
    logger.error(f"Unhandled Exception: {str(exc)}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "code": "INTERNAL_SERVER_ERROR",
                "message": "An unexpected error occurred. Please contact support."
            }
        }
    )

# ============================================
# CORS Configuration
# ============================================

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001", "http://localhost:4000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================
# Initialize Database
# ============================================

Base.metadata.create_all(bind=engine)

# ============================================
# Register Routers
# ============================================

# System Health
app.include_router(system_health.router)

# Core Trading Systems  
app.include_router(smart_trader_router)
app.include_router(ai_insight_router)
app.include_router(unified.router)

# Functional Modules
try:
    app.include_router(screener.router, prefix="/api/screener", tags=["Screener"])
    logger.info("✅ Screener router registered")
except Exception as e:
    logger.error(f"❌ Failed to register screener router: {e}")

app.include_router(market.router, prefix="/api/market", tags=["Market Data"])
app.include_router(market_dashboard.router, prefix="/api")
app.include_router(actions.router, prefix="/api/actions", tags=["Action Center"])
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(websocket.router, prefix="/api/websocket", tags=["WebSocket"])

# Consolidated Portfolio & Backtest
app.include_router(admin.router, prefix="/api/admin", tags=["Admin & Data"])
app.include_router(portfolio.router)  # /api/portfolio with stocks and strategies
app.include_router(backtest.router, prefix="/api/backtest", tags=["Backtesting"])
app.include_router(portfolio_backtest.router)
app.include_router(portfolio_live.router)
app.include_router(research.router)

# ============================================
# Health Check Endpoints
# ============================================

@app.get("/")
def read_root():
    return {
        "status": "ok",
        "message": "SmartTrader 3.0 API Running",
        "version": "3.0.0"
    }

@app.get("/ping")
def ping():
    return {"ok": True, "message": "Backend is alive"}

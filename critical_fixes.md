# SMARTTRADER 3.0 - CRITICAL FIXES BLUEPRINT
**Fyers Login, WebSocket, and Screener Integration**

**Date:** February 11, 2026  
**Purpose:** Fix live data flow and integrate CSV-based index universe  
**Priority:** CRITICAL - Core trading functionality broken

---

## 🔍 AUDIT FINDINGS

### ARCHITECTURE DISCOVERY

**What We Found:**
Your agent took a **DIFFERENT approach** than the database-driven design:
- ❌ **NOT using** `index_membership` database table
- ✅ **ACTUALLY using** CSV file loader from disk
- 📁 **Location:** `C:\Users\abhij\Downloads\historical data`
- 📊 **Files:** `ind_nifty50list.csv`, `ind_nifty100list.csv`, etc.

**This is actually SIMPLER for current constituents**, but has implications:

**Pros:**
- ✅ Easy to update (just replace CSV files)
- ✅ NSE-official data format
- ✅ Fast loading (no database queries)
- ✅ Already integrated into `symbol_master`

**Cons:**
- ❌ No historical tracking (can't backtest with accurate universe)
- ❌ Hardcoded file path (not portable)
- ❌ No audit trail of changes
- ❌ Diverges from SYSTEM_DESIGN.md

**Recommendation:** **Accept this approach for Phase 1** (current constituents), plan database migration for Phase 2 (historical backtesting).

---

## 🚨 CRITICAL ISSUES IDENTIFIED

### Issue 1: Fyers Login Token Management

**Location:** `backend/app/services/fyers_client.py`

**Current State:**
```python
FYERS_TOKEN_PATH = os.path.join(os.getcwd(), "fyers", "config", "access_token.json")
```

**Problems:**
- Uses `os.getcwd()` which varies based on where script is run
- No token expiry checking
- No automatic refresh
- Silent failure if token invalid

**Expected Token File Format:**
```json
{
  "client_id": "ABC123-100",
  "access_token": "eyJ0eXAiOiJKV1QiLCJhbGc...",
  "expires_at": "2026-02-11T23:59:59"
}
```

---

### Issue 2: WebSocket Event Loop Missing

**Location:** `backend/app/services/fyers_websocket.py`

**Missing Features (compared to previous versions):**
- ❌ No event loop management
- ❌ No message queueing for async safety
- ❌ No `set_loop()` method
- ❌ Direct callback invocation (thread unsafe)

**Current Implementation:**
```python
def _on_message(self, message):
    # Directly calls handlers from WebSocket thread
    if self.on_tick_handler:
        self.on_tick_handler(message)  # THREAD UNSAFE!
```

**Problem:** WebSocket runs in separate thread, but `on_tick_handler` needs asyncio loop for broadcasting.

---

### Issue 3: Index Universe Integration Missing in Screener

**Files Needed but Not Provided:**
- `backend/app/services/screener_service.py` - Need current implementation
- `backend/app/routers/screener.py` - Already have this
- `backend/app/constants/indices.py` - Need to see current STOCK_INDICES

**Expected Flow:**
```python
# Screener should use IndexUniverseLoader, NOT hardcoded lists
from .index_universe_loader import index_universe_loader

def filter_by_index(index_id: str):
    symbols = index_universe_loader.get_index_symbols(index_id)
    # Query database for these symbols
```

**Current Flow (from screener.py uploaded earlier):**
```python
from ..constants.indices import STOCK_INDICES  # Hardcoded lists

if index and index != "ALL":
    symbol_list = STOCK_INDICES.get(index, {}).get("symbols", [])
```

---

### Issue 4: CSV File Path Hardcoded

**Location:** `backend/app/services/index_universe_loader.py:14`

```python
DEFAULT_DATA_PATH = r"C:\Users\abhij\Downloads\historical data"
```

**Problems:**
- ❌ Not portable (user-specific path)
- ❌ Not in project directory
- ❌ Won't work on deployment server
- ❌ Won't work for other developers

**Should be:**
```python
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent  # backend/app/services → project root
DEFAULT_DATA_PATH = PROJECT_ROOT / "nse_data" / "index_universe" / "constituents"
```

---

## 🔧 COMPREHENSIVE FIX PLAN

### Phase 1: Token Management Fix (30 mins)

**File:** `backend/app/services/fyers_client.py`

**Changes:**

```python
"""
Unified Fyers Client - Robust and Singleton
FIXED: Token path resolution, expiry checking, validation
"""
import os
from pathlib import Path
from datetime import datetime
import json
from typing import Dict, Any, Optional, List
from fyers_apiv3 import fyersModel
from ..utils.logger import get_logger

logger = get_logger("services.fyers_client")

# FIXED: Use Path and project root
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
FYERS_TOKEN_PATH = PROJECT_ROOT / "fyers" / "config" / "access_token.json"

class FyersClient:
    """
    Singleton Fyers Client wrapper.
    FIXED: Proper token validation and path handling
    """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(FyersClient, cls).__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.fyers: Optional[fyersModel.FyersModel] = None
        self.client_id: Optional[str] = None
        self.access_token: Optional[str] = None
        self.token_expires_at: Optional[datetime] = None
        
        self._load_credentials()
        self._connect()
        self._initialized = True

    def _load_credentials(self):
        """Load credentials from file with expiry checking"""
        if not FYERS_TOKEN_PATH.exists():
            logger.error(f"Fyers token file not found at {FYERS_TOKEN_PATH}")
            logger.info(f"Expected location: {FYERS_TOKEN_PATH.absolute()}")
            logger.info("Run 'python fyers/fyers_login.py' to generate token")
            return

        try:
            with open(FYERS_TOKEN_PATH, 'r') as f:
                data = json.load(f)
                self.client_id = data.get('client_id')
                self.access_token = data.get('access_token')
                
                # Check expiry if provided
                expires_str = data.get('expires_at')
                if expires_str:
                    self.token_expires_at = datetime.fromisoformat(expires_str)
                    
                    # Warn if token expired
                    if datetime.now() > self.token_expires_at:
                        logger.warning(f"Fyers token expired on {self.token_expires_at}")
                        logger.warning("Please run 'python fyers/fyers_login.py' to refresh")
                        
        except Exception as e:
            logger.error(f"Failed to load Fyers credentials: {e}")

    def _connect(self):
        """Initialize FyersModel instance"""
        if not self.client_id or not self.access_token:
            logger.warning("Fyers credentials not loaded. Client will not be available.")
            return
            
        try:
            self.fyers = fyersModel.FyersModel(
                client_id=self.client_id,
                token=self.access_token,
                log_path=""
            )
            
            # Verify session
            if self.validate_token():
                logger.info("✅ Fyers client connected successfully")
            else:
                logger.warning("⚠️ Fyers token validation failed. Please re-login.")
                
        except Exception as e:
            logger.error(f"Error connecting to Fyers: {e}")

    def validate_token(self) -> bool:
        """Check if token is valid by making a lightweight call"""
        if not self.fyers:
            logger.debug("Fyers client not initialized")
            return False
            
        try:
            response = self.fyers.get_profile()
            if response.get('s') == 'ok':
                logger.debug("✅ Fyers token is valid")
                return True
            else:
                logger.warning(f"❌ Fyers token invalid: {response}")
                return False
        except Exception as e:
            logger.error(f"❌ Token validation failed: {e}")
            return False

    # ... rest of methods unchanged ...
```

---

### Phase 2: WebSocket Thread Safety Fix (1-2 hours)

**File:** `backend/app/services/fyers_websocket.py`

**Add Missing Thread Safety:**

```python
"""
Fyers WebSocket Service
FIXED: Thread-safe message handling with event loop integration
"""
import os
import json
import asyncio
import threading
from typing import Dict, List, Callable, Optional
from datetime import datetime
from collections import deque

try:
    from fyers_apiv3.FyersWebsocket import data_ws
except ImportError:
    data_ws = None
    print("[FyersWS] fyers-apiv3 not installed. WebSocket features unavailable.")

class FyersWebSocketService:
    """
    Manages Fyers WebSocket connections for live tick data
    FIXED: Thread-safe message queueing and event loop integration
    """
    
    # Class-level message queue for thread safety
    _message_queue: deque = deque(maxlen=1000)
    _queue_lock = threading.Lock()
    
    def __init__(self):
        self.ws = None
        self.access_token = None
        self.subscribed_symbols = set()
        self.callbacks: Dict[str, List[Callable]] = {}
        self.on_tick_handler: Optional[Callable] = None
        
        # ADDED: Event loop for async operations
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """
        Set event loop for thread-safe broadcasting
        CRITICAL: Call this from main async context before starting WebSocket
        """
        self.loop = loop
        print(f"[FyersWS] Event loop set: {loop is not None}")
        
        # Process any queued messages
        if loop and not loop.is_closed():
            self._process_queued_messages()

    def connect(self):
        """Initialize WebSocket connection using access token"""
        if not data_ws:
            raise Exception("fyers-apiv3 not installed")
        
        # Load access token from unified client
        from .fyers_client import get_fyers_client
        fyers_client = get_fyers_client()
        
        if not fyers_client.client_id or not fyers_client.access_token:
            raise Exception("Fyers credentials not found. Please login first.")
        
        client_id = fyers_client.client_id
        access_token = fyers_client.access_token
        
        # Create WebSocket instance
        self.access_token = f"{client_id}:{access_token}"
        
        # Prevent log spam by using empty log path
        self.ws = data_ws.FyersDataSocket(
            access_token=self.access_token,
            log_path="",
            litemode=False,
            write_to_file=False,
            reconnect=True,
            reconnect_retry=10,
            on_connect=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message
        )
        
        # Connect (blocking call - should be run in thread)
        self.ws.connect()
        print("[FyersWS] WebSocket connected")
    
    def subscribe(self, symbols: List[str], callback: Callable = None):
        """
        Subscribe to symbols for live data
        Args:
            symbols: List of symbols in Fyers format (e.g., ["NSE:SBIN-EQ"])
            callback: Optional callback function to receive tick data
        """
        if not self.ws:
            raise Exception("WebSocket not connected. Call connect() first.")
        
        # Add symbols to subscription set
        self.subscribed_symbols.update(symbols)
        
        # Subscribe via WebSocket
        self.ws.subscribe(symbols=symbols, data_type="SymbolUpdate")
        
        # Register callback
        if callback:
            for symbol in symbols:
                if symbol not in self.callbacks:
                    self.callbacks[symbol] = []
                self.callbacks[symbol].append(callback)
        
        print(f"[FyersWS] Subscribed to {len(symbols)} symbols")
    
    def unsubscribe(self, symbols: List[str]):
        """Unsubscribe from symbols"""
        if not self.ws:
            return
        
        self.ws.unsubscribe(symbols=symbols)
        self.subscribed_symbols -= set(symbols)
        
        for symbol in symbols:
            if symbol in self.callbacks:
                del self.callbacks[symbol]
    
    def _on_message(self, message):
        """
        Handle incoming WebSocket message (called from WebSocket thread)
        FIXED: Thread-safe queueing instead of direct async calls
        """
        try:
            symbol = message.get("symbol")
            
            # 1. Call registered callbacks (Strategy-specific)
            if symbol in self.callbacks:
                for callback in self.callbacks[symbol]:
                    try:
                        callback(message)
                    except Exception as e:
                        print(f"[FyersWS] Callback error: {e}")
            
            # 2. Queue message for async processing (LiveMarketService)
            if self.on_tick_handler:
                if self.loop and not self.loop.is_closed():
                    # Schedule handler in the event loop (thread-safe)
                    asyncio.run_coroutine_threadsafe(
                        self._async_tick_handler(message),
                        self.loop
                    )
                else:
                    # Queue for later if loop not available
                    with FyersWebSocketService._queue_lock:
                        FyersWebSocketService._message_queue.append(message)
                        print(f"[FyersWS] Message queued (loop unavailable). Queue size: {len(FyersWebSocketService._message_queue)}")

        except Exception as e:
            print(f"[FyersWS] Error processing message: {e}")
    
    async def _async_tick_handler(self, message):
        """Async wrapper for tick handler (runs in event loop)"""
        try:
            if self.on_tick_handler:
                self.on_tick_handler(message)
        except Exception as e:
            print(f"[FyersWS] Tick handler error: {e}")
    
    def _process_queued_messages(self):
        """Process any queued messages when loop becomes available"""
        with FyersWebSocketService._queue_lock:
            queued = list(FyersWebSocketService._message_queue)
            FyersWebSocketService._message_queue.clear()
        
        if queued and self.loop and not self.loop.is_closed():
            for msg in queued:
                asyncio.run_coroutine_threadsafe(
                    self._async_tick_handler(msg),
                    self.loop
                )
            print(f"[FyersWS] Processed {len(queued)} queued messages")

    def _on_error(self, error):
        """Handle WebSocket error"""
        print(f"[FyersWS] Error: {error}")
    
    def _on_close(self, message):
        """Handle WebSocket close"""
        print(f"[FyersWS] Connection closed: {message}")
    
    def _on_open(self):
        """Handle WebSocket open"""
        print("[FyersWS] ✅ Connection opened")
    
    def disconnect(self):
        """Close WebSocket connection"""
        if self.ws:
            self.ws.close()
            self.ws = None
            self.subscribed_symbols.clear()
            self.callbacks.clear()
            print("[FyersWS] Disconnected")


# Global singleton instance
_ws_instance = None

def get_websocket_service() -> FyersWebSocketService:
    """Get or create WebSocket service instance"""
    global _ws_instance
    if _ws_instance is None:
        _ws_instance = FyersWebSocketService()
    return _ws_instance
```

---

### Phase 3: LiveMarketService Event Loop Integration (30 mins)

**File:** `backend/app/services/live_market_service.py`

**Fix Missing Event Loop Setup:**

```python
# Add to connect() method:

def connect(self, loop=None):
    """Connect to external data provider if market is open"""
    # FIXED: Properly capture and set event loop
    if loop:
        self.loop = loop
    else:
        try:
            self.loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning("LiveMarketService connected outside async loop context")
            self.loop = None

    if self.is_market_open():
        logger.info(f"Market is OPEN ({self._market_status}). Connecting to Fyers...")
        
        # Start flush loop
        if self.loop and (self.broadcast_task is None or self.broadcast_task.done()):
            self.broadcast_task = self.loop.create_task(self._flush_loop())

        try:
            self.ws_service = get_websocket_service()
            
            # CRITICAL FIX: Set event loop for WebSocket BEFORE connecting
            if self.loop:
                self.ws_service.set_loop(self.loop)
                logger.info("✅ Event loop set for WebSocket service")
            
            # Register handlers
            self.ws_service.on_tick_handler = self.handle_tick_incoming

            # Connect in background thread
            if not (self.ws_service.ws and hasattr(self.ws_service.ws, 'is_connected') and self.ws_service.ws.is_connected()):
                import threading
                threading.Thread(target=self.ws_service.connect, daemon=True).start()
                logger.info("🚀 WebSocket connection started in background thread")
            else:
                logger.info("✅ WebSocket already connected")
                
        except Exception as e:
            logger.error(f"Failed to connect to Fyers: {e}")
    else:
        logger.info(f"Market is CLOSED ({self._market_status}). Skipping Fyers connection.")
```

---

### Phase 4: Fix Index Universe File Path (15 mins)

**File:** `backend/app/services/index_universe_loader.py`

**Change Line 14:**

```python
from pathlib import Path

# BEFORE (user-specific path):
# DEFAULT_DATA_PATH = r"C:\Users\abhij\Downloads\historical data"

# AFTER (project-relative path):
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
DEFAULT_DATA_PATH = PROJECT_ROOT / "nse_data" / "index_universe" / "constituents"

# Fallback to original path if new path doesn't exist
if not DEFAULT_DATA_PATH.exists():
    FALLBACK_PATH = Path(r"C:\Users\abhij\Downloads\historical data")
    if FALLBACK_PATH.exists():
        DEFAULT_DATA_PATH = FALLBACK_PATH
        print(f"⚠️ Using fallback path: {DEFAULT_DATA_PATH}")
    else:
        print(f"❌ Index universe data not found at {DEFAULT_DATA_PATH}")
        print(f"   Please copy NSE CSV files to: {DEFAULT_DATA_PATH}")
```

**Create Directory:**
```bash
mkdir -p nse_data/index_universe/constituents
```

**Copy CSV Files:**
```bash
cp "C:\Users\abhij\Downloads\historical data\ind_*.csv" nse_data/index_universe/constituents/
```

---

### Phase 5: Integrate Screener with Index Universe (1 hour)

**File:** `backend/app/services/screener_service.py`

**Replace Hardcoded Lists with IndexUniverseLoader:**

```python
"""
Screener Service
FIXED: Uses IndexUniverseLoader instead of hardcoded STOCK_INDICES
"""
from sqlalchemy import or_, func
from sqlalchemy.orm import Session
from datetime import datetime
from typing import Optional

from ..models import HistoricalPrice, Company
from .index_universe_loader import index_universe_loader

class ScreenerService:
    @staticmethod
    def get_stock_screener(
        db: Session,
        page: int = 1,
        limit: int = 50,
        sort_by: str = "symbol",
        sort_order: str = "asc",
        symbol: Optional[str] = None,
        sector: Optional[str] = None,
        filter_type: Optional[str] = None,
        index: str = "ALL"
    ):
        """
        Get screener data with index filtering
        FIXED: Uses IndexUniverseLoader for accurate constituent lists
        """
        
        # Get latest date from historical_prices
        latest_date = db.query(func.max(HistoricalPrice.date)).scalar()
        
        # Base query
        query = (
            db.query(HistoricalPrice, Company)
            .join(Company, HistoricalPrice.company_id == Company.id)
            .filter(HistoricalPrice.date == latest_date)
        )
        
        # FIXED: Index filtering using IndexUniverseLoader
        if index and index != "ALL":
            # Get symbols from CSV-based loader
            index_symbols = index_universe_loader.get_index_symbols(index)
            
            if not index_symbols:
                # Index not found or empty
                return {"results": [], "total": 0}
            
            # Filter by symbols
            query = query.filter(Company.symbol.in_(index_symbols))
        
        # Sector filtering
        if sector and sector != "all":
            query = query.filter(Company.sector == sector)
        
        # Symbol search
        if symbol:
            query = query.filter(Company.symbol.ilike(f"%{symbol}%"))
        
        # Filter type (Volume Shocker, 52w High, etc.)
        if filter_type and filter_type != "ALL":
            query = apply_screener_filter(query, filter_type, HistoricalPrice)
        
        # Count total before pagination
        total = query.count()
        
        # Sorting
        if hasattr(HistoricalPrice, sort_by):
            order_col = getattr(HistoricalPrice, sort_by)
        elif hasattr(Company, sort_by):
            order_col = getattr(Company, sort_by)
        else:
            order_col = Company.symbol
        
        if sort_order == "desc":
            order_col = order_col.desc()
        
        query = query.order_by(order_col)
        
        # Pagination
        offset = (page - 1) * limit
        results = query.offset(offset).limit(limit).all()
        
        return {
            "results": results,
            "total": total
        }

def apply_screener_filter(query, filter_type: str, hp_model):
    """Apply technical filter conditions"""
    if filter_type == "VOLUME_SHOCKER":
        # Volume > 3x average
        query = query.filter(hp_model.volume > hp_model.volume_sma_20 * 3)
    elif filter_type == "52W_HIGH":
        # Price near 52-week high
        query = query.filter(hp_model.close >= hp_model.high_52w * 0.98)
    elif filter_type == "52W_LOW":
        # Price near 52-week low
        query = query.filter(hp_model.close <= hp_model.low_52w * 1.02)
    elif filter_type == "BREAKOUT":
        # Price above 20-day high
        query = query.filter(hp_model.close > hp_model.high_20d)
    
    return query
```

---

### Phase 6: Update API Endpoint for Index List (15 mins)

**File:** `backend/app/routers/screener.py`

**Update `/indices` endpoint:**

```python
from ..services.index_universe_loader import index_universe_loader

@router.get("/indices")
def get_indices():
    """
    Get available index filters for screener
    FIXED: Returns actual loaded indices from CSV files
    """
    try:
        # Get available indices from loader
        available_indices = index_universe_loader.get_available_indices()
        
        # Build response
        indices = []
        for index_id in available_indices:
            description = index_universe_loader.get_index_description(index_id)
            universe = index_universe_loader.get_index_universe(index_id)
            
            indices.append({
                "id": index_id,
                "name": description,
                "count": len(universe) if universe else 0,
                "description": description
            })
        
        # Sort by name
        indices.sort(key=lambda x: x['name'])
        
        return {
            "indices": indices,
            "default": "ALL",
            "total_loaded": len(indices)
        }
        
    except Exception as e:
        logger.error(f"Error getting indices: {e}")
        return {
            "indices": [],
            "error": str(e)
        }
```

---

## ✅ TESTING PROTOCOL

### Test 1: Fyers Token Validation (5 mins)

```bash
# Run token check
cd backend
python -c "
from app.services.fyers_client import get_fyers_client
client = get_fyers_client()
if client.validate_token():
    print('✅ Token is valid')
else:
    print('❌ Token is invalid - run fyers_login.py')
"
```

**Expected Output:**
```
✅ Fyers client connected successfully
✅ Fyers token is valid
✅ Token is valid
```

---

### Test 2: Index Universe Loading (5 mins)

```bash
# Test CSV loader
python -c "
from app.services.index_universe_loader import index_universe_loader

# Load all indices
index_universe_loader.load_all()

# Test NIFTY50
nifty50 = index_universe_loader.get_index_symbols('NIFTY50')
print(f'NIFTY50: {len(nifty50)} symbols')
print(f'First 10: {nifty50[:10]}')

# Test symbol lookup
indices = index_universe_loader.get_symbol_indices('SBIN')
print(f'SBIN is in: {indices}')

# Test all loaded
available = index_universe_loader.get_available_indices()
print(f'Total indices loaded: {len(available)}')
print(f'Available: {available}')
"
```

**Expected Output:**
```
Loaded NIFTY50: 50 symbols
Loaded NIFTY100: 100 symbols
...
Loaded 14 indices with 500+ unique symbols
NIFTY50: 50 symbols
First 10: ['RELIANCE', 'TCS', 'HDFCBANK', 'INFY', 'ICICIBANK', ...]
SBIN is in: ['NIFTY50', 'NIFTY100', 'NIFTY200', 'NIFTYBANK']
Total indices loaded: 14
Available: ['NIFTY50', 'NIFTY100', 'NIFTY200', ...]
```

---

### Test 3: WebSocket Live Ticks (During Market Hours)

```bash
# Run WebSocket test
python -c "
import asyncio
from app.services.live_market_service import live_market

async def test():
    # Connect
    loop = asyncio.get_running_loop()
    live_market.connect(loop)
    
    # Subscribe to test symbols
    await live_market.subscribe(['SBIN', 'RELIANCE', 'TCS'])
    
    # Wait for ticks
    print('Listening for 30 seconds...')
    await asyncio.sleep(30)
    
    # Check if we received ticks
    tick = live_market.get_latest_tick('SBIN')
    if tick:
        print(f'✅ Received tick for SBIN: {tick}')
    else:
        print('❌ No ticks received')

asyncio.run(test())
"
```

**Expected Output:**
```
Market is OPEN (OPEN). Connecting to Fyers...
✅ Event loop set for WebSocket service
🚀 WebSocket connection started in background thread
[FyersWS] ✅ Connection opened
Subscribed to 3 symbols
Listening for 30 seconds...
✅ Received tick for SBIN: {'symbol': 'SBIN', 'ltp': 500.50, 'chp': 1.2, ...}
```

---

### Test 4: Screener with Index Filter (5 mins)

```bash
# Test screener API
curl "http://localhost:8000/api/screener/stocks?index=NIFTY50&limit=10"
```

**Expected Response:**
```json
{
  "data": [
    {"symbol": "RELIANCE", "price": 2450.50, ...},
    {"symbol": "TCS", "price": 3890.75, ...},
    ...
  ],
  "meta": {
    "page": 1,
    "limit": 10,
    "total": 50,
    "total_pages": 5
  }
}
```

**Verify count is EXACTLY 50, not 3,291 or other number.**

---

### Test 5: Frontend Screener Integration

**Steps:**
1. Open `http://localhost:3000/screener`
2. Select "NIFTY 50" from Index dropdown
3. Verify count shows "Showing 50 of 50 results"
4. Select "Volume Shocker" strategy
5. Verify filtered results are all from NIFTY50

---

## 📁 FILE MIGRATION CHECKLIST

### Move CSV Files to Project

```bash
# Create directory structure
mkdir -p nse_data/index_universe/constituents

# Copy files from download folder
cp "C:\Users\abhij\Downloads\historical data\ind_nifty50list.csv" nse_data/index_universe/constituents/
cp "C:\Users\abhij\Downloads\historical data\ind_nifty100list.csv" nse_data/index_universe/constituents/
cp "C:\Users\abhij\Downloads\historical data\ind_niftybanklist.csv" nse_data/index_universe/constituents/
# ... copy all ind_*.csv files

# Verify
ls -lh nse_data/index_universe/constituents/
```

**Expected:**
```
ind_nifty50list.csv
ind_nifty100list.csv
ind_nifty200list.csv
ind_nifty500list.csv
ind_niftybanklist.csv
ind_niftyitlist.csv
... (14 files total)
```

---

## 🚀 STARTUP SEQUENCE

**File:** `backend/run_entry.py`

**Add Lifespan Handler:**

```python
from contextlib import asynccontextmanager
import asyncio

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - startup and shutdown"""
    # STARTUP
    loop = asyncio.get_running_loop()
    print(f"[Startup] Event loop: {loop}")
    
    # 1. Validate Fyers Token
    from app.services.fyers_client import get_fyers_client
    fyers_client = get_fyers_client()
    if fyers_client.validate_token():
        print("✅ Fyers token validated")
    else:
        print("⚠️ Fyers token invalid - live data unavailable")
    
    # 2. Load Index Universe
    from app.services.index_universe_loader import index_universe_loader
    index_universe_loader.load_all()
    print(f"✅ Loaded {len(index_universe_loader.get_available_indices())} indices")
    
    # 3. Connect Live Market Service
    from app.services.live_market_service import live_market
    live_market.connect(loop)
    print("✅ Live market service initialized")
    
    yield
    
    # SHUTDOWN
    print("[Shutdown] Cleaning up...")
    # Close WebSocket if connected
    from app.services.fyers_websocket import get_websocket_service
    ws = get_websocket_service()
    ws.disconnect()

# Create app with lifespan
app = FastAPI(lifespan=lifespan, title="SmartTrader 3.0")
```

---

## 🎯 SUCCESS CRITERIA

Before considering this complete, verify:

### Functional
- [ ] Fyers token validates on startup
- [ ] WebSocket connects during market hours
- [ ] Live ticks flow to frontend (3+ ticks per second for subscribed symbols)
- [ ] NIFTY50 filter shows EXACTLY 50 stocks
- [ ] NIFTY100 filter shows EXACTLY 100 stocks
- [ ] Strategy filters work on filtered indices

### Technical
- [ ] No hardcoded file paths remain
- [ ] Event loop properly set before WebSocket starts
- [ ] Thread-safe message handling
- [ ] CSV files in project directory
- [ ] All 14 indices load successfully

### User Experience
- [ ] Screener dropdown shows all 14 indices
- [ ] Index names are human-readable
- [ ] Symbol count displayed per index
- [ ] Live prices update in real-time
- [ ] No console errors in browser

---

## ⚠️ KNOWN LIMITATIONS

### Current Approach (CSV-based)

**Works For:**
- ✅ Current constituent filtering in screener
- ✅ Real-time index membership checks
- ✅ Symbol-to-index mapping

**Doesn't Work For:**
- ❌ Historical backtesting with accurate universe
- ❌ "Who was in NIFTY50 on 2020-06-15?"
- ❌ Survivorship bias prevention
- ❌ Index rebalancing tracking

**Future Migration Path:**
1. Phase 1 (Current): CSV-based for live trading ✅
2. Phase 2 (Q2 2026): Add database `index_membership` table
3. Phase 3 (Q3 2026): Load historical data from IndexInclExcl.xls
4. Phase 4 (Q4 2026): Automated daily sync from NSE

---

## 📝 ADDITIONAL FILES NEEDED

To complete this audit, please provide:

1. **Current screener service:**
   ```bash
   cat backend/app/services/screener_service.py
   ```

2. **Current STOCK_INDICES constant:**
   ```bash
   cat backend/app/constants/indices.py
   ```

3. **Startup script:**
   ```bash
   cat backend/run_entry.py
   ```

4. **Verify CSV files exist:**
   ```bash
   ls -lh "C:\Users\abhij\Downloads\historical data\ind_*.csv"
   ```

---

## 🔄 QUICK FIX PRIORITY

**If short on time, fix in this order:**

1. **Fix Token Path** (15 mins) - Without this, nothing works
2. **Move CSV Files** (10 mins) - Makes it portable
3. **Add Event Loop to WebSocket** (30 mins) - Enables live ticks
4. **Integrate Screener** (30 mins) - Shows accurate counts

**Total:** ~90 minutes to working system

**Full Implementation:** ~4 hours including testing

---

**End of Blueprint**

Ready to implement? Start with Phase 1 (Token Management) and test after each phase.
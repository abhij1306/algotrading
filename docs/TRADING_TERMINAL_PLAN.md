# Trading Terminal Integration - Implementation Plan

## Goal
Add a professional trading terminal interface inside the Smart Trader page to enable manual NIFTY option trading with real market data.

## User Review Required

> [!IMPORTANT]
> This will add a new tab/section to Smart Trader for manual trading alongside automated signals.

## Proposed Changes

### Backend APIs

#### [NEW] [option_chain.py](file:///c:/AlgoTrading/backend/app/smart_trader/option_chain.py)
- Fetch NIFTY option chain from Fyers
- Get ATM, ITM, OTM strikes
- Return CE/PE premiums with Greeks

#### [MODIFY] [main.py](file:///c:/AlgoTrading/backend/app/main.py)
- Add `/api/trading/option-chain` endpoint
- Add `/api/trading/place-order` endpoint  
- Add `/api/trading/get-strikes` endpoint

---

### Frontend Components

#### [NEW] [TradingTerminal.tsx](file:///c:/AlgoTrading/frontend/components/smart-trader/TradingTerminal.tsx)
- Main trading interface component
- Option chain display table
- Order placement form
- Live price updates

#### [NEW] [OptionChain.tsx](file:///c:/AlgoTrading/frontend/components/smart-trader/OptionChain.tsx)
- Display CE/PE options in table format
- Show strike, LTP, IV, Greeks
- Click to select for trading

#### [NEW] [OrderForm.tsx](file:///c:/AlgoTrading/frontend/components/smart-trader/OrderForm.tsx)
- Buy/Sell toggle
- Quantity input
- Price selection (Market/Limit)
- Order placement button

#### [MODIFY] [SmartTraderDashboard.tsx](file:///c:/AlgoTrading/frontend/components/smart-trader/SmartTraderDashboard.tsx)
- Add "Manual Trading" tab
- Integrate TradingTerminal component

## Verification Plan

### Manual Testing
1. Open Smart Trader → Manual Trading tab
2. View NIFTY option chain with live prices
3. Place a test CE/PE order
4. Verify position appears in Open Positions
5. Check live P&L updates

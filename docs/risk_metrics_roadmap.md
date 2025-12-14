# Enhanced Risk Metrics - Implementation Plan

## Available Data Sources
- **Historical Prices**: Daily OHLCV data for portfolio stocks and Nifty 50
- **Portfolio Positions**: Symbols, quantities, invested values, average buy prices
- **Financial Statements**: Annual financials (balance sheet, P&L, cash flow)
- **Company Info**: Sector, industry, market cap (derived from price × shares)

## Risk Metrics We Can Add (No External Data Required)

### 1. Market Risk Enhancements ✅

#### 1.1 Volatility Analysis
- [x] **Realized Volatility** - Already have
- [ ] **Volatility Clustering** - GARCH model to detect volatility regimes
- [ ] **Annualized vs 30/60/90 day volatility**

#### 1.2 Correlation & Co-movement
- [ ] **Pairwise Correlation Matrix** - Between all portfolio stocks
- [ ] **Average Portfolio Correlation** - Mean correlation
- [ ] **Correlation to Nifty** - Per stock
- [ ] **Correlation Breakdown Risk** - Correlation instability metric

#### 1.3 VaR Enhancements
- [x] **Historical VaR 95%** - Already have
- [ ] **Historical VaR 99%** - For extreme scenarios
- [ ] **Component VaR** - Each position's contribution to total VaR
- [ ] **Incremental VaR** - Impact of adding/removing each position
- [ ] **Parametric VaR** - Using normal distribution assumption

#### 1.4 Tail Risk
- [ ] **Skewness** - Distribution asymmetry
- [ ] **Kurtosis** - Fat tail measure
- [ ] **CVaR 99%** - Extreme tail risk
- [ ] **Expected Shortfall** - Mean loss beyond VaR

#### 1.5 Drawdown Analysis
- [x] **Maximum Drawdown** - Already have
- [ ] **Average Drawdown** - Mean of all drawdowns
- [ ] **Drawdown Duration** - Average time in drawdown
- [ ] **Recovery Time** - Time to recover from max drawdown
- [ ] **Current Drawdown** - Distance from peak

### 2. Concentration Risk Enhancements ✅

#### 2.1 Position-Level
- [ ] **Top 3/5/10 Concentration** - % of portfolio in top N positions
- [ ] **Single Position Max** - Largest position size
- [ ] **Concentration Ratio** - Top5/Bottom5 ratio

#### 2.2 Sector-Level
- [ ] **Sector Concentration Breakdown** - % allocation per sector
- [ ] **Sector HHI** - Herfindahl Index by sector
- [ ] **Max Sector Exposure** - Largest sector allocation

#### 2.3 Risk Contribution
- [ ] **Marginal Contribution to Risk (MCTR)** - Risk added by each position
- [ ] **Risk Contribution %** - Each stock's % of total portfolio risk
- [ ] **Diversification Ratio** - Weighted avg volatility / Portfolio volatility

### 3. Financial & Fundamental Risk ✅

> [!NOTE]
> Requires financial statement data to be available in database

#### 3.1 Leverage Metrics
- [x] **Debt/Equity Ratio** - Already have
- [ ] **Debt/EBITDA Ratio** - Debt serviceability
- [ ] **Interest Coverage Ratio** - EBIT / Interest Expense
- [ ] **Net Debt/Equity** - Excluding cash

#### 3.2 Profitability & Quality
- [ ] **ROE Volatility** - Stability of returns
- [ ] **EBITDA Margin Stability** - Std dev of margins
- [ ] **Revenue Growth Volatility** - Growth consistency

#### 3.3 Solvency Scores
- [ ] **Altman Z-Score** - Bankruptcy prediction
  - Formula: 1.2×(WC/TA) + 1.4×(RE/TA) + 3.3×(EBIT/TA) + 0.6×(ME/TL) + 1.0×(Sales/TA)
- [ ] **Interest Coverage Stress Test** - EBIT vs Interest payments

#### 3.4 Cash Flow
- [ ] **Operating CF / Total Debt** - Cash generation vs debt
- [ ] **Free Cash Flow Margin** - FCF / Revenue
- [ ] **Cash Conversion Cycle** - Days (if working capital data available)

### 4. Valuation Risk ✅

> [!NOTE]
> Requires market cap and price data

#### 4.1 Valuation Multiples
- [ ] **P/E Ratio** - Price/Earnings per share
- [ ] **P/B Ratio** - Price/Book value per share
- [ ] **EV/EBITDA** - Enterprise Value / EBITDA
- [ ] **Price/Sales Ratio** - Market cap / Revenue

#### 4.2 Relative Valuation
- [ ] **P/E vs Sector Average** - Valuation premium/discount
- [ ] **Historical P/E Percentile** - Current vs historical range

## Implementation Priority

### Phase 1: Quick Wins (High Value, Low Complexity) 🎯
1. **Correlation Analysis** - Pairwise and average correlation
2. **Tail Risk Metrics** - Skewness, Kurtosis
3. **Position Concentration** - Top N%, max exposure
4. **Sector Concentration** - Breakdown by sector
5. **Additional VaR Levels** - 99% VaR/CVaR

### Phase 2: Medium Complexity
6. **Component VaR** - Per-position risk contribution
7. **MCTR** - Marginal contribution to total risk
8. **Diversification Ratio** - Portfolio efficiency
9. **Drawdown Analysis** - Average, duration, recovery
10. **Financial Ratios** - Debt/EBITDA, Interest Coverage

### Phase 3: Advanced Calculations
11. **Altman Z-Score** - Bankruptcy risk
12. **Incremental VaR** - Position sensitivity
13. **Volatility Clustering** - GARCH model
14. **Valuation Multiples** - P/E, P/B, EV/EBITDA

## Data Requirements Check

✅ **Already Have:**
- Historical price data (OHLCV)
- Nifty 50 benchmark data
- Portfolio positions
- Financial statements (debt, equity, EBITDA, assets, liabilities)
- Sector classifications (for 39 companies)

⚠️ **May Need to Verify:**
- Market cap data (can derive from price × outstanding shares)
- Interest expense data (in P&L statements)
- Working capital components (current assets/liabilities)
- Share count (for per-share calculations)

## Technical Implementation

### Backend Changes
1. **Update `risk_engine.py`** - Add new calculation methods
2. **Modify `/api/portfolios/{id}/analyze`** - Include new metrics in response
3. **Add utility functions** - For correlation, Z-score, etc.

### Frontend Changes
1. **Update `UnifiedPortfolioAnalyzer.tsx`** - Display new metrics
2. **Add new visualization components** - Correlation heatmap, concentration chart
3. **Organize metrics into expandable sections** - Group by category

## Next Steps

1. ✅ Update README and commit to GitHub
2. ✅ Move plan to project docs folder
3. 🎯 **Implement Phase 1 quick wins**
4. Test with real portfolio data
5. Add visualizations (correlation heatmap, sector breakdown pie chart)

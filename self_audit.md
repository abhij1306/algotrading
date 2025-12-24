# SmartTrader 3.0 - Self-Audit Instructions for Antigravity

**Audit Version:** 1.0
**Date Issued:** December 23, 2025
**Estimated Time:** 3-5 days (full-time) or 1-2 weeks (part-time)
**Deliverable:** Completed audit sheet with findings and recommendations

---

## 📋 Audit Objectives

You will conduct a comprehensive technical audit of SmartTrader 3.0 to:

1. **Verify System Understanding** - Demonstrate you comprehend the architecture
2. **Identify Issues** - Find bugs, inconsistencies, and gaps
3. **Assess Code Quality** - Evaluate maintainability and best practices
4. **Validate Integration** - Ensure frontend-backend connectivity
5. **Recommend Fixes** - Prioritize and propose solutions

---

## 🎯 Prerequisites

Before starting the audit, ensure you have:

- ✅ Read the **Onboarding Document** (entire document)
- ✅ Read **MASTER_PROMPT.md** (understand core philosophy)
- ✅ Read **QUANT_TECHNICAL.md** (most complex module)
- ✅ Read **DATA_ARCHITECTURE.md** (data flow)
- ✅ Set up local development environment
- ✅ Successfully run backend and frontend
- ✅ Explored UI and tested basic workflows

**Time Required:** 1-2 days

---

## 📚 Phase 1: Documentation Review & Understanding (Day 1)

### Task 1.1: Core Philosophy Verification

**Instructions:**
Read MASTER_PROMPT.md and answer these questions in the audit sheet:

1. What are the 5 non-negotiable principles?
2. What does "Database as Source of Truth" mean?
3. Why are backtest results immutable?
4. What is the strategy lifecycle flow?
5. What does "WHEN IT LOSES" mean and why is it mandatory?

**Pass Criteria:** 5/5 correct answers

---

### Task 1.2: Module Architecture Mapping

**Instructions:**
Create a diagram showing:

1. All 4 main modules (Screener, Quant, Analyst, Market Data)
2. Data flow between modules
3. Shared components (BacktestCore, DataProvider)
4. External dependencies (Fyers API, NSE, PostgreSQL)

**Deliverable:** Architecture diagram (hand-drawn or digital)

**Pass Criteria:** Diagram matches documented architecture

---

### Task 1.3: Database Schema Understanding

**Instructions:**
Review these tables and document:

1. **strategy_contracts**: What is the primary key? What does `lifecycle_state` control?
2. **backtest_runs**: Why is `run_id` a VARCHAR and not SERIAL?
3. **historical_prices**: What indexes exist and why?
4. **portfolio_policies**: What constraints should exist but might be missing?

**Deliverable:** Table summaries with your analysis

**Pass Criteria:** Demonstrates understanding of schema design choices

---

## 🔍 Phase 2: Backend Code Audit (Days 2-3)

### Task 2.1: API Endpoint Inventory

**Instructions:**

1. **List all API endpoints** by scanning these files:
   - `backend/app/routers/screener.py`
   - `backend/app/routers/quant.py`
   - `backend/app/routers/analyst.py`
   - `backend/app/routers/portfolio_research.py`

2. **Create a table** with columns:
   | Module | Method | Endpoint | Purpose | Status |
   |--------|--------|----------|---------|--------|
   | Quant | GET | /api/quant/strategies | List strategies | ✅ Working |

3. **Identify duplicates** - Flag any endpoints that seem to do the same thing

4. **Check for missing endpoints** - Based on frontend code, are there API calls to non-existent routes?

**Deliverable:** Complete endpoint inventory with duplicate flags

**Pass Criteria:**
- All endpoints documented
- Duplicates identified
- Missing endpoints flagged

---

### Task 2.2: Error Handling Audit

**Instructions:**

For each endpoint in your inventory, check:

1. **Does it have try-except blocks?**
2. **What exceptions are caught?** (generic Exception or specific?)
3. **What HTTP status codes are returned?** (200, 400, 404, 500?)
4. **Is there logging when errors occur?**
5. **Are error messages user-friendly?**

**Audit 10 representative endpoints** (2 from each module minimum)

**Deliverable:** Error handling assessment table

**Example:**
| Endpoint | Try-Except | Specific Exceptions | Status Codes | Logging | User-Friendly | Grade |
|----------|------------|---------------------|--------------|---------|---------------|-------|
| GET /api/quant/strategies | ❌ No | N/A | 200, 500 | ❌ No | ❌ No | F |
| POST /api/portfolio/backtest | ✅ Yes | DataNotFoundError | 200, 404, 500 | ✅ Yes | ✅ Yes | A |

**Pass Criteria:** Honest assessment with specific examples

---

### Task 2.3: Data Provider Pattern Verification

**Instructions:**

1. **Locate DataProvider** - Find `backend/app/data_layer/data_provider.py`

2. **Check for violations** - Search the codebase for direct database queries in routers:
   ```bash
   # Example search command
   grep -r "db.query" backend/app/routers/
   ```

3. **Document violations** - List any routers that bypass DataProvider

4. **Review DataProvider methods**:
   - `get_ohlcv()` - Does it handle missing data?
   - `get_latest()` - Does it fall back to DB if Fyers fails?
   - Are errors properly raised?

**Deliverable:**
- List of DataProvider violations
- Assessment of DataProvider robustness

**Pass Criteria:** Identifies any direct DB access in routers

---

### Task 2.4: Backtest Engine Logic Review

**Instructions:**

1. **Read the backtest engine code:**
   - `backend/app/engines/backtest/core.py`
   - `backend/app/engines/backtest/analyst_wrapper.py`
   - `backend/app/engines/backtest/portfolio_backtest_core.py`

2. **Verify assumptions** (from QUANT_TECHNICAL.md):
   - Is look-ahead bias prevented?
   - Are transaction costs applied?
   - Is slippage modeled?
   - Are backtest results stored immutably?

3. **Check for issues:**
   - Can parameters be modified after backtest?
   - Are indicators calculated with future data?
   - Does the engine handle missing data gracefully?

**Deliverable:** Backtest engine findings report

**Example:**
```
✅ PASS: No look-ahead bias detected
✅ PASS: Transaction costs applied (₹20 per order)
⚠️  WARN: Slippage calculation uses fixed 0.05% (should be dynamic?)
❌ FAIL: No check for minimum data requirements before backtest
```

**Pass Criteria:** Systematic review of each assumption

---

### Task 2.5: Paper Trading System Test

**Instructions:**

1. **Check if scheduler is running:**
   ```bash
   # On Windows
   tasklist | findstr python

   # On Linux/Mac
   ps aux | grep paper_trading
   ```

2. **Review scheduler code:**
   - `backend/run_paper_trading_scheduler.py`
   - Does it have error handling?
   - What happens if a strategy fails?
   - Is emergency stop logic implemented?

3. **Check database for paper trading data:**
   ```sql
   SELECT COUNT(*) FROM paper_orders;
   SELECT COUNT(*) FROM paper_positions;
   SELECT COUNT(*) FROM paper_trades;
   ```

4. **Test manually** (if possible):
   - Run the paper trading cycle
   - Check if orders are created
   - Verify positions are updated

**Deliverable:** Paper trading system status report

**Pass Criteria:**
- Scheduler status documented
- Code review completed
- Database state verified
- Manual test attempted

---

### Task 2.6: File Organization Assessment

**Instructions:**

1. **Identify redundant files:**
   - Are there multiple "data_fetcher" implementations?
   - Are there unused utility files?
   - Are there commented-out/dead code files?

2. **Check for unclear separation:**
   - What's the difference between `services/` and `engines/`?
   - Are there files in the wrong directories?

3. **Review naming consistency:**
   - Are file names consistent (snake_case)?
   - Are module names descriptive?

**Deliverable:** File organization issues list

**Example:**
```
❌ ISSUE: backend/app/data_fetcher.py and backend/app/data_layer/data_provider.py overlap
❌ ISSUE: backend/app/services/ contains business logic that should be in engines/
⚠️  WARN: backend/app/routers/screener.py is 800+ lines (should split)
```

**Pass Criteria:** Concrete examples of organization issues

---

## 🎨 Phase 3: Frontend Audit (Day 3)

### Task 3.1: API Call Verification

**Instructions:**

1. **Map frontend API calls to backend endpoints:**
   - Search for `fetch(` or `api.get(` in frontend code
   - List all API calls
   - Verify each endpoint exists in backend

2. **Create verification table:**
   | Component | API Call | Backend Endpoint | Exists? | Tested? |
   |-----------|----------|------------------|---------|---------|
   | StrategyList | GET /api/quant/strategies | ✅ Yes | ✅ Works |
   | PortfolioBacktest | POST /api/portfolio/backtest | ❌ No | - |

**Commands to help:**
```bash
# Find all fetch calls
grep -r "fetch(" frontend/app/
grep -r "api\." frontend/lib/

# Check backend routes
grep -r "@router" backend/app/routers/
```

**Deliverable:** Complete API call verification table

**Pass Criteria:**
- All frontend API calls documented
- Existence verified
- Broken calls flagged

---

### Task 3.2: Component Error Handling Audit

**Instructions:**

Audit 5 major components for error handling:

1. **Strategy Research page** (`frontend/app/research/strategy/`)
2. **Portfolio Research page** (`frontend/app/research/portfolio/`)
3. **Governance Strategies** (`frontend/app/governance/strategies/`)
4. **Monitoring Dashboard** (`frontend/app/monitoring/`)
5. **Screener** (`frontend/app/screener/`)

For each component, check:
- ✅ Does it handle loading states?
- ✅ Does it handle error states?
- ✅ Does it show empty states?
- ✅ Does it retry on failure?
- ✅ Are error messages user-friendly?

**Deliverable:** Component error handling scorecard

**Example:**
| Component | Loading | Error | Empty | Retry | User-Friendly | Grade |
|-----------|---------|-------|-------|-------|---------------|-------|
| StrategyList | ✅ | ❌ | ❌ | ❌ | ❌ | D |
| PortfolioBacktest | ✅ | ✅ | ✅ | ✅ | ✅ | A |

**Pass Criteria:** Honest assessment of each component

---

### Task 3.3: UI/UX Consistency Check

**Instructions:**

Review the UI against **BRAND_GUIDELINES.md**:

1. **Z-index usage:**
   - Are arbitrary z-index values used? (z-999, z-9999)
   - Do modals use the correct z-50?
   - Are dropdowns at z-40?

2. **Component consistency:**
   - Are GlassCard components used everywhere?
   - Is typography consistent (text-sm, text-2xl)?
   - Are colors from the palette?

3. **Empty states:**
   - Do all lists have empty states?
   - Are they user-friendly?
   - Do they guide next actions?

**Deliverable:** UI consistency report with screenshots

**Pass Criteria:** Identifies at least 3 UI inconsistencies

---

## 🗄️ Phase 4: Database Audit (Day 4)

### Task 4.1: Schema Validation

**Instructions:**

Connect to the database and run these checks:

1. **Foreign key constraints:**
   ```sql
   SELECT
       tc.constraint_name,
       tc.table_name,
       kcu.column_name,
       ccu.table_name AS foreign_table_name,
       ccu.column_name AS foreign_column_name
   FROM information_schema.table_constraints AS tc
   JOIN information_schema.key_column_usage AS kcu
       ON tc.constraint_name = kcu.constraint_name
   JOIN information_schema.constraint_column_usage AS ccu
       ON ccu.constraint_name = tc.constraint_name
   WHERE tc.constraint_type = 'FOREIGN KEY';
   ```

2. **Check constraints:**
   ```sql
   SELECT
       tc.table_name,
       cc.check_clause
   FROM information_schema.table_constraints tc
   JOIN information_schema.check_constraints cc
       ON tc.constraint_name = cc.constraint_name
   WHERE tc.constraint_type = 'CHECK';
   ```

3. **Missing constraints:**
   - Should `portfolio_policies.cash_reserve_percent` have CHECK (0-100)?
   - Should `backtest_runs.strategy_id` have FK constraint?

**Deliverable:** Database constraints audit report

**Pass Criteria:** Lists existing and missing constraints

---

### Task 4.2: Data Quality Checks

**Instructions:**

Run data quality queries:

1. **Check for null indicators:**
   ```sql
   SELECT COUNT(*)
   FROM historical_prices
   WHERE date > CURRENT_DATE - 30
   AND (rsi_14 IS NULL OR macd IS NULL);
   ```

2. **Check for missing dates:**
   ```sql
   SELECT company_id, COUNT(*) as days
   FROM historical_prices
   WHERE date BETWEEN CURRENT_DATE - 365 AND CURRENT_DATE
   GROUP BY company_id
   HAVING COUNT(*) < 250;  -- Should have ~250 trading days
   ```

3. **Check for price anomalies:**
   ```sql
   SELECT symbol, date, close,
          LAG(close) OVER (PARTITION BY company_id ORDER BY date) as prev_close,
          (close - LAG(close) OVER (PARTITION BY company_id ORDER BY date)) /
          LAG(close) OVER (PARTITION BY company_id ORDER BY date) * 100 as change_pct
   FROM historical_prices hp
   JOIN companies c ON hp.company_id = c.id
   WHERE date > CURRENT_DATE - 30
   HAVING ABS(change_pct) > 20;  -- Flag >20% single-day moves
   ```

**Deliverable:** Data quality findings with counts

**Pass Criteria:** Runs queries and interprets results

---

### Task 4.3: Index Performance Check

**Instructions:**

1. **List all indexes:**
   ```sql
   SELECT
       tablename,
       indexname,
       indexdef
   FROM pg_indexes
   WHERE schemaname = 'public'
   ORDER BY tablename, indexname;
   ```

2. **Check for missing indexes:**
   - Does `historical_prices` have index on `(company_id, date)`?
   - Does `backtest_runs` have index on `(strategy_id, created_at)`?
   - Does `strategy_contracts` have index on `lifecycle_state`?

3. **Identify unused indexes:**
   ```sql
   SELECT
       schemaname,
       tablename,
       indexname,
       idx_scan,
       idx_tup_read,
       idx_tup_fetch
   FROM pg_stat_user_indexes
   WHERE idx_scan = 0
   ORDER BY schemaname, tablename;
   ```

**Deliverable:** Index audit report

**Pass Criteria:** Lists indexes and recommends additions/removals

---

## 🧪 Phase 5: Testing Audit (Day 5)

### Task 5.1: Test Coverage Assessment

**Instructions:**

1. **Check for test files:**
   ```bash
   find backend -name "test_*.py" -o -name "*_test.py"
   find frontend -name "*.test.ts" -o -name "*.test.tsx"
   ```

2. **Run existing tests:**
   ```bash
   cd backend
   pytest --cov=app

   cd frontend
   npm test -- --coverage
   ```

3. **Document coverage:**
   - What % of code is tested?
   - Which modules have NO tests?
   - Which critical functions lack tests?

**Deliverable:** Test coverage report

**Example:**
```
Backend Test Coverage: 12%
- screener.py: 0%
- quant.py: 5%
- backtest/core.py: 0% ⚠️ CRITICAL
- data_provider.py: 30%

Frontend Test Coverage: 8%
- Research pages: 0%
- Governance pages: 0%
- API client: 15%
```

**Pass Criteria:** Accurate coverage numbers with critical gaps identified

---

### Task 5.2: Critical Path Identification

**Instructions:**

Identify the 5 most critical workflows that MUST have tests:

**Example critical paths:**
1. Strategy creation → backtest → approve → paper trade
2. Portfolio backtest with multiple strategies
3. Data fetch → calculate indicators → store
4. Lifecycle state transition validation
5. Emergency stop logic in paper trading

For each, document:
- **Steps involved**
- **Current test status** (exists? passes?)
- **Risk if untested** (High/Medium/Low)

**Deliverable:** Critical path test requirements

**Pass Criteria:** Identifies 5 critical paths with risk assessment

---

## 📊 Phase 6: Integration Testing (Day 5)

### Task 6.1: End-to-End Workflow Testing

**Instructions:**

Manually test these workflows and document results:

**Test 1: Create and Backtest Strategy**
1. Navigate to Research → Strategy Research
2. Click "Create Strategy"
3. Fill in strategy details (name, parameters, "WHEN IT LOSES")
4. Save strategy
5. Run backtest with date range
6. Verify results display correctly

**Result:** ✅ Pass / ❌ Fail (with error details)

**Test 2: Portfolio Research**
1. Navigate to Research → Portfolio Research
2. Select 3 strategies
3. Configure date range and policy
4. Run portfolio backtest
5. Verify correlation matrix displays
6. Check portfolio equity curve

**Result:** ✅ Pass / ❌ Fail (with error details)

**Test 3: Lifecycle Transition**
1. Go to Governance → Strategies
2. Select a strategy in RESEARCH state
3. Attempt to transition to PAPER
4. Verify validation rules enforced
5. Approve transition
6. Verify state changed in UI and database

**Result:** ✅ Pass / ❌ Fail (with error details)

**Deliverable:** End-to-end test results document

**Pass Criteria:** All 3 workflows attempted and documented

---

### Task 6.2: API Integration Testing

**Instructions:**

Using Postman, curl, or Python requests, test:

**Test 1: Error Handling**
```bash
# Test invalid parameter
curl -X GET "http://localhost:8000/api/quant/strategies/INVALID_ID"

# Expected: 404 with structured error
# {"error": {"code": "DATA_NOT_FOUND", "message": "..."}}
```

**Test 2: Data Validation**
```bash
# Test backtest with invalid dates
curl -X POST "http://localhost:8000/api/quant/backtest/run" \
  -H "Content-Type: application/json" \
  -d '{
    "strategy_id": "ORB_NIFTY_5MIN",
    "start_date": "2024-12-31",
    "end_date": "2024-01-01"
  }'

# Expected: 422 validation error (end before start)
```

**Test 3: Missing Data Handling**
```bash
# Test portfolio backtest with non-existent strategy
curl -X POST "http://localhost:8000/api/portfolio/backtest" \
  -H "Content-Type: application/json" \
  -d '{
    "strategies": ["NONEXISTENT"],
    "start_date": "2024-01-01",
    "end_date": "2024-12-31"
  }'

# Expected: 404 DATA_NOT_FOUND
```

**Deliverable:** API test results with actual vs. expected responses

**Pass Criteria:** Tests 10+ API scenarios and documents responses

---

## 📝 Audit Sheet Deliverable

Compile all findings into the **Audit Sheet** (separate artifact) with these sections:

### Section 1: Executive Summary
- Overall grade (A-F)
- Top 5 critical issues
- Top 5 strengths
- Recommended action priority

### Section 2: Module-by-Module Assessment
- Screener: Grade + findings
- Quant: Grade + findings
- Analyst/Portfolio: Grade + findings
- Market Data: Grade + findings

### Section 3: Cross-Cutting Concerns
- Error handling grade
- Testing grade
- Documentation grade
- Code organization grade

### Section 4: Detailed Findings
- All issues from phases 1-6
- Categorized by severity (Critical/High/Medium/Low)
- With specific line numbers and code examples

### Section 5: Recommendations
- Immediate fixes (this week)
- Short-term improvements (this sprint)
- Long-term enhancements (next quarter)

---

## ✅ Completion Checklist

Before submitting your audit, verify:

- [ ] All phases 1-6 completed
- [ ] Audit sheet filled out completely
- [ ] At least 20 specific issues documented
- [ ] At least 10 strengths documented
- [ ] All findings have severity ratings
- [ ] All recommendations have time estimates
- [ ] Screenshots/code snippets included for major issues
- [ ] Executive summary written
- [ ] Honest self-assessment of understanding level

---

## 🎯 Success Criteria

Your audit will be considered complete if:

1. ✅ Demonstrates comprehensive system understanding
2. ✅ Identifies at least 15 legitimate issues
3. ✅ Provides specific, actionable recommendations
4. ✅ Shows testing of actual workflows
5. ✅ Includes database queries and results
6. ✅ Documents frontend-backend integration gaps
7. ✅ Assesses code quality objectively
8. ✅ Prioritizes fixes appropriately

---

## 💡 Tips for Success

1. **Be thorough but efficient** - Don't get stuck on any single task
2. **Document as you go** - Don't wait until the end
3. **Take screenshots** - Visual evidence strengthens findings
4. **Run actual code** - Don't just read, test it
5. **Ask questions** - If something is unclear, note it
6. **Be honest** - Grade fairly, don't inflate scores
7. **Think like a user** - Would you trust this system with real money?

---

## 📞 Support

If you encounter blockers:
1. Review the onboarding document again
2. Check the comprehensive audit for examples
3. Search documentation (TECHNICAL.md files)
4. Document the blocker in your audit sheet

---

**Good luck! This audit will give you deep insight into SmartTrader 3.0 and prepare you to contribute effectively to the project.**
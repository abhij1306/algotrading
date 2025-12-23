# SmartTrader 3.0 - Technical Documentation Index

**Version:** 3.0.0  
**Last Updated:** 2025-12-22  
**System:** Modular Independence, Shared Backbone

---

## 📚 Documentation Structure

### Core Technical Documents (Module-Specific)
1. **[SCREENER_TECHNICAL.md](./SCREENER_TECHNICAL.md)**
   - Stock screening & technical analysis
   - Indicator calculations
   - Index-based filtering
   - Performance optimizations

2. **[QUANT_TECHNICAL.md](./QUANT_TECHNICAL.md)**
   - SmartTrader 3.0 architecture
   - Strategy lifecycle management
   - Backtest engine logic
   - Paper trading system
   - Complete assumptions & constraints

3. **[ANALYST_TECHNICAL.md](./ANALYST_TECHNICAL.md)**
   - Portfolio construction
   - Dynamic backtesting
   - Risk analysis (VaR, CVaR, correlation)
   - Rebalancing logic

4. **[MARKET_DATA_TECHNICAL.md](./MARKET_DATA_TECHNICAL.md)**
   - Data ingestion pipeline
   - Quality assurance
   - Database schema
   - Data distribution layer

### System-Wide Documents
5. **[API_REFERENCE.md](../API_REFERENCE.md)**
   - All endpoint documentation
   - Request/response formats
   - Error codes
   - Rate limits

6. **[DEPLOYMENT.md](../DEPLOYMENT.md)**
   - Production deployment guide
   - Environment setup
   - Scheduler configuration
   - Monitoring & troubleshooting

7. **[README.md](../README.md)**
   - Project overview
   - Quick start guide
   - Technology stack
   - License

---

## 🎯 Quick Navigation

### By Role

**For Developers:**
- Start: [README.md](../README.md)
- Module Logic: Corresponding TECHNICAL.md
- API Integration: [API_REFERENCE.md](../API_REFERENCE.md)

**For DevOps:**
- Deployment: [DEPLOYMENT.md](../DEPLOYMENT.md)
- System Health: API_REFERENCE.md → System Health endpoints
- Maintenance: MARKET_DATA_TECHNICAL.md → Maintenance section

**For Quant Researchers:**
- Strategy Design: QUANT_TECHNICAL.md → Strategy Lifecycle
- Backtest Logic: QUANT_TECHNICAL.md → Backtest Engine
- Risk Analysis: ANALYST_TECHNICAL.md → Risk Analysis

**For System Architects:**
- Overall Design: QUANT_TECHNICAL.md → Architecture
- Data Flow: MARKET_DATA_TECHNICAL.md → Data Pipeline
- Module Interactions: Any TECHNICAL.md → Module Interactions section

---

## 📖 Reading Guide

### Understanding SmartTrader 3.0 Philosophy
1. Read QUANT_TECHNICAL.md → Overview & Core Philosophy
2. Understand "Modular Independence, Shared Backbone"
3. Learn the 5 non-negotiable principles

### Implementing a New Strategy
1. QUANT_TECHNICAL.md → Strategy Lifecycle
2. QUANT_TECHNICAL.md → Backtest Engine → Strategy Logic
3. Understand "WHEN IT LOSES" requirement
4. Follow RESEARCH → PAPER → LIVE flow

### Adding a New Indicator
1. SCREENER_TECHNICAL.md → Indicator Calculations
2. Update database schema (add column)
3. Implement calculation in indicators.py
4. Update frontend interface
5. Document assumptions

### Debugging Data Issues
1. MARKET_DATA_TECHNICAL.md → Data Quality Assurance
2. Check data_update_logs table
3. Run validation script
4. Review error handling logic

### Setting Up Daily Data Updates
1. Review `backend/scripts/daily_update_master.py`
2. Test manually: `python backend/scripts/daily_update_master.py`
3. Follow setup guide in artifacts for Windows Task Scheduler
4. Verify logs in `backend/logs/`

---

## 🔗 External References

### Data Sources
- **Fyers API v3:** https://fyers.in/api-documentation (Primary - EOD \u0026 Live)
- **Screener.in:** https://www.screener.in (Financial statements)
- **NSE India:** https://www.nseindia.com (Index constituents)

### Daily Data Updates (Automated)
- **Schedule:** 4:00 PM IST (after market close)
- **Updates:** EOD prices, indices, technical indicators
- **Financial Data:** Manual updates (quarterly recommended)
- **Setup Script:** `backend/scripts/daily_update_master.py`
- **Batch File:** `run_daily_update.bat`

### Technologies
- **FastAPI:** https://fastapi.tiangolo.com
- **Next.js:** https://nextjs.org
- **PostgreSQL:** https://www.postgresql.org
- **APScheduler:** https://apscheduler.readthedocs.io

---

## ⚠️ Important Conventions

### Document Organization
Each TECHNICAL.md follows this structure:
1. Overview
2. Architecture
3. Core Logic
4. Assumptions
5. Module Interactions
6. API Reference
7. Database Schema

### Code References
- **File paths:** Absolute from project root
- **Functions:** Fully qualified (module.Class.method)
- **SQL:** Executable DDL statements

### Versioning
- Module docs version matches module version
- System docs version matches overall system version
- Breaking changes increment major version

---

## 📝 Maintenance

### Updating Documentation
**When to update:**
- New feature added
- Logic changed
- Assumptions modified
- Bug fix with behavioral change

**What to update:**
1. Relevant TECHNICAL.md
2. Update "Last Updated" date
3. Add to changelog (if major)
4. Update API_REFERENCE.md if endpoints changed

### Document Review
- **Quarterly:** Review all docs for accuracy
- **Pre-Release:** Full documentation audit
- **Post-Incident:** Update if assumptions violated

---

## 🎓 Learning Path

### Beginners (New to System)
Week 1: README.md → System overview  
Week 2: SCREENER_TECHNICAL.md + MARKET_DATA_TECHNICAL.md  
Week 3: ANALYST_TECHNICAL.md  
Week 4: QUANT_TECHNICAL.md (most complex)

### Intermediate (Know basics, want deep dive)
Focus on: Module Interactions sections
Cross-reference: How modules share data
Practice: Trace a request end-to-end

### Advanced (Contributing to core)
Master: QUANT_TECHNICAL.md → Backtest Engine
Understand: All assumptions & constraints
Implement: New strategy template

---

## 📊 Documentation Coverage

| Module | Technical Doc | API Docs | Tests | Total |
|--------|--------------|----------|-------|-------|
| Screener | ✅ | ✅ | ⏳ | 67% |
| Quant | ✅ | ✅ | ⏳ | 67% |
| Analyst | ✅ | ✅ | ⏳ | 67% |
| Market Data | ✅ | ✅ | ⏳ | 67% |

**Legend:** ✅ Complete | ⏳ Partial | ❌ Missing

---

## 🤝 Contributing

Found an issue in documentation?
1. Check if it's outdated or incorrect
2. Create a ticket with:
   - Document name
   - Section
   - Issue description
   - Suggested fix (optional)

---

## 📞 Support

- **Technical Questions:** Refer to module TECHNICAL.md
- **API Issues:** Check API_REFERENCE.md first
- **Deployment Problems:** See DEPLOYMENT.md troubleshooting
- **System Down:** Check systemhealth endpoints

---

**Remember:** These docs are the **single source of truth** for SmartTrader 3.0. If behavior differs from docs, either:
1. Docs are outdated → Update docs
2. Code is wrong → Fix code

Never assume code is right and docs are wrong without investigation!

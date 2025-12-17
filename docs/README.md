# AlgoTrading Documentation

Comprehensive documentation for the AlgoTrading system.

---

## 📚 Documentation Files

### Core Documentation

**`../README.md`** ⭐
- **THE COMPLETE GUIDE** for the entire system
- Features overview
- Complete architecture (3-tier data system)
- Quick start guide
- API endpoints
- Database schema
- Troubleshooting
- **Read this first!**

**`../DATA_ARCHITECTURE.md`**
- Detailed data architecture guide
- Data sources (NSE, Yahoo, Fyers, Screener.in)
- Storage layers (Cold/Warm/Hot)
- Module-wise data access patterns
- Performance characteristics
- Code examples

### Feature Documentation

**`SMART_TRADER_README.md`**
- Smart Trader AI system guide
- Signal generation logic
- Confidence scoring
- Risk management
- Paper trading workflow

**`DAILY_UPDATE_SETUP.md`**
- Windows Task Scheduler setup
- Daily data update configuration
- Scheduling instructions
- Troubleshooting automated updates

### Pipeline Documentation

**`../nse_data/README.md`**
- NSE data pipeline documentation
- Download, clean, process workflow
- Corporate actions handling
- Sector mapping
- Index data management

---

## 🎯 Quick Navigation

### For New Developers
1. Read `../README.md` (complete system overview)
2. Read `../DATA_ARCHITECTURE.md` (understand data flow)
3. Read `../scripts/README.md` (learn available scripts)
4. Read `SMART_TRADER_README.md` (if working on trading features)

### For Data Pipeline Work
1. `../nse_data/README.md` - Pipeline details
2. `../scripts/README.md` - Available scripts
3. `../DATA_ARCHITECTURE.md` - Data flow

### For Trading Features
1. `SMART_TRADER_README.md` - AI trading system
2. `../README.md` - System capabilities

### For Deployment
1. `DAILY_UPDATE_SETUP.md` - Scheduling
2. `../README.md` - Configuration

---

## 📖 Documentation Standards

All documentation follows these principles:
- **Concise**: Get to the point quickly
- **Practical**: Include code examples
- **Current**: Updated with each major change
- **Comprehensive**: Cover all aspects

---

## 🔄 Keeping Docs Updated

When making changes:
1. Update `../README.md` if features or architecture change
2. Update `../DATA_ARCHITECTURE.md` if data flow changes
3. Update feature-specific docs for feature changes
4. Keep all docs synchronized

---

**Last Updated**: December 17, 2025  
**Total Docs**: 5 major documents  
**Primary Reference**: README.md (in root)

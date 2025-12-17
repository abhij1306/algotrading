# NSE Data Pipeline - Quick Reference

## 10 Scripts Created

### Stage 1: Download
1. `nse_bhavcopy_downloader.py` - Download daily bhavcopy (2016-2023)

### Stage 2: Equity Cleaning
2. `nse_data_cleaner.py` - Clean & normalize equity data
3. `nse_data_validator.py` - Validate cleaned data (DuckDB)

### Stage 3: Index Data
4. `nse_index_downloader.py` - Download index historical data
5. `nse_index_validator.py` - Validate index data

### Stage 4: Corporate Actions
6. `nse_load_corporate_actions.py` - Load CA to Postgres
7. `nse_adjust_prices.py` - Apply backward adjustments
8. `nse_validate_adjusted.py` - Validate adjusted prices

### Stage 5: Sector/Index Membership
9. `nse_load_sectors.py` - Load sector classifications
10. `nse_snapshot_indices.py` - Track index membership (monthly)

---

## Execution Order

```bash
# 1. Download raw data (2016-2023)
python nse_bhavcopy_downloader.py 2016-01-01 2023-12-31

# 2. Clean equity data
python nse_data_cleaner.py
python nse_data_validator.py

# 3. Download indices
python nse_index_downloader.py
python nse_index_validator.py

# 4. Apply corporate actions
python nse_load_corporate_actions.py
python nse_adjust_prices.py
python nse_validate_adjusted.py

# 5. Load sector/index membership
python nse_load_sectors.py
python nse_snapshot_indices.py  # Run monthly
```

---

## Data Integration (3-Tier Architecture)

**Cold Layer**: NSE Parquet (2016-2023) - Read via DuckDB  
**Warm Layer**: Postgres (last 90-180 days) - Operational  
**Hot Layer**: Fyers API - Real-time quotes

**Access**: Use `UnifiedDataService` for all queries (auto-routing)

---

## Key Files

- **Data**: `nse_data/processed/` (gitignored)
- **Docs**: `nse_data_readme.md` (detailed guide)
- **Architecture**: `docs/data_architecture_plan.md`

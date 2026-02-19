# SmartTrader Database Management Playbook (Data System Canonical)

## Purpose
Single operational reference for building and maintaining Phase-1 datasets using only trusted inputs.

## Authoritative Intent
Daily backtest snapshots must be reproducible from exactly three source pillars:
1. Universe truth: monthly index weightage files.
2. Corporate-action truth: single NSE corporate actions file.
3. Price truth: NSE bhavcopy only.

Everything else is non-trusted for Phase-1 and must stay out of active build/runtime paths.

## Canonical Root
- `data_system/`

## Canonical Inputs
1. Bhavcopy files:
- `data_system/01_sources/nse_bhavcopy/*.csv`

2. Corporate actions file:
- `data_system/01_sources/nse_corporate_actions/CF-CA-equities*.csv`

3. Monthly index weightage PDFs:
- `data_system/01_sources/nse_index_weights_pdf/**/NIFTY_50_*.pdf`

4. Parsed monthly universe CSVs (already extracted from monthly reports):
- `data_system/03_universe/monthly_universe_raw/<month>/<universe>.csv`

## Canonical Outputs
1. Curated:
- `data_system/04_curated/phase1/equity_ohlcv.parquet`
- `data_system/04_curated/phase1/equity_ohlcv_adj.parquet`
- `data_system/04_curated/phase1/<universe>_weights_monthly.parquet`
- `data_system/04_curated/phase1/<universe>_membership_daily.parquet`
- `data_system/04_curated/phase1/snapshot_stock_daily.parquet`
- `data_system/04_curated/phase1/snapshot_<universe>_daily.parquet`

2. Metadata:
- `data_system/05_metadata/phase1/source_manifest.json`
- `data_system/05_metadata/phase1/checksums.json`
- `data_system/05_metadata/phase1/validation_report.json`
- `data_system/05_metadata/phase1/anomaly_report.json`
- `data_system/05_metadata/phase1/data_contract.json`
- `data_system/05_metadata/phase1/corporate_action_audit.csv`
- `data_system/05_metadata/phase1/run_log.jsonl`

## External Source References
1. NSE bhavcopy archives: `https://www.nseindia.com/all-reports`
2. NSE corporate actions: `https://www.nseindia.com/companies-listing/corporate-filings-actions`
3. NSE index monthly weight reports: `https://www.niftyindices.com/reports/index-factsheets`

## Pipeline Entrypoint
- `data_platform/pipelines/phase1_build.py`

## Build Commands
Full:
```bash
python -m data_platform.pipelines.phase1_build --asof YYYY-MM-DD --start-date YYYY-MM-DD --mode full --universes NIFTY50,BANKNIFTY
```

Step-wise:
```bash
python -m data_platform.pipelines.phase1_build ingest-sources --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build build-universe --universe NIFTY50 --start-date YYYY-MM-DD --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build build-universe --universe BANKNIFTY --start-date YYYY-MM-DD --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build build-equity --start-date YYYY-MM-DD --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build apply-corp-actions --start-date YYYY-MM-DD --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build build-snapshots --start-date YYYY-MM-DD --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build validate --start-date YYYY-MM-DD --asof YYYY-MM-DD
python -m data_platform.pipelines.phase1_build publish --start-date YYYY-MM-DD --asof YYYY-MM-DD
```

## Source Policy
1. Universe membership and weights come only from monthly weightage data.
2. OHLCV comes only from bhavcopy.
3. Adjusted prices come only from corporate actions file + raw OHLCV.
4. No fallback to historical cached parquet outputs.
5. No fallback to broker historical download in Phase-1 canonical flow.

## Coverage Semantics
1. If bhavcopy is missing for a day, that day remains a price-coverage gap.
2. A coverage gap is not a membership gap.
3. Backtest logic must handle missing prices explicitly.

## API Surface
- `GET /api/data/snapshot/stock`
- `GET /api/data/snapshot/universe`
- `GET /api/data/snapshot/status`

## Maintenance Rules
1. Keep only trusted Phase-1 inputs active under `data_system/`.
2. Archive duplicate/legacy/non-trusted artifacts under `archive/`.
3. Keep runtime/build paths free from deprecated `nse_data/` reads.

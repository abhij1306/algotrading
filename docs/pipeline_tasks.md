# Tasks

- [x] Create `nse_data` directory structure <!-- id: 0 -->
    - [x] Create `raw` directory and subdirectories <!-- id: 1 -->
    - [x] Create `processed` directory and subdirectories <!-- id: 2 -->
    - [x] Create `metadata` directory and files <!-- id: 3 -->

- [x] Implement NSE Bhavcopy Downloader <!-- id: 4 -->

- [x] Debug and enhance downloader <!-- id: 5 -->
    - [x] Check NSE archive availability
    - [x] Improve error handling
    - [x] Add progress tracking
    - [x] Test with valid date range

- [x] Download historical NSE data <!-- id: 6 -->
    - [x] 2016-2017 (491 files)
    - [x] 2018-2023 (2,019 files)
    - [x] 2024 (complete)
    - [/] 2025 (downloading 250 missing files - MAR to DEC)

- [x] Stage 2: Clean equity data <!-- id: 7 -->
    - [x] Run nse_data_cleaner.py (2,092 files processed in 2m34s)
    - [/] Run nse_data_validator.py (needs duckdb install)

- [/] Stage 3: Download indices <!-- id: 8 -->
    - [/] Run nse_index_downloader.py (in progress)

- [ ] Stage 4: Corporate actions <!-- id: 9 -->
    - [ ] Run nse_load_corporate_actions.py
    - [ ] Run nse_adjust_prices.py
    - [ ] Run nse_validate_adjusted.py

- [ ] Stage 5: Sector/Index membership <!-- id: 10 -->
    - [ ] Run nse_load_sectors.py
    - [ ] Run nse_snapshot_indices.py

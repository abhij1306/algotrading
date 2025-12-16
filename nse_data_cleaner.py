import os
import pandas as pd
from tqdm import tqdm

RAW_ROOT = "nse_data/raw/equities"
OUT_ROOT = "nse_data/processed/equities_clean"

os.makedirs(OUT_ROOT, exist_ok=True)

# -----------------------------------
# COLUMN NORMALIZATION MAP
# -----------------------------------
COL_MAP = {
    "SYMBOL": "symbol",
    "OPEN": "open",
    "HIGH": "high",
    "LOW": "low",
    "CLOSE": "close",
    "TOTTRDQTY": "volume",
    "TOTTRDVAL": "turnover",
    "TIMESTAMP": "trade_date"
}

REQUIRED_COLS = list(COL_MAP.keys()) + ["SERIES"]

CHUNK_SIZE = 200_000


def clean_file(csv_path):
    """Read bhavcopy safely and normalize schema"""
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return None

    # Defensive check
    if not set(REQUIRED_COLS).issubset(df.columns):
        return None

    # Keep only EQ series
    df = df[df["SERIES"] == "EQ"]

    if df.empty:
        return None

    df = df[REQUIRED_COLS].rename(columns=COL_MAP)

    # Date normalization
    df["trade_date"] = pd.to_datetime(
        df["trade_date"],
        errors="coerce",
        dayfirst=True
    ).dt.date

    df = df.dropna(subset=["trade_date"])

    # Numeric cleanup
    num_cols = ["open", "high", "low", "close", "volume", "turnover"]
    for c in num_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["volume"] = df["volume"].fillna(0).astype("int64")

    df["year"] = pd.to_datetime(df["trade_date"]).dt.year

    return df[
        ["trade_date", "symbol", "open", "high", "low", "close", "volume", "turnover", "year"]
    ]


def process_all():
    files = []

    for root, _, filenames in os.walk(RAW_ROOT):
        for f in filenames:
            if f.endswith(".csv"):
                files.append(os.path.join(root, f))

    print(f"Found {len(files)} bhavcopy files")

    buffer = []

    for path in tqdm(files):
        df = clean_file(path)
        if df is not None:
            buffer.append(df)

        if len(buffer) >= 20:
            flush(buffer)
            buffer.clear()

    if buffer:
        flush(buffer)


def flush(dfs):
    df = pd.concat(dfs, ignore_index=True)

    for year, g in df.groupby("year"):
        out_dir = os.path.join(OUT_ROOT, f"year={year}")
        os.makedirs(out_dir, exist_ok=True)

        out_file = os.path.join(out_dir, "equity_ohlcv.parquet")

        if os.path.exists(out_file):
            existing = pd.read_parquet(out_file)
            g = pd.concat([existing, g], ignore_index=True)

        g.drop_duplicates(
            subset=["symbol", "trade_date"],
            inplace=True
        )

        g.to_parquet(
            out_file,
            engine="pyarrow",
            compression="snappy",
            index=False
        )


if __name__ == "__main__":
    process_all()

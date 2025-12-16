import os
import pandas as pd
import requests
from io import StringIO

OUT_ROOT = "nse_data/processed/indices_clean"
os.makedirs(OUT_ROOT, exist_ok=True)

BASE_URL = "https://archives.nseindia.com/content/indices"

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "text/csv"
}

# -----------------------------
# INDEX MASTER (EXTENSIBLE)
# -----------------------------
INDEX_LIST = {
    "nifty50": "ind_nifty50.csv",
    "niftybank": "ind_niftybank.csv",
    "niftyfinservice": "ind_niftyfinservice.csv",
    "niftyit": "ind_niftyit.csv",
    "niftypharma": "ind_niftypharma.csv",
    "niftyfmcg": "ind_niftyfmcg.csv",
    "niftymetal": "ind_niftymetal.csv",
    "niftyenergy": "ind_niftyenergy.csv",
    "niftymidcap150": "ind_niftymidcap150.csv",
    "niftysmallcap250": "ind_niftysmallcap250.csv"
}

# -----------------------------
# COLUMN NORMALIZATION
# -----------------------------
COL_MAP = {
    "Date": "trade_date",
    "Open": "open",
    "High": "high",
    "Low": "low",
    "Close": "close",
    "Shares Traded": "volume",
    "Turnover (Rs. Cr.)": "turnover"
}


def download_index(csv_name):
    url = f"{BASE_URL}/{csv_name}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return pd.read_csv(StringIO(resp.text))


def clean_index(df, index_name):
    df = df.rename(columns=COL_MAP)

    df["trade_date"] = pd.to_datetime(
        df["trade_date"],
        errors="coerce",
        dayfirst=True
    ).dt.date

    df = df.dropna(subset=["trade_date"])

    for col in ["open", "high", "low", "close", "volume", "turnover"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    df["index"] = index_name

    final_cols = [
        "trade_date", "index",
        "open", "high", "low", "close",
        "volume", "turnover"
    ]

    return df[[c for c in final_cols if c in df.columns]]


def process_all():
    for index_name, csv_file in INDEX_LIST.items():
        print(f"Processing {index_name}...")

        try:
            raw = download_index(csv_file)
            clean = clean_index(raw, index_name)

            out_dir = os.path.join(OUT_ROOT, f"index={index_name}")
            os.makedirs(out_dir, exist_ok=True)

            out_file = os.path.join(out_dir, "index_ohlc.parquet")

            clean.to_parquet(
                out_file,
                engine="pyarrow",
                compression="snappy",
                index=False
            )
            
            print(f"  ✓ {index_name}: {len(clean):,} records saved")
            
        except Exception as e:
            print(f"  ✗ {index_name}: Failed - {e}")


if __name__ == "__main__":
    process_all()

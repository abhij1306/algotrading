import pandas as pd
import json

def check_parquet():
    file_path = 'nse_data/processed/equities_clean/equity_ohlcv.parquet'
    try:
        df = pd.read_parquet(file_path)
        
        info = {
            "shape": df.shape,
            "columns": df.columns.tolist(),
        }
        
        # Try different possible date columns
        date_col = next((c for c in ['date', 'trade_date', 'datetime', 'timestamp'] if c in df.columns), None)
        
        if date_col:
            info["date_range"] = {
                "col": date_col,
                "min": str(df[date_col].min()),
                "max": str(df[date_col].max())
            }
        
        if 'symbol' in df.columns:
            info["symbols_count"] = int(df.symbol.nunique())
            reliance_df = df[df.symbol == 'RELIANCE']
            if not reliance_df.empty and date_col:
                info["reliance_range"] = {
                    "min": str(reliance_df[date_col].min()),
                    "max": str(reliance_df[date_col].max()),
                    "count": len(reliance_df)
                }
        
        print(json.dumps(info, indent=2))
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    check_parquet()

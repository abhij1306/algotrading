
import pandas as pd
from datetime import datetime
import io

def parse_screener_excel(file_content: bytes):
    """
    Parse Screener.in Excel file
    Returns financial data extracted from sheets
    """
    try:
        xls = pd.ExcelFile(io.BytesIO(file_content))
        results = []
        
        # Try to find and parse financial sheets
        for sheet_name in xls.sheet_names:
            df = pd.read_excel(xls, sheet_name=sheet_name)
            print(f"\nParsing sheet '{sheet_name}' with shape {df.shape}")
            
            # Look for date columns (quarterly/annual periods)
            date_cols = [col for col in df.columns if 'Mar' in str(col) or 'Sep' in str(col) or 'Dec' in str(col) or 'Jun' in str(col)]
            
            if not date_cols:
                continue
            
            # Get the most recent period
            latest_col = date_cols[-1]
            
            # Extract metrics from the dataframe
            record = {
                'period_end': datetime.now().date(),  # Simplified - should parse from column name
                'period_type': 'quarterly',
                'fiscal_year': datetime.now().year,
            }
            
            # Common row names in Screener.in format
            for idx, row in df.iterrows():
                metric_name = str(row.iloc[0]).strip() if pd.notna(row.iloc[0]) else ""
                
                if latest_col in df.columns:
                    value = row[latest_col]
                    if pd.notna(value) and isinstance(value, (int, float)):
                        # Map Screener.in metric names to our database fields
                        if 'Sales' in metric_name or 'Revenue' in metric_name:
                            record['revenue'] = float(value)
                        elif 'Net Profit' in metric_name or 'PAT' in metric_name:
                            record['net_income'] = float(value)
                        elif 'Total Assets' in metric_name:
                            record['total_assets'] = float(value)
                        elif 'Total Liabilities' in metric_name or 'Total Debt' in metric_name:
                            record['total_liabilities'] = float(value)
                        elif 'Shareholders' in metric_name or 'Equity' in metric_name:
                            record['shareholders_equity'] = float(value)
            # Only add if we extracted at least some data
            if len(record) > 3:  # More than just the date fields
                results.append(record)
        
        return results
        
    except Exception as e:
        print(f"Excel Parse Error: {e}")
        import traceback
        traceback.print_exc()
        return []

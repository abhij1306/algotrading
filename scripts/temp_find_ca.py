import pandas as pd

def find_ca():
    file_path = "c:/AlgoTrading/nse_data/raw/corporate_actions/CF-CA-equities-01-01-2016-to-17-12-2025.csv"
    df = pd.read_csv(file_path)
    
    # Filter for RELIANCE and INFY
    targets = ['RELIANCE', 'INFY']
    filtered = df[df['SYMBOL'].isin(targets)].copy()
    
    # Filter for Bonus or Split
    filtered = filtered[filtered['PURPOSE'].str.contains('Bonus|Split', case=False, na=False)]
    
    print(filtered[['SYMBOL', 'EX-DATE', 'PURPOSE']].to_string())

if __name__ == "__main__":
    find_ca()

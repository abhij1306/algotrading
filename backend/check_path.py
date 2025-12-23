import os

path = '../nse_data/raw/intraday/NIFTY50_5min_complete.csv'
abs_path = os.path.abspath(path)
cwd = os.getcwd()

print(f"CWD: {cwd}")
print(f"Checking path: {path}")
print(f"Absolute path: {abs_path}")
print(f"Exists: {os.path.exists(path)}")

path2 = r'..\nse_data\raw\intraday\NIFTY50_5min_complete.csv'
print(f"Checking path2: {path2}")
print(f"Exists2: {os.path.exists(path2)}")

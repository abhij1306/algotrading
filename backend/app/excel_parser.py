"""
AI-Powered Excel Parser for Financial Data
Uses OpenRouter Kwai model for intelligent column mapping and data extraction
"""
import pandas as pd
import json
import os
from typing import Dict, List, Optional, Tuple
from sqlalchemy import func
import requests

class SmartFinancialParser:
    """Intelligent parser for financial data using AI"""
    
    def __init__(self):
        # Load from environment variable
        self.openrouter_api_key = os.getenv('OPENROUTER_API_KEY', '')
        self.model = "kwaipilot/kat-coder-pro-v1"
        
    def parse_excel(self, file_content: bytes, filename: str) -> Tuple[pd.DataFrame, Dict]:
        """
        Parse Excel file and extract financial data
        Returns: (dataframe, metadata)
        """
        # Read Excel - try "Data Sheet" first for multi-sheet files
        try:
            import io
            # Try to read "Data Sheet" specifically
            try:
                df = pd.read_excel(io.BytesIO(file_content), sheet_name='Data Sheet')
            except:
                # Fallback to first sheet
                df = pd.read_excel(io.BytesIO(file_content))
        except:
            # Try CSV
            import io
            df = pd.read_csv(io.BytesIO(file_content))
        
        # Extract symbol from filename if possible
        symbol_from_filename = None
        if filename:
            # Remove extension and clean up
            base_name = filename.replace('.xlsx', '').replace('.xls', '').replace('.csv', '').strip()
            # Common patterns: "HDFC Bank.xlsx" -> "HDFCBANK"
            symbol_from_filename = base_name.replace(' ', '').upper()
        
        # Get column names
        columns = list(df.columns)
        
        # For complex multi-table sheets (like Screener.in Data Sheet), use AI to parse
        # Check if this looks like a complex data sheet with multiple tables
        is_complex_sheet = False
        if len(columns) > 5 and len(df) > 20:
            # Likely a complex sheet with multiple tables
            is_complex_sheet = True
        
        if is_complex_sheet:
            # Use AI to intelligently parse the entire sheet
            results = self._ai_parse_complex_sheet(df, filename)
            return pd.DataFrame(results), {
                'original_columns': columns,
                'mapped_columns': {'ai_parsed': 'multi_table'},
                'rows_processed': len(results),
                'format': 'ai_complex_sheet'
            }
        
        # Standard format: Use AI to map columns
        column_mapping = self._ai_map_columns(columns)
        
        # Extract and calculate metrics
        results = []
        for _, row in df.iterrows():
            metrics = self._extract_metrics(row, column_mapping, df.columns)
            if metrics:
                results.append(metrics)
        
        return pd.DataFrame(results), {
            'original_columns': columns,
            'mapped_columns': column_mapping,
            'rows_processed': len(results),
            'format': 'standard'
        }
    
    def _ai_parse_complex_sheet(self, df: pd.DataFrame, filename: str) -> List[Dict]:
        """Parse Screener.in time-series financial statements"""
        
        # Extract symbol from filename
        symbol = filename.replace('.xlsx', '').replace('.xls', '').replace(' LTD', '').replace(' LIMITED', '').replace(' ', '').upper() if filename else 'UNKNOWN'
        
        # Create metrics dict
        metrics = {}
        
        # Iterate through rows to find financial metrics
        for idx, row in df.iterrows():
            metric_name = str(row.iloc[0]).strip().lower() if pd.notna(row.iloc[0]) else ''
            
            if not metric_name:
                continue
            
            # Get the latest non-null value (rightmost column with data)
            value = None
            for col_idx in range(len(row) - 1, 0, -1):
                val = row.iloc[col_idx]
                if pd.notna(val) and val != '' and val != 0:
                    try:
                        if isinstance(val, str):
                            val = val.replace(',', '').replace('Cr', '').replace('%', '').strip()
                        value = float(val)
                        break
                    except:
                        continue
            
            if value is not None:
                metrics[metric_name] = value
        
        # Map Screener.in metric names to our standard names
        sales = metrics.get('sales', 0) or metrics.get('revenue', 0) or metrics.get('total revenue', 0)
        net_profit = metrics.get('net profit', 0) or metrics.get('profit', 0) or metrics.get('net income', 0)
        
        # Balance sheet items
        equity = metrics.get('equity share capital', 0) or metrics.get('equity', 0)
        reserves = metrics.get('reserves', 0) or metrics.get('reserves and surplus', 0)
        total_equity = equity + reserves if (equity or reserves) else 0
        
        borrowings = metrics.get('borrowings', 0) or metrics.get('total debt', 0) or metrics.get('debt', 0)
        
        # Calculate derived metrics
        roe = (net_profit / total_equity * 100) if total_equity > 0 else 0
        debt_to_equity = (borrowings / total_equity) if total_equity > 0 else 0
        
        # Try to get EPS and PE from metrics
        eps = metrics.get('eps', 0) or metrics.get('earnings per share', 0)
        pe = metrics.get('pe', 0) or metrics.get('p/e', 0) or metrics.get('price to earnings', 0)
        
        # Match symbol with existing database symbols
        matched_symbol = self._match_symbol_from_db(symbol)
        
        return [{
            'symbol': matched_symbol,
            'revenue': sales,
            'net_income': net_profit,
            'eps': eps,
            'roe': roe,
            'debt_to_equity': debt_to_equity,
            'pe_ratio': pe
        }]
    
    def _match_symbol_from_db(self, raw_symbol: str) -> str:
        """Match raw symbol from filename/Excel with existing database symbols"""
        from .database import SessionLocal, Company
        
        # Clean the raw symbol - remove spaces and common corporate suffixes
        cleaned = raw_symbol.replace(' ', '').replace('LTD', '').replace('LIMITED', '').upper().strip()
        
        db = SessionLocal()
        try:
            # Try exact match first
            company = db.query(Company).filter(Company.symbol == cleaned).first()
            if company:
                return company.symbol
            
            # Try without common suffixes (but keep BANK if it might be part of symbol)
            # For "HDFC BANK" -> try "HDFCBANK" first before trying "HDFC"
            if 'BANK' in cleaned:
                # Try with BANK first
                company = db.query(Company).filter(Company.symbol == cleaned).first()
                if company:
                    return company.symbol
            
            # Try partial matches and pick the shortest one (most likely correct)
            companies = db.query(Company).filter(Company.symbol.like(f"{cleaned}%")).order_by(func.length(Company.symbol)).all()
            if companies:
                return companies[0].symbol  # Return shortest match
            
            # Try without HOSPITAL/BANK suffixes
            cleaned_no_suffix = cleaned.replace('HOSPITAL', '').replace('BANK', '').strip()
            if cleaned_no_suffix != cleaned:
                companies = db.query(Company).filter(Company.symbol.like(f"{cleaned_no_suffix}%")).order_by(func.length(Company.symbol)).all()
                if companies:
                    return companies[0].symbol
            
            # Try fuzzy match (cleaned is contained in symbol)
            companies = db.query(Company).filter(Company.symbol.contains(cleaned[:8])).order_by(func.length(Company.symbol)).all()
            if companies:
                return companies[0].symbol  # Return shortest match
            
            # No match found, return cleaned version
            return cleaned
        finally:
            db.close()
        
        # Check if this is a transposed financial statement (rows = metrics, columns = periods)
        # Indicators: First column has metric names like "Revenue", "Expenses", etc.
        is_transposed = False
        if len(df) > 0 and len(columns) > 0:
            first_col_values = df.iloc[:, 0].astype(str).str.lower()
            financial_keywords = ['revenue', 'sales', 'expenses', 'profit', 'income', 'assets', 'equity', 'debt']
            if any(keyword in ' '.join(first_col_values.tolist()) for keyword in financial_keywords):
                is_transposed = True
        
        if is_transposed and symbol_from_filename:
            # Handle transposed format: transpose and use filename as symbol
            results = self._extract_from_transposed(df, symbol_from_filename)
            return pd.DataFrame(results), {
                'original_columns': columns,
                'mapped_columns': {'transposed': 'financial_statement'},
                'rows_processed': len(results),
                'format': 'transposed',
                'symbol_source': 'filename'
            }
        
        # Standard format: Use AI to map columns
        column_mapping = self._ai_map_columns(columns)
        
        # Extract and calculate metrics
        results = []
        for _, row in df.iterrows():
            metrics = self._extract_metrics(row, column_mapping, df.columns)
            if metrics:
                results.append(metrics)
        
        return pd.DataFrame(results), {
            'original_columns': columns,
            'mapped_columns': column_mapping,
            'rows_processed': len(results),
            'format': 'standard'
        }
    
    def _extract_from_screener_format(self, df: pd.DataFrame) -> List[Dict]:
        """Extract financial data from Screener.in column-based format"""
        results = []
        
        # First column has metric names, other columns have company data
        metric_col = df.columns[0]
        company_cols = [c for c in df.columns[1:] if not str(c).startswith('Unnamed') and pd.notna(c)]
        
        for company_name in company_cols:
            # Create a dict mapping metric names to values for this company
            metrics_dict = {}
            
            for _, row in df.iterrows():
                metric_name = str(row[metric_col]).strip().lower()
                value = row[company_name]
                
                if pd.notna(value) and value != '':
                    try:
                        # Handle strings like "1,234.56 Cr" or "12.5%"
                        if isinstance(value, str):
                            value = value.replace(',', '').replace('Cr', '').replace('%', '').strip()
                        metrics_dict[metric_name] = float(value)
                    except:
                        continue
            
            # Extract symbol (clean up company name)
            symbol = company_name.replace(' LTD', '').replace(' LIMITED', '').replace(' ', '').upper()
            
            # Map common Screener.in metric names
            revenue = metrics_dict.get('sales', 0) or metrics_dict.get('revenue', 0) or metrics_dict.get('total revenue', 0)
            net_income = metrics_dict.get('net profit', 0) or metrics_dict.get('profit', 0) or metrics_dict.get('net income', 0)
            equity = metrics_dict.get('equity', 0) or metrics_dict.get('shareholders equity', 0) or metrics_dict.get('net worth', 0)
            debt = metrics_dict.get('debt', 0) or metrics_dict.get('total debt', 0) or metrics_dict.get('borrowings', 0)
            eps = metrics_dict.get('eps', 0) or metrics_dict.get('earnings per share', 0)
            roe = metrics_dict.get('roe', 0) or metrics_dict.get('return on equity', 0)
            pe = metrics_dict.get('pe', 0) or metrics_dict.get('p/e', 0) or metrics_dict.get('price to earnings', 0)
            
            # Calculate derived metrics if not present
            if roe == 0 and net_income > 0 and equity > 0:
                roe = (net_income / equity * 100)
            
            debt_to_equity = (debt / equity) if equity > 0 else 0
            
            results.append({
                'symbol': symbol,
                'revenue': revenue,
                'net_income': net_income,
                'eps': eps,
                'roe': roe,
                'debt_to_equity': debt_to_equity,
                'pe_ratio': pe
            })
        
        return results
    
    def _extract_from_transposed(self, df: pd.DataFrame, symbol: str) -> List[Dict]:
        """Extract financial data from transposed format (rows = metrics)"""
        # Create a dict mapping metric names to values
        metrics_dict = {}
        
        for _, row in df.iterrows():
            metric_name = str(row.iloc[0]).strip().lower()
            # Get the latest value (last column with data)
            for i in range(len(row) - 1, 0, -1):
                val = row.iloc[i]
                if pd.notna(val) and val != '' and val != 0:
                    try:
                        metrics_dict[metric_name] = float(val)
                        break
                    except:
                        continue
        
        # Map common metric names
        revenue = metrics_dict.get('revenue', 0) or metrics_dict.get('sales', 0) or metrics_dict.get('total revenue', 0)
        net_income = metrics_dict.get('net profit', 0) or metrics_dict.get('net income', 0) or metrics_dict.get('profit after tax', 0)
        equity = metrics_dict.get('equity', 0) or metrics_dict.get('shareholders equity', 0) or metrics_dict.get('net worth', 0)
        debt = metrics_dict.get('debt', 0) or metrics_dict.get('total debt', 0) or metrics_dict.get('borrowings', 0)
        
        # Calculate derived metrics
        roe = (net_income / equity * 100) if equity > 0 else 0
        debt_to_equity = (debt / equity) if equity > 0 else 0
        
        return [{
            'symbol': symbol,
            'revenue': revenue,
            'net_income': net_income,
            'eps': 0,  # Can't calculate without shares
            'roe': roe,
            'debt_to_equity': debt_to_equity,
            'pe_ratio': 0  # Can't calculate without market cap
        }]
    
    def _ai_map_columns(self, columns: List[str]) -> Dict[str, str]:
        """Use AI to map Excel columns to standard financial metrics"""
        
        prompt = f"""You are a financial data expert. Map these Excel column names to standard financial metrics.

Excel Columns: {json.dumps(columns)}

Standard Metrics:
- symbol: Company ticker/symbol
- revenue: Total revenue/sales (in Crores)
- net_income: Net profit/PAT (in Crores)
- total_assets: Total assets
- shareholders_equity: Equity/Net Worth
- total_debt: Total debt/borrowings
- outstanding_shares: Number of shares
- market_cap: Market capitalization
- eps: Earnings per share
- roe: Return on Equity (%)
- debt_to_equity: Debt to Equity ratio
- pe_ratio: Price to Earnings ratio

Return ONLY a JSON object mapping Excel column names to standard metric names. If a column doesn't match any metric, omit it.
Example: {{"Sales": "revenue", "Net Profit": "net_income", "Ticker": "symbol"}}

JSON:"""

        try:
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.openrouter_api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.1
                },
                timeout=30
            )
            
            if response.status_code == 200:
                content = response.json()['choices'][0]['message']['content']
                # Extract JSON from response
                json_start = content.find('{')
                json_end = content.rfind('}') + 1
                if json_start >= 0 and json_end > json_start:
                    mapping = json.loads(content[json_start:json_end])
                    return mapping
        except Exception as e:
            print(f"AI mapping failed: {e}")
        
        # Fallback to rule-based mapping
        return self._fallback_mapping(columns)
    
    def _fallback_mapping(self, columns: List[str]) -> Dict[str, str]:
        """Rule-based fallback if AI fails"""
        mapping = {}
        
        patterns = {
            'symbol': ['symbol', 'ticker', 'stock', 'company', 'name'],
            'revenue': ['revenue', 'sales', 'total revenue', 'turnover'],
            'net_income': ['net profit', 'net income', 'pat', 'profit after tax', 'profit'],
            'total_assets': ['total assets', 'assets'],
            'shareholders_equity': ['equity', 'shareholders equity', 'net worth', 'shareholder funds'],
            'total_debt': ['debt', 'total debt', 'borrowings', 'total borrowings'],
            'outstanding_shares': ['shares', 'outstanding shares', 'no. of shares'],
            'market_cap': ['market cap', 'mcap', 'market capitalization'],
            'eps': ['eps', 'earnings per share'],
            'roe': ['roe', 'return on equity'],
            'debt_to_equity': ['debt to equity', 'debt/equity', 'd/e'],
            'pe_ratio': ['pe', 'p/e', 'price to earnings', 'pe ratio']
        }
        
        for col in columns:
            col_lower = col.lower().strip()
            for metric, keywords in patterns.items():
                if any(kw in col_lower for kw in keywords):
                    mapping[col] = metric
                    break
        
        return mapping
    
    def _extract_metrics(self, row: pd.Series, mapping: Dict[str, str], all_columns: List[str]) -> Optional[Dict]:
        """Extract and calculate financial metrics from a row"""
        
        # Helper to get value
        def get_val(metric_name: str) -> float:
            for col, mapped in mapping.items():
                if mapped == metric_name and col in all_columns:
                    val = row[col]
                    if pd.isna(val) or val == '':
                        return 0.0
                    try:
                        # Handle strings like "1,234.56 Cr"
                        if isinstance(val, str):
                            val = val.replace(',', '').replace('Cr', '').replace('%', '').strip()
                        return float(val)
                    except:
                        return 0.0
            return 0.0
        
        # Extract base metrics
        symbol = None
        for col, mapped in mapping.items():
            if mapped == 'symbol':
                symbol = str(row[col]).strip().upper()
                break
        
        if not symbol or symbol == 'NAN':
            return None
        
        revenue = get_val('revenue')
        net_income = get_val('net_income')
        total_assets = get_val('total_assets')
        shareholders_equity = get_val('shareholders_equity')
        total_debt = get_val('total_debt')
        outstanding_shares = get_val('outstanding_shares')
        market_cap = get_val('market_cap')
        
        # Try to get direct values first
        eps = get_val('eps')
        roe = get_val('roe')
        debt_to_equity = get_val('debt_to_equity')
        pe_ratio = get_val('pe_ratio')
        
        # Calculate missing metrics
        if eps == 0 and net_income > 0 and outstanding_shares > 0:
            eps = net_income / outstanding_shares
        
        if roe == 0 and net_income > 0 and shareholders_equity > 0:
            roe = (net_income / shareholders_equity) * 100
        
        if debt_to_equity == 0 and total_debt > 0 and shareholders_equity > 0:
            debt_to_equity = total_debt / shareholders_equity
        
        if pe_ratio == 0 and market_cap > 0 and net_income > 0:
            pe_ratio = market_cap / net_income
        
        return {
            'symbol': symbol,
            'revenue': revenue,
            'net_income': net_income,
            'eps': eps,
            'roe': roe,
            'debt_to_equity': debt_to_equity,
            'pe_ratio': pe_ratio
        }

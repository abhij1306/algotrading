import numpy as np
import pandas as pd
import requests
import yfinance as yf

from ..utils.logger import get_logger

logger = get_logger("services.market_data")


class MarketDataService:
    @staticmethod
    def calculate_adx(high, low, close, period=14):
        """
        Manual ADX calculation to avoid pandas_ta dependency
        """
        try:
            # Calculate True Range
            tr1 = high - low
            tr2 = abs(high - close.shift())
            tr3 = abs(low - close.shift())
            tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)

            # Calculate +DM and -DM
            high_diff = high.diff()
            low_diff = -low.diff()

            plus_dm = pd.Series(
                np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0), index=high.index
            )
            minus_dm = pd.Series(
                np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0), index=low.index
            )

            # Smooth with EMA
            atr = tr.ewm(alpha=1 / period, min_periods=period).mean()
            plus_di = 100 * (plus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr)
            minus_di = 100 * (minus_dm.ewm(alpha=1 / period, min_periods=period).mean() / atr)

            # Calculate DX and ADX
            dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
            adx = dx.ewm(alpha=1 / period, min_periods=period).mean()

            return adx
        except Exception as e:
            logger.error(f"Error calculating ADX: {str(e)}")
            if hasattr(high, "index"):
                return pd.Series([0] * len(high), index=high.index)
            return None

    @staticmethod
    def get_global_indices():
        """
        Fetches global indices and commodities using yfinance.
        Includes S&P 500, Nasdaq, Nifty, Gold, Silver.
        """
        # Symbols mapping - Updated Commodities to Futures which are more reliable on YF
        symbols = {
            "S&P 500": "^GSPC",
            "Nasdaq": "^IXIC",
            "Dow Jones": "^DJI",
            "Nifty 50": "^NSEI",
            "Bank Nifty": "^NSEBANK",
            "Gold": "GC=F",
            "Silver": "SI=F",
            "VIX (India)": "^INDIAVIX",
            "VIX (US)": "^VIX",
        }

        results = []
        try:
            # Fetch all at once for efficiency
            tickers = list(symbols.values())
            # Fetch 1mo to ensure we have enough data to find last valid close
            data = yf.download(
                tickers, period="1mo", interval="1d", progress=False, group_by="ticker"
            )

            for name, symbol in symbols.items():
                try:
                    # yfinance returns MultiIndex if multiple tickers
                    if len(tickers) > 1:
                        if symbol in data:
                            ticker_data = data[symbol]
                        else:
                            # Try fallback or skip
                            logger.warning(f"No data for {symbol} in bulk download")
                            continue
                    else:
                        ticker_data = data

                    # Drop rows where all cols are NaN
                    ticker_data = ticker_data.dropna(how="all")

                    if ticker_data.empty:
                        continue

                    # Ensure columns are flat (Open, High, Low, Close)
                    # If ticker_data still has multiindex columns (e.g. Price), fix it
                    if isinstance(ticker_data.columns, pd.MultiIndex):
                        ticker_data.columns = ticker_data.columns.droplevel(0)

                    # Get latest valid close
                    latest = ticker_data.iloc[-1]

                    current_price = latest["Close"]
                    if pd.isna(current_price):
                        current_price = latest["Open"]

                    if pd.isna(current_price) and len(ticker_data) > 1:
                        latest = ticker_data.iloc[-2]
                        current_price = latest["Close"]

                    # Get previous close
                    if len(ticker_data) >= 2:
                        prev_row = ticker_data.iloc[-2]
                        prev_close = prev_row["Close"]

                        if latest.name == prev_row.name and len(ticker_data) >= 3:
                            prev_close = ticker_data.iloc[-3]["Close"]
                    else:
                        prev_close = current_price

                    # Float conversion for JSON safety
                    current_price = float(current_price)
                    prev_close = float(prev_close)

                    change = current_price - prev_close
                    change_pct = (change / prev_close) * 100 if prev_close != 0 else 0

                    results.append(
                        {
                            "name": name,
                            "symbol": symbol,
                            "price": current_price,
                            "change": change,
                            "change_pct": change_pct,
                            "status": "POSITIVE" if change >= 0 else "NEGATIVE",
                        }
                    )
                except Exception as e:
                    logger.error(f"Error processing {name} ({symbol}): {str(e)}")
                    pass

        except Exception as e:
            logger.error(f"Error fetching global indices: {str(e)}")

        return results

    @staticmethod
    def get_market_sentiment():
        """
        Fetches Fear & Greed Index from CNN (US) and approximates India sentiment.
        """
        sentiment = {
            "us_fear_greed": {"score": None, "status": "Unavailable", "source": "unavailable"},
            "india_sentiment": {"score": None, "status": "Unavailable", "source": "unavailable"},
        }

        # 1. US Fear & Greed (CNN)
        try:
            url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            # Use session with retries
            session = requests.Session()
            retries = requests.adapters.HTTPAdapter(max_retries=1)
            session.mount("https://", retries)

            resp = session.get(url, headers=headers, timeout=3)
            if resp.status_code == 200:
                data = resp.json()
                score = round(data["fear_and_greed"]["score"])
                rating = data["fear_and_greed"]["rating"].capitalize()
                sentiment["us_fear_greed"] = {"score": score, "status": rating, "source": "cnn"}
        except Exception as e:
            # Silent fallback or debug log only
            logger.debug(f"Error fetching CNN Fear & Greed: {str(e)}")

        # 2. India Sentiment (Tickertape Market Mood Index)
        try:
            url = "https://www.tickertape.in/market-mood-index"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            }

            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                import re
                from collections import Counter

                from bs4 import BeautifulSoup

                soup = BeautifulSoup(resp.content, "html.parser")

                # Find ALL decimal numbers and pick the most common one
                # Tickertape displays the MMI score multiple times on the page
                potential_scores = []
                for text in soup.find_all(string=True):
                    match = re.search(r"\b(\d{2}\.\d{2})\b", text.strip())
                    if match:
                        val = float(match.group(1))
                        # Filter: MMI is 0-100, exclude times and outliers
                        if 10 <= val <= 100:  # Reasonable MMI range
                            potential_scores.append(val)

                # The actual MMI score appears multiple times - pick the most common
                if potential_scores:
                    score_counts = Counter(potential_scores)
                    most_common_score = score_counts.most_common(1)[0][0]
                    score = int(round(most_common_score))

                    # Determine status based on Tickertape's scale
                    status = "Neutral"
                    if score >= 75:
                        status = "Extreme Greed"
                    elif score >= 60:
                        status = "Greed"
                    elif score <= 25:
                        status = "Extreme Fear"
                    elif score <= 40:
                        status = "Fear"

                    sentiment["india_sentiment"] = {
                        "score": score,
                        "status": status,
                        "source": "tickertape",
                    }
                    logger.info(
                        f"Tickertape MMI: {score} ({status}) from {len(potential_scores)} matches"
                    )
                else:
                    logger.warning(
                        f"Tickertape MMI: No valid scores found from {len(potential_scores)} candidates"
                    )
                    raise Exception("MMI score not found on page")

        except Exception as e:
            # Fallback: Use India VIX as backup
            import traceback

            logger.warning(f"Tickertape MMI fetch failed: {str(e)}")
            logger.debug(f"Traceback: {traceback.format_exc()}")
            try:
                vix_data = yf.Ticker("^INDIAVIX").history(period="5d")
                if not vix_data.empty:
                    vix = vix_data["Close"].iloc[-1]

                    # Simplified VIX to score conversion
                    if vix < 12:
                        score = 75
                    elif vix < 18:
                        score = 50
                    elif vix < 25:
                        score = 30
                    else:
                        score = 15

                    status = "Neutral"
                    if score >= 75:
                        status = "Extreme Greed"
                    elif score >= 60:
                        status = "Greed"
                    elif score <= 25:
                        status = "Extreme Fear"
                    elif score <= 40:
                        status = "Fear"

                    sentiment["india_sentiment"] = {
                        "score": score,
                        "status": status,
                        "vix": round(vix, 2),
                        "source": "vix_fallback",
                    }
            except Exception as vix_error:
                logger.error(f"VIX fallback also failed: {str(vix_error)}")

        return sentiment

    @staticmethod
    def get_market_condition():
        """
        Determines if Nifty 50 is Range-bound or Trending using Technical Analysis (ADX).
        """
        try:
            # Fetch 6mo of data
            # FIXED: Handle multi-index columns properly for single ticker download
            df = yf.download("^NSEI", period="6mo", interval="1d", progress=False)

            if df.empty:
                return {"status": "Unknown", "adx": 0, "reason": "No data"}

            # Clean MultiIndex columns if present (yfinance >= 0.2.40 change)
            if isinstance(df.columns, pd.MultiIndex):
                # If only one ticker, droplevel(1) removes the ticker name from columns
                if df.columns.nlevels > 1:
                    df.columns = df.columns.droplevel(1)

            # Ensure required columns exist
            required_cols = ["High", "Low", "Close"]
            if not all(col in df.columns for col in required_cols):
                logger.error(f"Missing columns in market data. Got: {df.columns.tolist()}")
                return {"status": "Unavailable", "adx": 0}

            # Drop NaNs
            df = df.dropna()

            # Calculate ADX using our custom function
            # Ensure series are 1D
            high = df["High"].squeeze()
            low = df["Low"].squeeze()
            close = df["Close"].squeeze()

            adx_series = MarketDataService.calculate_adx(high, low, close, period=14)

            current_adx = 0.0
            if adx_series is not None and not adx_series.empty:
                current_adx = float(adx_series.iloc[-1])

            # Trend determination
            # Use squeeze to ensure scalar values
            sma50 = float(close.rolling(window=50).mean().iloc[-1])
            current_price = float(close.iloc[-1])

            condition = "Sideways / Range-bound"
            trend_strength = "Weak"

            if current_adx > 25:
                trend_strength = "Strong"
                if current_price > sma50:
                    condition = "Bullish Trend"
                else:
                    condition = "Bearish Trend"
            else:
                trend_strength = "Weak"
                # Handle potential division by zero
                if sma50 > 0 and abs(current_price - sma50) / sma50 < 0.02:
                    condition = "Consolidation"

            # Handle NaN values before returning (JSON serialization error prevention)
            if pd.isna(current_adx):
                current_adx = 0.0
            if pd.isna(sma50):
                sma50 = 0.0
            if pd.isna(current_price):
                current_price = 0.0

            return {
                "status": condition,
                "adx": round(current_adx, 2),
                "trend_strength": trend_strength,
                "technical_summary": f"ADX is {current_adx:.1f}. Price is {'above' if current_price > sma50 else 'below'} SMA50.",
            }

        except Exception as e:
            logger.error(f"Error analyzing market condition: {str(e)}", exc_info=True)
            return {"status": "Unavailable", "adx": 0}

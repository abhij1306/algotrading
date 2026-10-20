from typing import Any

import numpy as np
import pandas as pd
import requests
import yfinance as yf

from ..utils.logger import get_logger

logger = get_logger("services.market_data")

_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
)


class MarketDataService:
    @staticmethod
    def calculate_adx(
        high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
    ) -> pd.Series | None:
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
            logger.exception(f"Error calculating ADX: {str(e)}")
            if hasattr(high, "index"):
                return pd.Series([0] * len(high), index=high.index)
            return None

    @staticmethod
    def get_global_indices() -> list[dict[str, Any]]:
        """
        Fetches global indices and commodities using yfinance.
        Includes S&P 500, Nasdaq, Nifty, Gold, Silver.
        """
        results = []
        try:
            # Fetch all at once for efficiency
            tickers = list(GLOBAL_INDEX_SYMBOLS.values())
            # Fetch 1mo to ensure we have enough data to find last valid close
            data = yf.download(
                tickers, period="1mo", interval="1d", progress=False, group_by="ticker"
            )

            for name, symbol in GLOBAL_INDEX_SYMBOLS.items():
                try:
                    row = _global_index_row(name, symbol, data, multi=len(tickers) > 1)
                    if row is not None:
                        results.append(row)
                except Exception as e:
                    logger.exception(f"Error processing {name} ({symbol}): {str(e)}")

        except Exception as e:
            logger.exception(f"Error fetching global indices: {str(e)}")

        return results

    @staticmethod
    def get_market_sentiment() -> dict[str, Any]:
        """
        Fetches Fear & Greed Index from CNN (US) and approximates India sentiment.
        """
        sentiment = {
            "us_fear_greed": {"score": None, "status": "Unavailable", "source": "unavailable"},
            "india_sentiment": {"score": None, "status": "Unavailable", "source": "unavailable"},
        }

        us_fear_greed = _us_fear_greed()
        if us_fear_greed is not None:
            sentiment["us_fear_greed"] = us_fear_greed

        india_sentiment = _india_sentiment()
        if india_sentiment is not None:
            sentiment["india_sentiment"] = india_sentiment

        return sentiment

    @staticmethod
    def get_market_condition() -> dict[str, Any]:
        """
        Determines if Nifty 50 is Range-bound or Trending using Technical Analysis (ADX).
        """
        try:
            # Fetch 6mo of data
            df = yf.download("^NSEI", period="6mo", interval="1d", progress=False)

            if df.empty:
                return {"status": "Unknown", "adx": 0, "reason": "No data"}

            # Clean MultiIndex columns if present (yfinance >= 0.2.40 change):
            # with one ticker, droplevel(1) removes the ticker name from the columns
            if isinstance(df.columns, pd.MultiIndex) and df.columns.nlevels > 1:
                df.columns = df.columns.droplevel(1)

            # Ensure required columns exist
            required_cols = ["High", "Low", "Close"]
            if not all(col in df.columns for col in required_cols):
                logger.error(f"Missing columns in market data. Got: {df.columns.tolist()}")
                return {"status": "Unavailable", "adx": 0}

            df = df.dropna()

            # Ensure series are 1D
            high = df["High"].squeeze()
            low = df["Low"].squeeze()
            close = df["Close"].squeeze()

            adx_series = MarketDataService.calculate_adx(high, low, close, period=14)

            current_adx = 0.0
            if adx_series is not None and not adx_series.empty:
                current_adx = float(adx_series.iloc[-1])

            sma50 = float(close.rolling(window=50).mean().iloc[-1])
            current_price = float(close.iloc[-1])

            condition, trend_strength = _trend_condition(current_adx, current_price, sma50)

            # Handle NaN values before returning (JSON serialization error prevention)
            current_adx, sma50, current_price = (
                0.0 if pd.isna(value) else value for value in (current_adx, sma50, current_price)
            )

            return {
                "status": condition,
                "adx": round(current_adx, 2),
                "trend_strength": trend_strength,
                "technical_summary": (
                    f"ADX is {current_adx:.1f}. "
                    f"Price is {'above' if current_price > sma50 else 'below'} SMA50."
                ),
            }

        except Exception as e:
            logger.exception(f"Error analyzing market condition: {str(e)}")
            return {"status": "Unavailable", "adx": 0}


# Symbols mapping - Commodities use Futures, which are more reliable on YF
GLOBAL_INDEX_SYMBOLS = {
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

_TICKERTAPE_MMI_URL = "https://www.tickertape.in/market-mood-index"
_CNN_FEAR_GREED_URL = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"


class MMIScoreNotFoundError(ValueError):
    """The Tickertape page loaded but held no Market Mood Index score."""


def _global_index_row(
    name: str, symbol: str, data: pd.DataFrame, multi: bool
) -> dict[str, Any] | None:
    """Latest price and change of one ticker from the bulk download, or None to skip it."""
    # yfinance returns MultiIndex if multiple tickers
    if multi and symbol not in data:
        logger.warning(f"No data for {symbol} in bulk download")
        return None
    ticker_data = data[symbol] if multi else data

    # Drop rows where all cols are NaN
    ticker_data = ticker_data.dropna(how="all")
    if ticker_data.empty:
        return None

    # Ensure columns are flat (Open, High, Low, Close)
    if isinstance(ticker_data.columns, pd.MultiIndex):
        ticker_data.columns = ticker_data.columns.droplevel(0)

    latest, current_price = _latest_price(ticker_data)
    prev_close = _previous_close(ticker_data, latest, current_price)

    # Float conversion for JSON safety
    current_price = float(current_price)
    prev_close = float(prev_close)

    change = current_price - prev_close
    change_pct = (change / prev_close) * 100 if prev_close != 0 else 0
    return {
        "name": name,
        "symbol": symbol,
        "price": current_price,
        "change": change,
        "change_pct": change_pct,
        "status": "POSITIVE" if change >= 0 else "NEGATIVE",
    }


def _latest_price(ticker_data: pd.DataFrame) -> tuple[pd.Series, Any]:
    """Latest row and its close, falling back to the open and then the prior row's close."""
    latest = ticker_data.iloc[-1]
    current_price = latest["Close"]
    if pd.isna(current_price):
        current_price = latest["Open"]

    if pd.isna(current_price) and len(ticker_data) > 1:
        latest = ticker_data.iloc[-2]
        current_price = latest["Close"]
    return latest, current_price


def _previous_close(ticker_data: pd.DataFrame, latest: pd.Series, current_price: object) -> object:
    """Close of the row before `latest` (the current price when there is a single row)."""
    if len(ticker_data) < 2:
        return current_price
    prev_row = ticker_data.iloc[-2]
    if latest.name == prev_row.name and len(ticker_data) >= 3:
        return ticker_data.iloc[-3]["Close"]
    return prev_row["Close"]


def _mmi_status(score: int) -> str:
    """Tickertape Market Mood Index zone for a 0-100 score."""
    if score >= 75:
        return "Extreme Greed"
    if score >= 60:
        return "Greed"
    if score <= 25:
        return "Extreme Fear"
    if score <= 40:
        return "Fear"
    return "Neutral"


def _us_fear_greed() -> dict[str, Any] | None:
    """CNN Fear & Greed score and rating, or None when unavailable."""
    try:
        headers = {"User-Agent": _BROWSER_USER_AGENT}
        # Use session with retries
        session = requests.Session()
        retries = requests.adapters.HTTPAdapter(max_retries=1)
        session.mount("https://", retries)

        resp = session.get(_CNN_FEAR_GREED_URL, headers=headers, timeout=3)
        if resp.status_code == 200:
            data = resp.json()
            score = round(data["fear_and_greed"]["score"])
            rating = data["fear_and_greed"]["rating"].capitalize()
            return {"score": score, "status": rating, "source": "cnn"}
    except Exception as e:
        # Silent fallback or debug log only
        logger.debug(f"Error fetching CNN Fear & Greed: {str(e)}")
    return None


def _india_sentiment() -> dict[str, Any] | None:
    """Tickertape MMI; India VIX when the page fails or has no score; None otherwise."""
    try:
        return _tickertape_mmi()
    except Exception as e:
        import traceback

        logger.warning(f"Tickertape MMI fetch failed: {str(e)}")
        logger.debug(f"Traceback: {traceback.format_exc()}")
        return _vix_sentiment()


def _tickertape_mmi() -> dict[str, Any] | None:
    """MMI from the Tickertape page; None on a non-200 reply.

    Raises MMIScoreNotFoundError when the page holds no score.
    """
    headers = {
        "User-Agent": _BROWSER_USER_AGENT,
        "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"),
        "Accept-Language": "en-US,en;q=0.5",
    }
    resp = requests.get(_TICKERTAPE_MMI_URL, headers=headers, timeout=10)
    if resp.status_code != 200:
        return None

    from collections import Counter

    potential_scores = _mmi_candidates(resp.content)
    if not potential_scores:
        logger.warning(
            "Tickertape MMI: No valid scores found from %s candidates",
            len(potential_scores),
        )
        raise MMIScoreNotFoundError("MMI score not found on page")

    # The actual MMI score appears multiple times - pick the most common
    most_common_score = Counter(potential_scores).most_common(1)[0][0]
    score = int(round(most_common_score))
    status = _mmi_status(score)
    logger.info(f"Tickertape MMI: {score} ({status}) from {len(potential_scores)} matches")
    return {"score": score, "status": status, "source": "tickertape"}


def _mmi_candidates(content: bytes) -> list[float]:
    """Every NN.NN number in the page text within the 10-100 MMI range."""
    import re

    from bs4 import BeautifulSoup

    soup = BeautifulSoup(content, "html.parser")
    potential_scores = []
    for text in soup.find_all(string=True):
        match = re.search(r"\b(\d{2}\.\d{2})\b", text.strip())
        # MMI is 0-100; exclude times and outliers
        if match and 10 <= (val := float(match.group(1))) <= 100:
            potential_scores.append(val)
    return potential_scores


def _vix_to_score(vix: float) -> int:
    """Simplified India VIX to sentiment score conversion."""
    if vix < 12:
        return 75
    if vix < 18:
        return 50
    if vix < 25:
        return 30
    return 15


def _vix_sentiment() -> dict[str, Any] | None:
    """Sentiment approximated from the latest India VIX close, or None when unavailable."""
    try:
        vix_data = yf.Ticker("^INDIAVIX").history(period="5d")
        if vix_data.empty:
            return None
        vix = vix_data["Close"].iloc[-1]
        score = _vix_to_score(vix)
        return {
            "score": score,
            "status": _mmi_status(score),
            "vix": round(vix, 2),
            "source": "vix_fallback",
        }
    except Exception as vix_error:
        logger.exception(f"VIX fallback also failed: {str(vix_error)}")
        return None


def _trend_condition(current_adx: float, current_price: float, sma50: float) -> tuple[str, str]:
    """(condition, trend strength) from ADX and price against the 50-day SMA."""
    if current_adx > 25:
        condition = "Bullish Trend" if current_price > sma50 else "Bearish Trend"
        return condition, "Strong"
    # Handle potential division by zero
    if sma50 > 0 and abs(current_price - sma50) / sma50 < 0.02:
        return "Consolidation", "Weak"
    return "Sideways / Range-bound", "Weak"

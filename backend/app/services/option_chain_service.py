"""
Option Chain Service
====================
Fetches, caches, and serves option chain data from Fyers API.
Includes Greeks calculation using Black-Scholes model.
"""

import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime

from ..strategies.black_scholes import calculate_implied_volatility, get_option_greeks
from .symbol_master import symbol_master

logger = logging.getLogger(__name__)

# Risk-free rate for Indian market (RBI repo rate)
RISK_FREE_RATE = 0.065


@dataclass
class OptionLeg:
    """Single option leg (Call or Put)"""
    symbol: str
    fyers_symbol: str
    strike: float
    expiry: date
    option_type: str  # CE or PE
    ltp: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    volume: int = 0
    oi: int = 0  # Open Interest
    iv: float = 0.0  # Implied Volatility
    greeks: dict = field(default_factory=dict)
    change: float = 0.0
    change_pct: float = 0.0
    prev_close: float = 0.0


@dataclass
class OptionStrike:
    """Represents a strike price with both Call and Put options"""
    strike_price: float
    call: OptionLeg | None = None
    put: OptionLeg | None = None


@dataclass
class OptionChainData:
    """Complete option chain for an underlying"""
    underlying: str
    spot_price: float
    expiry: date
    strikes: list[OptionStrike]
    timestamp: datetime = field(default_factory=datetime.now)

    def get_atm_strike(self) -> float:
        """Get the at-the-money strike"""
        if not self.strikes:
            return 0.0

        min_diff = float('inf')
        atm_strike = self.strikes[0].strike_price

        for strike in self.strikes:
            diff = abs(strike.strike_price - self.spot_price)
            if diff < min_diff:
                min_diff = diff
                atm_strike = strike.strike_price

        return atm_strike

    def get_strike(self, strike_price: float) -> OptionStrike | None:
        """Get a specific strike"""
        for strike in self.strikes:
            if strike.strike_price == strike_price:
                return strike
        return None


class OptionChainService:
    """
    Service for fetching and managing option chain data.
    Wraps Fyers optionchain API with caching and Greeks calculation.
    """

    def __init__(self):
        self._cache: dict[str, dict] = {}  # symbol -> {expiry -> (data, timestamp)}
        self._cache_ttl = 5  # 5 seconds for live market
        self._fyers_client = None

    def _get_fyers_client(self):
        """Lazy load Fyers client"""
        if self._fyers_client is None:
            from ..services.fyers_client import get_fyers_client
            self._fyers_client = get_fyers_client()
        return self._fyers_client

    def _underlying_to_fyers(self, underlying: str) -> str:
        """Convert underlying symbol to Fyers format for option chain"""
        underlying = underlying.upper()

        # Map common symbols to Fyers index format
        index_map = {
            "NIFTY": "NSE:NIFTY50-INDEX",
            "NIFTY50": "NSE:NIFTY50-INDEX",
            "BANKNIFTY": "NSE:NIFTYBANK-INDEX",
            "FINNIFTY": "NSE:FINNIFTY-INDEX",
            "MIDCPNIFTY": "NSE:MIDCPNIFTY-INDEX",
        }

        if underlying in index_map:
            return index_map[underlying]

        # For stocks, use symbol master
        return symbol_master.to_fyers(underlying)

    def get_option_chain(
        self,
        underlying: str,
        expiry: date | None = None,
        strike_count: int = 15
    ) -> OptionChainData | None:
        """
        Fetch option chain for an underlying.

        Args:
            underlying: Symbol (e.g., "NIFTY", "BANKNIFTY", "RELIANCE")
            expiry: Specific expiry date (if None, uses nearest expiry)
            strike_count: Number of strikes to return (centered around ATM)

        Returns:
            OptionChainData or None if fetch fails
        """
        cache_key = f"{underlying}_{expiry}"

        # Check cache
        if cache_key in self._cache:
            cached_data, cached_time = self._cache[cache_key]
            if time.time() - cached_time < self._cache_ttl:
                logger.debug(f"Returning cached option chain for {underlying}")
                return cached_data

        # Fetch from Fyers
        try:
            fyers_symbol = self._underlying_to_fyers(underlying)
            client = self._get_fyers_client()

            if not client or not client.fyers:
                logger.error("Fyers client not available")
                return None

            # Get spot price first
            spot_price = self._get_spot_price(fyers_symbol)
            if spot_price == 0:
                logger.error(f"Could not get spot price for {underlying}")
                return None

            # Fetch option chain from Fyers
            # Format: {"symbols": "NSE:NIFTY50-INDEX", "strikeCount": 20}
            data = {
                "symbol": fyers_symbol,
                "strikeCount": strike_count * 2  # Get more strikes than needed
            }

            response = client.fyers.optionchain(data=data)

            if response.get("s") != "ok":
                logger.error(f"Fyers option chain error: {response}")
                return None

            # Parse response
            option_chain = self._parse_option_chain(
                response, underlying, spot_price, expiry, strike_count
            )

            # Cache result
            self._cache[cache_key] = (option_chain, time.time())

            return option_chain

        except Exception as e:
            logger.error(f"Error fetching option chain for {underlying}: {e}")
            return None

    def _get_spot_price(self, fyers_symbol: str) -> float:
        """Get current spot price for underlying"""
        try:
            client = self._get_fyers_client()
            if not client or not client.fyers:
                return 0.0

            response = client.fyers.quotes({"symbols": fyers_symbol})

            if response.get("s") == "ok" and "d" in response:
                quotes = response["d"]
                if quotes:
                    return float(quotes[0].get("v", {}).get("lp", 0))

            return 0.0
        except Exception as e:
            logger.error(f"Error getting spot price: {e}")
            return 0.0

    def _parse_option_chain(
        self,
        response: dict,
        underlying: str,
        spot_price: float,
        target_expiry: date | None,
        strike_count: int
    ) -> OptionChainData:
        """Parse Fyers option chain response"""

        strikes_data = response.get("data", {})

        # Get available expiries
        expiries = []
        for strike_data in strikes_data.values():
            if isinstance(strike_data, dict):
                for exp_date_str in strike_data.keys():
                    try:
                        exp_date = datetime.strptime(exp_date_str, "%Y-%m-%d").date()
                        if exp_date not in expiries:
                            expiries.append(exp_date)
                    except ValueError:
                        continue

        expiries.sort()

        # Select expiry
        if target_expiry:
            selected_expiry = target_expiry
        elif expiries:
            selected_expiry = expiries[0]  # Nearest expiry
        else:
            selected_expiry = date.today()

        # Parse strikes
        strikes = []
        for strike_price_str, expiries_data in strikes_data.items():
            try:
                strike_price = float(strike_price_str)
            except ValueError:
                continue

            # Get data for selected expiry
            expiry_key = selected_expiry.strftime("%Y-%m-%d")
            expiry_data = expiries_data.get(expiry_key, {}) if isinstance(expiries_data, dict) else {}

            if not expiry_data:
                continue

            # Parse CE (Call) data
            ce_data = expiry_data.get("CE", {})
            ce_leg = None
            if ce_data:
                ce_leg = self._create_option_leg(
                    ce_data, strike_price, selected_expiry, "CE", spot_price
                )

            # Parse PE (Put) data
            pe_data = expiry_data.get("PE", {})
            pe_leg = None
            if pe_data:
                pe_leg = self._create_option_leg(
                    pe_data, strike_price, selected_expiry, "PE", spot_price
                )

            if ce_leg or pe_leg:
                strikes.append(OptionStrike(
                    strike_price=strike_price,
                    call=ce_leg,
                    put=pe_leg
                ))

        # Sort by strike
        strikes.sort(key=lambda x: x.strike_price)

        # Filter to center around ATM
        if strikes:
            atm_strike = min(strikes, key=lambda s: abs(s.strike_price - spot_price)).strike_price
            atm_index = next(i for i, s in enumerate(strikes) if s.strike_price == atm_strike)

            start_idx = max(0, atm_index - strike_count // 2)
            end_idx = min(len(strikes), start_idx + strike_count)
            strikes = strikes[start_idx:end_idx]

        return OptionChainData(
            underlying=underlying,
            spot_price=spot_price,
            expiry=selected_expiry,
            strikes=strikes,
            timestamp=datetime.now()
        )

    def _create_option_leg(
        self,
        data: dict,
        strike: float,
        expiry: date,
        option_type: str,
        spot_price: float
    ) -> OptionLeg:
        """Create OptionLeg from Fyers data"""

        ltp = float(data.get("ltp", 0))
        bid = float(data.get("bid", 0))
        ask = float(data.get("ask", 0))
        volume = int(data.get("volume", 0))
        oi = int(data.get("oi", 0))
        change = float(data.get("ch", 0))
        change_pct = float(data.get("chp", 0))
        prev_close = float(data.get("prev_close_price", 0))

        # Get symbol
        symbol = data.get("symbol", "")
        fyers_symbol = symbol

        # Calculate time to expiry in years
        tte = self._calculate_tte(expiry)

        # Calculate IV and Greeks if we have a price
        iv = 0.0
        greeks = {}

        if ltp > 0 and tte > 0:
            try:
                iv = calculate_implied_volatility(
                    option_price=ltp,
                    s=spot_price,
                    k=strike,
                    t=tte,
                    r=RISK_FREE_RATE,
                    option_type='call' if option_type == 'CE' else 'put'
                )

                greeks = get_option_greeks(
                    s=spot_price,
                    k=strike,
                    t=tte,
                    r=RISK_FREE_RATE,
                    sigma=iv,
                    option_type='call' if option_type == 'CE' else 'put'
                )
            except Exception as e:
                logger.debug(f"Could not calculate Greeks: {e}")

        return OptionLeg(
            symbol=symbol_master.to_db(symbol),
            fyers_symbol=fyers_symbol,
            strike=strike,
            expiry=expiry,
            option_type=option_type,
            ltp=ltp,
            bid=bid,
            ask=ask,
            volume=volume,
            oi=oi,
            iv=iv,
            greeks=greeks,
            change=change,
            change_pct=change_pct,
            prev_close=prev_close
        )

    def _calculate_tte(self, expiry: date) -> float:
        """Calculate time to expiry in years"""
        now = datetime.now()
        expiry_datetime = datetime.combine(expiry, datetime.strptime("15:30", "%H:%M").time())

        if now > expiry_datetime:
            return 0.001  # Minimum time

        # Calculate trading hours remaining (6.25 hours per day)
        time_diff = expiry_datetime - now
        hours_remaining = time_diff.total_seconds() / 3600

        # Trading hours per year: 6.25 * 252 = 1575
        trading_hours_per_year = 6.25 * 252

        return max(hours_remaining / trading_hours_per_year, 0.001)

    def get_expiries(self, underlying: str) -> list[date]:
        """Get list of available expiry dates for an underlying"""
        try:
            # Fetch full chain to get expiries
            chain = self.get_option_chain(underlying, strike_count=5)
            if chain:
                # Get expiries from response
                fyers_symbol = self._underlying_to_fyers(underlying)
                client = self._get_fyers_client()

                if client and client.fyers:
                    data = {"symbol": fyers_symbol, "strikeCount": 5}
                    response = client.fyers.optionchain(data=data)

                    if response.get("s") == "ok":
                        expiries = set()
                        for strike_data in response.get("data", {}).values():
                            if isinstance(strike_data, dict):
                                for exp_date_str in strike_data.keys():
                                    try:
                                        exp_date = datetime.strptime(exp_date_str, "%Y-%m-%d").date()
                                        expiries.add(exp_date)
                                    except ValueError:
                                        continue
                        return sorted(list(expiries))

            return []
        except Exception as e:
            logger.error(f"Error getting expiries for {underlying}: {e}")
            return []

    def get_atm_strike(self, underlying: str, expiry: date | None = None) -> float:
        """Get ATM strike for an underlying"""
        chain = self.get_option_chain(underlying, expiry, strike_count=5)
        if chain:
            return chain.get_atm_strike()
        return 0.0

    def get_greeks(
        self,
        underlying: str,
        strike: float,
        option_type: str,
        expiry: date | None = None
    ) -> dict:
        """Get Greeks for a specific option"""
        chain = self.get_option_chain(underlying, expiry, strike_count=20)
        if not chain:
            return {}

        strike_data = chain.get_strike(strike)
        if not strike_data:
            return {}

        leg = strike_data.call if option_type.upper() == "CE" else strike_data.put
        if leg:
            return leg.greeks

        return {}

    def clear_cache(self):
        """Clear the option chain cache"""
        self._cache.clear()
        logger.info("Option chain cache cleared")


# Singleton instance
option_chain_service = OptionChainService()

"""
Opening Range Breakout (ORB) Strategy
Intraday strategy that trades breakouts from opening range
"""

from typing import Dict, Optional
from datetime import datetime, time, timedelta
import pandas as pd
import numpy as np
from .base_strategy import BaseStrategy, Signal, Position
from .black_scholes import price_synthetic_option
from .atr_utils import calculate_atr


class ORBStrategy(BaseStrategy):
    """
    Opening Range Breakout Strategy
    
    Strategy Logic:
    1. Define opening range (first N minutes after market open)
    2. Trade breakout/breakdown from this range
    3. Use stop loss and take profit for risk management
    4. Close all positions before market close
    
    Parameters:
    - opening_range_minutes: Duration of opening range (default: 5)
    - stop_loss_pct: Stop loss percentage (default: 0.5)
    - take_profit_pct: Take profit percentage (default: 1.5)
    - max_positions_per_day: Maximum positions per day (default: 1)
    - trade_type: 'options' or 'equity' (default: 'options')
    - days_to_expiry: Days to option expiration (default: 5)
    """
    
    def __init__(self, params: Dict):
        super().__init__(params)
        
        # Strategy parameters
        self.opening_range_minutes = params.get('opening_range_minutes', 5)
        
        # ATR-based stops (multipliers, not percentages)
        self.stop_loss_atr_multiplier = params.get('stop_loss_atr_multiplier', 0.5)
        self.take_profit_atr_multiplier = params.get('take_profit_atr_multiplier', 1.5)
        
        # For fallback if ATR can't be calculated
        self.stop_loss_pct = params.get('stop_loss_pct', 20)  # 20% fallback
        self.take_profit_pct = params.get('take_profit_pct', 30)  # 30% fallback
        
        self.max_positions_per_day = params.get('max_positions_per_day', 1)
        self.trade_type = params.get('trade_type', 'options')
        self.days_to_expiry = params.get('days_to_expiry', 5)  # Weekly options default
        
        # State variables
        self.opening_range_high = None
        self.opening_range_low = None
        self.range_calculated = False
        self.trades_today = 0
        self.current_date = None
        
        # Market timings (IST)
        # DB is UTC, so we convert IST -> UTC for comparisons
        # 09:15 IST = 03:45 UTC
        # 15:15 IST = 09:45 UTC
        
        self.market_open = time(9, 15)  # IST (Display/Config)
        self.market_close = time(15, 15) # IST (Display/Config)
        
        # Derived UTC timings for data comparison
        self.market_open_utc = time(3, 45)
        self.market_close_utc = time(9, 45)
        
    def reset(self):
        """Reset strategy state for new backtest"""
        super().reset()
        self.opening_range_high = None
        self.opening_range_low = None
        self.opening_range_calculated = False
        self.trades_today = 0
        self.current_date = None
    
    def _calculate_opening_range(self, data: pd.DataFrame, current_time: datetime) -> bool:
        """
        Calculate opening range from first N minutes of data
        """
        # Check if we're past opening range period
        # Use UTC timings
        market_open_today = datetime.combine(current_time.date(), self.market_open_utc)
        opening_range_end = market_open_today + timedelta(minutes=self.opening_range_minutes)
        
        if current_time < opening_range_end:
            return False
        
        # Get candles from opening range period
        # Data timestamps are UTC, so this comparison works
        opening_candles = data[
            (data['timestamp'] >= market_open_today) &
            (data['timestamp'] <= opening_range_end)
        ]
        
        if len(opening_candles) == 0:
            return False
        
        self.opening_range_high = opening_candles['high'].max()
        self.opening_range_low = opening_candles['low'].min()
        self.opening_range_calculated = True
        
        return True
    
    def on_data(self, current_data: pd.DataFrame, historical_data: pd.DataFrame) -> Optional[Signal]:
        """
        Process new data and generate signals
        """
        if current_data.empty:
            return None
        
        current_row = current_data.iloc[0]
        current_time = current_row['timestamp'] # UTC from DB
        current_price = current_row['close']
        
        # Reset daily state if new day
        if self.current_date != current_time.date():
            self.current_date = current_time.date()
            self.trades_today = 0
            self.opening_range_calculated = False
            self.opening_range_high = None
            self.opening_range_low = None
        
        # Don't trade if max positions reached
        if self.trades_today >= self.max_positions_per_day:
            return None
        
        # Don't trade if already have open position
        if len(self.positions) > 0:
            return None
        
        # Don't trade near market close (Use UTC)
        if current_time.time() >= self.market_close_utc:
            return None
        
        # Calculate opening range if not done yet
        if not self.opening_range_calculated:
            self._calculate_opening_range(historical_data, current_time)
            return None
        
        # Check for breakout/breakdown
        signal_type = None
        instrument_suffix = None
        
        # Breakout - Buy CE (Call)
        if current_price > self.opening_range_high:
            signal_type = 'BUY'
            instrument_suffix = 'CE' if self.trade_type == 'options' else 'EQ'
        
        # Breakdown - Buy PE (Put)
        elif current_price < self.opening_range_low:
            signal_type = 'BUY'
            instrument_suffix = 'PE' if self.trade_type == 'options' else 'EQ'
        
        if signal_type:
            # Set default entry price (spot)
            entry_price = current_price
            
            if self.trade_type == 'options':
                # Round price to nearest 50 for strike
                strike = round(current_price / 50) * 50
                instrument = f"{self.params.get('symbol', 'STOCK')} {strike} {instrument_suffix}"
                
                # Calculate option premium with dynamic volatility and accurate expiry
                entry_price = price_synthetic_option(
                    underlying_price=current_price,
                    option_type=instrument_suffix,  # CE or PE
                    strike=strike,
                    days_to_expiry=7  # Weekly options
                )
            else:
                instrument = f"{self.params.get('symbol', 'STOCK')}-EQ"
            
            # Calculate ATR-based stop loss and take profit
            # Use last 20 candles for ATR calculation
            try:
                atr = calculate_atr(historical_data.tail(20))
                
                # ATR-based stops (more dynamic and volatility-adaptive)
                stop_loss = entry_price - (atr * self.stop_loss_atr_multiplier)
                take_profit = entry_price + (atr * self.take_profit_atr_multiplier)
                
            except Exception as e:
                # Fallback to percentage-based if ATR calculation fails
                stop_loss = entry_price * (1 - self.stop_loss_pct / 100)
                take_profit = entry_price * (1 + self.take_profit_pct / 100)
            
            self.trades_today += 1
            
            return Signal(
                timestamp=current_time,
                signal_type=signal_type,
                instrument=instrument,
                entry_price=entry_price,
                quantity=1,  # Will be calculated by position sizing
                stop_loss=stop_loss,
                take_profit=take_profit,
                metadata={
                    'opening_range_high': self.opening_range_high,
                    'opening_range_low': self.opening_range_low,
                    'breakout_type': 'UP' if instrument_suffix == 'CE' else 'DOWN'
                }
            )
        
        return None

    def calculate_position_size(self, price: float, capital: float, stop_loss: float = None) -> int:
        """
        Calculate position size based on Risk Amount (passed as 'capital')
        
        Args:
            price: Entry price
            capital: Total risk amount (e.g. 2% of equity)
            stop_loss: Actual stop loss price (optional)
        """
        if price <= 0:
            return 0
            
        # Determine Lot Size
        symbol = self.params.get('symbol', '').upper()
        if 'NIFTY' in symbol and 'BANK' not in symbol:
            lot_size = 75
        elif 'BANKNIFTY' in symbol:
            lot_size = 15
        else:
            lot_size = 1
            
        # Calculate Risk Per Unit
        if stop_loss and stop_loss > 0:
            # Use actual stop loss distance
            risk_per_unit = abs(price - stop_loss)
        else:
            # Fallback to percentage if SL not provided
            risk_per_unit = price * (self.stop_loss_pct / 100)
        
        if risk_per_unit <= 0:
            # Fallback if SL is 0
            qty = int(capital / price)
        else:
            # Qty = Risk Amount / Risk Per Unit
            qty = int(capital / risk_per_unit)
            
        # Round to nearest lot size
        if lot_size > 1:
            rounded_qty = (qty // lot_size) * lot_size
            # If calculated qty is less than 1 lot, use 1 lot minimum
            if rounded_qty == 0 and qty > 0:
                qty = lot_size  # Use 1 lot minimum
            else:
                qty = rounded_qty
            
        return max(qty, 0)
    
    def should_exit(self, position: Position, current_price: float, current_time: datetime) -> bool:
        """
        Check if position should be exited
        """
        # Time-based exit (Use UTC check)
        if current_time.time() >= self.market_close_utc:
            return True
        
        # Calculate current valuation of the position (Premium)
        current_val = current_price
        
        if self.trade_type == 'options':
             # Use Black-Scholes to get current premium
             current_val = self.get_exit_price(position, current_price, current_time)
        
        # Update current price in position tracking
        position.current_price = current_val
        
        # Stop loss & Take Profit checks
        if current_val <= position.stop_loss:
            return True
        if current_val >= position.take_profit:
            return True
        
        return False

    def get_exit_price(self, position: Position, current_spot_price: float, current_time: datetime) -> float:
        """
        Calculate exit price for a position (especially for options)
        """
        if self.trade_type != 'options':
            return current_spot_price
            
        try:
            # Parse instrument to get strike and type
            # Format: "SYMBOL STRIKE TYPE" e.g., "RELIANCE 2400 CE"
            parts = position.instrument.split()
            if len(parts) >= 3:
                strike = float(parts[-2])
                op_type = parts[-1] # CE or PE
                
                # Calculate synthetic option price based on current spot price
                return price_synthetic_option(
                    underlying_price=current_spot_price,
                    option_type=op_type.replace('CE', 'call').replace('PE', 'put'),
                    strike=strike,
                    days_to_expiry=self.days_to_expiry
                )
        except Exception as e:
            # Fallback to spot price or 0 if error
            print(f"Error calculating exit price: {e}")
            return current_spot_price
            
        return current_spot_price

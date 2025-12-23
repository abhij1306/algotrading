from datetime import datetime
from typing import Dict, Any, List, Optional
from ..base import IBroker, OrderResponse, Position, BrokerFunds

class BacktestBroker(IBroker):
    """
    Mock Broker for Backtesting.
    Maintains in-memory state and simulates order execution.
    Controlled by an external clock via update_market_state().
    """
    
    def __init__(self, initial_capital: float = 100000.0, commission_pct: float = 0.03, slippage_pct: float = 0.05):
        self.initial_capital = initial_capital
        self.available_balance = initial_capital
        self.used_margin = 0.0
        self.commission_pct = commission_pct
        self.slippage_pct = slippage_pct
        
        self.positions: Dict[str, Position] = {} # Key: symbol
        self.orders: List[Dict] = []
        self.trades: List[Dict] = []
        
        # Current Market State (Updated by Engine)
        self.current_time = datetime.now()
        self.market_prices: Dict[str, float] = {}
        
    def update_market_state(self, timestamp: datetime, prices: Dict[str, float]):
        """Advance time and update prices for simulation"""
        self.current_time = timestamp
        self.market_prices.update(prices)
        self._mark_to_market()

    def _mark_to_market(self):
        """Update PnL for all open positions"""
        for symbol, pos in self.positions.items():
            current_price = self.market_prices.get(symbol, pos['current_price'])
            pos['current_price'] = current_price
            
            # Calculate Unrealized PnL
            if pos['side'] == 'LONG':
                 pos['pnl'] = (current_price - pos['entry_price']) * pos['quantity']
            else:
                 pos['pnl'] = (pos['entry_price'] - current_price) * pos['quantity']
                 
    def get_quotes(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """Return current simulated quotes"""
        quotes = {}
        for sym in symbols:
            price = self.market_prices.get(sym, 0.0)
            quotes[sym] = {"ltp": price, "symbol": sym}
        return quotes

    def get_quote(self, symbol: str) -> Dict[str, Any]:
        """Get live quote (LTP, etc)."""
        price = self.market_prices.get(symbol, 0.0)
        return {"ltp": price, "symbol": symbol}

    def get_funds(self) -> BrokerFunds:
        # Calculate total equity
        unrealized_pnl = sum(p['pnl'] for p in self.positions.values())
        total_equity = self.available_balance + self.used_margin + unrealized_pnl
        
        return BrokerFunds(
            available=self.available_balance,
            used=self.used_margin,
            total=total_equity
        )

    def get_positions(self) -> List[Position]:
        return list(self.positions.values())
        
    def get_orders(self) -> List[Dict[str, Any]]:
        return self.orders
        
    def place_order(self, order: Dict[str, Any]) -> OrderResponse:
        symbol = order['symbol']
        quantity = int(order['quantity'])
        side = order['side'].upper() # BUY/SELL
        
        current_price = self.market_prices.get(symbol)
        if not current_price:
            return OrderResponse(
                order_id="", status="REJECTED", message=f"No price data for {symbol}", details={}
            )

        # Simulate Fill Price (Slippage)
        # BUY: Higher, SELL: Lower (Simplified View for conservative backtest?)
        # Standard: Slippage makes entry worse.
        # Buy at Ask (Price + Slip), Sell at Bid (Price - Slip)
        if side == 'BUY':
            fill_price = current_price * (1 + self.slippage_pct/100)
        else:
            fill_price = current_price * (1 - self.slippage_pct/100)
            
        trade_value = fill_price * quantity
        commission = trade_value * (self.commission_pct / 100)
        
        # Check Funds (Simple Check)
        if side == 'BUY':
            cost = trade_value + commission
            if cost > self.available_balance:
                 return OrderResponse(order_id="", status="REJECTED", message="Insufficient Funds", details={})
            self.available_balance -= cost
            self.used_margin += trade_value # Simplified
        else:
            # SELL
            # Assuming closing position or shorting?
            # Creating complex netting logic here is hard.
            # Reuse logic from PaperBroker for netting?
            # For Backtest Parity, we need netting.
            pass
            
        # Execute Netting Impl
        self._handle_execution(symbol, side, quantity, fill_price, commission)
        
        order_id = f"BT-ORD-{len(self.orders)+1}"
        
        return OrderResponse(
            order_id=order_id,
            status="FILLED",
            message="Backtest Fill",
            details={"average_price": fill_price, "commission": commission}
        )
        
    def _handle_execution(self, symbol: str, side: str, qty: int, price: float, commission: float):
        """Handle position update (Netting)"""
        # Mapping BUY/SELL to LONG/SHORT logic requires knowing current position
        
        # Simplified:
        # BUY -> Open LONG or Close SHORT
        # SELL -> Open SHORT or Close LONG
        
        position = self.positions.get(symbol)
        
        if position:
            # Existing Position
            if (position['side'] == 'LONG' and side == 'BUY') or \
               (position['side'] == 'SHORT' and side == 'SELL'):
                # Adding to position
                total_qty = position['quantity'] + qty
                new_avg = ((position['quantity'] * position['entry_price']) + (qty * price)) / total_qty
                position['quantity'] = total_qty
                position['entry_price'] = new_avg
                position['current_price'] = price
                # Margin update handled in place_order somewhat
            else:
                # Square off (partial or full)
                if position['quantity'] > qty:
                    # Partial
                    position['quantity'] -= qty
                    # Realize PnL
                    pnl = (price - position['entry_price']) * qty if position['side'] == 'LONG' else (position['entry_price'] - price) * qty
                    self.available_balance += (qty * position['entry_price']) + pnl # Return margin + pnl
                    self.used_margin -= (qty * position['entry_price'])
                elif position['quantity'] == qty:
                    # Full Close
                    pnl = (price - position['entry_price']) * qty if position['side'] == 'LONG' else (position['entry_price'] - price) * qty
                    self.available_balance += (qty * position['entry_price']) + pnl
                    self.used_margin -= (qty * position['entry_price'])
                    del self.positions[symbol]
                else:
                    # Reverse position
                    # Close existing
                    pnl = (price - position['entry_price']) * position['quantity'] if position['side'] == 'LONG' else (position['entry_price'] - price) * position['quantity']
                    self.available_balance += (position['quantity'] * position['entry_price']) + pnl
                    self.used_margin -= (position['quantity'] * position['entry_price'])
                    
                    # Open new remainder
                    new_qty = qty - position['quantity']
                    new_side = 'LONG' if side == 'BUY' else 'SHORT'
                    
                    new_trade_val = new_qty * price
                    self.available_balance -= new_trade_val
                    self.used_margin += new_trade_val
                    
                    self.positions[symbol] = Position(
                        symbol=symbol,
                        side=new_side,
                        quantity=new_qty,
                        entry_price=price,
                        current_price=price,
                        pnl=0.0 - commission, # Commission affects equity not pnl field usually?
                        product_type='INTRADAY'
                    )
        else:
            # New Position
            new_side = 'LONG' if side == 'BUY' else 'SHORT'
            self.positions[symbol] = Position(
                symbol=symbol,
                side=new_side,
                quantity=qty,
                entry_price=price,
                current_price=price,
                pnl=0.0 - commission,
                product_type='INTRADAY'
            )
            # Monkey patch entry_time onto the dict/object for tracking
            self.positions[symbol]['entry_time'] = self.current_time
            
        # Log execution to internal history
        self.trades.append({
            'timestamp': self.current_time,
            'symbol': symbol,
            'direction': side, 
            'quantity': qty,
            'price': price,
            'entry_price': price, # Needed for Engine
            'exit_price': 0.0,
            'commission': commission,
            'pnl': 0.0,
            'action': 'OPEN' if not position else 'MODIFY',
            'status': 'OPEN'
        }) 
        
        # If we closed a position, we should update the PnL of the *closing* trade log if possible
        # Or better: keep a separate list of "Closed Trades" which is what the frontend expects usually.
        # The frontend expects: entry_time, exit_time, entry_price, exit_price, pnl
        
        # We MUST log completed round-trips here for the Engine to pick up.
        
        if position:
             # Check if we reduced/closed position
            if (position['side'] == 'LONG' and side == 'SELL') or \
               (position['side'] == 'SHORT' and side == 'BUY'):
                   
                   # This was a closing trade (partial or full)
                   # We need to log it as a "Closed Trade" for the report
                   
                   pnl = (price - position['entry_price']) * qty if position['side'] == 'LONG' else (position['entry_price'] - price) * qty
                   
                   self.trades.append({
                       'entry_time': position.get('entry_time', self.current_time), # Correct dict access
                       'exit_time': self.current_time,
                       'symbol': symbol,
                       'direction': position['side'],
                       'entry_price': position['entry_price'],
                       'exit_price': price,
                       'quantity': qty,
                       'pnl': pnl,
                       'status': 'CLOSED'
                   })

    def cancel_order(self, order_id: str):
        pass
    
    def modify_order(self, order_id: str, new_price: float):
        pass
    
    def get_order_status(self, order_id: str):
        pass
        
    def get_holdings(self):
        return []

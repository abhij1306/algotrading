"""
Example usage of the TradingDatabase library
"""

from trading_db import TradingDatabase
from datetime import date, datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

def main():
    """Demonstrate database operations"""
    
    print("🗄️  TradingDatabase Example Usage")
    print("=" * 60)
    
    # Initialize database
    print("\n1️⃣ Initializing database connection...")
    db = TradingDatabase()
    
    # Create schema
    print("\n2️⃣ Creating database schema...")
    db.initialize_schema()
    
    # Insert sample stock prices
    print("\n3️⃣ Inserting sample stock prices...")
    sample_prices = [
        {
            'symbol': 'RELIANCE',
            'date': date(2024, 12, 9),
            'open': 1250.50,
            'high': 1265.75,
            'low': 1245.00,
            'close': 1260.25,
            'volume': 5000000
        },
        {
            'symbol': 'RELIANCE',
            'date': date(2024, 12, 10),
            'open': 1260.00,
            'high': 1275.50,
            'low': 1255.00,
            'close': 1270.75,
            'volume': 5500000
        }
    ]
    db.insert_stock_prices(sample_prices)
    
    # Retrieve stock prices
    print("\n4️⃣ Retrieving stock prices...")
    prices = db.get_stock_prices('RELIANCE', limit=10)
    print(f"Found {len(prices)} price records:")
    for price in prices:
        print(f"  {price['date']}: Close = ₹{price['close']}")
    
    # Insert order
    print("\n5️⃣ Inserting sample order...")
    order_data = {
        'order_id': 'ORD12345',
        'symbol': 'RELIANCE',
        'exchange': 'NSE',
        'order_type': 'LIMIT',
        'transaction_type': 'BUY',
        'quantity': 10,
        'price': 1260.00,
        'trigger_price': None,
        'status': 'COMPLETE',
        'order_timestamp': datetime.now()
    }
    db.insert_order(order_data)
    
    # Retrieve orders
    print("\n6️⃣ Retrieving orders...")
    orders = db.get_orders(symbol='RELIANCE')
    print(f"Found {len(orders)} orders")
    
    # Insert position
    print("\n7️⃣ Inserting sample position...")
    position_data = {
        'symbol': 'RELIANCE',
        'exchange': 'NSE',
        'product': 'CNC',
        'quantity': 10,
        'average_price': 1260.00,
        'last_price': 1270.75,
        'pnl': 107.50,
        'date': date.today()
    }
    db.upsert_position(position_data)
    
    # Retrieve positions
    print("\n8️⃣ Retrieving positions...")
    positions = db.get_positions()
    print(f"Found {len(positions)} positions:")
    for pos in positions:
        print(f"  {pos['symbol']}: Qty={pos['quantity']}, P&L=₹{pos['pnl']}")
    
    # Close database
    print("\n9️⃣ Closing database connection...")
    db.close()
    
    print("\n" + "=" * 60)
    print("✅ All database operations completed successfully!")

if __name__ == "__main__":
    main()

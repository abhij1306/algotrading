"""
PostgreSQL Database Library for AlgoTrading
Handles all database operations for trading data
"""

import psycopg2
from psycopg2 import pool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from datetime import datetime
import os
from typing import Optional, List, Dict, Any

class TradingDatabase:
    """PostgreSQL database handler for trading data"""
    
    def __init__(self, **db_config):
        """
        Initialize database connection pool
        
        Args:
            db_config: Database configuration (host, port, database, user, password)
        """
        self.db_config = db_config or {
            'host': os.getenv('DB_HOST', 'localhost'),
            'port': os.getenv('DB_PORT', '5432'),
            'database': os.getenv('DB_NAME', 'algotrading'),
            'user': os.getenv('DB_USER', 'postgres'),
            'password': os.getenv('DB_PASSWORD', 'postgres')
        }
        
        try:
            self.connection_pool = psycopg2.pool.SimpleConnectionPool(
                1, 20,  # Min and max connections
                **self.db_config
            )
            print(f"✅ Database connection pool created successfully")
        except Exception as e:
            print(f"❌ Error creating connection pool: {str(e)}")
            raise
    
    @contextmanager
    def get_cursor(self, dict_cursor=True):
        """
        Context manager for database cursor
        
        Args:
            dict_cursor: If True, returns RealDictCursor for dict-like results
        """
        connection = self.connection_pool.getconn()
        try:
            cursor = connection.cursor(
                cursor_factory=RealDictCursor if dict_cursor else None
            )
            yield cursor
            connection.commit()
        except Exception as e:
            connection.rollback()
            raise e
        finally:
            cursor.close()
            self.connection_pool.putconn(connection)
    
    def initialize_schema(self):
        """Create all necessary tables"""
        with self.get_cursor(dict_cursor=False) as cursor:
            # Stock prices table (OHLCV data)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS stock_prices (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    date DATE NOT NULL,
                    open DECIMAL(12, 2),
                    high DECIMAL(12, 2),
                    low DECIMAL(12, 2),
                    close DECIMAL(12, 2),
                    volume BIGINT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, date)
                );
                
                CREATE INDEX IF NOT EXISTS idx_stock_prices_symbol_date 
                ON stock_prices(symbol, date DESC);
            """)
            
            # Orders table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS orders (
                    id SERIAL PRIMARY KEY,
                    order_id VARCHAR(50) UNIQUE NOT NULL,
                    symbol VARCHAR(20) NOT NULL,
                    exchange VARCHAR(20),
                    order_type VARCHAR(20),
                    transaction_type VARCHAR(10),
                    quantity INTEGER,
                    price DECIMAL(12, 2),
                    trigger_price DECIMAL(12, 2),
                    status VARCHAR(20),
                    order_timestamp TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                
                CREATE INDEX IF NOT EXISTS idx_orders_symbol ON orders(symbol);
                CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            """)
            
            # Positions table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS positions (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    exchange VARCHAR(20),
                    product VARCHAR(20),
                    quantity INTEGER,
                    average_price DECIMAL(12, 2),
                    last_price DECIMAL(12, 2),
                    pnl DECIMAL(12, 2),
                    date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, date)
                );
                
                CREATE INDEX IF NOT EXISTS idx_positions_symbol_date 
                ON positions(symbol, date DESC);
            """)
            
            # Portfolio holdings table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS holdings (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20) NOT NULL,
                    quantity INTEGER,
                    average_price DECIMAL(12, 2),
                    last_price DECIMAL(12, 2),
                    pnl DECIMAL(12, 2),
                    date DATE NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(symbol, date)
                );
            """)
            
            # Trading strategies table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS strategies (
                    id SERIAL PRIMARY KEY,
                    name VARCHAR(100) UNIQUE NOT NULL,
                    description TEXT,
                    parameters JSONB,
                    is_active BOOLEAN DEFAULT TRUE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            
            # Trade logs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS trade_logs (
                    id SERIAL PRIMARY KEY,
                    symbol VARCHAR(20),
                    strategy_id INTEGER REFERENCES strategies(id),
                    action VARCHAR(20),
                    quantity INTEGER,
                    price DECIMAL(12, 2),
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    metadata JSONB
                );
                
                CREATE INDEX IF NOT EXISTS idx_trade_logs_symbol 
                ON trade_logs(symbol, timestamp DESC);
            """)
            
        print("✅ Database schema initialized successfully")
    
    # ========== Stock Prices CRUD ==========
    
    def insert_stock_prices(self, prices_data: List[Dict[str, Any]]):
        """Insert stock prices (bulk insert with conflict handling)"""
        with self.get_cursor(dict_cursor=False) as cursor:
            cursor.executemany("""
                INSERT INTO stock_prices (symbol, date, open, high, low, close, volume)
                VALUES (%(symbol)s, %(date)s, %(open)s, %(high)s, %(low)s, %(close)s, %(volume)s)
                ON CONFLICT (symbol, date) 
                DO UPDATE SET 
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
            """, prices_data)
        print(f"✅ Inserted/Updated {len(prices_data)} price records")
    
    def get_stock_prices(self, symbol: str, start_date: str = None, 
                        end_date: str = None, limit: int = 100) -> List[Dict]:
        """Get stock prices for a symbol"""
        with self.get_cursor() as cursor:
            query = """
                SELECT * FROM stock_prices 
                WHERE symbol = %s
            """
            params = [symbol]
            
            if start_date:
                query += " AND date >= %s"
                params.append(start_date)
            if end_date:
                query += " AND date <= %s"
                params.append(end_date)
            
            query += " ORDER BY date DESC LIMIT %s"
            params.append(limit)
            
            cursor.execute(query, params)
            return cursor.fetchall()
    
    # ========== Orders CRUD ==========
    
    def insert_order(self, order_data: Dict[str, Any]):
        """Insert a new order"""
        with self.get_cursor() as cursor:
            cursor.execute("""
                INSERT INTO orders 
                (order_id, symbol, exchange, order_type, transaction_type, 
                 quantity, price, trigger_price, status, order_timestamp)
                VALUES (%(order_id)s, %(symbol)s, %(exchange)s, %(order_type)s, 
                        %(transaction_type)s, %(quantity)s, %(price)s, 
                        %(trigger_price)s, %(status)s, %(order_timestamp)s)
                ON CONFLICT (order_id) DO UPDATE SET
                    status = EXCLUDED.status,
                    updated_at = CURRENT_TIMESTAMP
                RETURNING id
            """, order_data)
            result = cursor.fetchone()
            print(f"✅ Order inserted/updated: {order_data['order_id']}")
            return result['id']
    
    def get_orders(self, symbol: str = None, status: str = None) -> List[Dict]:
        """Get orders with optional filters"""
        with self.get_cursor() as cursor:
            query = "SELECT * FROM orders WHERE 1=1"
            params = []
            
            if symbol:
                query += " AND symbol = %s"
                params.append(symbol)
            if status:
                query += " AND status = %s"
                params.append(status)
            
            query += " ORDER BY order_timestamp DESC"
            cursor.execute(query, params)
            return cursor.fetchall()
    
    # ========== Positions CRUD ==========
    
    def upsert_position(self, position_data: Dict[str, Any]):
        """Insert or update position"""
        with self.get_cursor(dict_cursor=False) as cursor:
            cursor.execute("""
                INSERT INTO positions 
                (symbol, exchange, product, quantity, average_price, last_price, pnl, date)
                VALUES (%(symbol)s, %(exchange)s, %(product)s, %(quantity)s, 
                        %(average_price)s, %(last_price)s, %(pnl)s, %(date)s)
                ON CONFLICT (symbol, date) DO UPDATE SET
                    quantity = EXCLUDED.quantity,
                    average_price = EXCLUDED.average_price,
                    last_price = EXCLUDED.last_price,
                    pnl = EXCLUDED.pnl
            """, position_data)
        print(f"✅ Position upserted for {position_data['symbol']}")
    
    def get_positions(self, date: str = None) -> List[Dict]:
        """Get positions for a specific date"""
        with self.get_cursor() as cursor:
            if date:
                cursor.execute(
                    "SELECT * FROM positions WHERE date = %s", (date,)
                )
            else:
                cursor.execute("""
                    SELECT DISTINCT ON (symbol) * FROM positions 
                    ORDER BY symbol, date DESC
                """)
            return cursor.fetchall()
    
    # ========== Utilities ==========
    
    def execute_raw_query(self, query: str, params: tuple = None) -> List[Dict]:
        """Execute a raw SQL query"""
        with self.get_cursor() as cursor:
            cursor.execute(query, params)
            if cursor.description:
                return cursor.fetchall()
            return []
    
    def close(self):
        """Close all connections in the pool"""
        if self.connection_pool:
            self.connection_pool.closeall()
            print("✅ Database connections closed")

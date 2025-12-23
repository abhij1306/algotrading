"""
Database Migration: Create Watchlist Table
Run this once to create the missing table.
"""
import sys
sys.path.append('.')
from app.database import engine, Base, Watchlist

print("Creating Watchlist table...")
try:
    # Create only the Watchlist table
    Watchlist.__table__.create(bind=engine, checkfirst=True)
    print("✅ Watchlist table created successfully!")
except Exception as e:
    print(f"❌ Error: {e}")

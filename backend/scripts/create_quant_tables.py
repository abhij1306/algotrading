import sys
import os
sys.path.append(os.getcwd())

from app.database import Base, engine, PortfolioPolicy, ResearchPortfolio

def migrate():
    print("Creating tables...")
    try:
        # This will strictly create new tables without affecting existing ones
        # It might warn about existing tables but won't fail unless there's a code error
        Base.metadata.create_all(bind=engine)
        print("Tables created successfully.")
    except Exception as e:
        print(f"Error creating tables: {e}")

if __name__ == "__main__":
    migrate()

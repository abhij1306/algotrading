import sys
from pathlib import Path
import random
import datetime

# Add parent directory to path to import app modules
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.append(str(BASE_DIR))

from backend.app.database import SessionLocal, UserPortfolio, PortfolioPosition, Company, HistoricalPrice, ComputedRiskMetric

def reseed_portfolio():
    db = SessionLocal()
    try:
        print("🗑️  Cleaning up existing portfolios...")
        # Delete dependent tables first
        db.query(ComputedRiskMetric).delete()
        db.query(PortfolioPosition).delete()
        db.query(UserPortfolio).delete()
        db.commit()

        print("✨ Creating fresh 'Main Portfolio'...")
        # Create single portfolio
        portfolio = UserPortfolio(
            portfolio_name="Main Portfolio",
            description="Active trading portfolio (Auto-Generated)",
            user_id="default_user"
        )
        db.add(portfolio)
        db.commit()
        db.refresh(portfolio)

        # Get active companies that have recent price data
        print("🔍 Finding active stocks with recent data...")
        # Join with HistoricalPrice to ensure we have prices
        active_companies = db.query(Company).join(HistoricalPrice).filter(
            Company.is_active == True,
            HistoricalPrice.date >= (datetime.date.today() - datetime.timedelta(days=5))
        ).group_by(Company.id).all()

        if not active_companies:
            print("⚠️  No active companies found with recent data. Using any active companies.")
            active_companies = db.query(Company).filter(Company.is_active == True).limit(100).all()
        
        if len(active_companies) < 5:
            print("⚠️  Not enough companies found. Adding what makes sense.")
            selected_companies = active_companies
        else:
            selected_companies = random.sample(active_companies, 5)

        print(f"🎲 Selected 5 random stocks: {[c.symbol for c in selected_companies]}")

        # Create positions
        total_allocation = 100.0
        allocations = [random.randint(10, 30) for _ in range(5)]
        # Normalize to sum to 100
        total_rnd = sum(allocations)
        allocations = [round(a / total_rnd * 100, 2) for a in allocations]
        # Adjust last to ensure exactly 100
        allocations[-1] = round(100 - sum(allocations[:-1]), 2)

        for i, company in enumerate(selected_companies):
            # Get latest price
            latest_price = db.query(HistoricalPrice).filter(
                HistoricalPrice.company_id == company.id
            ).order_by(HistoricalPrice.date.desc()).first()
            
            price = latest_price.close if latest_price else 100.0
            
            # Simple assumption: 1L portfolio size
            invested_value = 100000 * (allocations[i] / 100)
            quantity = int(invested_value / price)
            
            position = PortfolioPosition(
                portfolio_id=portfolio.id,
                company_id=company.id,
                quantity=quantity,
                avg_buy_price=price * (1 - random.uniform(-0.05, 0.05)), # Random profit/loss
                invested_value=quantity * price, # Re-calc based on precise qty
                allocation_pct=allocations[i]
            )
            db.add(position)
            print(f"   + Added {company.symbol}: {quantity} qty @ {price}")

        db.commit()
        print("✅ Portfolio reset complete!")

    except Exception as e:
        print(f"❌ Error resetting portfolio: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    reseed_portfolio()

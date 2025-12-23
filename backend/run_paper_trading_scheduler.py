"""
Paper Trading Scheduler
Runs paper trading cycle at regular intervals

Setup:
1. Install: pip install APScheduler
2. Run: python run_paper_trading_scheduler.py

Or use cron:
    # Run every minute during market hours (9:15 AM - 3:30 PM IST)
    */1 9-15 * * 1-5 cd /path/to/backend && python -c "from app.services.paper_trading import run_paper_trading_cycle; import asyncio; asyncio.run(run_paper_trading_cycle())"
"""
import asyncio
import logging
from datetime import datetime, time
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# IST timezone
IST = pytz.timezone('Asia/Kolkata')


async def paper_trading_job():
    """
    Execute paper trading cycle
    Only runs during market hours (9:15 AM - 3:30 PM IST, Mon-Fri)
    """
    try:
        from app.services.paper_trading import run_paper_trading_cycle
        
        # Check if market is open
        now_ist = datetime.now(IST)
        current_time = now_ist.time()
        
        market_open = time(9, 15)    # 9:15 AM
        market_close = time(15, 30)  # 3:30 PM
        
        # Only run during market hours
        if current_time < market_open or current_time > market_close:
            logger.info(f"[PAPER] Market closed. Skipping cycle. Time: {current_time}")
            return
        
        # Check if weekday (Mon=0, Sun=6)
        if now_ist.weekday() >= 5:  # Saturday or Sunday
            logger.info("[PAPER] Weekend. Skipping cycle.")
            return
        
        logger.info(f"[PAPER] Starting cycle at {now_ist.strftime('%Y-%m-%d %H:%M:%S')}")
        
        # Run the paper trading cycle
        await run_paper_trading_cycle()
        
        logger.info("[PAPER] Cycle completed successfully")
        
    except Exception as e:
        logger.error(f"[PAPER] Error in paper trading cycle: {str(e)}", exc_info=True)


def run_scheduler():
    """
    Start the paper trading scheduler
    
    Runs every minute during market hours
    """
    scheduler = AsyncIOScheduler(timezone=IST)
    
    # Schedule paper trading every minute
    # Cron: minute=*/1, hour=9-15, day_of_week=mon-fri
    scheduler.add_job(
        paper_trading_job,
        trigger=CronTrigger(
            minute='*/1',
            hour='9-15',
            day_of_week='mon-fri',
            timezone=IST
        ),
        id='paper_trading',
        name='Paper Trading Cycle',
        replace_existing=True
    )
    
    logger.info("=" * 80)
    logger.info("PAPER TRADING SCHEDULER STARTED")
    logger.info("=" * 80)
    logger.info(f"Timezone: {IST}")
    logger.info("Schedule: Every minute during market hours (9:15 AM - 3:30 PM IST, Mon-Fri)")
    logger.info("Press Ctrl+C to stop")
    logger.info("=" * 80)
    
    scheduler.start()
    
    try:
        # Keep the scheduler running
        asyncio.get_event_loop().run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("\n[PAPER] Shutting down scheduler...")
        scheduler.shutdown()
        logger.info("[PAPER] Scheduler stopped")


if __name__ == "__main__":
    run_scheduler()

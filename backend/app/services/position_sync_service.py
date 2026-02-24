"""
Position Sync Service
=====================
Synchronizes positions from Fyers broker to local database.
Runs periodically during market hours to keep positions in sync.
"""

import asyncio
import logging
from datetime import datetime

from ..database import SessionLocal
from ..models.live_position import LivePosition
from ..utils.cache import cache_with_ttl
from .symbol_master import symbol_master

logger = logging.getLogger(__name__)


class PositionSyncService:
    """
    Service for synchronizing positions from broker to local database.
    """

    def __init__(self):
        self._is_running = False
        self._lock = asyncio.Lock()
        self._task = None
        self._sync_interval = 5  # seconds
        self._broker = None
        self._last_sync: datetime | None = None

    def _get_broker(self):
        """Lazy load Fyers broker"""
        if self._broker is None:
            from ..brokers.plugins.fyers import FyersBroker

            self._broker = FyersBroker()
        return self._broker

    async def start(self):
        """Start the position sync loop with atomic check-and-set"""
        async with self._lock:
            if self._is_running:
                logger.warning("Position sync already running")
                return
            self._is_running = True

        self._task = asyncio.create_task(self._sync_loop())
        logger.info("Position sync service started")

    async def stop(self):
        """Stop the position sync loop"""
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Position sync service stopped")

    async def _sync_loop(self):
        """Main sync loop"""
        from ..utils.market_hours import is_market_hours

        while self._is_running:
            try:
                # Only sync during market hours
                if is_market_hours():
                    # Run blocking sync in thread pool to avoid blocking event loop
                    await asyncio.to_thread(self.sync_positions)
                else:
                    logger.debug("Market closed, skipping position sync")

                await asyncio.sleep(self._sync_interval)

            except Exception as e:
                logger.error(f"Error in position sync loop: {e}")
                await asyncio.sleep(self._sync_interval)

    def sync_positions(self) -> dict:
        """
        Sync positions from Fyers to local database.

        Returns:
            Dict with sync results
        """
        db = SessionLocal()
        try:
            broker = self._get_broker()
            broker_positions = broker.get_positions()

            # Get current positions from DB
            db_positions = db.query(LivePosition).all()
            db_position_map = {p.id: p for p in db_positions}

            synced_count = 0
            updated_count = 0
            closed_count = 0

            # Process broker positions
            current_symbols = set()
            for pos in broker_positions:
                symbol = pos.symbol
                current_symbols.add(symbol)

                # Create composite ID
                position_id = f"default_user-{symbol}-{pos.product_type}"

                # Determine side - handle both LONG/SHORT and BUY/SELL formats
                if pos.side == "LONG" or pos.side == "BUY":
                    side = "LONG"
                elif pos.side == "SHORT" or pos.side == "SELL":
                    side = "SHORT"
                else:
                    logger.warning(f"Unknown side '{pos.side}' for {symbol}, defaulting to LONG")
                    side = "LONG"
                qty = pos.quantity

                if position_id in db_position_map:
                    # Update existing position
                    db_pos = db_position_map[position_id]
                    db_pos.net_qty = qty if side == "LONG" else -qty
                    db_pos.side = side
                    db_pos.ltp = pos.current_price
                    db_pos.net_avg = pos.entry_price
                    db_pos.unrealized_pl = pos.pnl
                    db_pos.pl_total = pos.pnl
                    db_pos.last_synced_at = datetime.now()
                    updated_count += 1
                else:
                    # Create new position
                    new_pos = LivePosition(
                        id=position_id,
                        user_id="default_user",
                        symbol=symbol_master.to_db(symbol),
                        fyers_symbol=symbol,
                        product_type=pos.product_type,
                        side=side,
                        net_qty=qty if side == "LONG" else -qty,
                        buy_qty=qty if side == "LONG" else 0,
                        sell_qty=qty if side == "SHORT" else 0,
                        buy_avg=pos.entry_price if side == "LONG" else 0,
                        sell_avg=pos.entry_price if side == "SHORT" else 0,
                        net_avg=pos.entry_price,
                        ltp=pos.current_price,
                        unrealized_pl=pos.pnl,
                        pl_total=pos.pnl,
                        last_synced_at=datetime.now(),
                    )
                    db.add(new_pos)
                    synced_count += 1

            # Mark closed positions (not in broker but in DB)
            for pos_id, db_pos in db_position_map.items():
                if db_pos.fyers_symbol not in current_symbols and db_pos.net_qty != 0:
                    db_pos.net_qty = 0
                    db_pos.unrealized_pl = 0
                    closed_count += 1

            db.commit()
            self._last_sync = datetime.now()

            result = {
                "status": "success",
                "synced": synced_count,
                "updated": updated_count,
                "closed": closed_count,
                "total_positions": len(broker_positions),
                "timestamp": self._last_sync.isoformat(),
            }

            logger.debug(f"Position sync complete: {result}")
            return result

        except Exception as e:
            db.rollback()
            logger.error(f"Error syncing positions: {e}")
            return {"status": "error", "message": str(e)}
        finally:
            db.close()

    @cache_with_ttl(ttl_seconds=5)
    def get_positions(self, user_id: str = "default_user") -> list[dict]:
        """
        Get current positions from database.
        Cached for 5 seconds to reduce DB load.

        Args:
            user_id: User ID

        Returns:
            List of position dictionaries
        """
        db = SessionLocal()
        try:
            positions = (
                db.query(LivePosition)
                .filter(LivePosition.user_id == user_id, LivePosition.net_qty != 0)
                .all()
            )

            return [
                {
                    "id": p.id,
                    "symbol": p.symbol,
                    "fyers_symbol": p.fyers_symbol,
                    "side": p.side,
                    "quantity": abs(p.net_qty),
                    "net_qty": p.net_qty,
                    "entry_price": p.net_avg,
                    "current_price": p.ltp,
                    "unrealized_pnl": p.unrealized_pl,
                    "product_type": p.product_type,
                    "instrument_type": p.instrument_type,
                    "strike_price": p.strike_price,
                    "expiry_date": p.expiry_date.isoformat() if p.expiry_date else None,
                    "last_synced": p.last_synced_at.isoformat() if p.last_synced_at else None,
                }
                for p in positions
            ]
        finally:
            db.close()

    def get_position_summary(self, user_id: str = "default_user") -> dict:
        """
        Get summary of all positions.

        Args:
            user_id: User ID

        Returns:
            Position summary
        """
        positions = self.get_positions(user_id)

        total_pnl = sum(p["unrealized_pnl"] for p in positions)
        long_value = sum(
            p["current_price"] * p["quantity"] for p in positions if p["side"] == "LONG"
        )
        short_value = sum(
            p["current_price"] * p["quantity"] for p in positions if p["side"] == "SHORT"
        )

        return {
            "total_positions": len(positions),
            "total_pnl": round(total_pnl, 2),
            "long_exposure": round(long_value, 2),
            "short_exposure": round(short_value, 2),
            "total_exposure": round(long_value + short_value, 2),
            "last_sync": self._last_sync.isoformat() if self._last_sync else None,
        }


# Singleton instance
position_sync_service = PositionSyncService()

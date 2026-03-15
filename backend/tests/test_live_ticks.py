"""
Live Ticks End-to-End Test
Tests complete tick flow from Fyers (mocked/real) to Frontend via WebSocket.
"""
import asyncio
import json
import logging
import time

import pytest
import websockets

logger = logging.getLogger(__name__)


@pytest.mark.asyncio
async def test_websocket_ticks() -> None:
    """Test WebSocket tick delivery"""

    # Connect to WebSocket
    uri = "ws://localhost:8000/api/websocket/stream"

    logger.info("Connecting to WebSocket...")
    try:
        async with websockets.connect(uri) as websocket:
            logger.info("Connected")

            # Subscribe to symbols
            symbols = ['SBIN', 'RELIANCE', 'TCS']
            subscribe_msg = {
                "action": "subscribe",
                "symbols": symbols
            }

            logger.info("Subscribing to: %s", symbols)
            await websocket.send(json.dumps(subscribe_msg))

            # Wait for ACK
            response = await websocket.recv()
            ack = json.loads(response)
            logger.info("ACK received: %s", ack)

            # Listen for ticks (timeout after 10 seconds for this test)
            logger.info("Listening for ticks (10s timeout)...")
            received_symbols = set()
            expected_symbols = set(symbols)

            timeout = time.time() + 10
            while time.time() < timeout:
                try:
                    message = await asyncio.wait_for(websocket.recv(), timeout=2.0)
                    data = json.loads(message)

                    if data.get('type') == 'ticker':
                        tick = data['data']
                        symbol = tick.get('symbol')
                        # Fyers format NSE:SBIN-EQ or DB format SBIN depending on conversion
                        ltp = tick.get('ltp') or tick.get('price')

                        logger.info("Tick: %s = ₹%s", symbol, ltp)
                        received_symbols.add(symbol)

                        # Stop if received all symbols
                        if len(received_symbols) >= len(symbols):
                            logger.info("Received ticks for all %s symbols", len(symbols))
                            break

                except TimeoutError:
                    logger.info("No ticks in last 2 seconds (expected if market closed/no mock)")
                    continue

            # Summary
            logger.info("%s", "=" * 50)
            logger.info("TEST SUMMARY")
            logger.info("%s", "=" * 50)
            logger.info("Subscribed to: %s symbols", len(symbols))
            logger.info("Received ticks: %s symbols", len(received_symbols))
            assert ack.get("status") in {"subscribed", "success", "ok", "connected"}
            assert received_symbols, "Expected at least one tick after subscribing"
            assert expected_symbols & received_symbols, "Expected subscribed symbols in tick stream"

    except Exception as e:
        logger.exception("Connection failed: %s", e)
        raise

if __name__ == "__main__":
    # Note: This requires the backend to be running
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:%(name)s:%(message)s")
    logger.setLevel(logging.INFO)
    logger.warning("Ensure backend is running before executing this test.")
    try:
        asyncio.run(test_websocket_ticks())
    except KeyboardInterrupt:
        pass

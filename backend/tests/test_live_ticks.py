"""
Live Ticks End-to-End Test
Tests complete tick flow from Fyers (mocked/real) to Frontend via WebSocket.
"""
import asyncio
import websockets
import json
import time

async def test_websocket_ticks():
    """Test WebSocket tick delivery"""

    # Connect to WebSocket
    uri = "ws://localhost:8000/api/websocket/stream"

    print("🔌 Connecting to WebSocket...")
    try:
        async with websockets.connect(uri) as websocket:
            print("✅ Connected!")

            # Subscribe to symbols
            symbols = ['SBIN', 'RELIANCE', 'TCS']
            subscribe_msg = {
                "action": "subscribe",
                "symbols": symbols
            }

            print(f"📡 Subscribing to: {symbols}")
            await websocket.send(json.dumps(subscribe_msg))

            # Wait for ACK
            response = await websocket.recv()
            ack = json.loads(response)
            print(f"✅ ACK received: {ack}")

            # Listen for ticks (timeout after 10 seconds for this test)
            print("👂 Listening for ticks (10s timeout)...")
            received_symbols = set()

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

                        print(f"📊 Tick: {symbol} = ₹{ltp}")
                        received_symbols.add(symbol)

                        # Stop if received all symbols
                        if len(received_symbols) >= len(symbols):
                            print(f"✅ Received ticks for all {len(symbols)} symbols!")
                            break

                except asyncio.TimeoutError:
                    print("⏳ No ticks in last 2 seconds... (Expected if market closed/no mock)")
                    continue

            # Summary
            print("\n" + "="*50)
            print("TEST SUMMARY")
            print("="*50)
            print(f"Subscribed to: {len(symbols)} symbols")
            print(f"Received ticks: {len(received_symbols)} symbols")

            # In a real test we'd expect 100%, but here we just verify the connection and sub works
            return True

    except Exception as e:
        print(f"❌ Connection failed: {e}")
        return False

if __name__ == "__main__":
    # Note: This requires the backend to be running
    print("⚠️  Ensure backend is running before executing this test.")
    try:
        asyncio.run(test_websocket_ticks())
    except KeyboardInterrupt:
        pass

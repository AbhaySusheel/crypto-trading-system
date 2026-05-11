import asyncio
import websockets
import json
import time

class BinanceStream:
    def __init__(self, symbols):
        self.symbols = symbols

    def get_url(self):
        streams = "/".join([f"{s.lower()}@kline_1m" for s in self.symbols])
        return f"wss://stream.binance.com:9443/stream?streams={streams}"

    async def start(self, on_candle_close):
        url = self.get_url()

        while True:  # 🔥 AUTO RECONNECT LOOP
            try:
                print("🔴 Connecting to Binance WebSocket...")

                async with websockets.connect(
                    url,
                    ping_interval=20,
                    ping_timeout=10
                ) as ws:

                    print("✅ Connected to Binance WebSocket")

                    async for message in ws:
                        data = json.loads(message)

                        if "data" in data:
                            k = data["data"]["k"]

                            candle = {
                                "symbol": k["s"],
                                "open": float(k["o"]),
                                "high": float(k["h"]),
                                "low": float(k["l"]),
                                "close": float(k["c"]),
                                "volume": float(k["v"]),
                                "is_closed": k["x"]
                            }

                            if candle["is_closed"]:
                                print(f"✅ Candle Closed: {candle['symbol']} @ {candle['close']}")
                                await on_candle_close(candle)

            except Exception as e:
                print(f"❌ WebSocket error: {e}")
                print("🔁 Reconnecting in 5 seconds...")
                await asyncio.sleep(5)
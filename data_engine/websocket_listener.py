import asyncio
import json
import logging
import websockets
import redis.asyncio as redis
from datetime import datetime, timezone

# ---------------- CONFIG ----------------
REDIS_URL = "redis://localhost:6379"

SYMBOLS = [
    "btcusdt",
    "ethusdt",
    "bnbusdt",
    "solusdt",
    "xrpusdt"
]

# ---------------------------------------

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("websocket")


def get_ws_url():
    streams = "/".join([f"{s}@aggTrade" for s in SYMBOLS])
    return f"wss://stream.binance.com:9443/stream?streams={streams}"


async def run():
    logger.info("🚀 Starting Multi-Symbol WebSocket...")

    r = redis.from_url(REDIS_URL, decode_responses=True)

    url = get_ws_url()
    logger.info(f"Connecting to: {url}")

    while True:
        try:
            async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
                logger.info("✅ Connected to Binance")

                while True:
                    msg = await ws.recv()
                    data = json.loads(msg)

                    trade = data["data"]

                    symbol = trade["s"].upper()

                    event = {
                        "symbol": symbol,
                        "price": float(trade["p"]),
                        "qty": float(trade["q"]),
                        "time": datetime.fromtimestamp(
                            trade["T"] / 1000, tz=timezone.utc
                        ).isoformat()
                    }

                    # 🔥 Publish to symbol-specific channel
                    channel = f"trades:{symbol}"
                    subs = await r.publish("trades", json.dumps(event))

                    print(f"{event['symbol']} → trades | subs={subs}")

        except Exception as e:
            logger.error(f"❌ WebSocket error: {e}")
            logger.info("Reconnecting in 2 seconds...")
            await asyncio.sleep(2)


if __name__ == "__main__":
    asyncio.run(run())
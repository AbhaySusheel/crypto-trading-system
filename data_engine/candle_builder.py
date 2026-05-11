import asyncio
import json
import logging
from datetime import datetime
import sys

import redis.asyncio as aioredis
import asyncpg

# Windows fix
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

REDIS_URL = "redis://localhost:6379"
REDIS_CHANNEL = "trades"

DB_DSN = "postgresql://trader:trader123@localhost:5432/crypto_trading"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s"
)
logger = logging.getLogger("candle_builder")


class CandleBuilder:
    def __init__(self):
        self.redis = None
        self.db_pool = None

        # 🔥 KEY CHANGE
        self.candles = {}  # symbol -> candle
        self.candle_times = {}  # symbol -> current minute

    async def init(self):
        self.redis = await aioredis.from_url(REDIS_URL)
        self.db_pool = await asyncpg.create_pool(DB_DSN)
        logger.info("✅ Connected to Redis + PostgreSQL")

    def get_bucket(self, dt):
        return dt.replace(second=0, microsecond=0)

    async def save_candle(self, candle):
        async with self.db_pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO candles_1m 
                (time, symbol, open, high, low, close, volume, trade_count)
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8)
                ON CONFLICT (time, symbol) DO UPDATE SET
                    high = GREATEST(candles_1m.high, EXCLUDED.high),
                    low = LEAST(candles_1m.low, EXCLUDED.low),
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume,
                    trade_count = EXCLUDED.trade_count
            """,
                candle["time"],
                candle["symbol"],
                candle["open"],
                candle["high"],
                candle["low"],
                candle["close"],
                candle["volume"],
                candle["trade_count"]
            )

    async def process_trade(self, trade):
        symbol = trade["symbol"].upper()
        price = float(trade["price"])
        qty = float(trade["qty"])
        t = datetime.fromisoformat(trade["time"])

        bucket = self.get_bucket(t)

        current_candle = self.candles.get(symbol)
        current_time = self.candle_times.get(symbol)

        # NEW CANDLE
        if current_candle is None or bucket > current_time:
            if current_candle:
                await self.save_candle(current_candle)

                logger.info(
                    f"🟢 {symbol} CLOSED | {current_candle['time']} | "
                    f"O={current_candle['open']:.2f} "
                    f"H={current_candle['high']:.2f} "
                    f"L={current_candle['low']:.2f} "
                    f"C={current_candle['close']:.2f} "
                    f"V={current_candle['volume']:.6f} "
                    f"T={current_candle['trade_count']}"
                )

            self.candle_times[symbol] = bucket
            self.candles[symbol] = {
                "time": bucket,
                "symbol": symbol,
                "open": price,
                "high": price,
                "low": price,
                "close": price,
                "volume": qty,
                "trade_count": 1
            }

        # UPDATE EXISTING
        else:
            c = self.candles[symbol]
            c["high"] = max(c["high"], price)
            c["low"] = min(c["low"], price)
            c["close"] = price
            c["volume"] += qty
            c["trade_count"] += 1

    async def run(self):
        await self.init()

        pubsub = self.redis.pubsub()
        await pubsub.subscribe(REDIS_CHANNEL)

        logger.info(f"📡 Subscribed to: {REDIS_CHANNEL}")

        async for msg in pubsub.listen():
            if msg["type"] != "message":
                continue

            try:
                trade = json.loads(msg["data"])
                await self.process_trade(trade)

            except Exception as e:
                logger.error(f"❌ Error: {e}")

    async def shutdown(self):
        for candle in self.candles.values():
            await self.save_candle(candle)

        if self.db_pool:
            await self.db_pool.close()

        if self.redis:
            await self.redis.close()


if __name__ == "__main__":
    builder = CandleBuilder()

    try:
        asyncio.run(builder.run())
    except KeyboardInterrupt:
        print("\n🛑 Stopped")
        asyncio.run(builder.shutdown())
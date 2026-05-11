import asyncio
from utils.telegram import send_telegram

async def test():
    await send_telegram("🚀 TEST MESSAGE")

asyncio.run(test())
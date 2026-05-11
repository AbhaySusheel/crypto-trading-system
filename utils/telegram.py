import os
import aiohttp
from dotenv import load_dotenv

load_dotenv()   # 🔥 ADD THIS

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")


async def send_telegram(message: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"

    try:
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(url, json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": message
            }) as resp:
                print("📲 Telegram status:", resp.status)

    except Exception as e:
        print("❌ Telegram error:", e)
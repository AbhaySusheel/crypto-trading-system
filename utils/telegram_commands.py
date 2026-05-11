import requests
import time
import os
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

last_update_id = None

def get_updates():
    global last_update_id

    url = f"https://api.telegram.org/bot{TOKEN}/getUpdates"
    params = {"timeout": 100}

    if last_update_id:
        params["offset"] = last_update_id + 1

    res = requests.get(url, params=params).json()

    if "result" in res:
        for update in res["result"]:
            last_update_id = update["update_id"]
            yield update

def handle_command(text, binance):
    text = text.lower()

    if text == "/balance":
        portfolio = binance.get_portfolio()
        return f"💰 Balance: {portfolio['balances']}"

    elif text == "/positions":
        portfolio = binance.get_portfolio()
        return f"📊 Positions: {portfolio['positions']}"

    elif text in ["/close", "/closeall"]:
        binance.close_all_positions()
        return "🚨 Closed all positions"

    return "❓ Unknown command"
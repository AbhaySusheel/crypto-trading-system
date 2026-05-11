import os
from dotenv import load_dotenv

load_dotenv()

BINANCE_SYMBOL = os.getenv("BINANCE_SYMBOL", "btcusdt").lower()
BINANCE_USE_TESTNET = os.getenv("BINANCE_USE_TESTNET", "true").lower() == "true"

REDIS_URL = os.getenv("REDIS_URL")
REDIS_TRADES_CHANNEL = os.getenv("REDIS_TRADES_CHANNEL")
REDIS_LAST_PRICE_KEY = os.getenv("REDIS_LAST_PRICE_KEY")
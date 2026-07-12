# utils/test.py

from dotenv import load_dotenv
from binance.client import Client
import os
import time

load_dotenv()

client = Client(
    os.getenv("BINANCE_FUTURE_API_KEY"),
    os.getenv("BINANCE_FUTURE_SECRET_KEY")
)

# Demo Futures endpoint
client.FUTURES_URL = "https://demo-fapi.binance.com/fapi"

# Sync time
server_time = client.get_server_time()

client.timestamp_offset = (
    server_time["serverTime"]
    - int(time.time() * 1000)
)

print("🕒 Time synchronized")

account = client.futures_account()

print("\n===== ACCOUNT =====")
print("Can Trade:", account["canTrade"])
print("Balance:", account["availableBalance"])

print("\n===== OPEN ORDERS =====")
try:
    orders = client.futures_get_open_orders()
    print(orders)
except Exception as e:
    print("Open Orders Error:", e)

print("\n===== POSITIONS =====")
try:
    positions = client.futures_position_information()

    found = False

    for p in positions:
        qty = float(p["positionAmt"])

        if qty != 0:
            found = True

            print(
                p["symbol"],
                "Qty:", qty,
                "Entry:", p["entryPrice"]
            )

    if not found:
        print("No open positions found")

except Exception as e:
    print("Position Error:", e)
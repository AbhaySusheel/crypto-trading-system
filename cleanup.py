# cleanup.py
from execution_engine.binance_client import BinanceClient

binance = BinanceClient()

print("🧹 CLEANUP UTILITY")
print("=" * 50)

# 1. Cancel ALL open orders first
print("\n📋 Cancelling all open orders...")
positions = binance.client.futures_position_information()

for p in positions:
    symbol = p["symbol"]
    qty = float(p["positionAmt"])
    
    if qty != 0:
        try:
            # Cancel ALL open orders for this symbol
            binance.client.futures_cancel_all_open_orders(symbol=symbol)
            print(f"✅ Cancelled all orders for {symbol}")
        except Exception as e:
            print(f"⚠️ {symbol}: {e}")

# 2. Close all positions
print("\n🔻 Closing all positions...")
binance.close_all_positions()

print("\n✅ CLEANUP COMPLETE")
print("Now restart the bot with: python -m data_engine.feature_engine")
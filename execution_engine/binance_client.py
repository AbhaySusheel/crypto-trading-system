import os
from dotenv import load_dotenv
from binance.client import Client

load_dotenv()


class BinanceClient:

    def __init__(self):
        import time
        import os
        from binance.client import Client

        api_key = os.getenv("BINANCE_FUTURE_API_KEY")
        api_secret = os.getenv("BINANCE_FUTURE_SECRET_KEY")

        if not api_key or not api_secret:
            raise Exception("❌ Missing Binance API keys")

        # 🔥 Retry logic
        for i in range(5):
            try:
                self.client = Client(
                    api_key=api_key,
                    api_secret=api_secret,
                    testnet=True
                )

                # 🔥 Important for futures testnet
                self.client.FUTURES_URL = "https://testnet.binancefuture.com/fapi"
                server_time = self.client.get_server_time()
                self.client.timestamp_offset = server_time["serverTime"] - int(time.time() * 1000)
                print("🕒 Binance time synchronized")

                # 🔥 Ping check
                self.client.ping()

                print("✅ Binance connected")
                break

            except Exception as e:
                print(f"⚠️ Binance connection failed (attempt {i+1}): {e}")
                time.sleep(2)

        else:
            raise Exception("❌ Could not connect to Binance after retries")


    # ---------------- FUTURES BALANCE ----------------
    def get_balances(self):
        balances = self.client.futures_account_balance()

        result = {}
        for b in balances:
            balance = float(b["balance"])
            if balance > 0:
                result[b["asset"]] = balance

        return result

    # ---------------- FUTURES POSITIONS ----------------
    def get_positions(self):
        positions = self.client.futures_position_information()

        result = {}

        for p in positions:
            qty = float(p["positionAmt"])

            if qty != 0:
                result[p["symbol"]] = {
                    "qty": qty,
                    "entry_price": float(p["entryPrice"])
                }

        print("📊 FUTURES POSITIONS:", result)

        return result

    # ---------------- PORTFOLIO ----------------
    def get_portfolio(self):
        return {
            "balances": self.get_balances(),
            "positions": self.get_positions()
        }

    # ---------------- CLOSE ALL POSITIONS ----------------
    def close_all_positions(self):
        positions = self.client.futures_position_information()

        print("📡 Fetching futures positions...")

        for p in positions:
            symbol = p["symbol"]
            qty = float(p["positionAmt"])

            if qty == 0:
                continue

            # 🔥 CRITICAL: handle LONG + SHORT
            side = "SELL" if qty > 0 else "BUY"

            print(f"🔻 Closing {symbol} | Qty: {qty}")

            try:
                self.client.futures_create_order(
                    symbol=symbol,
                    side=side,
                    type="MARKET",
                    quantity=abs(qty)
                )

                print(f"✅ Closed {symbol}")

            except Exception as e:
                print(f"❌ Error closing {symbol}: {e}")

        print("🎯 All positions closed")

    def get_symbol_precision(self, symbol):
        info = self.client.futures_exchange_info()

        for s in info["symbols"]:
            if s["symbol"] == symbol:

                qty_precision = s["quantityPrecision"]
                price_precision = s["pricePrecision"]

                return qty_precision, price_precision

        return 3, 2
        

    def set_leverage(self, symbol, leverage=3):
        try:
            self.client.futures_change_leverage(
                symbol=symbol,
                leverage=leverage
            )
            print(f"⚙️ Leverage set {symbol} x{leverage}")
        except Exception as e:
            print(f"❌ Leverage error: {e}")    
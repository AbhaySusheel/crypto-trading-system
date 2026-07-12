import os
from dotenv import load_dotenv
from binance.client import Client

load_dotenv()

DEMO_FUTURES_ENDPOINT = "https://demo-fapi.binance.com/fapi"
MAINNET_FUTURES_ENDPOINT = "https://fapi.binance.com/fapi"


class BinanceClient:

    def __init__(self):
        import time
        import os
        from binance.client import Client

        api_key = os.getenv("BINANCE_FUTURE_API_KEY")
        api_secret = os.getenv("BINANCE_FUTURE_SECRET_KEY")
        use_testnet = os.getenv("BINANCE_USE_TESTNET", "true").strip().lower() in (
            "true",
            "1",
            "yes",
            "y",
        )
        use_demo = os.getenv("BINANCE_USE_DEMO", str(use_testnet)).strip().lower() in (
            "true",
            "1",
            "yes",
            "y",
        )

        if not api_key or not api_secret:
            raise Exception("❌ Missing Binance API keys")

        futures_url = DEMO_FUTURES_ENDPOINT if use_demo else MAINNET_FUTURES_ENDPOINT

        # 🔥 Retry logic
        for i in range(5):
            try:
                self.client = Client(
                    api_key=api_key,
                    api_secret=api_secret,
                )

                if use_demo:
                    self.client.FUTURES_URL = futures_url
                    setattr(self.client, "_testnet", True)

                # FIRST sync time
                server_time = self.client.get_server_time()

                self.client.timestamp_offset = (
                    server_time["serverTime"]
                    - int(time.time() * 1000)
                )

                print("🕒 Binance time synchronized")

                account = self.client.futures_account()

                print("\n========== ACCOUNT ==========")
                print("Total Wallet Balance:", account["totalWalletBalance"])
                print("Available Balance:", account["availableBalance"])
                print("Can Trade:", account["canTrade"])
                print("=============================\n")

                server_time = self.client.get_server_time()
                self.client.timestamp_offset = server_time["serverTime"] - int(time.time() * 1000)
                print("🕒 Binance time synchronized")

                # 🔥 Ping check
                self.client.ping()

                print("✅ Binance connected")
                self._print_diagnostics(account, futures_url, use_demo)
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

        print("\n===== RAW POSITIONS =====")

        for p in positions:
            qty = float(p["positionAmt"])

            if qty != 0:
                print(
                    p["symbol"],
                    qty,
                    p["entryPrice"]
                )

        print("========================")

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
            "positions": self.get_positions(),
            "open_orders": self.get_open_orders(),
        }

    def get_open_orders(self, symbol=None):
        if symbol is None:
            return self.client.futures_get_open_orders()
        return self.client.futures_get_open_orders(symbol=symbol)

    def _print_diagnostics(self, account, endpoint, use_demo):
        environment = "DEMO" if use_demo else "MAINNET"
        api_key_loaded = bool(os.getenv("BINANCE_FUTURE_API_KEY"))
        spot_key_loaded = bool(os.getenv("BINANCE_API_KEY"))

        print("\n===== BINANCE DIAGNOSTICS =====")
        print(f"Environment: {environment}")
        print(f"Futures endpoint URL: {endpoint}")
        print(f"Futures API key loaded: {api_key_loaded}")
        print(f"Spot API key loaded (unused by this client): {spot_key_loaded}")
        print(f"Account alias: {account.get('accountAlias', 'UNKNOWN')}")
        print(f"Account canTrade: {account.get('canTrade', 'UNKNOWN')}")
        print(f"Account response keys: {sorted(account.keys())}")

        try:
            open_orders = self.client.futures_get_open_orders()
            print(f"Open futures orders: {open_orders}")
        except Exception as e:
            print(f"⚠️ Unable to fetch open orders: {e}")

        try:
            positions = self.client.futures_position_information()
            non_zero_positions = [p for p in positions if float(p.get('positionAmt', 0)) != 0]
            print(f"Open futures positions: {non_zero_positions}")
        except Exception as e:
            print(f"⚠️ Unable to fetch positions: {e}")

        print("===== END BINANCE DIAGNOSTICS =====\n")

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
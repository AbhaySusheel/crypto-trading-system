import time

class TradeTracker:
    def __init__(self):
        self.active_trades = {}
        self.history = []

    def add_trade(self, trade):
        self.active_trades[trade["symbol"]] = {
            "symbol": trade["symbol"],

            # Trade information
            "side": trade["side"],
            "entry": trade["entry"],
            "qty": trade["executed_qty"],

            # Binance metadata
            "entry_order_id": trade.get("entry_order_id"),
            "entry_time": trade.get("entry_time"),
            "entry_price": trade.get("entry_price"),
            "client_order_id": trade.get("client_order_id"),

            # Local bookkeeping
            "local_time": time.time()
        }

    def close_trade(self, symbol, exit_price):
        if symbol not in self.active_trades:
            return None

        trade = self.active_trades.pop(symbol)

        entry = trade["entry"]
        qty = trade["qty"]

        pnl = (exit_price - entry) * qty

        result = "WIN" if pnl > 0 else "LOSS"

        record = {
            "symbol": symbol,
            "entry": entry,
            "exit": exit_price,
            "qty": qty,
            "pnl": pnl,
            "result": result,
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        }

        self.history.append(record)

        return record

    def get_trade(self, symbol):
        return self.active_trades.get(symbol)

    def stats(self):
        total = len(self.history)
        wins = sum(1 for t in self.history if t["result"] == "WIN")

        win_rate = (wins / total * 100) if total > 0 else 0

        return {
            "total": total,
            "wins": wins,
            "win_rate": round(win_rate, 2)
        }
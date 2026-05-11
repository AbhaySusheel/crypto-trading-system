# execution_engine/trade_manager.py
import time


class TradeManager:

    def __init__(self, binance_client, risk_manager):
        self.binance = binance_client
        self.risk = risk_manager
        self.last_trade_time = {}
        self.last_protection_check = {}
        self.trade_metadata = {}  # symbol -> {side, entry, sl, tp, partial_taken}

    def get_portfolio(self):
        return self.binance.get_portfolio()

    def open_trades_count(self, portfolio):
        return len(portfolio.get("positions", {}))

    def is_already_in_trade(self, symbol, portfolio):
        return symbol in portfolio.get("positions", {})

    def prepare_trade(self, symbol, price, portfolio, side="LONG", atr_pct=None):
        """
        Prepare trade with ATR-based dynamic SL/TP.
        - side: "LONG" or "SHORT"
        - atr_pct: ATR as percentage of price (for dynamic stops)
        """
        balance = portfolio.get("balances", {}).get("USDT", 0)

        # ----- DYNAMIC SL/TP based on ATR -----
        if atr_pct and atr_pct > 0:
            # Use ATR with multiplier (safer than fixed %)
            sl_distance = max(atr_pct * 1.5, 0.0025)   # min 0.25%
            tp_distance = max(atr_pct * 3.0, 0.006)    # min 0.6%, 1:2 R:R
            # Cap maximum SL distance to avoid huge losses
            sl_distance = min(sl_distance, 0.015)      # max 1.5%
            tp_distance = min(tp_distance, 0.03)       # max 3.0%
        else:
            sl_distance = 0.0025
            tp_distance = 0.006

        if side == "LONG":
            stop_loss = price * (1 - sl_distance)
            take_profit = price * (1 + tp_distance)
        else:  # SHORT
            stop_loss = price * (1 + sl_distance)
            take_profit = price * (1 - tp_distance)

        qty = abs(self.risk.position_size(balance, price, stop_loss))

        return {
            "symbol": symbol,
            "side": side,
            "entry": price,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "qty": qty,
            "sl_distance": sl_distance,
            "tp_distance": tp_distance
        }

    def execute_trade(self, trade):
        symbol = trade["symbol"]
        qty = trade["qty"]
        side = trade.get("side", "LONG")

        if qty < 0.001:
            print("⚠️ Qty too small, skipping trade")
            return None

        qty_precision, _ = self.binance.get_symbol_precision(symbol)
        quantity = round(qty, qty_precision)

        order_side = "BUY" if side == "LONG" else "SELL"

        print(f"⚡ EXECUTING {side}: {symbol} | Qty: {quantity}")

        try:
            order = self.binance.client.futures_create_order(
                symbol=symbol,
                side=order_side,
                type="MARKET",
                quantity=quantity
            )

            executed_qty = abs(float(order["executedQty"]))
            trade["executed_qty"] = executed_qty

            # Store metadata for trailing stop & partial TP
            self.trade_metadata[symbol] = {
                "side": side,
                "entry": trade["entry"],
                "sl": trade["stop_loss"],
                "tp": trade["take_profit"],
                "qty": executed_qty,
                "partial_taken": False,
                "be_moved": False  # breakeven moved
            }

            print(f"✅ ORDER PLACED: {symbol} {side}")
            return order

        except Exception as e:
            print(f"❌ ORDER FAILED: {e}")
            return None

    def set_sl_tp(self, trade):
        symbol = trade["symbol"]
        side = trade.get("side", "LONG")

        try:
            _, price_precision = self.binance.get_symbol_precision(symbol)
            sl = round(trade["stop_loss"], price_precision)
            tp = round(trade["take_profit"], price_precision)

            # Opposite side to close
            exit_side = "SELL" if side == "LONG" else "BUY"

            # STOP LOSS
            self.binance.client.futures_create_order(
                symbol=symbol,
                side=exit_side,
                type="STOP_MARKET",
                stopPrice=sl,
                closePosition=True,
                workingType="MARK_PRICE"
            )
            time.sleep(0.2)

            # TAKE PROFIT
            self.binance.client.futures_create_order(
                symbol=symbol,
                side=exit_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp,
                closePosition=True,
                workingType="MARK_PRICE"
            )

            print(f"🎯 SL/TP SET {symbol} {side} | SL: {sl} | TP: {tp}")

        except Exception as e:
            print(f"❌ SL/TP FAILED for {symbol}: {e}")

    def check_trailing_stop(self, symbol, current_price, portfolio):
        """
        Move SL to breakeven once price moves +0.4% in favor.
        This converts potential losses into breakeven.
        """
        if symbol not in self.trade_metadata:
            return

        meta = self.trade_metadata[symbol]
        if meta.get("be_moved"):
            return

        positions = portfolio.get("positions", {})
        if symbol not in positions:
            return

        entry = meta["entry"]
        side = meta["side"]

        if side == "LONG":
            profit_pct = (current_price - entry) / entry
            trigger = 0.004  # +0.4%
            if profit_pct >= trigger:
                new_sl = entry * 1.0005  # Slightly above entry (cover fees)
                self._update_stop_loss(symbol, new_sl, side)
                meta["be_moved"] = True
                print(f"🔒 BREAKEVEN MOVED for {symbol} @ {new_sl:.4f}")
        else:  # SHORT
            profit_pct = (entry - current_price) / entry
            trigger = 0.004
            if profit_pct >= trigger:
                new_sl = entry * 0.9995
                self._update_stop_loss(symbol, new_sl, side)
                meta["be_moved"] = True
                print(f"🔒 BREAKEVEN MOVED for {symbol} @ {new_sl:.4f}")

    def _update_stop_loss(self, symbol, new_sl, side):
        """Cancel old SL and place new one"""
        try:
            # Cancel existing SL orders
            open_orders = self.binance.client.futures_get_open_orders(symbol=symbol)
            for order in open_orders:
                if order["type"] == "STOP_MARKET":
                    self.binance.client.futures_cancel_order(
                        symbol=symbol, orderId=order["orderId"]
                    )

            _, price_precision = self.binance.get_symbol_precision(symbol)
            new_sl = round(new_sl, price_precision)
            exit_side = "SELL" if side == "LONG" else "BUY"

            self.binance.client.futures_create_order(
                symbol=symbol,
                side=exit_side,
                type="STOP_MARKET",
                stopPrice=new_sl,
                closePosition=True,
                workingType="MARK_PRICE"
            )
        except Exception as e:
            print(f"❌ Update SL error {symbol}: {e}")

    def ensure_sl_tp(self, portfolio):
        positions = portfolio.get("positions", {})

        for symbol, pos in positions.items():
            qty = float(pos["qty"])
            if qty == 0:
                continue

            try:
                now = time.time()
                last_check = self.last_protection_check.get(symbol, 0)
                if now - last_check < 30:
                    continue
                self.last_protection_check[symbol] = now

                open_orders = self.binance.client.futures_get_open_orders(symbol=symbol)
                has_sl = any(o["type"] == "STOP_MARKET" for o in open_orders)
                has_tp = any(o["type"] == "TAKE_PROFIT_MARKET" for o in open_orders)

                if has_sl and has_tp:
                    continue

                print(f"⚠️ Missing SL/TP for {symbol}")

                entry = pos["entry_price"]
                side = "LONG" if qty > 0 else "SHORT"

                # Use stored metadata if available
                if symbol in self.trade_metadata:
                    sl = self.trade_metadata[symbol]["sl"]
                    tp = self.trade_metadata[symbol]["tp"]
                else:
                    # Fallback defaults
                    if side == "LONG":
                        sl = entry * 0.9975
                        tp = entry * 1.006
                    else:
                        sl = entry * 1.0025
                        tp = entry * 0.994

                _, price_precision = self.binance.get_symbol_precision(symbol)
                sl = round(sl, price_precision)
                tp = round(tp, price_precision)

                exit_side = "SELL" if side == "LONG" else "BUY"

                if not has_sl:
                    self.binance.client.futures_create_order(
                        symbol=symbol, side=exit_side, type="STOP_MARKET",
                        stopPrice=sl, closePosition=True, workingType="MARK_PRICE"
                    )
                    time.sleep(0.2)

                if not has_tp:
                    self.binance.client.futures_create_order(
                        symbol=symbol, side=exit_side, type="TAKE_PROFIT_MARKET",
                        stopPrice=tp, closePosition=True, workingType="MARK_PRICE"
                    )
                    time.sleep(0.2)

                print(f"✅ Protection restored for {symbol}")

            except Exception as e:
                print(f"❌ Protection error {symbol}: {e}")

    def can_trade_symbol(self, symbol):
        now = time.time()
        last_time = self.last_trade_time.get(symbol, 0)
        if now - last_time < 60:
            return False
        self.last_trade_time[symbol] = now
        return True

    def cleanup_closed_trade(self, symbol):
        """Remove metadata when trade closes"""
        if symbol in self.trade_metadata:
            del self.trade_metadata[symbol]
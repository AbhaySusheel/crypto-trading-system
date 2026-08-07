# execution_engine/trade_manager.py
import logging
import os
import time
import uuid

from execution_engine.order_state import (
    FillEvent,
    JsonOrderStateStorage,
    OrderRecord,
    OrderStateManager,
    OrderStatus,
    OrderType,
)

logger = logging.getLogger(__name__)


class TradeManager:

    def __init__(self, binance_client, risk_manager, order_state_path=None, portfolio_cache=None):
        self.binance = binance_client
        self.risk = risk_manager
        self.portfolio_cache = portfolio_cache
        self.last_trade_time = {}
        self.last_protection_check = {}
        self.trade_metadata = {}  # symbol -> {side, entry, sl, tp, partial_taken}
        self.order_state_manager = None
        self._init_order_state(order_state_path)

    def _init_order_state(self, order_state_path):
        if order_state_path is None:
            order_state_path = os.path.join(os.path.dirname(__file__), "order_state.json")
        try:
            storage = JsonOrderStateStorage(order_state_path)
            self.order_state_manager = OrderStateManager(storage)
            logger.info("Order state manager initialized at %s", order_state_path)
            # TODO: future reconciliation should hydrate live trade state from exchange on startup.
        except Exception as exc:
            self.order_state_manager = None
            #logger.warning("Order state manager unavailable: %s", exc)
            logger.exception("Order state manager unavailable")
            logger.info("Order state path: %s", order_state_path)

    def _save_order_state(self):
        if not self.order_state_manager:
            return
        try:
            self.order_state_manager.save_state()
        except Exception as exc:
            logger.warning("Order state save failed: %s", exc)

    def _record_order_shadow(
        self,
        order_id,
        symbol,
        side,
        order_type,
        qty,
        price=None,
        stop_price=None,
        trade_role=None,
        parent_order_id=None,
    ):
        if not self.order_state_manager:
            return None
        try:
            trade = self.order_state_manager.get_trade_by_symbol(symbol)
            trade_id = trade.trade_id if trade else f"trade-{uuid.uuid4().hex}"
            client_order_id = self.order_state_manager.allocate_client_order_id()
            print("DEBUG SHADOW ORDER")
            print("order_id =", order_id)
            print("symbol =", symbol)
            print("qty =", qty)
            print("price    :", price)
            print("stop     :", stop_price)
            print("type     :", order_type)
            print("role     :", trade_role)
            print("=" * 80)
            record = OrderRecord(
                order_id=order_id,
                client_order_id=client_order_id,
                trade_id=trade_id,
                symbol=symbol,
                side=side,
                type=order_type,
                qty=qty,
                price=price,
                stop_price=stop_price,
                parent_order_id=parent_order_id,
                trade_role=trade_role,
            )
            print("OrderRecord.qty =", record.qty)
            self.order_state_manager.record_new_order(record)
            self._save_order_state()
            logger.info(
                "Shadow order recorded: order_id=%s symbol=%s type=%s trade_id=%s qty=%s stop_price=%s",
                order_id,
                symbol,
                order_type.name,
                trade_id,
                qty,
                stop_price,
            )
            # TODO: reconcile shadow order records with exchange order IDs during startup recovery.
            return record
        except Exception as exc:

            #logger.warning("Failed to record shadow order %s: %s", order_id, exc)
            logger.exception("Failed to record shadow order")
            return None

    def _record_fill_shadow(self, order_id, qty, price, commission=0.0, commission_asset="USDT"):
        if not self.order_state_manager:
            return
        try:
            fill = FillEvent(
                fill_id=f"fill-{uuid.uuid4().hex}",
                order_id=order_id,
                qty=qty,
                price=price,
                commission=commission,
                commission_asset=commission_asset,
            )
            self.order_state_manager.record_fill(order_id, fill)
            self._save_order_state()
            logger.info(
                "Shadow fill recorded: order_id=%s qty=%s price=%s commission=%s asset=%s",
                order_id,
                qty,
                price,
                commission,
                commission_asset,
            )
            # TODO: future reconciliation should validate these fills against exchange order reports.
        except Exception as exc:
            logger.warning("Failed to record shadow fill for order %s: %s", order_id, exc)

    def _mirror_cancel_order(self, order_id):
        if not self.order_state_manager or order_id is None:
            return
        try:
            order = self.order_state_manager.orders_by_id.get(int(order_id))
            if order and order.status in {OrderStatus.PENDING, OrderStatus.PARTIAL_FILLED}:
                order.mark_cancelled()
                self._save_order_state()
                logger.info("Shadow cancel recorded: order_id=%s status=%s", order_id, order.status.value)
                # TODO: reconciliation should verify actual exchange cancellation state before trusting local cancel state.
        except Exception as exc:
            logger.warning("Failed to mirror cancel for order %s: %s", order_id, exc)

    def _mirror_trade_closure(self, symbol):
        if not self.order_state_manager:
            return
        try:
            trade = self.order_state_manager.get_trade_by_symbol(symbol)
            if not trade:
                return
            trade.closed_qty = trade.entry_filled_qty
            self.order_state_manager.mark_trade_closed(trade.trade_id)
            self._save_order_state()
            logger.info("Shadow trade closed: symbol=%s trade_id=%s status=%s", symbol, trade.trade_id, trade.status.value)
            # TODO: future reconciliation should confirm trade closure with exchange fill/cancel history.
        except Exception as exc:
            logger.warning("Failed to mirror trade close for %s: %s", symbol, exc)

    def get_portfolio(self):
        if self.portfolio_cache is not None:
            return self.portfolio_cache.get_portfolio()
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

            logger.info("Market order response: %s", order)

            logger.info(
                "Entry execution: symbol=%s orderId=%s status=%s origQty=%s executedQty=%s avgPrice=%s",
                symbol,
                order.get("orderId"),
                order.get("status"),
                order.get("origQty"),
                order.get("executedQty"),
                order.get("avgPrice"),
            )

            executed_qty = abs(float(order.get("executedQty", 0)))


            logger.info(
                "Creating TradeRecord: entry_qty=%s entry_order_id=%s",
                executed_qty,
                order["orderId"],
            )

            logger.info(
                "Parsed quantities: requested_qty=%s executed_qty=%s",
                quantity,
                executed_qty,
            )
            trade["executed_qty"] = executed_qty


            logger.info(
                "Trade dict before SL/TP: qty=%s executed_qty=%s",
                trade.get("qty"),
                trade.get("executed_qty"),
            )

            # -------------------------------------------------
            # Save Binance execution metadata into trade dict
            # -------------------------------------------------

            trade["entry_order_id"] = int(order["orderId"])

            trade["entry_time"] = int(
                order.get(
                    "updateTime",
                    order.get("transactTime", 0)
                )
            )

            trade["entry_price"] = float(
                order.get(
                    "avgPrice",
                    trade["entry"]
                )
            )

            trade["client_order_id"] = order.get(
                "clientOrderId"
            )

            if self.order_state_manager:
                try:
                    state_trade = self.order_state_manager.create_trade(
                        symbol=symbol,
                        side=side,
                        entry_qty=executed_qty,
                        entry_order_id=int(order["orderId"]),
                    )
                    state_trade.entry_filled_qty = executed_qty

                    logger.info(
                        "[ENTRY SHADOW] symbol=%s orderId=%s "
                        "requested_qty=%s executed_qty=%s "
                        "status=%s origQty=%s executedQty=%s avgPrice=%s",
                        symbol,
                        order["orderId"],
                        trade.get("qty"),
                        executed_qty,
                        order.get("status"),
                        order.get("origQty"),
                        order.get("executedQty"),
                        order.get("avgPrice"),
                    )

                    self._record_order_shadow(
                        order_id=int(order["orderId"]),
                        symbol=symbol,
                        side=order_side,
                        order_type=OrderType.ENTRY,
                        qty=executed_qty, 
                        price=trade["entry_price"], 
                        trade_role="entry",
                    )
                    self._record_fill_shadow(
                        order_id=int(order["orderId"]),
                        qty=executed_qty,
                        price=trade["entry_price"],
                    )
                except Exception as exc:
                    #logger.warning("Failed to mirror entry trade for %s: %s", symbol, exc)
                    logger.exception(
                        "Failed to mirror entry trade for %s",
                        symbol,
                    )
                    raise

            # Store metadata for trailing stop & partial TP
            self.trade_metadata[symbol] = {
                "side": side,
                "entry": trade["entry"],
                "sl": trade["stop_loss"],
                "tp": trade["take_profit"],
                "qty": executed_qty,
                "partial_taken": False,
                "be_moved": False, 
                "entry_order_id": int(order["orderId"]),
                "entry_time": int(order.get("updateTime", order.get("transactTime", 0))),
                "entry_price": float(order.get("avgPrice", trade["entry"])),

                # Optional but useful
                "client_order_id": order.get("clientOrderId")
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
            exit_side = "SELL" if side == "LONG" else "BUY"

            # STOP LOSS (using STOP_MARKET for guaranteed execution)
            sl_order = self.binance.client.futures_create_order(
                symbol=symbol,
                side=exit_side,
                type="STOP_MARKET",
                stopPrice=sl,
                closePosition=True,
                workingType="MARK_PRICE",  # Use mark price (more stable)
                priceProtect=True            # ← NEW: Prevents bad fills
            )
            if self.order_state_manager:
                try:
                    state_trade = self.order_state_manager.get_trade_by_symbol(symbol)
                    print("SL ORDER RESPONSE")
                    print(sl_order)
                    sl_order_id = int(sl_order.get("orderId", 0))

                    logger.info(
                        "[SL SHADOW] symbol=%s "
                        "trade.qty=%s "
                        "trade.executed_qty=%s "
                        "computed_qty=%s "
                        "stop=%s",
                        symbol,
                        trade.get("qty"),
                        trade.get("executed_qty"),
                        abs(trade.get("executed_qty", trade.get("qty", 0.0))),
                        sl,
                    )
                    self._record_order_shadow(
                        order_id=sl_order_id,
                        symbol=symbol,
                        side=exit_side,
                        order_type=OrderType.STOP_LOSS,
                        qty=abs(trade.get("executed_qty", trade.get("qty", 0.0))),
                        stop_price=sl,
                        trade_role="stop_loss",
                        parent_order_id=(state_trade.entry_order_id if state_trade else None),
                    )
                    if state_trade:
                        state_trade.sl_order_id = sl_order_id
                        state_trade.sl_price = sl
                        self._save_order_state()
                except Exception as exc:
                    logger.warning("Failed to mirror SL order for %s: %s", symbol, exc)

            time.sleep(0.2)

            # TAKE PROFIT
            tp_order = self.binance.client.futures_create_order(
                symbol=symbol,
                side=exit_side,
                type="TAKE_PROFIT_MARKET",
                stopPrice=tp,
                closePosition=True,
                workingType="MARK_PRICE",
                priceProtect=True            # ← NEW
            )
            if self.order_state_manager:
                try:
                    state_trade = self.order_state_manager.get_trade_by_symbol(symbol)
                    print("TP ORDER RESPONSE")
                    print(tp_order)
                    tp_order_id = int(tp_order.get("orderId", 0))
                    logger.info(
                        "[TP SHADOW] symbol=%s "
                        "trade.qty=%s "
                        "trade.executed_qty=%s "
                        "computed_qty=%s "
                        "tp=%s",
                        symbol,
                        trade.get("qty"),
                        trade.get("executed_qty"),
                        abs(trade.get("executed_qty", trade.get("qty", 0.0))),
                        tp,
                    )
                    self._record_order_shadow(
                        order_id=tp_order_id,
                        symbol=symbol,
                        side=exit_side,
                        order_type=OrderType.TAKE_PROFIT,
                        qty=abs(trade.get("executed_qty", trade.get("qty", 0.0))),
                        stop_price=tp,
                        trade_role="take_profit",
                        parent_order_id=(state_trade.entry_order_id if state_trade else None),
                    )
                    if state_trade:
                        state_trade.tp_order_id = tp_order_id
                        state_trade.tp_price = tp
                        self._save_order_state()
                except Exception as exc:
                    logger.warning("Failed to mirror TP order for %s: %s", symbol, exc)

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

    def _get_open_orders(self, symbol=None):
        """
        Always fetch live open orders from Binance.

        Protection recovery must use real-time exchange state,
        not the cached portfolio snapshot.
        """
        try:
            return self.binance.client.futures_get_open_orders(symbol=symbol)
        except Exception as exc:
            logger.warning(
                "Failed to fetch open orders for %s: %s",
                symbol,
                exc,
            )
            return []

    def _update_stop_loss(self, symbol, new_sl, side):
        """Cancel old SL and place new one"""
        try:
            # Cancel existing SL orders
            open_orders = self._get_open_orders(symbol=symbol)
            for order in open_orders:
                if order["type"] == "STOP_MARKET":
                    self.binance.client.futures_cancel_order(
                        symbol=symbol, orderId=order["orderId"]
                    )
                    self._mirror_cancel_order(order["orderId"])

            _, price_precision = self.binance.get_symbol_precision(symbol)
            new_sl = round(new_sl, price_precision)
            exit_side = "SELL" if side == "LONG" else "BUY"

            sl_order = self.binance.client.futures_create_order(
                symbol=symbol,
                side=exit_side,
                type="STOP_MARKET",
                stopPrice=new_sl,
                closePosition=True,
                workingType="MARK_PRICE"
            )
            if self.order_state_manager:
                try:
                    state_trade = self.order_state_manager.get_trade_by_symbol(symbol)
                    sl_order_id = int(sl_order.get("orderId", 0))
                    logger.info(
                        "[ENSURE SL] symbol=%s "
                        "metadata=%s "
                        "computed_qty=%s",
                        symbol,
                        self.trade_metadata.get(symbol),
                        abs(self.trade_metadata.get(symbol, {}).get("qty", 0.0)),
                    )
                    self._record_order_shadow(
                        order_id=sl_order_id,
                        symbol=symbol,
                        side=exit_side,
                        order_type=OrderType.STOP_LOSS,
                        qty=abs(self.trade_metadata.get(symbol, {}).get("qty", 0.0)),
                        stop_price=new_sl,
                        trade_role="stop_loss",
                        parent_order_id=(state_trade.entry_order_id if state_trade else None),
                    )
                    if state_trade:
                        state_trade.sl_order_id = sl_order_id
                        state_trade.sl_price = new_sl
                        self._save_order_state()
                except Exception as exc:
                    logger.warning("Failed to mirror updated SL for %s: %s", symbol, exc)
        except Exception as e:
            print(f"❌ Update SL error {symbol}: {e}")

    def ensure_sl_tp(self, portfolio):
        """
        Bulletproof SL/TP recovery.
        Handles:
        - Existing orders detection (all variants)
        - Price validation (avoid -2021 errors)
        - Side detection (LONG vs SHORT)
        """
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

                # Get current market price
                try:
                    ticker = self.binance.client.futures_symbol_ticker(symbol=symbol)
                    current_price = float(ticker["price"])
                except Exception as e:
                    print(f"⚠️ Cannot get price for {symbol}: {e}")
                    continue

                # Check existing orders (improved detection)
                open_orders = self._get_open_orders(symbol=symbol)
                
                has_sl = False
                has_tp = False
                expected_exit_side = (
                    "SELL"
                    if qty > 0
                    else "BUY"
                )

                logger.info("===== OPEN ORDERS FOR %s =====", symbol)
                
                for order in open_orders:
                    logger.info("OPEN ORDER: %s", order)
                    order_type = order.get("type", "")
                    order_side = order.get("side", "")
                    close_position = order.get("closePosition", False)
                    reduce_only = order.get("reduceOnly", False)


                    # Wrong side -> ignore
                    if order_side != expected_exit_side:
                        continue

                       

                        # Not protection order -> ignore
                    if not (close_position or reduce_only):
                        continue
                    
                    # SL: STOP_MARKET or STOP
                    if order_type in ("STOP_MARKET", "STOP"):
                        
                        has_sl = True
                    
                    # TP: TAKE_PROFIT_MARKET or TAKE_PROFIT
                    if order_type in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
                        
                        has_tp = True

                logger.info(
                    "Protection detection: has_sl=%s has_tp=%s",
                    has_sl,
                    has_tp,
                )        

                if has_sl and has_tp:
                    continue  # Already protected

                print(f"⚠️ Missing protection for {symbol} (SL={has_sl}, TP={has_tp})")

                entry = float(pos["entry_price"])
                side = "LONG" if qty > 0 else "SHORT"

                # Use stored metadata if available
                if symbol in self.trade_metadata:
                    sl = self.trade_metadata[symbol]["sl"]
                    tp = self.trade_metadata[symbol]["tp"]
                else:
                    # Fallback defaults based on entry
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

                # ===== CRITICAL: Price validation to avoid -2021 errors =====
                if side == "LONG":
                    # For LONG: SL must be BELOW current price, TP must be ABOVE
                    if sl >= current_price:
                        # Position is in heavy loss, place SL just below current price
                        sl = round(current_price * 0.995, price_precision)
                        print(f"⚠️ {symbol} SL adjusted (position in loss): {sl}")
                    if tp <= current_price:
                        # Position already past TP target, place TP just above current
                        tp = round(current_price * 1.005, price_precision)
                        print(f"⚠️ {symbol} TP adjusted: {tp}")
                else:  # SHORT
                    # For SHORT: SL must be ABOVE current price, TP must be BELOW
                    if sl <= current_price:
                        sl = round(current_price * 1.005, price_precision)
                        print(f"⚠️ {symbol} SL adjusted: {sl}")
                    if tp >= current_price:
                        tp = round(current_price * 0.995, price_precision)
                        print(f"⚠️ {symbol} TP adjusted: {tp}")

                # Place SL
                if not has_sl:
                    try:
                        sl_order = self.binance.client.futures_create_order(
                            symbol=symbol,
                            side=exit_side,
                            type="STOP_MARKET",
                            stopPrice=sl,
                            closePosition=True,
                            workingType="MARK_PRICE"
                        )
                        print(f"✅ SL placed for {symbol} @ {sl}")
                        if self.order_state_manager:
                            try:
                                state_trade = self.order_state_manager.get_trade_by_symbol(symbol)
                                sl_order_id = int(sl_order.get("orderId", 0))
                                logger.info(
                                    "[ENSURE SL] symbol=%s "
                                    "metadata=%s "
                                    "computed_qty=%s",
                                    symbol,
                                    self.trade_metadata.get(symbol),
                                    abs(self.trade_metadata.get(symbol, {}).get("qty", 0.0)),
                                )
                                self._record_order_shadow(
                                    order_id=sl_order_id,
                                    symbol=symbol,
                                    side=exit_side,
                                    order_type=OrderType.STOP_LOSS,
                                    qty=abs(self.trade_metadata.get(symbol, {}).get("qty", 0.0)),
                                    stop_price=sl,
                                    trade_role="stop_loss",
                                    parent_order_id=(state_trade.entry_order_id if state_trade else None),
                                )
                                if state_trade:
                                    state_trade.sl_order_id = sl_order_id
                                    state_trade.sl_price = sl
                                    self._save_order_state()
                            except Exception as exc:
                                logger.warning("Failed to mirror recovered SL %s: %s", symbol, exc)
                        time.sleep(0.3)
                    except Exception as e:
                        print(f"❌ SL placement failed {symbol}: {e}")

                # Place TP
                if not has_tp:
                    try:
                        tp_order = self.binance.client.futures_create_order(
                            symbol=symbol,
                            side=exit_side,
                            type="TAKE_PROFIT_MARKET",
                            stopPrice=tp,
                            closePosition=True,
                            workingType="MARK_PRICE"
                        )
                        print(f"✅ TP placed for {symbol} @ {tp}")
                        if self.order_state_manager:
                            try:
                                state_trade = self.order_state_manager.get_trade_by_symbol(symbol)
                                tp_order_id = int(tp_order.get("orderId", 0))
                                logger.info(
                                    "[ENSURE TP] symbol=%s "
                                    "metadata=%s "
                                    "computed_qty=%s",
                                    symbol,
                                    self.trade_metadata.get(symbol),
                                    abs(self.trade_metadata.get(symbol, {}).get("qty", 0.0)),
                                )
                                self._record_order_shadow(
                                    order_id=tp_order_id,
                                    symbol=symbol,
                                    side=exit_side,
                                    order_type=OrderType.TAKE_PROFIT,
                                    qty=abs(self.trade_metadata.get(symbol, {}).get("qty", 0.0)),
                                    stop_price=tp,
                                    trade_role="take_profit",
                                    parent_order_id=(state_trade.entry_order_id if state_trade else None),
                                )
                                if state_trade:
                                    state_trade.tp_order_id = tp_order_id
                                    state_trade.tp_price = tp
                                    self._save_order_state()
                            except Exception as exc:
                                logger.warning("Failed to mirror recovered TP %s: %s", symbol, exc)
                        time.sleep(0.3)
                    except Exception as e:
                        print(f"❌ TP placement failed {symbol}: {e}")

            except Exception as e:
                print(f"❌ Protection error {symbol}: {e}")


    def get_last_fill_price(self, symbol):
        """
        Get the actual fill price from the last closed trade.
        Used for accurate PnL calculation.
        """
        try:
            trades = self.binance.client.futures_account_trades(
                symbol=symbol,
                limit=5
            )
            if trades:
                # Get most recent trade
                last_trade = trades[-1]
                return float(last_trade["price"])
        except Exception as e:
            print(f"⚠️ Could not get fill price for {symbol}: {e}")
        return None            

    def can_trade_symbol(self, symbol):
        now = time.time()
        last_time = self.last_trade_time.get(symbol, 0)
        if now - last_time < 60:
            return False
        self.last_trade_time[symbol] = now
        return True

    def get_last_fill_data(self, symbol, trade_info):
        """
        Returns REAL close information for THIS trade only.

        Uses the stored entry metadata to locate the correct closing
        order from Binance trade history.

        Returns:
        {
            "exit_price": float,
            "realized_pnl": float,
            "commission": float,
            "net_pnl": float
        }
        """

        try:

            trades = self.binance.client.futures_account_trades(
                symbol=symbol,
                limit=100
            )

            if not trades:
                print("No Binance trades returned.")
                return None

            ##############################################################
            # Sort trades chronologically
            ##############################################################

            trades = sorted(
                trades,
                key=lambda t: int(t.get("time", 0))
            )

            entry_time = int(trade_info.get("entry_time", 0))
            entry_side = trade_info.get("side", "LONG")
            entry_qty = abs(float(trade_info.get("qty", 0.0)))
            closed_qty = 0.0

            expected_exit_side = (
                "SELL"
                if entry_side == "LONG"
                else "BUY"
            )

            print("\n" + "=" * 80)
            print(f"RAW BINANCE TRADES FOR {symbol}")

            print(f"""
    ENTRY METADATA

    Entry Order ID : {trade_info.get("entry_order_id")}
    Entry Time     : {entry_time}
    Entry Price    : {trade_info.get("entry_price")}
    Entry Qty      : {trade_info.get("qty")}
    Entry Side     : {entry_side}
    Expected Exit  : {expected_exit_side}
    Client Order   : {trade_info.get("client_order_id")}
    """)

            for i, t in enumerate(trades):

                print(f"""
    Trade #{i}

    Time          : {t.get("time")}
    Order ID      : {t.get("orderId")}
    Side          : {t.get("side")}
    Position Side : {t.get("positionSide")}
    Price         : {t.get("price")}
    Qty           : {t.get("qty")}
    RealizedPnL   : {t.get("realizedPnl")}
    Commission    : {t.get("commission")}
    Buyer         : {t.get("buyer")}
    Maker         : {t.get("maker")}
    """)

            print("=" * 80)

            ##############################################################
            # Find matching closing order
            ##############################################################

            closing_order_id = None

            for t in trades:

                trade_time = int(t.get("time", 0))
                realized = float(t.get("realizedPnl", 0))
                qty = abs(float(t.get("qty", 0)))
                side = t.get("side")

                # Trade happened before this position opened
                if trade_time <= entry_time:
                    continue

                # Opening fills have zero realized pnl
                if abs(realized) < 1e-8:
                    continue

                # Ignore funding / adjustment rows
                if qty == 0:
                    continue

                # Exit must be opposite side
                if side != expected_exit_side:
                    continue

                closed_qty += qty

                if closed_qty >= entry_qty:
                    closing_order_id = t["orderId"]
                    


                

                    print(f"""
        MATCHED CLOSING ORDER

        Order ID      : {closing_order_id}
        Trade Time    : {trade_time}
        Side          : {side}
        Price         : {t["price"]}
        Qty           : {qty}
        RealizedPnL   : {realized}
        """)

                    break

            if closing_order_id is None:

                print(f"""
    ❌ NO MATCHING CLOSE FOUND

    Entry Time      : {entry_time}
    Entry Side      : {entry_side}
    Expected Exit   : {expected_exit_side}
    """)

                return None

            ##############################################################
            # Collect fills belonging ONLY to this order
            ##############################################################

            close_trades = []

            for t in trades:

                if t["orderId"] != closing_order_id:
                    continue

                if t.get("side") != expected_exit_side:
                    continue

                print(f"""
    SELECTED CLOSE FILL

    Order ID      : {t['orderId']}
    Price         : {t['price']}
    Qty           : {t['qty']}
    RealizedPnL   : {t['realizedPnl']}
    Commission    : {t.get('commission')}
    """)

                close_trades.append(t)

            if not close_trades:

                print("No fills found for matched closing order.")
                return None

            ##############################################################
            # Weighted average exit
            ##############################################################

            total_qty = 0.0
            total_value = 0.0
            total_pnl = 0.0
            total_commission = 0.0

            for t in close_trades:

                qty = abs(float(t["qty"]))
                price = float(t["price"])

                total_qty += qty
                total_value += qty * price

                total_pnl += float(t.get("realizedPnl", 0))
                total_commission += abs(
                    float(t.get("commission", 0))
                )

            avg_exit = (
                total_value / total_qty
                if total_qty else 0
            )

            print("\n" + "=" * 80)
            print("FINAL EXIT CALCULATION")

            print(f"Matched Order ID : {closing_order_id}")
            print(f"Exit Qty         : {total_qty}")
            print(f"Average Exit     : {avg_exit}")
            print(f"Realized PnL     : {total_pnl}")
            print(f"Commission       : {total_commission}")
            print(f"Net PnL          : {total_pnl - total_commission}")
            print("=" * 80)

            return {
                "exit_price": avg_exit,
                "realized_pnl": total_pnl,
                "commission": total_commission,
                "net_pnl": total_pnl - total_commission
            }

        except Exception as e:

            print(f"❌ get_last_fill_data error {symbol}: {e}")

        return None

    def cleanup_closed_trade(self, symbol):
        """Remove metadata when trade closes"""
        self._mirror_trade_closure(symbol)
        if symbol in self.trade_metadata:
            del self.trade_metadata[symbol]
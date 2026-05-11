# data_engine/feature_engine.py
import pandas as pd
import asyncio
import time

from data_engine.market_stream import BinanceStream
from data_engine.live_buffer import add_candle, get_candles

from execution_engine.binance_client import BinanceClient
from execution_engine.trade_manager import TradeManager
from risk_engine.risk_manager import RiskManager
from monitoring.portfolio_cache import PortfolioCache

from strategy_engine.features import compute_features
from strategy_engine.btc_regime import BTCRegimeDetector
from utils.telegram import send_telegram
from utils.telegram_commands import get_updates, handle_command
from monitoring.trade_tracker import TradeTracker

tracker = TradeTracker()

# ---------------- INIT SYSTEM ----------------
binance = BinanceClient()
risk = RiskManager()
trade_manager = TradeManager(binance, risk)
portfolio_cache = PortfolioCache(refresh_sec=10)
btc_regime = BTCRegimeDetector(binance, refresh_sec=60)

symbols = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "AVAXUSDT",
    "LINKUSDT",
    "SUIUSDT",
    "NEARUSDT"
]


def generate_signal(last, regime, symbol):
    """
    Generate LONG or SHORT signal based on indicators + BTC regime.
    Returns: ("LONG", score) or ("SHORT", score) or (None, 0)
    """
    long_score = 0
    short_score = 0

    ema_distance = last["ema_distance"]
    trend_strength = last["trend_strength"]
    momentum = last["momentum"]
    rsi = last.get("rsi", 50)
    volume_spike = last["volume_spike"]
    close = last["close"]
    vwap = last.get("vwap", close)

    # Skip flat market
    if trend_strength < 0.0015:
        return None, 0

    # ----- LONG conditions -----
    if regime in ("BULLISH", "NEUTRAL"):
        if ema_distance > 0.001:
            long_score += 1
        if momentum > 0.003:
            long_score += 1
        if volume_spike:
            long_score += 1
        if 40 < rsi < 70:  # not overbought
            long_score += 1
        if close > vwap:
            long_score += 1

    # ----- SHORT conditions -----
    if regime in ("BEARISH", "NEUTRAL"):
        if ema_distance < -0.001:
            short_score += 1
        if momentum < -0.003:
            short_score += 1
        if volume_spike:
            short_score += 1
        if 30 < rsi < 60:  # not oversold
            short_score += 1
        if close < vwap:
            short_score += 1

    # Need at least 4/5 confirmation for high quality
    threshold = 4

    if long_score >= threshold and long_score > short_score:
        return "LONG", long_score
    elif short_score >= threshold and short_score > long_score:
        return "SHORT", short_score

    return None, 0


# ---------------- MAIN CANDLE HANDLER ----------------
async def on_candle(candle):
    symbol = candle["symbol"]
    price = candle["close"]

    # ---------------- PORTFOLIO ----------------
    if portfolio_cache.should_refresh():
        portfolio = portfolio_cache.update(trade_manager.get_portfolio)
        if portfolio:
            trade_manager.ensure_sl_tp(portfolio)
    else:
        portfolio = portfolio_cache.get()

    if not portfolio:
        return

    current_positions = portfolio.get("positions", {})

    # ---------------- TRAILING STOP / BREAKEVEN ----------------
    if symbol in current_positions:
        trade_manager.check_trailing_stop(symbol, price, portfolio)

    # ---------------- DETECT CLOSED TRADES ----------------
    for sym in list(tracker.active_trades.keys()):
        if sym not in current_positions:
            exit_price = price
            result = tracker.close_trade(sym, exit_price)

            if result:
                # Update risk manager
                risk.update_pnl(result["pnl"])
                trade_manager.cleanup_closed_trade(sym)

                stats = tracker.stats()
                risk_status = risk.get_status()

                asyncio.create_task(send_telegram(f"""
📉 TRADE CLOSED

Symbol: {result['symbol']}
Result: {result['result']}
PnL: {round(result['pnl'], 4)} USDT

📊 Today's Stats:
Daily PnL: ${risk_status['daily_pnl']}
Trades: {risk_status['trades_today']}
Win Rate: {stats['win_rate']}%
Consecutive Losses: {risk_status['consecutive_losses']}
"""))

    # ---------------- RISK CHECK ----------------
    if not risk.can_trade(trade_manager.open_trades_count(portfolio)):
        return

    # ---------------- DATA & FEATURES ----------------
    add_candle(symbol, candle)
    df = pd.DataFrame(get_candles(symbol))

    if len(df) < 30:  # Need enough for ATR/RSI
        return

    df = compute_features(df)

    if df is None or df.empty:
        return

    last = df.iloc[-1]
    if last is None or last.get("is_synthetic", False):
        return

    # Skip if any critical indicator is NaN
    if pd.isna(last.get("atr")) or pd.isna(last.get("rsi")):
        return

    # ---------------- BTC REGIME CHECK ----------------
    regime = btc_regime.detect()

    # Skip altcoins if BTC is bearish and we want LONG only (optional)
    # if regime == "BEARISH" and symbol != "BTCUSDT":
    #     return

    # ---------------- GENERATE SIGNAL ----------------
    side, score = generate_signal(last, regime, symbol)

    # Debug print
    print(f"""
📊 {symbol} | Regime: {regime}
Close: {last['close']:.4f} | EMA9: {last['ema_9']:.4f} | EMA21: {last['ema_21']:.4f}
RSI: {last['rsi']:.2f} | ATR%: {last['atr_pct']*100:.3f}% | Momentum: {last['momentum']:.5f}
VolSpike: {last['volume_spike']} | Signal: {side} (score={score})
""")

    if side is None:
        return

    # ---------------- EXECUTE ----------------
    if trade_manager.is_already_in_trade(symbol, portfolio):
        return

    if not trade_manager.can_trade_symbol(symbol):
        return

    atr_pct = float(last["atr_pct"])
    trade = trade_manager.prepare_trade(symbol, price, portfolio, side=side, atr_pct=atr_pct)

    if trade["qty"] < 0.001:
        print(f"⚠️ Skipping {symbol} (qty too small)")
        return

    msg = f"""
🚀 {side} SIGNAL: {symbol}

Entry: {trade['entry']}
SL: {trade['stop_loss']:.4f} ({trade['sl_distance']*100:.3f}%)
TP: {trade['take_profit']:.4f} ({trade['tp_distance']*100:.3f}%)
Qty: {trade['qty']}
Score: {score}/5
Regime: {regime}
ATR%: {atr_pct*100:.3f}%
RSI: {last['rsi']:.2f}
"""
    print(msg)
    asyncio.create_task(send_telegram(msg))

    binance.set_leverage(symbol, 3)
    order = trade_manager.execute_trade(trade)

    if order:
        tracker.add_trade(trade)
        trade_manager.set_sl_tp(trade)

        asyncio.create_task(send_telegram(f"""
✅ {side} EXECUTED: {symbol}
Entry: {trade['entry']}
Qty: {trade['qty']}
SL: {trade['stop_loss']:.4f}
TP: {trade['take_profit']:.4f}
"""))


async def telegram_listener():
    print("📲 Telegram listener started")
    while True:
        for update in get_updates():
            if "message" in update:
                text = update["message"]["text"]
                response = handle_command(text, binance)
                asyncio.create_task(send_telegram(response))
        await asyncio.sleep(2)


async def main():
    print("⚡ PHASE 5 ENGINE STARTED (BTC Regime + Dynamic SL/TP + SHORT)")
    stream = BinanceStream(symbols)
    await asyncio.gather(
        stream.start(on_candle_close=on_candle),
        telegram_listener()
    )


if __name__ == "__main__":
    asyncio.run(main())
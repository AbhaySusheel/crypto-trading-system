# strategy_engine/btc_regime.py
import time
import pandas as pd

class BTCRegimeDetector:
    """
    Detects BTC market regime using higher timeframe.
    Altcoins follow BTC ~80% of time, so we filter alt trades by BTC trend.
    """

    def __init__(self, binance_client, refresh_sec=60):
        self.binance = binance_client
        self.refresh_sec = refresh_sec
        self.last_check = 0
        self.cached_regime = "NEUTRAL"
        self.cached_data = {}

    def get_btc_klines(self, interval="15m", limit=100):
        """Fetch BTC 15m klines for trend analysis"""
        try:
            klines = self.binance.client.futures_klines(
                symbol="BTCUSDT",
                interval=interval,
                limit=limit
            )
            df = pd.DataFrame(klines, columns=[
                "time", "open", "high", "low", "close", "volume",
                "close_time", "qav", "trades", "tbbav", "tbqav", "ignore"
            ])
            df["close"] = df["close"].astype(float)
            df["high"] = df["high"].astype(float)
            df["low"] = df["low"].astype(float)
            return df
        except Exception as e:
            print(f"❌ BTC regime fetch error: {e}")
            return None

    def detect(self):
        """Returns: BULLISH, BEARISH, or NEUTRAL"""
        now = time.time()

        # Return cached value
        if now - self.last_check < self.refresh_sec:
            return self.cached_regime

        df = self.get_btc_klines()
        if df is None or len(df) < 60:
            return "NEUTRAL"

        # Calculate EMAs on 15m
        df["ema_20"] = df["close"].ewm(span=20, adjust=False).mean()
        df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()

        last = df.iloc[-1]
        prev = df.iloc[-5]  # 5 candles ago = 75min ago

        close = last["close"]
        ema_20 = last["ema_20"]
        ema_50 = last["ema_50"]

        # Calculate slope (momentum)
        slope = (last["ema_20"] - prev["ema_20"]) / prev["ema_20"]

        # BULLISH: price > ema_20 > ema_50 AND positive slope
        if close > ema_20 > ema_50 and slope > 0.001:
            regime = "BULLISH"
        # BEARISH: price < ema_20 < ema_50 AND negative slope
        elif close < ema_20 < ema_50 and slope < -0.001:
            regime = "BEARISH"
        else:
            regime = "NEUTRAL"

        self.cached_regime = regime
        self.cached_data = {
            "regime": regime,
            "btc_price": close,
            "ema_20": ema_20,
            "ema_50": ema_50,
            "slope": slope
        }
        self.last_check = now

        print(f"🌍 BTC REGIME: {regime} | Price: {close:.2f} | Slope: {slope:.5f}")
        return regime

    def allow_long(self):
        return self.detect() == "BULLISH"

    def allow_short(self):
        return self.detect() == "BEARISH"
# strategy_engine/features.py
import pandas as pd
import numpy as np

def compute_features(df):
    """Enhanced feature engineering with ATR + RSI"""
    if df is None or len(df) < 20:
        return df

    df = df.copy()

    # Ensure numeric types
    for col in ["open", "high", "low", "close", "volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # ----- EMAs -----
    df["ema_9"] = df["close"].ewm(span=9, adjust=False).mean()
    df["ema_21"] = df["close"].ewm(span=21, adjust=False).mean()
    df["ema_50"] = df["close"].ewm(span=50, adjust=False).mean()

    # ----- Momentum -----
    df["momentum"] = (df["close"] - df["close"].shift(3)) / df["close"].shift(3)

    # ----- Volume Spike -----
    df["vol_avg"] = df["volume"].rolling(20).mean()
    df["volume_spike"] = df["volume"] > (df["vol_avg"] * 1.5)

    # ----- ATR (Average True Range) for dynamic SL/TP -----
    df["tr1"] = df["high"] - df["low"]
    df["tr2"] = abs(df["high"] - df["close"].shift(1))
    df["tr3"] = abs(df["low"] - df["close"].shift(1))
    df["true_range"] = df[["tr1", "tr2", "tr3"]].max(axis=1)
    df["atr"] = df["true_range"].rolling(14).mean()
    df["atr_pct"] = df["atr"] / df["close"]  # ATR as % of price

    # ----- RSI (14) -----
    delta = df["close"].diff()
    gain = delta.where(delta > 0, 0).rolling(14).mean()
    loss = -delta.where(delta < 0, 0).rolling(14).mean()
    rs = gain / loss.replace(0, np.nan)
    df["rsi"] = 100 - (100 / (1 + rs))

    # ----- VWAP (session) -----
    df["typical"] = (df["high"] + df["low"] + df["close"]) / 3
    df["vwap"] = (df["typical"] * df["volume"]).cumsum() / df["volume"].cumsum()

    # ----- Trend Strength -----
    df["trend_strength"] = abs(df["ema_9"] - df["ema_21"]) / df["close"]

    # ----- EMA Distance -----
    df["ema_distance"] = (df["ema_9"] - df["ema_21"]) / df["close"]

    # Mark non-synthetic rows
    df["is_synthetic"] = False

    # Clean up
    df.drop(columns=["tr1", "tr2", "tr3", "true_range", "typical"], inplace=True, errors="ignore")

    return df
import pandas as pd
from ta.momentum import RSIIndicator

def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()

    # RSI indicator
    df["rsi"] = RSIIndicator(close=df["close"], window=14).rsi()

    # Remove NaN rows (initial rows will be NaN)
    df.dropna(inplace=True)

    return df
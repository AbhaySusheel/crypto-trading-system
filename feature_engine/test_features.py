import pandas as pd
from feature_engine.indicators import compute_features

# Create sample candle data
data = []

for i in range(60):
    data.append({
        "open": 100 + i,
        "high": 105 + i,
        "low": 95 + i,
        "close": 100 + i,
        "volume": 10 + i
    })

df = pd.DataFrame(data)

# Compute features
df = compute_features(df)

# Print output
print("✅ FEATURES OUTPUT:")
print(df.tail())
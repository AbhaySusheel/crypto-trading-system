live_candles = {}

def add_candle(symbol, candle):
    if symbol not in live_candles:
        live_candles[symbol] = []

    live_candles[symbol].append(candle)

    # keep last 300 candles
    if len(live_candles[symbol]) > 300:
        live_candles[symbol].pop(0)


def get_candles(symbol):
    return live_candles.get(symbol, [])
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def test_portfolio_cache_tracks_separate_ttls_and_staleness(monkeypatch):
    module = importlib.import_module("monitoring.portfolio_cache")

    now = 1000.0
    monkeypatch.setattr(module.time, "time", lambda: now)

    fetchers = {
        "balances": lambda: {"USDT": 100.0},
        "positions": lambda: {"BTCUSDT": {"qty": 1.0, "entry_price": 50000.0}},
        "open_orders": lambda: [{"symbol": "ETHUSDT"}],
    }

    cache = module.PortfolioCache(fetchers=fetchers, ttl_seconds={"balances": 30, "positions": 10, "open_orders": 10})

    cache.refresh_all()

    portfolio = cache.get_portfolio()
    assert portfolio["balances"]["USDT"] == 100.0
    assert portfolio["positions"]["BTCUSDT"]["qty"] == 1.0
    assert portfolio["open_orders"][0]["symbol"] == "ETHUSDT"

    now = 1011.0
    assert cache.is_stale("positions") is True
    assert cache.is_stale("balances") is False
    assert cache.is_trading_ready() is False

    now = 1031.0
    assert cache.is_stale("balances") is True
    assert cache.is_trading_ready() is False

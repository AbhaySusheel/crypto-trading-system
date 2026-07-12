import asyncio
import logging
import time
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


class PortfolioCache:
    CACHE_KEYS = ("balances", "positions", "open_orders")

    def __init__(self, binance_client=None, fetchers=None, ttl_seconds=None):
        self.binance_client = binance_client
        self._fetchers = fetchers or {}
        self.ttl_seconds = ttl_seconds or {
            "balances": 30,
            "positions": 10,
            "open_orders": 10,
        }
        self._cache_state = {
            "balances": {"data": {}, "last_update": 0.0},
            "positions": {"data": {}, "last_update": 0.0},
            "open_orders": {"data": [], "last_update": 0.0},
        }
        self._refresh_task = None
        self._last_errors: Dict[str, str] = {}
        self._initialize_default_fetchers()

    def _initialize_default_fetchers(self):
        if self.binance_client is None:
            return
        self._fetchers.setdefault("balances", lambda: self.binance_client.get_balances())
        self._fetchers.setdefault("positions", lambda: self.binance_client.get_positions())
        self._fetchers.setdefault("open_orders", lambda: self.binance_client.get_open_orders())

    def is_stale(self, cache_key: str) -> bool:
        ttl = self.ttl_seconds.get(cache_key, 0)
        last_update = self._cache_state[cache_key]["last_update"]
        return time.time() - last_update > ttl

    def should_refresh(self, cache_key: str = None) -> bool:
        if cache_key is None:
            return any(self.is_stale(key) for key in self.CACHE_KEYS)
        return self.is_stale(cache_key)

    def refresh(self, cache_key: str) -> Any:
        if cache_key not in self._cache_state:
            raise KeyError(f"Unknown portfolio cache {cache_key}")

        fetcher = self._fetchers.get(cache_key)
        if fetcher is None:
            return self._cache_state[cache_key]["data"]

        try:
            data = fetcher()
            self._cache_state[cache_key]["data"] = data
            self._cache_state[cache_key]["last_update"] = time.time()
            self._last_errors.pop(cache_key, None)
            return data
        except Exception as exc:
            self._last_errors[cache_key] = str(exc)
            logger.warning("Portfolio cache refresh failed for %s: %s", cache_key, exc)
            return self._cache_state[cache_key]["data"]

    def refresh_all(self) -> Dict[str, Any]:
        for cache_key in self.CACHE_KEYS:
            self.refresh(cache_key)
        return self.get_portfolio()

    def refresh_if_needed(self) -> bool:
        refreshed = False
        for cache_key in self.CACHE_KEYS:
            if self.is_stale(cache_key):
                self.refresh(cache_key)
                refreshed = True
        return refreshed

    def is_trading_ready(self) -> bool:
        return not any(self.is_stale(cache_key) for cache_key in ("balances", "positions", "open_orders"))

    def get_balances(self) -> Dict[str, float]:
        return self._cache_state["balances"]["data"]

    def get_positions(self) -> Dict[str, Dict[str, Any]]:
        return self._cache_state["positions"]["data"]

    def get_open_orders(self, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
        open_orders = self._cache_state["open_orders"]["data"]
        if symbol is None:
            return open_orders
        return [order for order in open_orders if order.get("symbol") == symbol]

    def get_portfolio(self) -> Dict[str, Any]:
        return {
            "balances": self.get_balances(),
            "positions": self.get_positions(),
            "open_orders": self.get_open_orders(),
        }

    async def start_background_refresh(self):
        if self._refresh_task and not self._refresh_task.done():
            return self._refresh_task
        self._refresh_task = asyncio.create_task(self._background_refresh_loop())
        return self._refresh_task

    async def stop_background_refresh(self):
        if self._refresh_task is None:
            return
        self._refresh_task.cancel()
        try:
            await self._refresh_task
        except asyncio.CancelledError:
            pass
        self._refresh_task = None

    async def _background_refresh_loop(self):
        while True:
            self.refresh_if_needed()
            await asyncio.sleep(min(self.ttl_seconds.values()) / 2)
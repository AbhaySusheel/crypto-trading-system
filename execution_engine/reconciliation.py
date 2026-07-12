import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Dict, List, Optional, Set

from execution_engine.order_state import (
    OrderStateManager,
    OrderRecord,
    OrderStatus,
    OrderType,
    TradeRecord,
    TradeStatus,
)

logger = logging.getLogger(__name__)


class Severity(Enum):
    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


@dataclass
class ReconciliationMismatch:
    symbol: str
    category: str
    severity: Severity
    description: str
    suggested_action: str
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReconciliationReport:
    timestamp: str = field(default_factory=lambda: datetime.utcnow().replace(tzinfo=timezone.utc).isoformat())
    mismatches: List[ReconciliationMismatch] = field(default_factory=list)
    severity: Severity = Severity.INFO
    symbols: Set[str] = field(default_factory=set)
    suggested_actions: List[str] = field(default_factory=list)

    def add_mismatch(self, mismatch: ReconciliationMismatch) -> None:
        self.mismatches.append(mismatch)
        self.symbols.add(mismatch.symbol)
        self.suggested_actions.append(mismatch.suggested_action)

        if mismatch.severity == Severity.CRITICAL:
            self.severity = Severity.CRITICAL
        elif mismatch.severity == Severity.WARNING and self.severity == Severity.INFO:
            self.severity = Severity.WARNING

    def summary(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "severity": self.severity.value,
            "mismatch_count": len(self.mismatches),
            "affected_symbols": sorted(self.symbols),
            "suggested_actions": sorted(set(self.suggested_actions)),
        }


class ReconciliationEngine:
    def __init__(self, binance_client: Any, order_state_manager: Optional[OrderStateManager] = None, stale_seconds: int = 300, portfolio_cache: Optional[Any] = None):
        self.binance = binance_client
        self.order_state_manager = order_state_manager
        self.stale_seconds = stale_seconds
        self.portfolio_cache = portfolio_cache
        logger.info("Reconciliation engine initialized (read-only)")

    def startup_scan(self) -> ReconciliationReport:
        logger.info("Starting reconciliation startup scan")
        try:
            report = self._run_scan()
            self._log_report(report, stage="startup")
            return report
        except Exception as exc:  # fail-open
            logger.warning("Startup reconciliation failed: %s", exc, exc_info=True)
            return ReconciliationReport()

    async def periodic_scan(self, interval_seconds: int = 60) -> None:
        logger.info("Starting periodic reconciliation loop interval=%s", interval_seconds)
        while True:
            await asyncio.sleep(interval_seconds)
            try:
                report = self._run_scan()
                self._log_report(report, stage="periodic")
            except Exception as exc:  # fail-open
                logger.warning("Periodic reconciliation error: %s", exc, exc_info=True)

    def _run_scan(self) -> ReconciliationReport:
        report = ReconciliationReport()
        local_trades = self._load_local_trades()
        local_orders = self._load_local_orders()
        exchange_portfolio = self._load_exchange_portfolio(report)
        exchange_positions = exchange_portfolio.get("positions", {}) if exchange_portfolio else {}
        exchange_open_orders = self._load_exchange_open_orders(report)

        self._detect_position_trade_gap(report, local_trades, exchange_positions)
        self._detect_protection_orders(report, local_trades, local_orders, exchange_open_orders)
        self._detect_quantity_direction_mismatch(report, local_trades, exchange_positions)
        self._detect_stale_local_orders(report, local_orders, exchange_open_orders)
        self._detect_orphaned_protection_orders(report, local_trades, exchange_open_orders)
        self._detect_lifecycle_inconsistencies(report, local_trades, exchange_positions)

        return report

    def _load_local_trades(self) -> Dict[str, TradeRecord]:
        if not self.order_state_manager:
            logger.warning("No OrderStateManager available for reconciliation")
            return {}
        return {trade.symbol: trade for trade in self.order_state_manager.trades_by_id.values()}

    def _load_local_orders(self) -> Dict[str, List[OrderRecord]]:
        if not self.order_state_manager:
            return {}
        orders_by_symbol: Dict[str, List[OrderRecord]] = {}
        for order in self.order_state_manager.orders_by_client_id.values():
            orders_by_symbol.setdefault(order.symbol, []).append(order)
        return orders_by_symbol

    def _load_exchange_portfolio(self, report: ReconciliationReport) -> Dict[str, Any]:
        try:
            if self.portfolio_cache is not None:
                return self.portfolio_cache.get_portfolio()
            return self.binance.get_portfolio()
        except Exception as exc:
            report.add_mismatch(
                ReconciliationMismatch(
                    symbol="ALL",
                    category="exchange_connectivity",
                    severity=Severity.WARNING,
                    description="Unable to fetch live portfolio from Binance",
                    suggested_action="Verify Binance API connectivity and permissions",
                    details={"error": str(exc)},
                )
            )
            return {}

    def _load_exchange_open_orders(self, report: ReconciliationReport) -> List[Dict[str, Any]]:
        try:
            if self.portfolio_cache is not None:
                return self.portfolio_cache.get_open_orders()
            return self.binance.client.futures_get_open_orders()
        except Exception as exc:
            report.add_mismatch(
                ReconciliationMismatch(
                    symbol="ALL",
                    category="exchange_orders",
                    severity=Severity.WARNING,
                    description="Unable to fetch Binance open orders",
                    suggested_action="Verify Binance open order API and retry",
                    details={"error": str(exc)},
                )
            )
            return []

    def _normalize_symbol_orders(self, orders: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for order in orders:
            symbol = order.get("symbol") or "UNKNOWN"
            grouped.setdefault(symbol, []).append(order)
        return grouped

    def _detect_position_trade_gap(
        self,
        report: ReconciliationReport,
        local_trades: Dict[str, TradeRecord],
        exchange_positions: Dict[str, Dict[str, Any]],
    ) -> None:
        local_symbols = set(local_trades)
        exchange_symbols = set(exchange_positions)
        all_symbols = local_symbols | exchange_symbols

        for symbol in sorted(all_symbols):
            local_trade = local_trades.get(symbol)
            exchange_pos = exchange_positions.get(symbol)
            has_exchange_position = bool(exchange_pos and float(exchange_pos.get("qty", 0)) != 0)
            has_local_trade = local_trade is not None

            if has_local_trade and local_trade.status in {
                TradeStatus.OPEN,
                TradeStatus.EXIT_PENDING,
                TradeStatus.PARTIALLY_EXITED,
                TradeStatus.ENTRY_PARTIALLY_FILLED,
                TradeStatus.ENTRY_PENDING,
            } and not has_exchange_position:
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="local_open_without_binance_position",
                        severity=Severity.CRITICAL if local_trade.status == TradeStatus.OPEN else Severity.WARNING,
                        description=(
                            "Local trade is active but Binance reports no open position for this symbol."
                        ),
                        suggested_action=(
                            "Investigate local open trade state and confirm whether the order was filled on Binance."
                        ),
                        details={
                            "local_trade_status": local_trade.status.value,
                            "entry_qty": local_trade.entry_qty,
                            "entry_filled_qty": local_trade.entry_filled_qty,
                        },
                    )
                )

            if has_exchange_position and not has_local_trade:
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="binance_position_without_local_trade",
                        severity=Severity.WARNING,
                        description=(
                            "Binance reports an open position but there is no matching local trade record."
                        ),
                        suggested_action=(
                            "Review exchange position history and consider syncing or archiving missing trades."
                        ),
                        details={
                            "position_qty": float(exchange_pos.get("qty", 0)) if exchange_pos else 0,
                            "entry_price": float(exchange_pos.get("entryPrice", 0)) if exchange_pos else 0,
                        },
                    )
                )

    def _detect_protection_orders(
        self,
        report: ReconciliationReport,
        local_trades: Dict[str, TradeRecord],
        local_orders: Dict[str, List[OrderRecord]],
        exchange_open_orders: List[Dict[str, Any]],
    ) -> None:
        exchange_orders_by_symbol = self._normalize_symbol_orders(exchange_open_orders)
        open_states = {OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.PARTIAL_FILLED}

        for symbol, trade in local_trades.items():
            if trade.status not in {TradeStatus.OPEN, TradeStatus.EXIT_PENDING, TradeStatus.PARTIALLY_EXITED}:
                continue

            symbol_orders = local_orders.get(symbol, [])
            local_sl = [o for o in symbol_orders if o.type == OrderType.STOP_LOSS and o.status in open_states]
            local_tp = [o for o in symbol_orders if o.type == OrderType.TAKE_PROFIT and o.status in open_states]
            exchange_orders = exchange_orders_by_symbol.get(symbol, [])
            exchange_sl = [o for o in exchange_orders if o.get("type") in {"STOP_MARKET", "STOP"}]
            exchange_tp = [o for o in exchange_orders if o.get("type") in {"TAKE_PROFIT_MARKET", "TAKE_PROFIT"}]

            if not local_sl and not exchange_sl:
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="missing_stop_loss",
                        severity=Severity.WARNING,
                        description=(
                            "Open trade has no local or Binance stop-loss order attached."
                        ),
                        suggested_action=(
                            "Inspect trade protection and ensure SL orders are created or reconciled."
                        ),
                        details={
                            "trade_status": trade.status.value,
                            "local_sl_count": len(local_sl),
                            "exchange_sl_count": len(exchange_sl),
                        },
                    )
                )

            if not local_tp and not exchange_tp:
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="missing_take_profit",
                        severity=Severity.WARNING,
                        description=(
                            "Open trade has no local or Binance take-profit order attached."
                        ),
                        suggested_action=(
                            "Inspect trade protection and ensure TP orders are created or reconciled."
                        ),
                        details={
                            "trade_status": trade.status.value,
                            "local_tp_count": len(local_tp),
                            "exchange_tp_count": len(exchange_tp),
                        },
                    )
                )

            if trade.sl_order_id is not None and not any(
                int(o.get("orderId", 0)) == trade.sl_order_id for o in exchange_orders
            ):
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="missing_sl_on_exchange",
                        severity=Severity.WARNING,
                        description=(
                            "Local trade records a stop-loss order ID but the order was not found among Binance open orders."
                        ),
                        suggested_action=(
                            "Verify whether the stop-loss order has already executed or was canceled manually."
                        ),
                        details={
                            "local_sl_order_id": trade.sl_order_id,
                            "exchange_sl_count": len(exchange_sl),
                        },
                    )
                )

            if trade.tp_order_id is not None and not any(
                int(o.get("orderId", 0)) == trade.tp_order_id for o in exchange_orders
            ):
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="missing_tp_on_exchange",
                        severity=Severity.WARNING,
                        description=(
                            "Local trade records a take-profit order ID but the order was not found among Binance open orders."
                        ),
                        suggested_action=(
                            "Verify whether the take-profit order has already executed or was canceled manually."
                        ),
                        details={
                            "local_tp_order_id": trade.tp_order_id,
                            "exchange_tp_count": len(exchange_tp),
                        },
                    )
                )

    def _detect_quantity_direction_mismatch(
        self,
        report: ReconciliationReport,
        local_trades: Dict[str, TradeRecord],
        exchange_positions: Dict[str, Dict[str, Any]],
    ) -> None:
        for symbol, trade in local_trades.items():
            exchange_pos = exchange_positions.get(symbol)
            if not exchange_pos:
                continue

            position_qty = float(exchange_pos.get("qty", 0))
            entry_qty = float(trade.entry_filled_qty)
            abs_qty = abs(position_qty)

            if abs_qty != 0 and abs(abs_qty - entry_qty) > max(1e-6, entry_qty * 0.001):
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="quantity_mismatch",
                        severity=Severity.WARNING,
                        description=(
                            "Binance position quantity does not match local recorded entry fill quantity."
                        ),
                        suggested_action=(
                            "Validate quantity reconciliation and ensure fills are recorded correctly."
                        ),
                        details={
                            "position_qty": position_qty,
                            "local_entry_filled_qty": entry_qty,
                        },
                    )
                )

            direction = "LONG" if position_qty > 0 else "SHORT" if position_qty < 0 else "FLAT"
            if direction != "FLAT" and direction != trade.side:
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="direction_mismatch",
                        severity=Severity.CRITICAL,
                        description=(
                            "Binance position direction conflicts with local trade side."
                        ),
                        suggested_action=(
                            "Review the trade entry and position direction immediately."
                        ),
                        details={
                            "trade_side": trade.side,
                            "position_direction": direction,
                            "position_qty": position_qty,
                        },
                    )
                )

    def _detect_stale_local_orders(
        self,
        report: ReconciliationReport,
        local_orders: Dict[str, List[OrderRecord]],
        exchange_open_orders: List[Dict[str, Any]],
    ) -> None:
        open_exchange_ids = {int(o.get("orderId", 0)) for o in exchange_open_orders if o.get("orderId") is not None}
        now = datetime.utcnow().replace(tzinfo=timezone.utc)

        for symbol, orders in local_orders.items():
            for order in orders:
                if order.status not in {OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.PARTIAL_FILLED}:
                    continue
                order_ts = self._parse_timestamp(order.timestamp)
                age = now - order_ts
                if age < timedelta(seconds=self.stale_seconds):
                    continue
                if order.order_id is None or int(order.order_id) not in open_exchange_ids:
                    report.add_mismatch(
                        ReconciliationMismatch(
                            symbol=symbol,
                            category="stale_pending_order",
                            severity=Severity.WARNING,
                            description=(
                                "Local pending order is stale and not present in open Binance orders."
                            ),
                            suggested_action=(
                                "Check whether the order was filled, canceled, or failed."
                            ),
                            details={
                                "order_id": order.order_id,
                                "order_status": order.status.value,
                                "order_age_seconds": int(age.total_seconds()),
                            },
                        )
                    )

    def _detect_orphaned_protection_orders(
        self,
        report: ReconciliationReport,
        local_trades: Dict[str, TradeRecord],
        exchange_open_orders: List[Dict[str, Any]],
    ) -> None:
        known_order_ids = {
            int(order.order_id)
            for order in self.order_state_manager.orders_by_client_id.values()
            if order.order_id is not None
        } if self.order_state_manager else set()

        for order in exchange_open_orders:
            order_id = order.get("orderId")
            if order_id is None:
                continue
            order_type = order.get("type", "")
            if order_type not in {"STOP_MARKET", "STOP", "TAKE_PROFIT_MARKET", "TAKE_PROFIT"}:
                continue
            symbol = order.get("symbol", "UNKNOWN")
            if order_id not in known_order_ids:
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="orphaned_protection_order",
                        severity=Severity.WARNING,
                        description=(
                            "Binance protection order is open but not represented in local state."
                        ),
                        suggested_action=(
                            "Inspect orphaned protection orders and add reconciliation support."
                        ),
                        details={
                            "order_id": order_id,
                            "order_type": order_type,
                            "symbol": symbol,
                        },
                    )
                )

    def _detect_lifecycle_inconsistencies(
        self,
        report: ReconciliationReport,
        local_trades: Dict[str, TradeRecord],
        exchange_positions: Dict[str, Dict[str, Any]],
    ) -> None:
        for symbol, trade in local_trades.items():
            exchange_pos = exchange_positions.get(symbol)
            position_qty = float(exchange_pos.get("qty", 0)) if exchange_pos else 0.0

            if trade.status == TradeStatus.CLOSED and abs(position_qty) > 0:
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="closed_trade_with_open_position",
                        severity=Severity.CRITICAL,
                        description=(
                            "Local trade is marked closed while Binance still reports an open position."
                        ),
                        suggested_action=(
                            "Verify whether the position was reopened or local state was not updated."
                        ),
                        details={
                            "position_qty": position_qty,
                            "trade_status": trade.status.value,
                        },
                    )
                )

            if trade.status == TradeStatus.OPEN and trade.entry_filled_qty <= 0:
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="open_trade_without_executed_qty",
                        severity=Severity.WARNING,
                        description=(
                            "Local trade is open but has no recorded executed quantity."
                        ),
                        suggested_action=(
                            "Confirm entry fill data and ensure order execution is recorded correctly."
                        ),
                        details={
                            "entry_qty": trade.entry_qty,
                            "entry_filled_qty": trade.entry_filled_qty,
                        },
                    )
                )

            if trade.status == TradeStatus.ENTRY_PARTIALLY_FILLED and trade.entry_filled_qty == 0:
                report.add_mismatch(
                    ReconciliationMismatch(
                        symbol=symbol,
                        category="partial_entry_without_fills",
                        severity=Severity.WARNING,
                        description=(
                            "Local trade is partially filled but no entry fill quantity is recorded."
                        ),
                        suggested_action=(
                            "Validate partial fill recording and order fill reconciliation."
                        ),
                        details={
                            "entry_qty": trade.entry_qty,
                            "entry_filled_qty": trade.entry_filled_qty,
                        },
                    )
                )

    def _parse_timestamp(self, timestamp: str) -> datetime:
        if not timestamp:
            return datetime.utcnow().replace(tzinfo=timezone.utc)
        try:
            if timestamp.endswith("Z"):
                timestamp = timestamp[:-1] + "+00:00"
            return datetime.fromisoformat(timestamp)
        except Exception:
            return datetime.utcnow().replace(tzinfo=timezone.utc)

    def _log_report(self, report: ReconciliationReport, stage: str) -> None:
        summary = report.summary()
        if report.mismatches:
            logger.warning(
                "Reconciliation %s report: severity=%s symbols=%s count=%s",
                stage,
                summary["severity"],
                summary["affected_symbols"],
                summary["mismatch_count"],
            )
            for mismatch in report.mismatches:
                log_fn = logger.info if mismatch.severity == Severity.INFO else logger.warning if mismatch.severity == Severity.WARNING else logger.critical
                log_fn(
                    "Reconciliation mismatch %s symbol=%s severity=%s description=%s suggested_action=%s details=%s",
                    mismatch.category,
                    mismatch.symbol,
                    mismatch.severity.value,
                    mismatch.description,
                    mismatch.suggested_action,
                    mismatch.details,
                )
        else:
            logger.info("Reconciliation %s scan found no mismatches", stage)

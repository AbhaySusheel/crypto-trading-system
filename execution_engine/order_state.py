import abc
import json
import logging
import os
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1


class InvalidStateTransition(Exception):
    """Raised when a requested state transition is not valid."""


class SchemaMigrationRequired(Exception):
    """Raised when persisted state schema version does not match current version."""


class OrderStateError(Exception):
    """Generic order state management error."""


class OrderStatus(Enum):
    NEW = "NEW"
    PENDING = "PENDING"
    PARTIAL_FILLED = "PARTIAL_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


class OrderType(Enum):
    ENTRY = "ENTRY"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"
    EXIT = "EXIT"


class TradeStatus(Enum):
    NEW = "NEW"
    ENTRY_PENDING = "ENTRY_PENDING"
    ENTRY_PARTIALLY_FILLED = "ENTRY_PARTIALLY_FILLED"
    OPEN = "OPEN"
    EXIT_PENDING = "EXIT_PENDING"
    PARTIALLY_EXITED = "PARTIALLY_EXITED"
    CLOSED = "CLOSED"
    CANCELLED = "CANCELLED"
    FAILED = "FAILED"


ORDER_TRANSITIONS: Dict[OrderStatus, Tuple[OrderStatus, ...]] = {
    OrderStatus.NEW: (OrderStatus.PENDING, OrderStatus.FAILED),
    OrderStatus.PENDING: (
        OrderStatus.PARTIAL_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    ),
    OrderStatus.PARTIAL_FILLED: (
        OrderStatus.PARTIAL_FILLED,
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.FAILED,
    ),
    OrderStatus.FILLED: (),
    OrderStatus.CANCELLED: (),
    OrderStatus.FAILED: (),
}

TRADE_TRANSITIONS: Dict[TradeStatus, Tuple[TradeStatus, ...]] = {
    TradeStatus.NEW: (
        TradeStatus.ENTRY_PENDING,
        TradeStatus.CANCELLED,
        TradeStatus.FAILED,
    ),
    TradeStatus.ENTRY_PENDING: (
        TradeStatus.ENTRY_PARTIALLY_FILLED,
        TradeStatus.OPEN,
        TradeStatus.CANCELLED,
        TradeStatus.FAILED,
    ),
    TradeStatus.ENTRY_PARTIALLY_FILLED: (
        TradeStatus.OPEN,
        TradeStatus.CANCELLED,
        TradeStatus.FAILED,
    ),
    TradeStatus.OPEN: (
        TradeStatus.EXIT_PENDING,
        TradeStatus.PARTIALLY_EXITED,
        TradeStatus.CLOSED,
        TradeStatus.FAILED,
    ),
    TradeStatus.EXIT_PENDING: (
        TradeStatus.PARTIALLY_EXITED,
        TradeStatus.CLOSED,
        TradeStatus.FAILED,
    ),
    TradeStatus.PARTIALLY_EXITED: (
        TradeStatus.EXIT_PENDING,
        TradeStatus.CLOSED,
        TradeStatus.FAILED,
    ),
    TradeStatus.CLOSED: (),
    TradeStatus.CANCELLED: (),
    TradeStatus.FAILED: (),
}


@dataclass
class FillEvent:
    fill_id: str
    order_id: Optional[int]
    qty: float
    price: float
    commission: float = 0.0
    commission_asset: str = "USDT"
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError("Fill qty must be positive")
        if self.price <= 0:
            raise ValueError("Fill price must be positive")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "fill_id": self.fill_id,
            "order_id": self.order_id,
            "qty": self.qty,
            "price": self.price,
            "commission": self.commission,
            "commission_asset": self.commission_asset,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "FillEvent":
        return cls(
            fill_id=str(data["fill_id"]),
            order_id=data.get("order_id"),
            qty=float(data["qty"]),
            price=float(data["price"]),
            commission=float(data.get("commission", 0.0)),
            commission_asset=str(data.get("commission_asset", "USDT")),
            timestamp=str(data.get("timestamp", datetime.utcnow().isoformat() + "Z")),
        )


@dataclass
class OrderRecord:
    order_id: Optional[int]
    client_order_id: str
    trade_id: str
    symbol: str
    side: str
    type: OrderType
    qty: float
    price: Optional[float] = None
    stop_price: Optional[float] = None
    reduce_only: bool = False
    close_position: bool = False
    time_in_force: Optional[str] = None
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    parent_order_id: Optional[int] = None
    trade_role: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    fills: List[FillEvent] = field(default_factory=list)

    _status: OrderStatus = field(default=OrderStatus.NEW, init=False, repr=False)
    _filled_qty: float = field(default=0.0, init=False, repr=False)
    _remaining_qty: float = field(default=0.0, init=False, repr=False)
    _avg_fill_price: Optional[float] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._normalize_quantities()
        self._status = OrderStatus.NEW

    @property
    def status(self) -> OrderStatus:
        return self._status

    @property
    def filled_qty(self) -> float:
        return self._filled_qty

    @property
    def remaining_qty(self) -> float:
        return self._remaining_qty

    @property
    def avg_fill_price(self) -> Optional[float]:
        return self._avg_fill_price

    def _normalize_quantities(self) -> None:
        self._filled_qty = max(0.0, self._filled_qty)
        self._remaining_qty = max(0.0, self.qty - self._filled_qty)
        if self._remaining_qty == 0.0 and self._filled_qty == 0.0:
            self._remaining_qty = self.qty
        if self._filled_qty > self.qty:
            self._filled_qty = self.qty
            self._remaining_qty = 0.0

    def _transition_to(self, new_status: OrderStatus) -> None:
        if new_status == self._status:
            return
        allowed = ORDER_TRANSITIONS.get(self._status, ())
        if new_status not in allowed:
            raise InvalidStateTransition(
                f"Invalid order transition {self._status.value} -> {new_status.value}"
            )
        logger.debug(
            "Order %s transitioning %s -> %s",
            self.client_order_id,
            self._status.value,
            new_status.value,
        )
        self._status = new_status

    def mark_pending(self) -> None:
        self._transition_to(OrderStatus.PENDING)

    def mark_partial_filled(self) -> None:
        self._transition_to(OrderStatus.PARTIAL_FILLED)

    def mark_filled(self) -> None:
        if self._filled_qty != self.qty:
            raise InvalidStateTransition(
                "Cannot mark FILLED unless filled_qty == qty"
            )
        self._transition_to(OrderStatus.FILLED)

    def mark_cancelled(self) -> None:
        if self._status not in {OrderStatus.PENDING, OrderStatus.PARTIAL_FILLED}:
            raise InvalidStateTransition(
                f"Cannot cancel order from {self._status.value}"
            )
        self._transition_to(OrderStatus.CANCELLED)

    def mark_failed(self) -> None:
        if self._status not in {OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.PARTIAL_FILLED}:
            raise InvalidStateTransition(
                f"Cannot fail order from {self._status.value}"
            )
        self._transition_to(OrderStatus.FAILED)

    def _recalculate_fill_totals(self) -> None:
        self._filled_qty = sum(fill.qty for fill in self.fills)
        self._remaining_qty = max(0.0, self.qty - self._filled_qty)
        self._avg_fill_price = None
        if self._filled_qty > 0.0:
            total_value = sum(fill.qty * fill.price for fill in self.fills)
            self._avg_fill_price = total_value / self._filled_qty

    def apply_fill(self, fill: FillEvent) -> None:
        if fill.qty <= 0:
            raise ValueError("Fill quantity must be positive")
        if any(existing.fill_id == fill.fill_id for existing in self.fills):
            logger.debug("Ignoring duplicate fill %s for order %s", fill.fill_id, self.client_order_id)
            return

        if self._status == OrderStatus.NEW:
            self.mark_pending()

        self.fills.append(fill)
        self._recalculate_fill_totals()

        if self._filled_qty == 0.0:
            self._transition_to(OrderStatus.PENDING)
        elif self._filled_qty < self.qty:
            self._transition_to(OrderStatus.PARTIAL_FILLED)
        else:
            self._transition_to(OrderStatus.FILLED)

    def update_from_exchange(
        self,
        status: Optional[OrderStatus] = None,
        filled_qty: Optional[float] = None,
        avg_fill_price: Optional[float] = None,
        price: Optional[float] = None,
        stop_price: Optional[float] = None,
    ) -> None:
        if price is not None:
            self.price = price
        if stop_price is not None:
            self.stop_price = stop_price
        if filled_qty is not None:
            self._filled_qty = max(0.0, min(self.qty, filled_qty))
            self._remaining_qty = max(0.0, self.qty - self._filled_qty)
        if avg_fill_price is not None:
            self._avg_fill_price = avg_fill_price
        if status is not None:
            if status != self._status:
                self._transition_to(status)
        self.repair_invariants()

    def validate_invariants(self) -> None:
        if self.qty <= 0:
            raise OrderStateError("Order qty must be positive")
        if self._filled_qty < 0.0:
            raise OrderStateError("filled_qty cannot be negative")
        if self._filled_qty > self.qty:
            raise OrderStateError("filled_qty cannot exceed qty")
        if self._remaining_qty != round(self.qty - self._filled_qty, 10):
            raise OrderStateError("remaining_qty must equal qty - filled_qty")
        if self._status == OrderStatus.FILLED and self._remaining_qty != 0.0:
            raise OrderStateError("FILLED status requires remaining_qty == 0")
        if self._status == OrderStatus.PARTIAL_FILLED and not (0.0 < self._filled_qty < self.qty):
            raise OrderStateError("PARTIAL_FILLED requires 0 < filled_qty < qty")
        if self._status == OrderStatus.PENDING and self._filled_qty != 0.0:
            raise OrderStateError("PENDING status requires filled_qty == 0")

    def repair_invariants(self) -> None:
        self._recalculate_fill_totals()

        if self._filled_qty == 0.0 and self._status == OrderStatus.PARTIAL_FILLED:
            self._status = OrderStatus.PENDING

        if self._filled_qty == self.qty and self._status != OrderStatus.FILLED:
            self._status = OrderStatus.FILLED

        if 0.0 < self._filled_qty < self.qty and self._status not in {
            OrderStatus.PARTIAL_FILLED,
            OrderStatus.FAILED,
            OrderStatus.CANCELLED,
        }:
            self._status = OrderStatus.PARTIAL_FILLED

        if self._filled_qty == 0.0 and self._status == OrderStatus.FILLED:
            self._status = OrderStatus.PENDING

        if self._remaining_qty < 0.0:
            self._remaining_qty = 0.0

        self._normalize_quantities()
        self.validate_invariants()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "order_id": self.order_id,
            "client_order_id": self.client_order_id,
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "type": self.type.value,
            "qty": self.qty,
            "price": self.price,
            "stop_price": self.stop_price,
            "reduce_only": self.reduce_only,
            "close_position": self.close_position,
            "time_in_force": self.time_in_force,
            "timestamp": self.timestamp,
            "parent_order_id": self.parent_order_id,
            "trade_role": self.trade_role,
            "metadata": self.metadata,
            "fills": [fill.to_dict() for fill in self.fills],
            "status": self._status.value,
            "filled_qty": self._filled_qty,
            "remaining_qty": self._remaining_qty,
            "avg_fill_price": self._avg_fill_price,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "OrderRecord":
        order = cls(
            order_id=data.get("order_id"),
            client_order_id=str(data["client_order_id"]),
            trade_id=str(data["trade_id"]),
            symbol=str(data["symbol"]),
            side=str(data["side"]),
            type=OrderType(data["type"]),
            qty=float(data["qty"]),
            price=data.get("price"),
            stop_price=data.get("stop_price"),
            reduce_only=bool(data.get("reduce_only", False)),
            close_position=bool(data.get("close_position", False)),
            time_in_force=data.get("time_in_force"),
            timestamp=str(data.get("timestamp", datetime.utcnow().isoformat() + "Z")),
            parent_order_id=data.get("parent_order_id"),
            trade_role=data.get("trade_role"),
            metadata=dict(data.get("metadata", {})),
            fills=[FillEvent.from_dict(item) for item in data.get("fills", [])],
        )

        order._filled_qty = float(data.get("filled_qty", order._filled_qty))
        order._remaining_qty = float(data.get("remaining_qty", order.qty - order._filled_qty))
        order._avg_fill_price = data.get("avg_fill_price")
        order._status = OrderStatus(data.get("status", order._status.value))
        order.repair_invariants()
        return order


@dataclass
class TradeRecord:
    trade_id: str
    symbol: str
    side: str
    entry_order_id: Optional[int]
    entry_qty: float
    entry_filled_qty: float = 0.0
    closed_qty: float = 0.0
    sl_order_id: Optional[int] = None
    tp_order_id: Optional[int] = None
    sl_price: Optional[float] = None
    tp_price: Optional[float] = None
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    closed_at: Optional[str] = None
    partial_taken: bool = False
    be_moved: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    _status: TradeStatus = field(default=TradeStatus.NEW, init=False, repr=False)

    def __post_init__(self) -> None:
        self._status = TradeStatus.NEW
        self._normalize_quantities()

    @property
    def status(self) -> TradeStatus:
        return self._status

    @property
    def open_qty(self) -> float:
        return max(0.0, self.entry_filled_qty - self.closed_qty)

    def _normalize_quantities(self) -> None:
        self.entry_qty = max(0.0, self.entry_qty)
        self.entry_filled_qty = max(0.0, min(self.entry_filled_qty, self.entry_qty))
        self.closed_qty = max(0.0, min(self.closed_qty, self.entry_filled_qty))

    def _transition_to(self, new_status: TradeStatus) -> None:
        if new_status == self._status:
            return
        allowed = TRADE_TRANSITIONS.get(self._status, ())
        if new_status not in allowed:
            raise InvalidStateTransition(
                f"Invalid trade transition {self._status.value} -> {new_status.value}"
            )
        logger.debug(
            "Trade %s transitioning %s -> %s",
            self.trade_id,
            self._status.value,
            new_status.value,
        )
        self._status = new_status

    def mark_entry_pending(self) -> None:
        self._transition_to(TradeStatus.ENTRY_PENDING)

    def mark_partially_filled(self, filled_qty: float) -> None:
        if filled_qty <= 0.0 or filled_qty >= self.entry_qty:
            raise InvalidStateTransition(
                "Partial entry fill must be between 0 and entry_qty"
            )
        self.entry_filled_qty = filled_qty
        self._transition_to(TradeStatus.ENTRY_PARTIALLY_FILLED)

    def mark_open(self) -> None:
        if self.entry_filled_qty <= 0.0:
            raise InvalidStateTransition("Cannot open trade with zero filled quantity")
        self._transition_to(TradeStatus.OPEN)

    def mark_exit_pending(self) -> None:
        if self.open_qty <= 0.0:
            raise InvalidStateTransition("Cannot begin exit without open quantity")
        self._transition_to(TradeStatus.EXIT_PENDING)

    def mark_partially_exited(self, closed_qty: float) -> None:
        if closed_qty <= 0.0 or closed_qty >= self.entry_filled_qty:
            raise InvalidStateTransition(
                "Partial exit must close less than open quantity"
            )
        self.closed_qty = closed_qty
        self._transition_to(TradeStatus.PARTIALLY_EXITED)

    def mark_closed(self, closed_at: Optional[str] = None) -> None:
        if self.open_qty != 0.0 and self.entry_filled_qty != 0.0:
            raise InvalidStateTransition(
                "Cannot close trade while open quantity remains"
            )
        self.closed_at = closed_at or datetime.utcnow().isoformat() + "Z"
        self._transition_to(TradeStatus.CLOSED)

    def mark_cancelled(self) -> None:
        if self._status not in {TradeStatus.NEW, TradeStatus.ENTRY_PENDING, TradeStatus.ENTRY_PARTIALLY_FILLED}:
            raise InvalidStateTransition(
                f"Cannot cancel trade from {self._status.value}"
            )
        self._transition_to(TradeStatus.CANCELLED)

    def mark_failed(self) -> None:
        if self._status == TradeStatus.CLOSED:
            raise InvalidStateTransition("Cannot fail a closed trade")
        self._transition_to(TradeStatus.FAILED)

    def record_entry_fill(self, qty: float) -> None:
        if qty <= 0.0:
            raise ValueError("Entry fill quantity must be positive")
        self.entry_filled_qty = min(self.entry_qty, self.entry_filled_qty + qty)
        if self.entry_filled_qty < self.entry_qty:
            if self._status == TradeStatus.NEW:
                self.mark_entry_pending()
            self._transition_to(TradeStatus.ENTRY_PARTIALLY_FILLED)
        else:
            self._transition_to(TradeStatus.OPEN)

    def record_exit_fill(self, qty: float) -> None:
        if qty <= 0.0:
            raise ValueError("Exit fill quantity must be positive")
        self.closed_qty = min(self.entry_filled_qty, self.closed_qty + qty)
        if self.open_qty == 0.0:
            self.mark_closed()
        else:
            self._transition_to(TradeStatus.PARTIALLY_EXITED)

    def attach_protection_orders(
        self,
        sl_order_id: Optional[int],
        tp_order_id: Optional[int],
        sl_price: Optional[float],
        tp_price: Optional[float],
    ) -> None:
        self.sl_order_id = sl_order_id
        self.tp_order_id = tp_order_id
        self.sl_price = sl_price
        self.tp_price = tp_price

    def validate_invariants(self) -> None:
        raw_entry_filled_qty = self.entry_filled_qty
        raw_closed_qty = self.closed_qty
        self._normalize_quantities()
        if self.entry_qty <= 0.0:
            raise OrderStateError("Trade entry_qty must be positive")
        if raw_entry_filled_qty < 0.0:
            raise OrderStateError("Trade entry_filled_qty cannot be negative")
        if raw_entry_filled_qty > self.entry_qty:
            raise OrderStateError("Trade entry_filled_qty cannot exceed entry_qty")
        if raw_closed_qty < 0.0:
            raise OrderStateError("Trade closed_qty cannot be negative")
        if raw_closed_qty > raw_entry_filled_qty:
            raise OrderStateError("Trade closed_qty cannot exceed entry_filled_qty")
        if self.open_qty < 0.0:
            raise OrderStateError("Trade open quantity cannot be negative")
        if self._status == TradeStatus.OPEN and self.open_qty <= 0.0:
            raise OrderStateError("OPEN trade must have open quantity")
        if self._status == TradeStatus.PARTIALLY_EXITED and self.open_qty <= 0.0:
            raise OrderStateError("PARTIALLY_EXITED requires remaining open quantity")
        if self._status == TradeStatus.CLOSED and self.open_qty != 0.0:
            raise OrderStateError("CLOSED trade must have no open quantity")
        if self._status == TradeStatus.ENTRY_PARTIALLY_FILLED and not (0.0 < self.entry_filled_qty < self.entry_qty):
            raise OrderStateError("ENTRY_PARTIALLY_FILLED requires partial entry exposure")

    def repair_invariants(self) -> None:
        self._normalize_quantities()
        if self._status in {TradeStatus.NEW, TradeStatus.ENTRY_PENDING}:
            if self.entry_filled_qty >= self.entry_qty and self.entry_qty > 0.0:
                self._status = TradeStatus.OPEN
            elif self.entry_filled_qty > 0.0:
                self._status = TradeStatus.ENTRY_PARTIALLY_FILLED
        if self._status == TradeStatus.ENTRY_PARTIALLY_FILLED and self.entry_filled_qty == 0.0:
            self._status = TradeStatus.ENTRY_PENDING
        if self._status == TradeStatus.OPEN and self.open_qty == 0.0:
            self._status = TradeStatus.CLOSED
        if self._status == TradeStatus.EXIT_PENDING and self.open_qty == 0.0:
            self._status = TradeStatus.CLOSED
        if self._status == TradeStatus.PARTIALLY_EXITED and self.open_qty == 0.0:
            self._status = TradeStatus.CLOSED
        if self._status == TradeStatus.CLOSED and self.open_qty > 0.0:
            self._status = TradeStatus.OPEN
        self.validate_invariants()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": self.trade_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry_order_id": self.entry_order_id,
            "entry_qty": self.entry_qty,
            "entry_filled_qty": self.entry_filled_qty,
            "closed_qty": self.closed_qty,
            "sl_order_id": self.sl_order_id,
            "tp_order_id": self.tp_order_id,
            "sl_price": self.sl_price,
            "tp_price": self.tp_price,
            "created_at": self.created_at,
            "closed_at": self.closed_at,
            "partial_taken": self.partial_taken,
            "be_moved": self.be_moved,
            "metadata": self.metadata,
            "status": self._status.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TradeRecord":
        trade = cls(
            trade_id=str(data["trade_id"]),
            symbol=str(data["symbol"]),
            side=str(data["side"]),
            entry_order_id=data.get("entry_order_id"),
            entry_qty=float(data["entry_qty"]),
            entry_filled_qty=float(data.get("entry_filled_qty", 0.0)),
            closed_qty=float(data.get("closed_qty", 0.0)),
            sl_order_id=data.get("sl_order_id"),
            tp_order_id=data.get("tp_order_id"),
            sl_price=data.get("sl_price"),
            tp_price=data.get("tp_price"),
            created_at=str(data.get("created_at", datetime.utcnow().isoformat() + "Z")),
            closed_at=data.get("closed_at"),
            partial_taken=bool(data.get("partial_taken", False)),
            be_moved=bool(data.get("be_moved", False)),
            metadata=dict(data.get("metadata", {})),
        )
        trade._status = TradeStatus(data.get("status", trade._status.value))
        trade.repair_invariants()
        return trade


class OrderStateStorage(abc.ABC):
    def __init__(self, path: str) -> None:
        self.path = path
        self.tmp_path = f"{path}.tmp"
        self.backup_path = f"{path}.bak"

    @abc.abstractmethod
    def load(self) -> Dict[str, Any]:
        raise NotImplementedError

    @abc.abstractmethod
    def save(self, state: Dict[str, Any]) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def backup(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def cleanup_temp(self) -> None:
        raise NotImplementedError


class JsonOrderStateStorage(OrderStateStorage):
    def load(self) -> Dict[str, Any]:
        if os.path.exists(self.tmp_path):
            logger.warning("Found stale state temp file %s, attempting recovery", self.tmp_path)
            try:
                temp_state = self._load_json(self.tmp_path)
                self._atomic_rename(self.tmp_path, self.path)
                logger.info("Recovered state from temp file %s", self.tmp_path)
            except Exception as exc:  # pylint: disable=broad-except
                logger.error("Failed to recover temp state file: %s", exc)
                try:
                    os.remove(self.tmp_path)
                except OSError:
                    pass

        if not os.path.exists(self.path):
            logger.info("No persisted order state found at %s", self.path)
            return self._empty_state()

        try:
            return self._load_json(self.path)
        except Exception as exc:
            logger.error("Failed to load order state from %s: %s", self.path, exc)
            if os.path.exists(self.backup_path):
                logger.warning("Attempting recovery from backup %s", self.backup_path)
                return self._load_json(self.backup_path)
            raise

    def save(self, state: Dict[str, Any]) -> None:
        directory = os.path.dirname(self.path) or "."
        if os.path.exists(self.path):
            self.backup()

        try:
            with open(self.tmp_path, "w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            self._atomic_rename(self.tmp_path, self.path)
            self._fsync_directory(directory)
            logger.debug("Atomically wrote order state to %s", self.path)
        except Exception:
            if os.path.exists(self.tmp_path):
                try:
                    os.remove(self.tmp_path)
                except OSError:
                    pass
            raise

    def backup(self) -> None:
        try:
            shutil.copy2(self.path, self.backup_path)
            logger.debug("Created order state backup %s", self.backup_path)
        except Exception as exc:
            logger.warning("Unable to create order state backup: %s", exc)

    def cleanup_temp(self) -> None:
        if os.path.exists(self.tmp_path):
            try:
                os.remove(self.tmp_path)
                logger.info("Removed stale temp order state %s", self.tmp_path)
            except OSError as exc:
                logger.warning("Unable to remove stale temp state: %s", exc)

    def _load_json(self, filepath: str) -> Dict[str, Any]:
        with open(filepath, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def _atomic_rename(self, source: str, destination: str) -> None:
        os.replace(source, destination)

    def _fsync_directory(self, directory: str) -> None:
        if not hasattr(os, "O_DIRECTORY"):
            logger.debug("Skipping directory fsync on unsupported platform for %s", directory)
            return
        try:
            dir_fd = os.open(directory, os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError as exc:
            logger.debug("Directory fsync failed for %s: %s", directory, exc)

    def _empty_state(self) -> Dict[str, Any]:
        return {
            "version": CURRENT_SCHEMA_VERSION,
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "next_client_order_id": 1,
            "orders": {},
            "trades": {},
            "indexes": {
                "symbol_trade": {},
            },
        }


class OrderStateManager:
    def __init__(self, storage: OrderStateStorage) -> None:
        self.storage = storage
        self.orders_by_client_id: Dict[str, OrderRecord] = {}
        self.orders_by_id: Dict[int, OrderRecord] = {}
        self.trades_by_id: Dict[str, TradeRecord] = {}
        self.symbol_to_trade: Dict[str, str] = {}
        self.state: Dict[str, Any] = self.storage._empty_state()
        self.load_state()

    def load_state(self) -> None:
        raw_state = self.storage.load()
        version = raw_state.get("version", 0)
        if version != CURRENT_SCHEMA_VERSION:
            raise SchemaMigrationRequired(
                f"Order state schema version {version} is incompatible with current {CURRENT_SCHEMA_VERSION}"
            )
        self.state = raw_state
        self._load_collections(raw_state)
        self.repair_invariants()
        logger.info("Loaded order state version %s with %d orders and %d trades",
                    version, len(self.orders_by_client_id), len(self.trades_by_id))

    def save_state(self) -> None:
        self.state = self._serialize_state()
        self.state["version"] = CURRENT_SCHEMA_VERSION
        self.state["last_updated"] = datetime.utcnow().isoformat() + "Z"
        self.storage.save(self.state)

    def _load_collections(self, raw_state: Dict[str, Any]) -> None:
        self.orders_by_client_id.clear()
        self.orders_by_id.clear()
        self.trades_by_id.clear()
        self.symbol_to_trade.clear()

        for client_id, order_payload in raw_state.get("orders", {}).items():
            order = OrderRecord.from_dict(order_payload)
            self.orders_by_client_id[client_id] = order
            if order.order_id is not None:
                self.orders_by_id[order.order_id] = order

        for trade_id, trade_payload in raw_state.get("trades", {}).items():
            trade = TradeRecord.from_dict(trade_payload)
            self.trades_by_id[trade_id] = trade

        self.symbol_to_trade = dict(raw_state.get("indexes", {}).get("symbol_trade", {}))

    def _serialize_state(self) -> Dict[str, Any]:
        return {
            "version": CURRENT_SCHEMA_VERSION,
            "last_updated": datetime.utcnow().isoformat() + "Z",
            "next_client_order_id": int(self.state.get("next_client_order_id", 1)),
            "orders": {
                client_id: order.to_dict()
                for client_id, order in self.orders_by_client_id.items()
            },
            "trades": {
                trade_id: trade.to_dict()
                for trade_id, trade in self.trades_by_id.items()
            },
            "indexes": {
                "symbol_trade": dict(self.symbol_to_trade),
            },
        }

    def allocate_client_order_id(self) -> str:
        next_id = int(self.state.get("next_client_order_id", 1))
        client_id = f"order-{next_id:06d}"
        self.state["next_client_order_id"] = next_id + 1
        return client_id

    def create_trade(
        self,
        symbol: str,
        side: str,
        entry_qty: float,
        entry_order_id: Optional[int] = None,
        sl_price: Optional[float] = None,
        tp_price: Optional[float] = None,
    ) -> TradeRecord:
        trade_id = f"trade-{uuid.uuid4().hex}"
        trade = TradeRecord(
            trade_id=trade_id,
            symbol=symbol,
            side=side,
            entry_order_id=entry_order_id,
            entry_qty=entry_qty,
            sl_price=sl_price,
            tp_price=tp_price,
        )
        self.trades_by_id[trade_id] = trade
        self.symbol_to_trade[symbol] = trade_id
        return trade

    def record_new_order(self, order: OrderRecord) -> None:
        if order.client_order_id in self.orders_by_client_id:
            raise OrderStateError(f"Order client id already exists: {order.client_order_id}")

        if order.status == OrderStatus.NEW:
            order.mark_pending()

        self.orders_by_client_id[order.client_order_id] = order
        if order.order_id is not None:
            self.orders_by_id[order.order_id] = order

    def update_order(self, order_id: int, **fields: Any) -> OrderRecord:
        order = self._get_order_by_id(order_id)
        if order is None:
            raise OrderStateError(f"Order not found for id {order_id}")

        if "status" in fields:
            status_value = fields.pop("status")
            if isinstance(status_value, str):
                status_value = OrderStatus(status_value)
            if status_value == OrderStatus.PENDING:
                order.mark_pending()
            elif status_value == OrderStatus.PARTIAL_FILLED:
                order.mark_partial_filled()
            elif status_value == OrderStatus.FILLED:
                order.mark_filled()
            elif status_value == OrderStatus.CANCELLED:
                order.mark_cancelled()
            elif status_value == OrderStatus.FAILED:
                order.mark_failed()

        for key, value in fields.items():
            if key in {"_status", "_filled_qty", "_remaining_qty", "_avg_fill_price"}:
                raise OrderStateError(f"Cannot update private order field {key}")
            if not hasattr(order, key):
                raise OrderStateError(f"Unknown order field {key}")
            setattr(order, key, value)

        order.repair_invariants()
        if order.order_id is not None:
            self.orders_by_id[order.order_id] = order
        return order

    def record_fill(self, order_id: int, fill_event: FillEvent) -> OrderRecord:
        order = self._get_order_by_id(order_id)
        if order is None:
            raise OrderStateError(f"Order not found for id {order_id}")
        order.apply_fill(fill_event)
        if order.order_id is not None:
            self.orders_by_id[order.order_id] = order
        return order

    def get_trade(self, trade_id: str) -> Optional[TradeRecord]:
        return self.trades_by_id.get(trade_id)

    def get_trade_by_symbol(self, symbol: str) -> Optional[TradeRecord]:
        trade_id = self.symbol_to_trade.get(symbol)
        if trade_id is None:
            return None
        return self.trades_by_id.get(trade_id)

    def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderRecord]:
        open_statuses = {
            OrderStatus.NEW,
            OrderStatus.PENDING,
            OrderStatus.PARTIAL_FILLED,
        }
        orders = [
            order
            for order in self.orders_by_client_id.values()
            if order.status in open_statuses
        ]
        if symbol is not None:
            orders = [order for order in orders if order.symbol == symbol]
        return orders

    def mark_trade_closed(self, trade_id: str) -> TradeRecord:
        trade = self.get_trade(trade_id)
        if trade is None:
            raise OrderStateError(f"Trade not found: {trade_id}")
        trade.closed_at = datetime.utcnow().isoformat() + "Z"
        if trade.open_qty != 0.0:
            raise InvalidStateTransition(
                "Cannot close trade while open quantity remains"
            )
        trade.mark_closed(trade.closed_at)
        for order in self.orders_by_client_id.values():
            if order.trade_id != trade_id:
                continue
            if order.status in {OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.PARTIAL_FILLED}:
                order.mark_cancelled()
        return trade

    def _get_order_by_id(self, order_id: int) -> Optional[OrderRecord]:
        return self.orders_by_id.get(order_id)

    def repair_invariants(self) -> None:
        for order in self.orders_by_client_id.values():
            try:
                order.repair_invariants()
            except Exception as exc:
                logger.warning("Order invariant repair failed for %s: %s", order.client_order_id, exc)
        for trade in self.trades_by_id.values():
            try:
                trade.repair_invariants()
            except Exception as exc:
                logger.warning("Trade invariant repair failed for %s: %s", trade.trade_id, exc)

    def validate_invariants(self) -> None:
        for order in self.orders_by_client_id.values():
            order.validate_invariants()
        for trade in self.trades_by_id.values():
            trade.validate_invariants()

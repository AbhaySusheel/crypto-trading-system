import json
import os
from pathlib import Path

import pytest

from execution_engine.order_state import (
    CURRENT_SCHEMA_VERSION,
    FillEvent,
    InvalidStateTransition,
    JsonOrderStateStorage,
    OrderRecord,
    OrderStateManager,
    OrderStateStorage,
    OrderStatus,
    OrderType,
    OrderStateError,
    SchemaMigrationRequired,
    TradeRecord,
    TradeStatus,
)


def make_order(client_order_id: str, trade_id: str) -> OrderRecord:
    return OrderRecord(
        order_id=None,
        client_order_id=client_order_id,
        trade_id=trade_id,
        symbol="BTCUSDT",
        side="LONG",
        type=OrderType.ENTRY,
        qty=0.01,
    )


def make_trade() -> TradeRecord:
    return TradeRecord(
        trade_id="trade-1",
        symbol="BTCUSDT",
        side="LONG",
        entry_order_id=None,
        entry_qty=0.01,
    )


def test_order_record_valid_transitions():
    order = make_order("order-000001", "trade-1")
    assert order.status == OrderStatus.NEW

    order.mark_pending()
    assert order.status == OrderStatus.PENDING

    order.apply_fill(FillEvent(fill_id="fill-1", order_id=None, qty=0.005, price=50000.0))
    assert order.status == OrderStatus.PARTIAL_FILLED
    assert order.filled_qty == pytest.approx(0.005)
    assert order.remaining_qty == pytest.approx(0.005)
    assert order.avg_fill_price == pytest.approx(50000.0)

    order.apply_fill(FillEvent(fill_id="fill-2", order_id=None, qty=0.005, price=50010.0))
    assert order.status == OrderStatus.FILLED
    assert order.filled_qty == pytest.approx(0.01)
    assert order.remaining_qty == pytest.approx(0.0)
    assert order.avg_fill_price == pytest.approx((0.005 * 50000.0 + 0.005 * 50010.0) / 0.01)


def test_order_record_invalid_transitions():
    order = make_order("order-000002", "trade-1")

    with pytest.raises(InvalidStateTransition):
        order.mark_cancelled()

    with pytest.raises(InvalidStateTransition):
        order.mark_cancelled()

    order.mark_pending()
    with pytest.raises(InvalidStateTransition):
        order.mark_filled()

    order.apply_fill(FillEvent(fill_id="fill-3", order_id=None, qty=0.01, price=50000.0))
    assert order.status == OrderStatus.FILLED

    with pytest.raises(InvalidStateTransition):
        order.mark_cancelled()

    with pytest.raises(InvalidStateTransition):
        order.mark_failed()


@pytest.mark.parametrize(
    "initial,method,args",
    [
        (TradeStatus.NEW, "mark_partially_filled", (0.001,)),
        (TradeStatus.NEW, "mark_open", ()),
        (TradeStatus.NEW, "mark_exit_pending", ()),
        (TradeStatus.NEW, "mark_partially_exited", (0.001,)),
        (TradeStatus.NEW, "mark_closed", ()),
        (TradeStatus.ENTRY_PENDING, "mark_partially_exited", (0.001,)),
        (TradeStatus.OPEN, "mark_entry_pending", ()),
        (TradeStatus.CLOSED, "mark_open", ()),
        (TradeStatus.CANCELLED, "mark_open", ()),
    ],
)
def test_trade_record_invalid_transitions(initial, method, args):
    trade = make_trade()
    trade._status = initial
    with pytest.raises((InvalidStateTransition, ValueError)):
        getattr(trade, method)(*args)


def test_trade_record_valid_transitions():
    trade = make_trade()
    trade.mark_entry_pending()
    assert trade.status == TradeStatus.ENTRY_PENDING

    trade.mark_partially_filled(0.005)
    assert trade.status == TradeStatus.ENTRY_PARTIALLY_FILLED
    assert trade.entry_filled_qty == pytest.approx(0.005)

    trade.mark_open()
    assert trade.status == TradeStatus.OPEN

    trade.mark_exit_pending()
    assert trade.status == TradeStatus.EXIT_PENDING

    trade.mark_partially_exited(0.003)
    assert trade.status == TradeStatus.PARTIALLY_EXITED
    assert trade.closed_qty == pytest.approx(0.003)

    with pytest.raises(InvalidStateTransition):
        trade.mark_closed()

    trade.closed_qty = trade.entry_filled_qty
    trade._status = TradeStatus.PARTIALLY_EXITED
    trade.mark_closed()
    assert trade.status == TradeStatus.CLOSED


def test_partial_fill_behavior_for_order_and_trade():
    order = make_order("order-000003", "trade-1")
    order.apply_fill(FillEvent(fill_id="fill-4", order_id=None, qty=0.004, price=50000.0))
    assert order.status == OrderStatus.PARTIAL_FILLED

    trade = make_trade()
    trade.mark_entry_pending()
    trade.record_entry_fill(0.004)
    assert trade.status == TradeStatus.ENTRY_PARTIALLY_FILLED
    assert trade.entry_filled_qty == pytest.approx(0.004)

    trade.mark_open()
    trade.record_exit_fill(0.002)
    assert trade.status == TradeStatus.PARTIALLY_EXITED
    assert trade.open_qty == pytest.approx(0.002)

    trade.record_exit_fill(0.002)
    assert trade.status == TradeStatus.CLOSED
    assert trade.open_qty == pytest.approx(0.0)


def test_invariant_normalization_on_load():
    payload = {
        "version": CURRENT_SCHEMA_VERSION,
        "last_updated": "2026-05-25T00:00:00Z",
        "next_client_order_id": 1,
        "orders": {
            "order-000001": {
                "order_id": 1,
                "client_order_id": "order-000001",
                "trade_id": "trade-1",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "type": "ENTRY",
                "qty": 0.01,
                "status": "FILLED",
                "filled_qty": 0.004,
                "remaining_qty": 0.006,
                "avg_fill_price": 50000.0,
                "fills": [
                    {
                        "fill_id": "fill-1",
                        "order_id": 1,
                        "qty": 0.004,
                        "price": 50000.0,
                        "commission": 0.0,
                        "commission_asset": "USDT",
                        "timestamp": "2026-05-25T00:00:00Z",
                    }
                ],
            }
        },
        "trades": {
            "trade-1": {
                "trade_id": "trade-1",
                "symbol": "BTCUSDT",
                "side": "LONG",
                "entry_order_id": 1,
                "entry_qty": 0.01,
                "entry_filled_qty": 0.004,
                "closed_qty": 0.0,
                "status": "NEW",
            }
        },
        "indexes": {"symbol_trade": {"BTCUSDT": "trade-1"}},
    }
    storage_path = Path("tmp_order_state.json")
    try:
        storage_path.write_text(json.dumps(payload))
        storage = JsonOrderStateStorage(str(storage_path))
        manager = OrderStateManager(storage)
        order = next(iter(manager.orders_by_client_id.values()))
        trade = manager.get_trade("trade-1")
        assert order.status == OrderStatus.PARTIAL_FILLED
        assert trade.status == TradeStatus.ENTRY_PARTIALLY_FILLED
    finally:
        storage_path.unlink(missing_ok=True)


def test_invariant_violation_detection():
    order = make_order("order-000004", "trade-1")
    order._filled_qty = 0.02
    order._remaining_qty = -0.01
    with pytest.raises(OrderStateError):
        order.validate_invariants()

    trade = make_trade()
    trade.entry_filled_qty = 0.02
    with pytest.raises(OrderStateError):
        trade.validate_invariants()


def test_zero_qty_protection_order_loads():
    payload = {
        "order_id": 0,
        "client_order_id": "order-000002",
        "trade_id": "trade-1",
        "symbol": "SOLUSDT",
        "side": "BUY",
        "type": "STOP_LOSS",
        "qty": 0.0,
        "price": None,
        "stop_price": 82.4656,
        "reduce_only": False,
        "close_position": True,
        "time_in_force": None,
        "timestamp": "2026-05-25T00:00:00Z",
        "parent_order_id": None,
        "trade_role": "stop_loss",
        "metadata": {},
        "fills": [],
        "status": "PENDING",
        "filled_qty": 0.0,
        "remaining_qty": 0.0,
        "avg_fill_price": None,
    }

    restored = OrderRecord.from_dict(payload)

    assert restored.type == OrderType.STOP_LOSS
    assert restored.qty == pytest.approx(0.0)
    assert restored.status == OrderStatus.PENDING


def test_serialization_roundtrip():
    order = make_order("order-000005", "trade-1")
    order.mark_pending()
    order.apply_fill(FillEvent(fill_id="fill-5", order_id=None, qty=0.01, price=50000.0))
    data = order.to_dict()
    restored = OrderRecord.from_dict(data)
    assert restored.status == order.status
    assert restored.filled_qty == pytest.approx(order.filled_qty)
    assert restored.remaining_qty == pytest.approx(order.remaining_qty)
    assert restored.avg_fill_price == pytest.approx(order.avg_fill_price)

    trade = make_trade()
    trade.mark_entry_pending()
    trade.mark_partially_filled(0.005)
    trade.mark_open()
    data = trade.to_dict()
    restored_trade = TradeRecord.from_dict(data)
    assert restored_trade.status == trade.status
    assert restored_trade.entry_filled_qty == pytest.approx(trade.entry_filled_qty)


def test_atomic_persistence_and_save_load(tmp_path):
    path = tmp_path / "order_state.json"
    storage = JsonOrderStateStorage(str(path))
    manager = OrderStateManager(storage)
    trade = manager.create_trade("BTCUSDT", "LONG", 0.01)
    order = make_order("order-000006", trade.trade_id)
    manager.record_new_order(order)
    manager.save_state()

    assert path.exists()
    assert not path.with_suffix(path.suffix + ".tmp").exists()

    manager2 = OrderStateManager(storage)
    assert len(manager2.orders_by_client_id) == 1
    assert len(manager2.trades_by_id) == 1


def test_tmp_file_recovery(tmp_path):
    path = tmp_path / "order_state.json"
    temp_path = tmp_path / "order_state.json.tmp"
    valid_state = {
        "version": CURRENT_SCHEMA_VERSION,
        "last_updated": "2026-05-25T00:00:00Z",
        "next_client_order_id": 1,
        "orders": {},
        "trades": {},
        "indexes": {"symbol_trade": {}},
    }
    temp_path.write_text(json.dumps(valid_state))
    storage = JsonOrderStateStorage(str(path))
    loaded_state = storage.load()
    assert path.exists()
    assert not temp_path.exists()
    assert loaded_state["version"] == CURRENT_SCHEMA_VERSION


def test_backup_restoration(tmp_path):
    path = tmp_path / "order_state.json"
    backup_path = tmp_path / "order_state.json.bak"
    path.write_text("not valid json")
    backup_payload = {
        "version": CURRENT_SCHEMA_VERSION,
        "last_updated": "2026-05-25T00:00:00Z",
        "next_client_order_id": 1,
        "orders": {},
        "trades": {},
        "indexes": {"symbol_trade": {}},
    }
    backup_path.write_text(json.dumps(backup_payload))
    storage = JsonOrderStateStorage(str(path))
    loaded_state = storage.load()
    assert loaded_state["version"] == CURRENT_SCHEMA_VERSION


def test_corrupted_json_handling(tmp_path):
    path = tmp_path / "order_state.json"
    path.write_text("not valid json")
    storage = JsonOrderStateStorage(str(path))
    with pytest.raises(Exception):
        storage.load()


def test_duplicate_client_order_id_prevention():
    path = Path("tmp_duplicate_ids.json")
    try:
        storage = JsonOrderStateStorage(str(path))
        manager = OrderStateManager(storage)
        order = make_order("order-000007", "trade-1")
        manager.record_new_order(order)
        duplicate = make_order("order-000007", "trade-1")
        with pytest.raises(OrderStateError):
            manager.record_new_order(duplicate)
    finally:
        path.unlink(missing_ok=True)


def test_concurrent_save_load_safety_assumptions(tmp_path):
    path = tmp_path / "order_state.json"
    storage = JsonOrderStateStorage(str(path))
    manager = OrderStateManager(storage)
    trade = manager.create_trade("BTCUSDT", "LONG", 0.01)
    order = make_order("order-000008", trade.trade_id)
    manager.record_new_order(order)
    manager.save_state()

    # Simulate an interrupted save: create a valid temp file while main exists.
    backup_state = manager._serialize_state()
    temp_path = tmp_path / "order_state.json.tmp"
    temp_path.write_text(json.dumps(backup_state))
    loaded_state = storage.load()
    assert loaded_state["version"] == CURRENT_SCHEMA_VERSION
    assert path.exists()
    assert not temp_path.exists()

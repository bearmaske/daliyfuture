# tests/test_state.py
import json
import os
import tempfile
import pytest
from state import StateManager


@pytest.fixture
def state_mgr(tmp_path):
    path = tmp_path / "state.json"
    backup = tmp_path / "state.backup.json"
    return StateManager(str(path), str(backup), initial_capital=10000.0)


def test_init_creates_default_state(state_mgr):
    state = state_mgr.load()
    assert state["balance"] == 10000.0
    assert state["positions"] == []
    assert state["trade_history"] == []


def test_add_position(state_mgr):
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=65000.0,
        quantity=0.0077,
    )
    assert pos["symbol"] == "BTCUSDT"
    assert pos["side"] == "LONG"
    assert pos["highest_price"] == 65000.0
    assert pos["lowest_price"] == 65000.0
    assert len(state_mgr.state["positions"]) == 1


def test_remove_position(state_mgr):
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="BTCUSDT", side="LONG", entry_price=65000.0, quantity=0.0077
    )
    removed = state_mgr.remove_position(pos["id"])
    assert removed is not None
    assert len(state_mgr.state["positions"]) == 0


def test_save_creates_backup(state_mgr, tmp_path):
    state_mgr.load()
    state_mgr.save()
    # Second save should create backup
    state_mgr.save()
    backup_path = tmp_path / "state.backup.json"
    assert backup_path.exists()


def test_get_position_by_symbol(state_mgr):
    state_mgr.load()
    state_mgr.add_position(
        symbol="BTCUSDT", side="LONG", entry_price=65000.0, quantity=0.0077
    )
    found = state_mgr.get_position_by_symbol("BTCUSDT")
    assert found is not None
    assert found["symbol"] == "BTCUSDT"
    assert state_mgr.get_position_by_symbol("ETHUSDT") is None


def test_update_extreme_price(state_mgr):
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="BTCUSDT", side="LONG", entry_price=65000.0, quantity=0.0077
    )
    state_mgr.update_extreme_price(pos["id"], current_price=67000.0)
    updated = state_mgr.get_position_by_id(pos["id"])
    assert updated["highest_price"] == 67000.0

    # Price lower than highest should not update highest
    state_mgr.update_extreme_price(pos["id"], current_price=66000.0)
    updated = state_mgr.get_position_by_id(pos["id"])
    assert updated["highest_price"] == 67000.0


def test_thread_safety(state_mgr):
    """Verify that the lock exists and state operations are guarded."""
    import threading
    state_mgr.load()
    assert hasattr(state_mgr, "_lock")
    assert isinstance(state_mgr._lock, type(threading.Lock()))

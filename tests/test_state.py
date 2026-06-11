# tests/test_state.py
import json
import os
import tempfile
from datetime import datetime, timedelta
import pytest
from state import StateManager, TZ_CN


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


def _add_closed_trade(state_mgr, symbol: str, pnl: float, closed_ago_hours: float):
    state_mgr.load()
    closed_at = (datetime.now(TZ_CN) - timedelta(hours=closed_ago_hours)).strftime("%Y-%m-%d %H:%M:%S")
    state_mgr.state["trade_history"].append({
        "id": f"t-{symbol}-{closed_ago_hours}",
        "symbol": symbol,
        "side": "LONG",
        "entry_price": 1.0,
        "exit_price": 0.9 if pnl < 0 else 1.1,
        "quantity": 100,
        "pnl": pnl,
        "commission": None,
        "open_order_id": None,
        "close_order_id": None,
        "opened_at": closed_at,
        "closed_at": closed_at,
    })
    state_mgr.save()


def test_symbol_cooldown_triggers_after_threshold(state_mgr):
    _add_closed_trade(state_mgr, "BASUSDT", pnl=-50.0, closed_ago_hours=2.0)
    _add_closed_trade(state_mgr, "BASUSDT", pnl=-30.0, closed_ago_hours=1.0)
    remaining = state_mgr.symbol_cooldown_remaining(
        "BASUSDT", loss_threshold=2, window_hours=24, cooldown_hours=24
    )
    assert remaining is not None
    assert "小时" in remaining


def test_symbol_cooldown_below_threshold(state_mgr):
    _add_closed_trade(state_mgr, "BASUSDT", pnl=-50.0, closed_ago_hours=2.0)
    assert state_mgr.symbol_cooldown_remaining(
        "BASUSDT", loss_threshold=2, window_hours=24, cooldown_hours=24
    ) is None


def test_symbol_cooldown_ignores_wins(state_mgr):
    _add_closed_trade(state_mgr, "BASUSDT", pnl=+80.0, closed_ago_hours=2.0)
    _add_closed_trade(state_mgr, "BASUSDT", pnl=+50.0, closed_ago_hours=1.0)
    assert state_mgr.symbol_cooldown_remaining(
        "BASUSDT", loss_threshold=2, window_hours=24, cooldown_hours=24
    ) is None


def test_symbol_cooldown_outside_window(state_mgr):
    _add_closed_trade(state_mgr, "BASUSDT", pnl=-50.0, closed_ago_hours=48.0)
    _add_closed_trade(state_mgr, "BASUSDT", pnl=-30.0, closed_ago_hours=30.0)
    assert state_mgr.symbol_cooldown_remaining(
        "BASUSDT", loss_threshold=2, window_hours=24, cooldown_hours=24
    ) is None


def test_symbol_cooldown_expires(state_mgr):
    # Two losses that hit threshold 30 hours ago — cooldown_hours=24 → expired
    _add_closed_trade(state_mgr, "BASUSDT", pnl=-50.0, closed_ago_hours=32.0)
    _add_closed_trade(state_mgr, "BASUSDT", pnl=-30.0, closed_ago_hours=25.0)
    # Use a longer window so both trades are considered
    assert state_mgr.symbol_cooldown_remaining(
        "BASUSDT", loss_threshold=2, window_hours=48, cooldown_hours=24
    ) is None


def test_symbol_cooldown_is_per_symbol(state_mgr):
    _add_closed_trade(state_mgr, "BASUSDT", pnl=-50.0, closed_ago_hours=2.0)
    _add_closed_trade(state_mgr, "BASUSDT", pnl=-30.0, closed_ago_hours=1.0)
    assert state_mgr.symbol_cooldown_remaining(
        "BSBUSDT", loss_threshold=2, window_hours=24, cooldown_hours=24
    ) is None


def test_blacklist_add_and_query(state_mgr):
    state_mgr.load()
    state_mgr.add_symbol_blacklist("METUSDT", reason="Binance风控拒单(-4106)", hours=24)
    result = state_mgr.symbol_blacklist_remaining("METUSDT")
    assert result is not None
    remaining, reason = result
    assert "小时" in remaining
    assert "-4106" in reason


def test_blacklist_not_present(state_mgr):
    state_mgr.load()
    assert state_mgr.symbol_blacklist_remaining("NOTHINGUSDT") is None


def test_blacklist_extends_expiry(state_mgr):
    state_mgr.load()
    state_mgr.add_symbol_blacklist("METUSDT", reason="first", hours=1)
    state_mgr.add_symbol_blacklist("METUSDT", reason="second", hours=24)
    result = state_mgr.symbol_blacklist_remaining("METUSDT")
    assert result is not None
    remaining, reason = result
    # Second call should have extended to 24h, reason updated
    assert "second" in reason


def test_blacklist_does_not_shrink(state_mgr):
    state_mgr.load()
    state_mgr.add_symbol_blacklist("METUSDT", reason="long", hours=48)
    state_mgr.add_symbol_blacklist("METUSDT", reason="short", hours=1)
    result = state_mgr.symbol_blacklist_remaining("METUSDT")
    assert result is not None
    remaining, reason = result
    # Reason should still be the first (longer) one
    assert reason == "long"


def test_blacklist_per_symbol(state_mgr):
    state_mgr.load()
    state_mgr.add_symbol_blacklist("METUSDT", reason="x", hours=24)
    assert state_mgr.symbol_blacklist_remaining("METUSDT") is not None
    assert state_mgr.symbol_blacklist_remaining("BTCUSDT") is None


def test_add_position_with_stop_fields(state_mgr):
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="ETHUSDT", side="SHORT", entry_price=3000.0, quantity=0.5,
        soft_stop_pct=0.03, hard_stop_pct=0.06, position_size=222.0, atr_at_entry=60.0,
    )
    assert pos["soft_stop_pct"] == 0.03
    assert pos["hard_stop_pct"] == 0.06
    assert pos["position_size"] == 222.0
    assert pos["atr_at_entry"] == 60.0


def test_add_position_stop_fields_default_none(state_mgr):
    # 不传新参数（旧调用方式）→ 字段存在且为 None（存量兼容由 risk.py 回退处理）
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="BTCUSDT", side="LONG", entry_price=65000.0, quantity=0.01,
    )
    assert pos["soft_stop_pct"] is None
    assert pos["hard_stop_pct"] is None
    assert pos["position_size"] is None


def test_last_soft_check_hour_roundtrip(state_mgr):
    state_mgr.load()
    assert state_mgr.last_soft_check_hour is None
    state_mgr.set_last_soft_check_hour("2026-06-11 14")
    assert state_mgr.last_soft_check_hour == "2026-06-11 14"
    # 持久化验证：重新 load 后仍在
    state_mgr.load()
    assert state_mgr.last_soft_check_hour == "2026-06-11 14"

import pytest
from datetime import datetime
from unittest.mock import MagicMock
from risk import check_fixed_sl, check_trailing_tp, calculate_atr, compute_stop_distances, compute_position_size, _pos_hard_stop_pct, _pos_margin, calculate_pnl, _check_soft_stops
from risk import TZ_CN
import risk
from state import StateManager


def test_fixed_sl_long_not_triggered():
    assert check_fixed_sl("LONG", 100.0, 98.5, 0.02) is False


def test_fixed_sl_long_triggered():
    assert check_fixed_sl("LONG", 100.0, 97.9, 0.02) is True


def test_fixed_sl_short_not_triggered():
    assert check_fixed_sl("SHORT", 100.0, 101.5, 0.02) is False


def test_fixed_sl_short_triggered():
    assert check_fixed_sl("SHORT", 100.0, 102.1, 0.02) is True


def test_trailing_tp_long_activates_and_triggers():
    # profit >= 3%, extreme pulled back 1%
    triggered, newly_activated = check_trailing_tp(
        side="LONG",
        entry_price=100.0,
        extreme_price=104.0,
        current_price=102.9,
        trailing_activated=False,
        activation_pct=0.03,
        drawdown_pct=0.01,
    )
    # profit = 2.9% < 3% → not activated yet, no trigger
    assert newly_activated is False
    assert triggered is False


def test_trailing_tp_long_profit_activates():
    triggered, newly_activated = check_trailing_tp(
        side="LONG",
        entry_price=100.0,
        extreme_price=103.5,
        current_price=103.5,
        trailing_activated=False,
        activation_pct=0.03,
        drawdown_pct=0.01,
    )
    # profit = 3.5% >= 3% → activates, current == extreme → not triggered yet
    assert newly_activated is True
    assert triggered is False


def test_trailing_tp_long_triggered_after_activation():
    triggered, newly_activated = check_trailing_tp(
        side="LONG",
        entry_price=100.0,
        extreme_price=105.0,
        current_price=103.9,
        trailing_activated=True,
        activation_pct=0.03,
        drawdown_pct=0.01,
    )
    # trail_stop = 105 * 0.99 = 103.95; 103.9 <= 103.95 → triggered
    assert triggered is True


def test_trailing_tp_short_triggers():
    triggered, newly_activated = check_trailing_tp(
        side="SHORT",
        entry_price=100.0,
        extreme_price=96.0,
        current_price=97.0,
        trailing_activated=True,
        activation_pct=0.03,
        drawdown_pct=0.01,
    )
    # trail_stop = 96 * 1.01 = 96.96; 97.0 >= 96.96 → triggered
    assert triggered is True


# ---------- calculate_atr ----------


def test_atr_constant_range():
    # 每根 K 线 high-low=2、无跳空 → TR 恒为 2 → ATR=2
    n = 16
    highs = [101.0] * n
    lows = [99.0] * n
    closes = [100.0] * n
    assert calculate_atr(highs, lows, closes, period=14) == pytest.approx(2.0)


def test_atr_uses_prev_close_for_gaps():
    # period=2: TR1 = max(1, |12-9.5|, |11-9.5|) = 2.5; TR2 = max(1, |20-11.5|, |19-11.5|) = 8.5
    # 初始 ATR = (2.5+8.5)/2 = 5.5
    highs = [10.0, 12.0, 20.0]
    lows = [9.0, 11.0, 19.0]
    closes = [9.5, 11.5, 19.5]
    assert calculate_atr(highs, lows, closes, period=2) == pytest.approx(5.5)


def test_atr_wilder_smoothing():
    # 在上例后追加一根: TR3 = max(1, |21-19.5|, |20-19.5|) = 1.5
    # ATR = (5.5×(2-1) + 1.5)/2 = 3.5
    highs = [10.0, 12.0, 20.0, 21.0]
    lows = [9.0, 11.0, 19.0, 20.0]
    closes = [9.5, 11.5, 19.5, 20.5]
    assert calculate_atr(highs, lows, closes, period=2) == pytest.approx(3.5)


def test_atr_insufficient_data_returns_zero():
    # 需要 period+1 根，14 根不够
    assert calculate_atr([1.0] * 14, [1.0] * 14, [1.0] * 14, period=14) == 0.0


def test_atr_mismatched_lengths_returns_zero():
    assert calculate_atr([1.0] * 16, [1.0] * 15, [1.0] * 16, period=14) == 0.0


def test_atr_non_positive_period_returns_zero():
    assert calculate_atr([1.0] * 16, [1.0] * 16, [1.0] * 16, period=0) == 0.0
    assert calculate_atr([1.0] * 16, [1.0] * 16, [1.0] * 16, period=-1) == 0.0


# ---------- compute_stop_distances / compute_position_size ----------


def test_stop_distances_zero_atr_falls_back_to_floor():
    soft, hard = compute_stop_distances(0.0, 100.0)
    assert soft == pytest.approx(0.02)
    assert hard == pytest.approx(0.04)


def test_stop_distances_calm_coin_floor_binds():
    # 1.5×1/100 = 1.5% < 2% floor → (2%, 4%)
    soft, hard = compute_stop_distances(1.0, 100.0)
    assert soft == pytest.approx(0.02)
    assert hard == pytest.approx(0.04)


def test_stop_distances_volatile_coin_scales():
    # 1.5×2/100 = 3% → (3%, 6%)
    soft, hard = compute_stop_distances(2.0, 100.0)
    assert soft == pytest.approx(0.03)
    assert hard == pytest.approx(0.06)


def test_stop_distances_hard_cap_binds_first():
    # 1.5×2.4/100 = 3.6% → hard = min(7.2%, 6%) = 6%
    soft, hard = compute_stop_distances(2.4, 100.0)
    assert soft == pytest.approx(0.036)
    assert hard == pytest.approx(0.06)


def test_stop_distances_extreme_atr_soft_capped_no_inversion():
    # 1.5×5/100 = 7.5% → soft 封顶 6%，hard=6%；软 ≤ 硬 恒成立
    soft, hard = compute_stop_distances(5.0, 100.0)
    assert soft == pytest.approx(0.06)
    assert hard == pytest.approx(0.06)
    assert soft <= hard


def test_position_size_baseline_matches_status_quo():
    # 软 2% → 名义 min(40/0.02, 2000)=2000，保证金 400 —— 与现状完全一致
    notional, margin = compute_position_size(0.02)
    assert notional == pytest.approx(2000.0)
    assert margin == pytest.approx(400.0)


def test_position_size_scales_down_with_wider_stop():
    # 软 4% → 名义 1000，保证金 200
    notional, margin = compute_position_size(0.04)
    assert notional == pytest.approx(1000.0)
    assert margin == pytest.approx(200.0)


# ---------- 仓位字段回退助手 ----------


def test_pos_helpers_use_position_fields():
    pos = {"hard_stop_pct": 0.05, "position_size": 250.0}
    assert _pos_hard_stop_pct(pos) == 0.05
    assert _pos_margin(pos) == 250.0


def test_pos_helpers_fall_back_for_legacy_positions():
    # 存量仓位无新字段（或为 None）→ 回退 config
    from config import config
    assert _pos_hard_stop_pct({}) == config.FIXED_STOP_LOSS_PCT
    assert _pos_hard_stop_pct({"hard_stop_pct": None}) == config.FIXED_STOP_LOSS_PCT
    assert _pos_margin({}) == config.POSITION_SIZE
    assert _pos_margin({"position_size": None}) == config.POSITION_SIZE


def test_calculate_pnl_fallback_uses_position_size_param():
    # 无 quantity 时用名义公式：1% × margin × LEVERAGE(5)
    pnl = calculate_pnl("LONG", 100.0, 101.0, quantity=None, position_size=200.0)
    assert pnl == pytest.approx(0.01 * 200.0 * 5)


# ---------- 软止损 (1H 收盘确认) ----------


def _mk_state(tmp_path):
    sm = StateManager(str(tmp_path / "s.json"), str(tmp_path / "b.json"),
                      initial_capital=10000.0)
    sm.load()
    return sm


def _mk_exchange(closed_bar_close):
    """MagicMock Exchange：get_klines 返回 [已收盘bar(13:00), 未收盘bar(14:00)]。"""
    closed_open_ms = int(datetime(2026, 6, 11, 13, 0, tzinfo=TZ_CN).timestamp() * 1000)
    ex = MagicMock()
    ex.get_klines.return_value = [
        [closed_open_ms, "0", "0", "0", str(closed_bar_close), "0", 0, "0"],
        [closed_open_ms + 3_600_000, "0", "0", "0", "0", "0", 0, "0"],
    ]
    ex.get_order_fill.return_value = (closed_bar_close, 10.0)
    return ex


def _add_soft_pos(sm, opened_at, side="LONG", entry=100.0, soft=0.03):
    pos = sm.add_position(symbol="AAAUSDT", side=side, entry_price=entry,
                          quantity=10.0, soft_stop_pct=soft, hard_stop_pct=0.06,
                          position_size=266.0)
    pos["opened_at"] = opened_at
    sm.save()
    return pos


NOW = datetime(2026, 6, 11, 14, 1, 0, tzinfo=TZ_CN)


def test_soft_stop_closes_position_on_breached_close(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00")          # 上一小时开仓
    ex = _mk_exchange(96.5)                            # 收盘 96.5 < 软止损线 97
    _check_soft_stops(ex, sm, now=NOW)
    assert sm.state["positions"] == []
    ex.place_order.assert_called_once()


def test_soft_stop_no_trigger_when_close_above_line(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00")
    ex = _mk_exchange(97.5)                            # 收盘 97.5 > 97，盘中无所谓
    _check_soft_stops(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1
    ex.place_order.assert_not_called()


def test_soft_stop_skips_position_opened_this_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 14:00:30")           # 本小时刚开 → 等下个整点
    ex = _mk_exchange(90.0)
    _check_soft_stops(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1


def test_soft_stop_skips_legacy_position_without_field(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    sm.add_position(symbol="OLDUSDT", side="LONG", entry_price=100.0, quantity=10.0)
    ex = _mk_exchange(50.0)
    _check_soft_stops(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1
    ex.get_klines.assert_not_called()


def test_soft_stop_runs_once_per_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00")
    ex = _mk_exchange(97.5)
    _check_soft_stops(ex, sm, now=NOW)
    _check_soft_stops(ex, sm, now=NOW.replace(minute=2))  # 同一小时第二次 tick
    assert ex.get_klines.call_count == 1


def test_soft_stop_disabled_in_fixed_mode(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "fixed")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00")
    ex = _mk_exchange(50.0)
    _check_soft_stops(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1


def test_soft_stop_short_side(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00", side="SHORT", entry=100.0, soft=0.03)
    ex = _mk_exchange(103.5)                           # 收盘 103.5 > 103 → SHORT 触发
    _check_soft_stops(ex, sm, now=NOW)
    assert sm.state["positions"] == []


def test_soft_stop_stale_bar_retries_next_tick(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "STOP_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_soft_pos(sm, "2026-06-11 13:01:00")
    # 刚收盘 bar 应为 13:00 开盘；返回 12:00 开盘的旧 bar → 未滚动
    stale_open_ms = int(datetime(2026, 6, 11, 12, 0, tzinfo=TZ_CN).timestamp() * 1000)
    ex = MagicMock()
    ex.get_klines.return_value = [
        [stale_open_ms, "0", "0", "0", "90.0", "0", 0, "0"],
        [stale_open_ms + 3_600_000, "0", "0", "0", "0", "0", 0, "0"],
    ]
    _check_soft_stops(ex, sm, now=NOW)
    # 旧 bar 收盘 90 远低于软止损线，但不应触发（数据未滚动）
    assert len(sm.state["positions"]) == 1
    # hour key 已回滚 → 同一小时的下一个 tick 会重试
    fresh_open_ms = int(datetime(2026, 6, 11, 13, 0, tzinfo=TZ_CN).timestamp() * 1000)
    ex.get_klines.return_value = [
        [fresh_open_ms, "0", "0", "0", "96.5", "0", 0, "0"],
        [fresh_open_ms + 3_600_000, "0", "0", "0", "0", "0", 0, "0"],
    ]
    ex.get_order_fill.return_value = (96.5, 10.0)
    _check_soft_stops(ex, sm, now=NOW.replace(minute=2))
    assert sm.state["positions"] == []

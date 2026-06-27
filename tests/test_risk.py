import pytest
from datetime import datetime
from unittest.mock import MagicMock
from risk import check_fixed_sl, check_trailing_tp, calculate_atr, compute_stop_distances, compute_position_size, _pos_hard_stop_pct, _pos_margin, calculate_pnl, _check_soft_stops, _check_phase_exits, check_stop_loss
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


# ---------- 相位出场 (_check_phase_exits) ----------

# Timestamps used across phase-exit tests.  NOW is reused from the soft-stop
# section (2026-06-11 14:01 UTC+8).  The just-closed 1H bar opens at 13:00.

_HOUR_MS = 3_600_000
_13H_MS = int(datetime(2026, 6, 11, 13, 0, tzinfo=TZ_CN).timestamp() * 1000)
_14H_MS = int(datetime(2026, 6, 11, 14, 0, tzinfo=TZ_CN).timestamp() * 1000)
_12H_MS = int(datetime(2026, 6, 11, 12, 0, tzinfo=TZ_CN).timestamp() * 1000)
_11H_MS = int(datetime(2026, 6, 11, 11, 0, tzinfo=TZ_CN).timestamp() * 1000)


def _mk_kline(open_ms, close_val, high_val=None, low_val=None):
    """Build a minimal kline row: [open_ms, open, high, low, close, vol, close_ms, ...]"""
    if high_val is None:
        high_val = close_val
    if low_val is None:
        low_val = close_val
    return [open_ms, str(close_val), str(high_val), str(low_val), str(close_val),
            "1000", open_ms + _HOUR_MS - 1, "0"]


def _mk_phase_exchange(closed_bars, just_closed_close):
    """Return a MagicMock Exchange whose get_klines returns:
      closed_bars (already formed) + one unclosed bar at 14:00.
    Also wires get_order_fill so _close_position can complete."""
    unclosed = _mk_kline(_14H_MS, just_closed_close)
    ex = MagicMock()
    ex.get_klines.return_value = list(closed_bars) + [unclosed]
    ex.get_order_fill.return_value = (just_closed_close, 10.0)
    return ex


def _build_closed_bars_20(just_closed_close, pre_bar_override=None, entry_bar_open_ms=_12H_MS):
    """Build exactly 20 closed bars ending with the just-closed bar at 13:00.
    - Bars 0-18: generic bars at 13:00 - 20h .. 13:00 - 2h, close=high=low=105.0
    - Bar 19: just-closed bar at 13:00 with close=just_closed_close

    If pre_bar_override is given it replaces the bar at index 18 (which sits
    strictly between entry_bar_open_ms and the just-closed bar only when
    entry_bar_open_ms < bar_18_open_ms < _13H_MS).
    """
    bars = []
    for i in range(20):
        open_ms = _13H_MS - (19 - i) * _HOUR_MS  # bar 0 → 13:00-19h, bar 19 → 13:00
        if i == 19:
            bars.append(_mk_kline(open_ms, just_closed_close))
        elif pre_bar_override is not None and i == 18:
            bars.append(pre_bar_override)
        else:
            bars.append(_mk_kline(open_ms, 105.0))
    return bars


def _add_phase_pos(sm, opened_at_str, opened_ms, side="LONG", entry=100.0):
    """Add a position with the extra 'opened_ms' field required by _check_phase_exits."""
    pos = sm.add_position(symbol="PHASUSDT", side=side, entry_price=entry,
                          quantity=10.0, soft_stop_pct=0.03, hard_stop_pct=0.06,
                          position_size=266.0)
    pos["opened_at"] = opened_at_str
    pos["opened_ms"] = opened_ms
    sm.save()
    return pos


# Test 1: LONG exits when just-closed 1H close < BB middle (1h_bb_middle)
def test_phase_exit_long_bb_middle_close(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "EXIT_MODE", "phase_bb")
    sm = _mk_state(tmp_path)
    # Entry bar at 12:00, position opened before the just-closed bar at 13:00
    _add_phase_pos(sm, "2026-06-11 12:01:00", _12H_MS)
    # bars 0-18 close=105, bar 19 (just-closed, 13:00) close=103
    # BB middle = mean of last 20 closes = (19*105 + 103)/20 = 104.9
    # bar_close=103 < 104.9 → triggers "1h_bb_middle"
    closed_bars = _build_closed_bars_20(103.0)
    ex = _mk_phase_exchange(closed_bars, 103.0)
    _check_phase_exits(ex, sm, now=NOW)
    assert sm.state["positions"] == [], "LONG should be closed when close < BB middle"
    ex.place_order.assert_called_once()


# Test 2: LONG exits on 3.5% retrace from a confirmed pre-bar high
def test_phase_exit_long_trailing_retrace(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "EXIT_MODE", "phase_bb")
    sm = _mk_state(tmp_path)
    # Entry bar at 11:00; pre-bar at 12:00 (strictly between entry and 13:00 bar)
    _add_phase_pos(sm, "2026-06-11 11:01:00", _11H_MS)
    # pre-bar at 12:00 with high=110; entry_price=100 → pre_high=110
    pre_bar = _mk_kline(_12H_MS, 105.0, high_val=110.0, low_val=100.0)
    # just-closed close=106: above BB middle (~105.05) but ≤ 110 * 0.965 = 106.15
    closed_bars = _build_closed_bars_20(106.0, pre_bar_override=pre_bar,
                                        entry_bar_open_ms=_11H_MS)
    ex = _mk_phase_exchange(closed_bars, 106.0)
    _check_phase_exits(ex, sm, now=NOW)
    assert sm.state["positions"] == [], "LONG should be closed on 3.5% retrace from pre-bar high"
    ex.place_order.assert_called_once()


# Test 3: LONG holds — close above BB middle, no retrace
def test_phase_exit_long_holds_when_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "EXIT_MODE", "phase_bb")
    sm = _mk_state(tmp_path)
    _add_phase_pos(sm, "2026-06-11 12:01:00", _12H_MS)
    # just-closed close=106: > BB middle ~104.9, pre_high=entry=100, no retrace condition
    closed_bars = _build_closed_bars_20(106.0)
    ex = _mk_phase_exchange(closed_bars, 106.0)
    _check_phase_exits(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1, "LONG should hold when no exit condition met"
    ex.place_order.assert_not_called()


# Test 4: Breathe — position opened in the just-closed bar is NOT exited
def test_phase_exit_breathe_entry_bar_is_just_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "EXIT_MODE", "phase_bb")
    sm = _mk_state(tmp_path)
    # opened_ms == 13:00_ms → same as last closed bar → compute_phase_exit_inputs returns None
    _add_phase_pos(sm, "2026-06-11 13:00:30", _13H_MS)
    closed_bars = _build_closed_bars_20(103.0)  # would normally trigger BB exit
    ex = _mk_phase_exchange(closed_bars, 103.0)
    _check_phase_exits(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1, "Position opened in just-closed bar must not be exited (breathe)"
    ex.place_order.assert_not_called()


# Test 5: Once per hour — two ticks in the same hour only call get_klines once
def test_phase_exit_runs_once_per_hour(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "EXIT_MODE", "phase_bb")
    sm = _mk_state(tmp_path)
    _add_phase_pos(sm, "2026-06-11 12:01:00", _12H_MS)
    closed_bars = _build_closed_bars_20(106.0)  # safe close, position holds
    ex = _mk_phase_exchange(closed_bars, 106.0)
    _check_phase_exits(ex, sm, now=NOW)
    _check_phase_exits(ex, sm, now=NOW.replace(minute=2))  # second tick same hour
    assert ex.get_klines.call_count == 1, "get_klines should only be called once per hour"


# Test 6: Disabled when EXIT_MODE != "phase_bb"
def test_phase_exit_disabled_when_mode_not_phase_bb(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "EXIT_MODE", "atr_dual")
    sm = _mk_state(tmp_path)
    _add_phase_pos(sm, "2026-06-11 12:01:00", _12H_MS)
    closed_bars = _build_closed_bars_20(103.0)  # would trigger if enabled
    ex = _mk_phase_exchange(closed_bars, 103.0)
    _check_phase_exits(ex, sm, now=NOW)
    assert len(sm.state["positions"]) == 1, "Position should be held when EXIT_MODE != phase_bb"
    ex.get_klines.assert_not_called()


# Test 7: SHORT closes when just-closed close > BB middle
def test_phase_exit_short_bb_middle_close(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "EXIT_MODE", "phase_bb")
    sm = _mk_state(tmp_path)
    _add_phase_pos(sm, "2026-06-11 12:01:00", _12H_MS, side="SHORT", entry=100.0)
    # bars 0-18 close=105, bar 19 close=107: > BB middle ~104.95 → SHORT exits
    closed_bars = _build_closed_bars_20(107.0)
    ex = _mk_phase_exchange(closed_bars, 107.0)
    _check_phase_exits(ex, sm, now=NOW)
    assert sm.state["positions"] == [], "SHORT should be closed when close > BB middle"
    ex.place_order.assert_called_once()


# ---------- check_stop_loss dispatch exclusivity ----------


def _mk_dispatch_state(tmp_path):
    """Empty positions state — dispatch tests skip the per-position loop."""
    sm = _mk_state(tmp_path)
    return sm


# Test 8: EXIT_MODE="phase_bb" → _check_phase_exits called, _check_soft_stops NOT called
def test_check_stop_loss_dispatches_to_phase_exits(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "EXIT_MODE", "phase_bb")
    monkeypatch.setattr(risk, "check_drawdown", lambda ex, sm: False)
    monkeypatch.setattr(risk, "_sync_positions_with_exchange", lambda ex, sm: None)
    mock_phase = MagicMock()
    mock_soft = MagicMock()
    monkeypatch.setattr(risk, "_check_phase_exits", mock_phase)
    monkeypatch.setattr(risk, "_check_soft_stops", mock_soft)
    sm = _mk_dispatch_state(tmp_path)
    ex = MagicMock()
    check_stop_loss(ex, sm)
    mock_phase.assert_called_once()
    mock_soft.assert_not_called()


# Test 9: EXIT_MODE="atr_dual" → _check_soft_stops called, _check_phase_exits NOT called
def test_check_stop_loss_dispatches_to_soft_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(risk.config, "EXIT_MODE", "atr_dual")
    monkeypatch.setattr(risk, "check_drawdown", lambda ex, sm: False)
    monkeypatch.setattr(risk, "_sync_positions_with_exchange", lambda ex, sm: None)
    mock_phase = MagicMock()
    mock_soft = MagicMock()
    monkeypatch.setattr(risk, "_check_phase_exits", mock_phase)
    monkeypatch.setattr(risk, "_check_soft_stops", mock_soft)
    sm = _mk_dispatch_state(tmp_path)
    ex = MagicMock()
    check_stop_loss(ex, sm)
    mock_soft.assert_called_once()
    mock_phase.assert_not_called()


# ---------- Fix 1: catastrophe-disabled positions skip fixed-SL management ----------


def _add_phase_pos_no_catastrophe(sm, opened_at_str, opened_ms, side="LONG", entry=100.0):
    """Position with hard_stop_pct=None — simulates CATASTROPHE_STOP_ENABLED=False at entry."""
    pos = sm.add_position(symbol="CATUSDT", side=side, entry_price=entry,
                          quantity=10.0, soft_stop_pct=0.03, hard_stop_pct=None,
                          position_size=266.0)
    pos["opened_at"] = opened_at_str
    pos["opened_ms"] = opened_ms
    # Overwrite hard_stop_pct to None (add_position may default it)
    pos["hard_stop_pct"] = None
    sm.save()
    return pos


# Test 10: phase_bb + hard_stop_pct=None → check_stop_loss must NOT place a stop order
# and must NOT close the position via the 2% fixed-SL fallback.
# We stub _check_phase_exits to a no-op so the phase exit doesn't close it either,
# allowing us to verify the per-position fixed-SL block is skipped cleanly.
def test_catastrophe_disabled_skips_stop_order_and_fixed_sl(tmp_path, monkeypatch):
    """End-to-end: with EXIT_MODE='phase_bb' and hard_stop_pct=None, check_stop_loss
    must not call exchange.place_stop_order and must not close the position via fixed SL,
    even if the current price is far below the 2% fallback stop that _pos_hard_stop_pct
    would otherwise return."""
    monkeypatch.setattr(risk.config, "EXIT_MODE", "phase_bb")
    monkeypatch.setattr(risk, "check_drawdown", lambda ex, sm: False)
    monkeypatch.setattr(risk, "_sync_positions_with_exchange", lambda ex, sm: None)
    # Stub _check_phase_exits to a no-op so phase logic doesn't close the position
    monkeypatch.setattr(risk, "_check_phase_exits", MagicMock())

    sm = _mk_state(tmp_path)
    _add_phase_pos_no_catastrophe(sm, "2026-06-11 12:01:00", _12H_MS)

    ex = MagicMock()
    # Price is 10% below entry — would trip the 2% fallback stop if the block were entered
    ex.get_price.return_value = 90.0

    check_stop_loss(ex, sm)

    # The position must remain open (no fixed-SL close)
    assert len(sm.state["positions"]) == 1, (
        "Position with hard_stop_pct=None must not be closed by the fixed-SL block"
    )
    # No exchange stop order must have been placed
    ex.place_stop_order.assert_not_called(), (
        "place_stop_order must not be called when catastrophe stop is disabled"
    )

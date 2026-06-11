import pytest
from risk import check_fixed_sl, check_trailing_tp


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

from risk import calculate_atr


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

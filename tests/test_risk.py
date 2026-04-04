import pytest
from risk import should_stop_loss, calculate_atr


def test_long_atr_stop_triggered():
    result = should_stop_loss(
        side="LONG",
        highest_price=100.0,
        lowest_price=90.0,
        current_price=96.0,
        atr=2.5,
        atr_multiplier=2.0,
        max_stop_pct=0.06,
    )
    # stop_price = max(100 - 2*2.5, 100*0.94) = max(95, 94) = 95
    # 96 > 95 → not triggered
    assert result is False


def test_long_atr_stop_triggered_below():
    result = should_stop_loss(
        side="LONG",
        highest_price=100.0,
        lowest_price=90.0,
        current_price=94.5,
        atr=2.5,
        atr_multiplier=2.0,
        max_stop_pct=0.06,
    )
    # stop_price = 95, current 94.5 <= 95 → triggered
    assert result is True


def test_long_hard_cap_triggers_first():
    # ATR would allow wider stop, but 6% cap is tighter
    result = should_stop_loss(
        side="LONG",
        highest_price=100.0,
        lowest_price=90.0,
        current_price=93.5,
        atr=5.0,
        atr_multiplier=2.0,
        max_stop_pct=0.06,
    )
    # atr_stop = 100 - 10 = 90, hard_stop = 94 → stop_price = 94
    # 93.5 <= 94 → triggered by hard cap
    assert result is True


def test_short_atr_stop_triggered():
    result = should_stop_loss(
        side="SHORT",
        highest_price=110.0,
        lowest_price=100.0,
        current_price=105.5,
        atr=2.5,
        atr_multiplier=2.0,
        max_stop_pct=0.06,
    )
    # stop_price = min(100 + 5, 106) = 105
    # 105.5 >= 105 → triggered
    assert result is True


def test_short_atr_stop_not_triggered():
    result = should_stop_loss(
        side="SHORT",
        highest_price=110.0,
        lowest_price=100.0,
        current_price=103.0,
        atr=2.5,
        atr_multiplier=2.0,
        max_stop_pct=0.06,
    )
    # stop_price = 105, 103 < 105 → safe
    assert result is False


def test_calculate_atr():
    # Simulated klines: [open_time, open, high, low, close, volume]
    klines = []
    for i in range(16):
        klines.append([0, 100, 105, 95, 100, 1000])
    atr = calculate_atr(klines, period=14)
    # TR = max(105-95, |105-100|, |95-100|) = 10 for each bar
    assert abs(atr - 10.0) < 0.01


def test_calculate_atr_insufficient_data():
    klines = [[0, 100, 105, 95, 100, 1000]] * 5
    atr = calculate_atr(klines, period=14)
    assert atr == 0.0

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

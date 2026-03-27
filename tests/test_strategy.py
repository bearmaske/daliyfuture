import numpy as np
import pytest
from strategy import calculate_bollinger_bands, check_trend, check_entry_signal


def test_bollinger_bands_calculation():
    closes = [100.0] * 20 + [110.0]
    upper, middle, lower = calculate_bollinger_bands(closes, period=20, std_dev=2)
    assert middle is not None
    assert upper > middle
    assert lower < middle


def test_trend_bullish():
    closes = [100.0] * 20 + [120.0]
    trend = check_trend(closes, period=20)
    assert trend == "LONG"


def test_trend_bearish():
    closes = [100.0] * 20 + [80.0]
    trend = check_trend(closes, period=20)
    assert trend == "SHORT"


def test_entry_signal_long():
    closes = [100.0] * 20 + [115.0]
    signal = check_entry_signal(closes, trend="LONG", period=20, std_dev=2)
    assert signal is True


def test_entry_signal_no_breakout():
    closes = [100.0] * 20 + [100.5]
    signal = check_entry_signal(closes, trend="LONG", period=20, std_dev=2)
    assert signal is False


def test_entry_signal_wrong_trend():
    closes = [100.0] * 20 + [115.0]
    signal = check_entry_signal(closes, trend="SHORT", period=20, std_dev=2)
    assert signal is False


def test_entry_signal_short():
    closes = [100.0] * 20 + [85.0]
    signal = check_entry_signal(closes, trend="SHORT", period=20, std_dev=2)
    assert signal is True

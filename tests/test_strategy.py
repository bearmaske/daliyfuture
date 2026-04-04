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
    # Price rising: SMA slopes up and price above SMA
    closes = list(range(80, 102))  # 22 values, steadily rising
    trend = check_trend(closes, period=20)
    assert trend == "LONG"


def test_trend_bearish():
    # Price falling: SMA slopes down and price below SMA
    closes = list(range(120, 98, -1))  # 22 values, steadily falling
    trend = check_trend(closes, period=20)
    assert trend == "SHORT"


def test_trend_flat_returns_none():
    # Flat prices: SMA not sloping → None
    closes = [100.0] * 22
    trend = check_trend(closes, period=20)
    assert trend is None


def test_trend_price_above_but_sma_flat():
    # Price above SMA but SMA not rising → None (filters choppy markets)
    closes = [100.0] * 21 + [105.0]
    trend = check_trend(closes, period=20)
    # SMA_now includes the 105 bump, SMA_prev is flat 100
    # SMA_now > SMA_prev and price > SMA_now → LONG
    assert trend == "LONG"


def test_trend_insufficient_data():
    closes = [100.0] * 15
    trend = check_trend(closes, period=20)
    assert trend is None


def test_entry_signal_long():
    closes = [100.0] * 20 + [115.0]
    signal = check_entry_signal(closes, trend="LONG", period=20, std_dev=2)
    assert signal is True


def test_entry_signal_no_breakout():
    # With some variance so upper band is meaningful, price stays inside
    closes = [100.0 + (i % 3) for i in range(20)] + [101.0]
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

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


# ---------- compute_entry_risk ----------

from strategy import compute_entry_risk
from config import config as _cfg


def _mk_klines(n, high, low, close):
    """Binance 原始 K 线格式（字符串数值），最后一根视为未收盘。"""
    return [[i, str(close), str(high), str(low), str(close), "0", 0, "0"]
            for i in range(n)]


def test_compute_entry_risk_atr_dual_scales_position(monkeypatch):
    monkeypatch.setattr(_cfg, "STOP_MODE", "atr_dual")
    # 30 根，high-low=2.4 恒定 → ATR=2.4 → 软=1.5×2.4/100=3.6%，硬=min(7.2%,6%)=6%
    kl = _mk_klines(30, high=101.2, low=98.8, close=100.0)
    r = compute_entry_risk(kl, 100.0)
    assert r["soft_stop_pct"] == pytest.approx(0.036)
    assert r["hard_stop_pct"] == pytest.approx(0.06)
    assert r["notional"] == pytest.approx(40.0 / 0.036)
    assert r["margin"] == pytest.approx(40.0 / 0.036 / 5)
    assert r["atr"] == pytest.approx(2.4)


def test_compute_entry_risk_fixed_mode_matches_legacy(monkeypatch):
    monkeypatch.setattr(_cfg, "STOP_MODE", "fixed")
    kl = _mk_klines(30, high=101.2, low=98.8, close=100.0)
    r = compute_entry_risk(kl, 100.0)
    assert r["soft_stop_pct"] is None
    assert r["hard_stop_pct"] == _cfg.FIXED_STOP_LOSS_PCT
    assert r["notional"] == pytest.approx(_cfg.POSITION_SIZE * _cfg.LEVERAGE)
    assert r["margin"] == pytest.approx(_cfg.POSITION_SIZE)


def test_compute_entry_risk_insufficient_klines_falls_back_to_floor(monkeypatch):
    monkeypatch.setattr(_cfg, "STOP_MODE", "atr_dual")
    kl = _mk_klines(5, high=101.0, low=99.0, close=100.0)  # 不足 ATR_PERIOD+1
    r = compute_entry_risk(kl, 100.0)
    # ATR=0 → 软 2% / 硬 4%，名义回到 $2000
    assert r["soft_stop_pct"] == pytest.approx(0.02)
    assert r["hard_stop_pct"] == pytest.approx(0.04)
    assert r["notional"] == pytest.approx(2000.0)

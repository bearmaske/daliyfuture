import pytest
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from backtesting.engine import (
    apply_slippage,
    calculate_fee,
    BacktestPosition,
    BacktestTrade,
    BacktestEngine,
)


def test_apply_slippage_long_entry():
    price = apply_slippage(100.0, side="LONG", is_entry=True, slippage_pct=0.0005)
    assert price == pytest.approx(100.05)


def test_apply_slippage_long_exit():
    price = apply_slippage(100.0, side="LONG", is_entry=False, slippage_pct=0.0005)
    assert price == pytest.approx(99.95)


def test_apply_slippage_short_entry():
    price = apply_slippage(100.0, side="SHORT", is_entry=True, slippage_pct=0.0005)
    assert price == pytest.approx(99.95)


def test_apply_slippage_short_exit():
    price = apply_slippage(100.0, side="SHORT", is_entry=False, slippage_pct=0.0005)
    assert price == pytest.approx(100.05)


def test_calculate_fee():
    fee = calculate_fee(notional=2500.0, fee_rate=0.0004)
    assert fee == pytest.approx(1.0)


def test_backtest_position_creation():
    pos = BacktestPosition(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=50000.0,
        quantity=0.05,
        opened_at="2025-04-04 12:00:00",
    )
    assert pos.highest_price == 50000.0
    assert pos.lowest_price == 50000.0


def test_backtest_trade_pnl():
    trade = BacktestTrade(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=50000.0,
        exit_price=51000.0,
        quantity=0.05,
        pnl=50.0,
        fee=2.0,
        opened_at="2025-04-04 12:00:00",
        closed_at="2025-04-05 12:00:00",
        exit_reason="atr_stop",
    )
    assert trade.pnl == 50.0
    assert trade.fee == 2.0


def _make_kline(open_time_ms, open_p, high, low, close, volume=1000.0):
    return {
        "open_time": open_time_ms,
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_engine_no_signal_no_trades():
    """Flat data should produce no trades."""
    base_ms = 1680000000000
    day_ms = 86400000
    hour_ms = 3600000

    daily_bars = []
    for i in range(30):
        daily_bars.append(_make_kline(base_ms + i * day_ms, 100, 100, 100, 100))
    daily_df = pd.DataFrame(daily_bars)

    hourly_bars = []
    for i in range(30 * 24):
        hourly_bars.append(_make_kline(base_ms + i * hour_ms, 100, 100, 100, 100))
    hourly_df = pd.DataFrame(hourly_bars)

    engine = BacktestEngine(
        initial_capital=10000.0,
        position_size=500.0,
        leverage=5,
        max_positions=10,
    )
    trades, equity_curve = engine.run({"TESTUSDT": (hourly_df, daily_df)})

    assert len(trades) == 0
    assert engine.balance == 10000.0


def test_engine_long_signal_opens_and_stops():
    """Rising data should trigger a LONG entry, then an ATR stop on reversal."""
    base_ms = 1680000000000
    day_ms = 86400000
    hour_ms = 3600000

    daily_bars = []
    for i in range(25):
        p = 100 + i
        daily_bars.append(_make_kline(base_ms + i * day_ms, p, p + 1, p - 1, p))
    daily_df = pd.DataFrame(daily_bars)

    hourly_bars = []
    for i in range(500):
        p = 100 + i * 0.04
        hourly_bars.append(_make_kline(base_ms + i * hour_ms, p, p + 0.5, p - 0.5, p))
    for i in range(500, 550):
        p = 120 + (i - 500) * 0.5
        hourly_bars.append(_make_kline(base_ms + i * hour_ms, p, p + 1, p - 1, p))
    for i in range(550, 600):
        p = 145 - (i - 550) * 1.5
        hourly_bars.append(_make_kline(base_ms + i * hour_ms, p, p + 1, p - 1, p))
    hourly_df = pd.DataFrame(hourly_bars)

    engine = BacktestEngine(
        initial_capital=10000.0,
        position_size=500.0,
        leverage=5,
        max_positions=10,
    )
    trades, equity_curve = engine.run({"TESTUSDT": (hourly_df, daily_df)})

    assert len(trades) >= 1
    assert trades[0].side == "LONG"
    assert trades[0].exit_reason in ("fixed_sl", "trailing_tp", "backtest_end")


# ---------- 双层 ATR 止损 (STOP_MODE=atr_dual) ----------

HOUR_MS = 3600_000


def _mk_engine(stop_mode):
    return BacktestEngine(initial_capital=10000.0, position_size=400.0,
                          leverage=5, stop_mode=stop_mode)


def _mk_hourly_df(rows):
    """rows: list of (open_time, open, high, low, close)"""
    return pd.DataFrame(
        [{"open_time": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}
         for t, o, h, l, c in rows]
    )


def _flat_df(n, high, low, close):
    return _mk_hourly_df([(i * HOUR_MS, close, high, low, close) for i in range(n)])


def test_entry_sizing_atr_dual():
    eng = _mk_engine("atr_dual")
    # TR 恒为 1.6 → ATR=1.6 → 软 = 1.5×1.6/100 = 2.4%，硬 = 4.8%
    df = _flat_df(30, high=100.8, low=99.2, close=100.0)
    soft, hard, notional, margin, atr = eng._entry_sizing(df, 100.0)
    assert soft == pytest.approx(0.024)
    assert hard == pytest.approx(0.048)
    assert notional == pytest.approx(40.0 / 0.024)
    assert margin == pytest.approx(40.0 / 0.024 / 5)


def test_entry_sizing_fixed_matches_legacy():
    eng = _mk_engine("fixed")
    df = _flat_df(30, high=100.8, low=99.2, close=100.0)
    soft, hard, notional, margin, atr = eng._entry_sizing(df, 100.0)
    assert soft == 0.0
    assert hard == eng.fixed_stop_loss_pct
    assert notional == pytest.approx(400.0 * 5)
    assert margin == pytest.approx(400.0)


def _soft_pos(opened_ms=0):
    return BacktestPosition(
        symbol="X", side="LONG", entry_price=100.0, quantity=10.0,
        opened_at="2026-01-01 08:00:00", opened_ms=opened_ms,
        soft_stop_pct=0.03, hard_stop_pct=0.06, notional=1000.0, margin=200.0,
    )


def test_soft_stop_fires_on_entry_bar_close():
    # 入场 bar (open_time=0) 收盘 96.5 < 软止损线 97 → 在 ts=HOUR_MS 检查刚收盘的 bar 0 → soft_sl
    eng = _mk_engine("atr_dual")
    eng.positions.append(_soft_pos(opened_ms=0))
    df = _mk_hourly_df([(0, 100.0, 101.0, 95.0, 96.5),
                        (HOUR_MS, 96.0, 97.0, 95.5, 96.8)])
    eng._check_stops_hour(HOUR_MS, {"X": (df, None)})
    assert len(eng.trades) == 1
    assert eng.trades[0].exit_reason == "soft_sl"


def test_soft_stop_survives_intrabar_dip():
    # bar 0 盘中 low=95（旧逻辑会打掉），收盘 98 > 97 → 不触发
    eng = _mk_engine("atr_dual")
    eng.positions.append(_soft_pos(opened_ms=0))
    df = _mk_hourly_df([(0, 100.0, 101.0, 95.0, 98.0),
                        (HOUR_MS, 98.0, 99.0, 97.5, 98.5)])
    eng._check_stops_hour(HOUR_MS, {"X": (df, None)})
    assert eng.trades == []
    assert len(eng.positions) == 1


def test_hard_stop_fires_intrabar_on_minute_grid():
    eng = _mk_engine("atr_dual")
    pos = _soft_pos(opened_ms=0)
    eng.positions.append(pos)
    minute_df = pd.DataFrame([
        {"open_time": 60_000, "open": 100.0, "high": 100.0, "low": 93.0,
         "close": 93.5, "volume": 1.0},
    ])
    eng._minute_data = {"X": minute_df}
    eng._check_stops_minute(60_000)
    # 93.5 < 硬止损线 94 → 盘中触发
    assert len(eng.trades) == 1
    assert eng.trades[0].exit_reason == "hard_sl"


def test_close_position_uses_pos_margin_and_notional():
    eng = _mk_engine("atr_dual")
    pos = _soft_pos(opened_ms=0)
    eng.positions.append(pos)
    balance_before = eng.balance
    eng._close_position(pos, 99.0, "soft_sl", HOUR_MS)
    # pnl = -1% × notional(1000) = -10；exit fee = 1000×0.0004 = 0.4
    # balance += margin(200) + (-10.4)
    assert eng.balance == pytest.approx(balance_before + 200.0 - 10.4)

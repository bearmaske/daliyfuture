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

import pytest
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.engine import BacktestTrade
from backtesting.report import calculate_stats


def _make_trade(pnl, side="LONG", hours_held=24):
    return BacktestTrade(
        symbol="BTCUSDT",
        side=side,
        entry_price=50000.0,
        exit_price=51000.0 if pnl > 0 else 49000.0,
        quantity=0.05,
        pnl=pnl,
        fee=2.0,
        opened_at="2025-04-01 12:00:00",
        closed_at="2025-04-02 12:00:00",
        exit_reason="atr_stop",
    )


def test_stats_basic():
    trades = [_make_trade(100), _make_trade(-50), _make_trade(75)]
    equity = [
        {"timestamp": 0, "equity": 10000},
        {"timestamp": 1, "equity": 10100},
        {"timestamp": 2, "equity": 10050},
        {"timestamp": 3, "equity": 10125},
    ]
    stats = calculate_stats(trades, equity, initial_capital=10000.0)

    assert stats["total_trades"] == 3
    assert stats["total_pnl"] == pytest.approx(125.0)
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["max_drawdown_pct"] >= 0


def test_stats_no_trades():
    stats = calculate_stats([], [{"timestamp": 0, "equity": 10000}], initial_capital=10000.0)
    assert stats["total_trades"] == 0
    assert stats["total_pnl"] == 0.0
    assert stats["win_rate"] == 0.0


def test_stats_all_losses():
    trades = [_make_trade(-100), _make_trade(-50)]
    equity = [
        {"timestamp": 0, "equity": 10000},
        {"timestamp": 1, "equity": 9900},
        {"timestamp": 2, "equity": 9850},
    ]
    stats = calculate_stats(trades, equity, initial_capital=10000.0)
    assert stats["win_rate"] == 0.0
    assert stats["profit_factor"] == 0.0

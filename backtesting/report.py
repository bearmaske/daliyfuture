"""Backtest result statistics, console report, and CSV export."""
import os
import sys
from datetime import datetime, timezone, timedelta

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.engine import BacktestTrade

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")


def calculate_stats(
    trades: list[BacktestTrade],
    equity_curve: list[dict],
    initial_capital: float,
) -> dict:
    """Calculate key performance metrics."""
    stats = {
        "initial_capital": initial_capital,
        "total_trades": len(trades),
        "total_pnl": 0.0,
        "total_return_pct": 0.0,
        "annualized_return_pct": 0.0,
        "max_drawdown": 0.0,
        "max_drawdown_pct": 0.0,
        "sharpe_ratio": 0.0,
        "win_rate": 0.0,
        "profit_factor": 0.0,
        "avg_pnl": 0.0,
        "avg_win": 0.0,
        "avg_loss": 0.0,
        "long_trades": 0,
        "short_trades": 0,
        "avg_hold_hours": 0.0,
    }

    if not trades:
        return stats

    pnls = [t.pnl for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    stats["total_pnl"] = sum(pnls)
    stats["total_return_pct"] = stats["total_pnl"] / initial_capital * 100
    stats["total_trades"] = len(trades)
    stats["avg_pnl"] = np.mean(pnls)
    stats["win_rate"] = len(wins) / len(trades) if trades else 0.0
    stats["avg_win"] = np.mean(wins) if wins else 0.0
    stats["avg_loss"] = np.mean(losses) if losses else 0.0
    stats["profit_factor"] = (sum(wins) / abs(sum(losses))) if losses and sum(losses) != 0 else (float("inf") if wins else 0.0)
    stats["long_trades"] = sum(1 for t in trades if t.side == "LONG")
    stats["short_trades"] = sum(1 for t in trades if t.side == "SHORT")

    hold_hours = []
    for t in trades:
        try:
            fmt = "%Y-%m-%d %H:%M:%S"
            opened = datetime.strptime(t.opened_at, fmt)
            closed = datetime.strptime(t.closed_at, fmt)
            hold_hours.append((closed - opened).total_seconds() / 3600)
        except (ValueError, TypeError):
            pass
    stats["avg_hold_hours"] = np.mean(hold_hours) if hold_hours else 0.0

    if equity_curve:
        equities = [e["equity"] for e in equity_curve]
        peak = equities[0]
        max_dd = 0.0
        max_dd_pct = 0.0
        for eq in equities:
            if eq > peak:
                peak = eq
            dd = peak - eq
            dd_pct = dd / peak * 100 if peak > 0 else 0
            if dd > max_dd:
                max_dd = dd
                max_dd_pct = dd_pct
        stats["max_drawdown"] = max_dd
        stats["max_drawdown_pct"] = max_dd_pct

    if len(equity_curve) >= 2:
        hours = len(equity_curve)
        years = hours / (365.25 * 24)
        if years > 0:
            total_return = stats["total_pnl"] / initial_capital
            stats["annualized_return_pct"] = ((1 + total_return) ** (1 / years) - 1) * 100

    if len(equity_curve) >= 2:
        equities = [e["equity"] for e in equity_curve]
        returns = [(equities[i] - equities[i - 1]) / equities[i - 1]
                    for i in range(1, len(equities)) if equities[i - 1] > 0]
        if returns and np.std(returns) > 0:
            stats["sharpe_ratio"] = (np.mean(returns) / np.std(returns)) * np.sqrt(365.25 * 24)

    return stats


def print_report(stats: dict):
    """Print backtest summary to console."""
    print("\n" + "=" * 60)
    print("  BACKTEST RESULTS")
    print("=" * 60)
    print(f"  Initial Capital:     ${stats['initial_capital']:>12,.2f}")
    print(f"  Final PnL:           ${stats['total_pnl']:>12,.2f}")
    print(f"  Total Return:         {stats['total_return_pct']:>11.2f}%")
    print(f"  Annualized Return:    {stats['annualized_return_pct']:>11.2f}%")
    print(f"  Max Drawdown:        ${stats['max_drawdown']:>12,.2f} ({stats['max_drawdown_pct']:.2f}%)")
    print(f"  Sharpe Ratio:         {stats['sharpe_ratio']:>11.2f}")
    print("-" * 60)
    print(f"  Total Trades:         {stats['total_trades']:>11d}")
    print(f"  Win Rate:             {stats['win_rate']:>10.1%}")
    print(f"  Profit Factor:        {stats['profit_factor']:>11.2f}")
    print(f"  Avg PnL/Trade:       ${stats['avg_pnl']:>12,.2f}")
    print(f"  Avg Win:             ${stats['avg_win']:>12,.2f}")
    print(f"  Avg Loss:            ${stats['avg_loss']:>12,.2f}")
    print(f"  Long / Short:         {stats['long_trades']:>5d} / {stats['short_trades']}")
    print(f"  Avg Hold Time:        {stats['avg_hold_hours']:>10.1f}h")
    print("=" * 60)


def export_csv(trades: list[BacktestTrade], equity_curve: list[dict]):
    """Export trades and equity curve to CSV files."""
    os.makedirs(RESULTS_DIR, exist_ok=True)

    if trades:
        rows = []
        for t in trades:
            rows.append({
                "symbol": t.symbol,
                "side": t.side,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "pnl": round(t.pnl, 2),
                "fee": round(t.fee, 2),
                "opened_at": t.opened_at,
                "closed_at": t.closed_at,
                "exit_reason": t.exit_reason,
            })
        trades_df = pd.DataFrame(rows)
        trades_path = os.path.join(RESULTS_DIR, "trades.csv")
        trades_df.to_csv(trades_path, index=False)
        print(f"\nTrades exported to: {os.path.abspath(trades_path)}")

    if equity_curve:
        eq_df = pd.DataFrame(equity_curve)
        peak = eq_df["equity"].cummax()
        eq_df["drawdown"] = peak - eq_df["equity"]
        eq_path = os.path.join(RESULTS_DIR, "equity.csv")
        eq_df.to_csv(eq_path, index=False)
        print(f"Equity curve exported to: {os.path.abspath(eq_path)}")

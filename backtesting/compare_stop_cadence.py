#!/usr/bin/env python3
"""Compare stop-loss scan cadences (1m / 2m / 3m) against the same entries.

Loads 1H + 1D for signal evaluation and 1m for intra-hour stop checks.
For each minute stride, runs the backtest and prints side-by-side stats.
"""
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.download_data import DEFAULT_SYMBOLS, DATA_DIR
from backtesting.engine import BacktestEngine
from backtesting.report import calculate_stats
from config import config


def load_data(symbols: list[str]):
    """Return (hd_data, minute_data, active_symbols)."""
    hd_data = {}
    minute_data = {}
    missing = []
    for symbol in symbols:
        h = os.path.join(DATA_DIR, f"{symbol}_1h.csv")
        d = os.path.join(DATA_DIR, f"{symbol}_1d.csv")
        m = os.path.join(DATA_DIR, f"{symbol}_1m.csv")
        if not (os.path.exists(h) and os.path.exists(d) and os.path.exists(m)):
            missing.append(symbol)
            continue
        hd_data[symbol] = (pd.read_csv(h), pd.read_csv(d))
        minute_data[symbol] = pd.read_csv(m)
    return hd_data, minute_data, missing


def run_one(stride_minutes: int, hd_data, minute_data):
    engine = BacktestEngine(
        initial_capital=config.INITIAL_CAPITAL,
        position_size=config.POSITION_SIZE,
        leverage=config.LEVERAGE,
        max_positions=config.MAX_POSITIONS,
        stop_check_minutes=stride_minutes,
    )
    trades, equity = engine.run(hd_data, minute_data=minute_data)
    return calculate_stats(trades, equity, config.INITIAL_CAPITAL), trades


def main():
    hd_data, minute_data, missing = load_data(DEFAULT_SYMBOLS)
    if missing:
        print(f"[WARN] missing data for {len(missing)} symbols: {missing[:5]}...")
    print(f"Loaded {len(hd_data)} symbols with full 1H+1D+1m data")

    # Trim hourly timeline to the window covered by 1m data so comparisons line up
    earliest_m = min(df["open_time"].iloc[0] for df in minute_data.values())
    latest_m = max(df["open_time"].iloc[-1] for df in minute_data.values())
    for sym in list(hd_data.keys()):
        h, d = hd_data[sym]
        h = h[(h["open_time"] >= earliest_m - 3600_000 * 24) & (h["open_time"] <= latest_m + 3600_000)]
        hd_data[sym] = (h.reset_index(drop=True), d)
    from datetime import datetime, timezone, timedelta
    TZ_CN = timezone(timedelta(hours=8))
    print(f"Minute window: {datetime.fromtimestamp(earliest_m/1000, tz=TZ_CN):%Y-%m-%d %H:%M} → {datetime.fromtimestamp(latest_m/1000, tz=TZ_CN):%Y-%m-%d %H:%M} (CN)")
    print()

    results = {}
    for stride in [1, 2, 3]:
        label = f"{stride}m"
        print(f"[{label}] running...")
        t0 = time.time()
        stats, trades = run_one(stride, hd_data, minute_data)
        elapsed = time.time() - t0
        print(f"[{label}] done in {elapsed:.1f}s — {len(trades)} trades")
        results[label] = (stats, trades)

    print()
    print("=" * 78)
    print(f"{'metric':<22}" + "".join(f"{k:>16}" for k in results))
    print("-" * 78)
    keys = [
        ("total_trades", "total_trades", "{:d}"),
        ("win_rate", "win_rate", "{:.1%}"),
        ("total_pnl", "total_pnl", "${:.2f}"),
        ("total_return", "total_return_pct", "{:.2f}%"),
        ("max_drawdown", "max_drawdown_pct", "{:.2f}%"),
        ("sharpe", "sharpe_ratio", "{:.3f}"),
        ("profit_factor", "profit_factor", "{:.2f}"),
        ("avg_pnl", "avg_pnl", "${:.2f}"),
        ("avg_win", "avg_win", "${:.2f}"),
        ("avg_loss", "avg_loss", "${:.2f}"),
        ("avg_hold_hours", "avg_hold_hours", "{:.1f}h"),
    ]
    for label, key, fmt in keys:
        row = f"{label:<22}"
        for k in results:
            stats = results[k][0]
            val = stats.get(key, 0)
            row += f"{fmt.format(val):>16}"
        print(row)
    print("=" * 78)

    # Divergence vs 1m baseline
    print()
    print("Exit-reason mix:")
    for label, (stats, trades) in results.items():
        reasons = {}
        for t in trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print(f"  {label}: {reasons}")


if __name__ == "__main__":
    main()

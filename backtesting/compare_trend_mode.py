#!/usr/bin/env python3
"""Compare daily-SMA (lagging) vs rolling-hourly-SMA (hourly-refreshed) trend
filters on the same 1H+1D backtest data. Stop cadence is 1H-close (no minute
data needed here — we're isolating the trend-filter variable)."""
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.download_data import DEFAULT_SYMBOLS, DATA_DIR
from backtesting.engine import BacktestEngine
from backtesting.report import calculate_stats
from config import config


def load_data(symbols):
    hd = {}
    missing = []
    for s in symbols:
        h = os.path.join(DATA_DIR, f"{s}_1h.csv")
        d = os.path.join(DATA_DIR, f"{s}_1d.csv")
        if not (os.path.exists(h) and os.path.exists(d)):
            missing.append(s)
            continue
        hd[s] = (pd.read_csv(h), pd.read_csv(d))
    return hd, missing


def run_mode(mode, hd):
    original = config.TREND_FILTER_MODE
    config.TREND_FILTER_MODE = mode
    try:
        engine = BacktestEngine(
            initial_capital=config.INITIAL_CAPITAL,
            position_size=config.POSITION_SIZE,
            leverage=config.LEVERAGE,
            max_positions=config.MAX_POSITIONS,
        )
        trades, equity = engine.run(hd)
        return calculate_stats(trades, equity, config.INITIAL_CAPITAL), trades
    finally:
        config.TREND_FILTER_MODE = original


def main():
    hd, missing = load_data(DEFAULT_SYMBOLS)
    if missing:
        print(f"[WARN] missing data for: {missing}")
    print(f"Loaded {len(hd)} symbols")

    modes = ["sma", "rolling_sma", "asymmetric"]
    labels = {"sma": "daily-sma", "rolling_sma": "rolling-hourly", "asymmetric": "asymmetric"}
    results = {}
    for m in modes:
        print(f"[{labels[m]}] running...")
        t0 = time.time()
        stats, trades = run_mode(m, hd)
        print(f"[{labels[m]}] done in {time.time() - t0:.1f}s — {len(trades)} trades")
        results[m] = (stats, trades)

    print()
    print("=" * 78)
    cols = [labels[m] for m in modes]
    print(f"{'metric':<22}" + "".join(f"{c:>22}" for c in cols))
    print("-" * 78)
    rows = [
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
        ("long_trades", "long_trades", "{:d}"),
        ("short_trades", "short_trades", "{:d}"),
    ]
    for label, key, fmt in rows:
        row = f"{label:<22}"
        for m in modes:
            v = results[m][0].get(key, 0)
            row += f"{fmt.format(v):>22}"
        print(row)
    print("=" * 78)

    print()
    print("Exit-reason mix:")
    for m in modes:
        reasons = {}
        for t in results[m][1]:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print(f"  {labels[m]}: {reasons}")

    print()
    print("Direction split (trades / win% / PnL):")
    for m in modes:
        long_trades = [t for t in results[m][1] if t.side == "LONG"]
        short_trades = [t for t in results[m][1] if t.side == "SHORT"]
        lw = sum(1 for t in long_trades if t.pnl > 0) / len(long_trades) if long_trades else 0
        sw = sum(1 for t in short_trades if t.pnl > 0) / len(short_trades) if short_trades else 0
        lp = sum(t.pnl for t in long_trades)
        sp = sum(t.pnl for t in short_trades)
        print(f"  {labels[m]:<18}  LONG {len(long_trades):>4} / {lw:.1%} / ${lp:+.0f}   "
              f"SHORT {len(short_trades):>4} / {sw:.1%} / ${sp:+.0f}")

    # Monthly PnL
    print()
    print("Monthly PnL:")
    print(f"{'month':<10}" + "".join(f"{labels[m]:>18}" for m in modes))
    months = sorted({pd.to_datetime(t.closed_at).to_period("M")
                     for m in modes for t in results[m][1]})
    for month in months:
        row = f"{str(month):<10}"
        for m in modes:
            month_pnl = sum(t.pnl for t in results[m][1]
                            if pd.to_datetime(t.closed_at).to_period("M") == month)
            row += f"${month_pnl:>17.2f}"
        print(row)


if __name__ == "__main__":
    main()

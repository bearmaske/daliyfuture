#!/usr/bin/env python3
"""Backtest CLI entry point for the Trend Sniper strategy."""
import argparse
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.download_data import DEFAULT_SYMBOLS, DATA_DIR
from backtesting.engine import BacktestEngine
from backtesting.report import calculate_stats, print_report, export_csv
from config import config


def load_data(symbols: list[str]) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    """Load hourly and daily CSV data for each symbol."""
    data = {}
    for symbol in symbols:
        h_path = os.path.join(DATA_DIR, f"{symbol}_1h.csv")
        d_path = os.path.join(DATA_DIR, f"{symbol}_1d.csv")

        if not os.path.exists(h_path) or not os.path.exists(d_path):
            print(f"[WARN] Missing data for {symbol}, skipping. Run download_data.py first.")
            continue

        hourly_df = pd.read_csv(h_path)
        daily_df = pd.read_csv(d_path)
        data[symbol] = (hourly_df, daily_df)
        print(f"  Loaded {symbol}: {len(hourly_df)} hourly bars, {len(daily_df)} daily bars")

    return data


def main():
    parser = argparse.ArgumentParser(description="Trend Sniper Backtester")
    parser.add_argument(
        "--symbols",
        type=str,
        default=",".join(DEFAULT_SYMBOLS),
        help="Comma-separated symbol list (default: 30 major coins)",
    )
    parser.add_argument("--capital", type=float, default=config.INITIAL_CAPITAL, help="Initial capital")
    parser.add_argument("--position-size", type=float, default=config.POSITION_SIZE, help="Position size per trade")
    parser.add_argument("--leverage", type=int, default=config.LEVERAGE, help="Leverage multiplier")
    parser.add_argument("--max-positions", type=int, default=config.MAX_POSITIONS, help="Max concurrent positions")
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]

    print("=" * 60)
    print("  TREND SNIPER BACKTESTER")
    print("=" * 60)
    print(f"  Symbols:       {len(symbols)}")
    print(f"  Capital:       ${args.capital:,.0f}")
    print(f"  Position Size: ${args.position_size:,.0f}")
    print(f"  Leverage:      {args.leverage}x")
    print(f"  Max Positions: {args.max_positions}")
    print("=" * 60)
    print()

    print("Loading data...")
    data = load_data(symbols)
    if not data:
        print("ERROR: No data loaded. Run download_data.py first:")
        print("  python backtesting/download_data.py")
        sys.exit(1)
    print(f"\nLoaded {len(data)} symbols\n")

    print("Running backtest...")
    start_time = time.time()

    engine = BacktestEngine(
        initial_capital=args.capital,
        position_size=args.position_size,
        leverage=args.leverage,
        max_positions=args.max_positions,
    )
    trades, equity_curve = engine.run(data)

    elapsed = time.time() - start_time
    print(f"Backtest completed in {elapsed:.1f}s")

    stats = calculate_stats(trades, equity_curve, args.capital)
    print_report(stats)
    export_csv(trades, equity_curve)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Compare trend-filter timeframes: daily(20) vs 6H(20) vs 6H(80).

Same hourly entry signal across all three; only the trend-filter
timeframe + lookback period change. Stop checks run on 1-minute grid.
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


def resample_to_6h(hourly_df: pd.DataFrame) -> pd.DataFrame:
    """Resample 1H bars into 6H bars aligned to UTC 0/6/12/18."""
    df = hourly_df.copy()
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("ts")
    out = df.resample("6h", label="left", closed="left").agg({
        "open_time": "first",
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }).dropna(subset=["close"])
    out = out.reset_index(drop=True)
    out["open_time"] = out["open_time"].astype("int64")
    return out


def load_data(symbols: list[str]):
    hd_data = {}
    h6_data = {}
    minute_data = {}
    missing = []
    for s in symbols:
        h = os.path.join(DATA_DIR, f"{s}_1h.csv")
        d = os.path.join(DATA_DIR, f"{s}_1d.csv")
        m = os.path.join(DATA_DIR, f"{s}_1m.csv")
        if not (os.path.exists(h) and os.path.exists(d) and os.path.exists(m)):
            missing.append(s)
            continue
        hourly_df = pd.read_csv(h)
        daily_df = pd.read_csv(d)
        minute_df = pd.read_csv(m)
        hd_data[s] = (hourly_df, daily_df)
        h6_data[s] = (hourly_df, resample_to_6h(hourly_df))
        minute_data[s] = minute_df
    return hd_data, h6_data, minute_data, missing


def run_variant(label, data, minute_data, sma_period, trend_tf_hours, stride=1):
    # Force bb_middle mode (the live mode currently used). Restore after.
    original = config.TREND_FILTER_MODE
    config.TREND_FILTER_MODE = "bb_middle"
    try:
        engine = BacktestEngine(
            initial_capital=config.INITIAL_CAPITAL,
            position_size=config.POSITION_SIZE,
            leverage=config.LEVERAGE,
            max_positions=config.MAX_POSITIONS,
            sma_period=sma_period,
            stop_check_minutes=stride,
            trend_timeframe_hours=trend_tf_hours,
        )
        trades, equity = engine.run(data, minute_data=minute_data)
        return calculate_stats(trades, equity, config.INITIAL_CAPITAL), trades
    finally:
        config.TREND_FILTER_MODE = original


def main():
    print("Loading data...")
    hd_data, h6_data, minute_data, missing = load_data(DEFAULT_SYMBOLS)
    if missing:
        print(f"[WARN] missing for {len(missing)}: {missing[:5]}...")
    print(f"Loaded {len(hd_data)} symbols (with 1H+1D+1m+resampled 6H)")

    earliest_m = min(df["open_time"].iloc[0] for df in minute_data.values())
    latest_m = max(df["open_time"].iloc[-1] for df in minute_data.values())
    HOUR_MS = 3600_000
    for sym in list(hd_data.keys()):
        h, d = hd_data[sym]
        h_trim = h[(h["open_time"] >= earliest_m - HOUR_MS * 24 * 30) &
                   (h["open_time"] <= latest_m + HOUR_MS)]
        hd_data[sym] = (h_trim.reset_index(drop=True), d)
        h2, h6 = h6_data[sym]
        h2_trim = h2[(h2["open_time"] >= earliest_m - HOUR_MS * 24 * 30) &
                     (h2["open_time"] <= latest_m + HOUR_MS)]
        h6_data[sym] = (h2_trim.reset_index(drop=True), h6)

    from datetime import datetime, timezone, timedelta
    TZ_CN = timezone(timedelta(hours=8))
    print(f"Window: {datetime.fromtimestamp(earliest_m/1000, tz=TZ_CN):%Y-%m-%d %H:%M} → "
          f"{datetime.fromtimestamp(latest_m/1000, tz=TZ_CN):%Y-%m-%d %H:%M} (CN)")
    print()

    variants = [
        ("daily(20)", hd_data, 20, 24),
        ("6H(20)",    h6_data, 20, 6),
        ("6H(80)",    h6_data, 80, 6),
    ]
    results = {}
    for label, data, sma_p, tf_h in variants:
        print(f"[{label}] running...")
        t0 = time.time()
        stats, trades = run_variant(label, data, minute_data, sma_p, tf_h)
        elapsed = time.time() - t0
        print(f"[{label}] done in {elapsed:.1f}s — {len(trades)} trades")
        results[label] = (stats, trades)

    print()
    print("=" * 78)
    cols = list(results.keys())
    print(f"{'metric':<22}" + "".join(f"{c:>18}" for c in cols))
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
        for c in cols:
            v = results[c][0].get(key, 0)
            row += f"{fmt.format(v):>18}"
        print(row)
    print("=" * 78)

    print()
    print("Exit-reason mix:")
    for c in cols:
        reasons = {}
        for t in results[c][1]:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        print(f"  {c:<10} {reasons}")

    print()
    print("Direction split (LONG / SHORT):")
    for c in cols:
        L = [t for t in results[c][1] if t.side == "LONG"]
        S = [t for t in results[c][1] if t.side == "SHORT"]
        lw = sum(1 for t in L if t.pnl > 0) / len(L) if L else 0
        sw = sum(1 for t in S if t.pnl > 0) / len(S) if S else 0
        lp = sum(t.pnl for t in L)
        sp = sum(t.pnl for t in S)
        print(f"  {c:<10} LONG {len(L):>4} / {lw:.1%} / ${lp:+.0f}   "
              f"SHORT {len(S):>4} / {sw:.1%} / ${sp:+.0f}")

    print()
    print("Monthly PnL:")
    print(f"{'month':<10}" + "".join(f"{c:>16}" for c in cols))
    months = sorted({pd.to_datetime(t.closed_at).to_period("M")
                     for c in cols for t in results[c][1]})
    for month in months:
        row = f"{str(month):<10}"
        for c in cols:
            month_pnl = sum(t.pnl for t in results[c][1]
                            if pd.to_datetime(t.closed_at).to_period("M") == month)
            row += f"${month_pnl:>15.2f}"
        print(row)


if __name__ == "__main__":
    main()

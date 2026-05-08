#!/usr/bin/env python3
"""Same as compare_trend_timeframe.py but loads 1m kline data from
crypto_1m.kline_1m on Aliyun RDS and derives 1H / 6H / 1D bars in pandas.

Usage:
    python -m backtesting.compare_trend_timeframe_db
"""
import os
import sys
import time

import pandas as pd
import pymysql

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.engine import BacktestEngine
from backtesting.report import calculate_stats
from config import config

# DB 连接信息从 .env 读取（KLINE_DB_HOST / KLINE_DB_USER / KLINE_DB_PASSWORD ...）
DB_CFG = dict(
    host=os.getenv("KLINE_DB_HOST", ""),
    port=int(os.getenv("KLINE_DB_PORT", "3306")),
    user=os.getenv("KLINE_DB_USER", ""),
    password=os.getenv("KLINE_DB_PASSWORD", ""),
    database=os.getenv("KLINE_DB_NAME", "crypto_1m"),
    connect_timeout=15,
)
if not DB_CFG["host"] or not DB_CFG["user"]:
    raise SystemExit(
        "请在 .env 中配置 KLINE_DB_HOST / KLINE_DB_USER / KLINE_DB_PASSWORD"
    )

CACHE_DIR = os.path.join(os.path.dirname(__file__), "..", "data", "_db_cache")
os.makedirs(CACHE_DIR, exist_ok=True)

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "UNIUSDT", "LTCUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "FILUSDT",
    "INJUSDT", "SUIUSDT", "SEIUSDT",
    "WLDUSDT", "PENDLEUSDT", "FETUSDT", "RUNEUSDT",
]  # 24 symbols (DB 中可用的子集)


def to_db_symbol(s: str) -> str:
    return s[:-4] + "/USDT" if s.endswith("USDT") else s


def fetch_1m(conn, symbol: str) -> pd.DataFrame:
    cache = os.path.join(CACHE_DIR, f"{symbol}_1m.parquet")
    if os.path.exists(cache):
        return pd.read_parquet(cache)
    df = pd.read_sql(
        "SELECT open_time, open, high, low, close, volume FROM kline_1m "
        "WHERE symbol=%s ORDER BY open_time",
        conn, params=(to_db_symbol(symbol),),
    )
    # convert datetime → epoch ms (match CSV format)
    df["open_time"] = (df["open_time"].astype("int64") // 10**6)
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = df[c].astype(float)
    df.to_parquet(cache, index=False)
    return df


def aggregate(df_1m: pd.DataFrame, freq: str) -> pd.DataFrame:
    """Resample 1m → freq (e.g. '1h', '6h', '1D'). Aligned to UTC clock."""
    df = df_1m.copy()
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    df = df.set_index("ts")
    out = df.resample(freq, label="left", closed="left").agg({
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


def run_variant(label, data, minute_data, sma_period, trend_tf_hours, stride=1):
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
    print("Connecting to RDS...")
    conn = pymysql.connect(**DB_CFG)

    print(f"Fetching 1m + aggregating for {len(DEFAULT_SYMBOLS)} symbols...")
    minute_data = {}
    hd_data = {}      # symbol → (1H df, 1D df)
    h6_data = {}      # symbol → (1H df, 6H df)
    t0 = time.time()
    for i, sym in enumerate(DEFAULT_SYMBOLS, 1):
        df_1m = fetch_1m(conn, sym)
        df_1h = aggregate(df_1m, "1h")
        df_6h = aggregate(df_1m, "6h")
        df_1d = aggregate(df_1m, "1D")
        minute_data[sym] = df_1m
        hd_data[sym] = (df_1h, df_1d)
        h6_data[sym] = (df_1h, df_6h)
        print(f"  [{i}/{len(DEFAULT_SYMBOLS)}] {sym}: 1m={len(df_1m)}, 1h={len(df_1h)}, 6h={len(df_6h)}, 1d={len(df_1d)}")
    conn.close()
    print(f"Data load + aggregate: {time.time()-t0:.1f}s")
    print()

    # Trim hourly data to the 1m coverage window (some symbols start later)
    earliest_m = min(df["open_time"].iloc[0] for df in minute_data.values())
    latest_m = max(df["open_time"].iloc[-1] for df in minute_data.values())
    HOUR_MS = 3600_000
    for sym in DEFAULT_SYMBOLS:
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
        print(f"[{label}] done in {time.time()-t0:.1f}s — {len(trades)} trades")
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
    print("Direction split:")
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

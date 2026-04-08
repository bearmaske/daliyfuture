#!/usr/bin/env python3
"""Run multiple backtest configurations and generate a comparison report."""
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.download_data import DEFAULT_SYMBOLS, DATA_DIR
from backtesting.engine import BacktestEngine
from backtesting.report import calculate_stats
from config import config


CONFIGS = [
    {"name": "SMA20 无波动率过滤", "trend_mode": "sma", "sma_period": 20, "vol_filter": False},
    {"name": "SMA20 + 波动率1.0", "trend_mode": "sma", "sma_period": 20, "vol_filter": True, "vol_threshold": 1.0},
    {"name": "SMA20 + 波动率0.8", "trend_mode": "sma", "sma_period": 20, "vol_filter": True, "vol_threshold": 0.8},
    {"name": "SMA20 + 波动率1.2", "trend_mode": "sma", "sma_period": 20, "vol_filter": True, "vol_threshold": 1.2},
]


def load_data(symbols: list[str]) -> dict[str, tuple[pd.DataFrame, pd.DataFrame]]:
    data = {}
    for symbol in symbols:
        h_path = os.path.join(DATA_DIR, f"{symbol}_1h.csv")
        d_path = os.path.join(DATA_DIR, f"{symbol}_1d.csv")
        if not os.path.exists(h_path) or not os.path.exists(d_path):
            continue
        data[symbol] = (pd.read_csv(h_path), pd.read_csv(d_path))
    return data


def per_symbol_stats(trades) -> list[dict]:
    """Calculate per-symbol stats from trades."""
    from collections import defaultdict
    by_symbol = defaultdict(list)
    for t in trades:
        by_symbol[t.symbol].append(t.pnl)
    result = []
    for symbol, pnls in sorted(by_symbol.items(), key=lambda x: sum(x[1]), reverse=True):
        wins = [p for p in pnls if p > 0]
        result.append({
            "symbol": symbol,
            "trades": len(pnls),
            "total_pnl": sum(pnls),
            "avg_pnl": sum(pnls) / len(pnls),
            "win_rate": len(wins) / len(pnls) * 100 if pnls else 0,
        })
    return result


def run_one(cfg: dict, data: dict) -> dict:
    """Run a single backtest config, return stats dict."""
    # Temporarily override config
    old_mode = config.TREND_FILTER_MODE
    old_sma = config.SMA_PERIOD
    old_vol = config.VOL_FILTER_ENABLED
    old_vol_thresh = config.VOL_ATR_THRESHOLD
    config.TREND_FILTER_MODE = cfg["trend_mode"]
    config.SMA_PERIOD = cfg["sma_period"]
    config.VOL_FILTER_ENABLED = cfg.get("vol_filter", False)
    config.VOL_ATR_THRESHOLD = cfg.get("vol_threshold", 1.0)

    try:
        engine = BacktestEngine(
            initial_capital=config.INITIAL_CAPITAL,
            position_size=config.POSITION_SIZE,
            leverage=config.LEVERAGE,
            max_positions=config.MAX_POSITIONS,
            sma_period=cfg["sma_period"],
        )
        trades, equity_curve = engine.run(data)
        stats = calculate_stats(trades, equity_curve, config.INITIAL_CAPITAL)
        stats["name"] = cfg["name"]
        stats["per_symbol"] = per_symbol_stats(trades)
        return stats
    finally:
        config.TREND_FILTER_MODE = old_mode
        config.SMA_PERIOD = old_sma
        config.VOL_FILTER_ENABLED = old_vol
        config.VOL_ATR_THRESHOLD = old_vol_thresh


def format_report(results: list[dict]) -> str:
    lines = []
    lines.append("# 回测对比报告")
    lines.append("")
    lines.append(f"**回测区间:** 2025-04-04 ~ 2026-04-04 (1年)")
    lines.append(f"**参数:** 初始资金 ${config.INITIAL_CAPITAL:,.0f} | "
                 f"每笔保证金 ${config.POSITION_SIZE:,.0f} | "
                 f"杠杆 {config.LEVERAGE}x | 最大持仓 {config.MAX_POSITIONS}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Summary table
    lines.append("## 总体对比")
    lines.append("")
    lines.append("| 指标 | " + " | ".join(r["name"] for r in results) + " |")
    lines.append("|------|" + "|".join("------:" for _ in results) + "|")

    rows = [
        ("总PnL", lambda r: f"${r.get('total_pnl', 0):,.2f}"),
        ("总收益率", lambda r: f"{r.get('total_return_pct', 0):.2f}%"),
        ("最大回撤", lambda r: f"{r.get('max_drawdown_pct', 0):.2f}%"),
        ("夏普比率", lambda r: f"{r.get('sharpe_ratio', 0):.2f}"),
        ("总交易数", lambda r: f"{r.get('total_trades', 0):,}"),
        ("胜率", lambda r: f"{r.get('win_rate', 0) * 100:.1f}%"),
        ("盈亏比", lambda r: f"{r.get('profit_factor', 0):.2f}"),
        ("平均盈利", lambda r: f"${r.get('avg_win', 0):,.2f}"),
        ("平均亏损", lambda r: f"${r.get('avg_loss', 0):,.2f}"),
        ("平均持仓时间", lambda r: f"{r.get('avg_hold_hours', 0):.1f}h"),
    ]

    for label, fmt in rows:
        lines.append(f"| {label} | " + " | ".join(fmt(r) for r in results) + " |")

    lines.append("")
    lines.append("---")
    lines.append("")

    # Per-symbol breakdown for each config
    for r in results:
        lines.append(f"## {r['name']} — 各币种表现")
        lines.append("")
        symbol_stats = r.get("per_symbol", [])
        if symbol_stats:
            lines.append("| Symbol | Trades | Total PnL | Avg PnL | Win% |")
            lines.append("|--------|-------:|----------:|--------:|-----:|")
            for s in symbol_stats:
                lines.append(
                    f"| {s['symbol']} | {s['trades']} | {s['total_pnl']:,.2f} | "
                    f"{s['avg_pnl']:.2f} | {s['win_rate']:.1f}% |"
                )
        lines.append("")

    return "\n".join(lines)


def main():
    print("Loading data...")
    data = load_data(DEFAULT_SYMBOLS)
    if not data:
        print("ERROR: No data. Run download_data.py first.")
        sys.exit(1)
    print(f"Loaded {len(data)} symbols\n")

    results = []
    for cfg in CONFIGS:
        print(f"Running: {cfg['name']}...")
        t0 = time.time()
        stats = run_one(cfg, data)
        elapsed = time.time() - t0
        print(f"  Done in {elapsed:.1f}s — {stats.get('total_trades', 0)} trades, "
              f"PnL: ${stats.get('total_pnl', 0):,.2f}")
        results.append(stats)

    report = format_report(results)

    out_path = os.path.join(os.path.dirname(__file__), "..", "docs", "backtest-comparison.md")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as f:
        f.write(report)

    print(f"\nReport saved to {out_path}")
    print("\n" + report)


if __name__ == "__main__":
    main()

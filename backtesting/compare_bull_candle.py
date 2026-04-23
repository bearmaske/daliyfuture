"""Compare backtest with/without LONG_REQUIRE_BULL_CANDLE filter."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import config, Config  # noqa: E402
from backtesting.backtest import load_data, DEFAULT_SYMBOLS  # noqa: E402
from backtesting.engine import BacktestEngine  # noqa: E402
from backtesting.report import calculate_stats  # noqa: E402


def _run(symbols, data, tag: str, flag: bool):
    config.LONG_REQUIRE_BULL_CANDLE = flag
    Config.LONG_REQUIRE_BULL_CANDLE = flag
    t0 = time.time()
    engine = BacktestEngine(
        initial_capital=Config.INITIAL_CAPITAL,
        position_size=Config.POSITION_SIZE,
        leverage=Config.LEVERAGE,
        max_positions=Config.MAX_POSITIONS,
    )
    trades, equity = engine.run(data)
    stats = calculate_stats(trades, equity, Config.INITIAL_CAPITAL)
    elapsed = time.time() - t0

    longs = [t for t in trades if t.side == "LONG"]
    shorts = [t for t in trades if t.side == "SHORT"]
    long_pnl = sum(t.pnl for t in longs)
    short_pnl = sum(t.pnl for t in shorts)
    long_wins = sum(1 for t in longs if t.pnl > 0)
    short_wins = sum(1 for t in shorts if t.pnl > 0)

    print()
    print(f"=== {tag} (LONG_REQUIRE_BULL_CANDLE={flag}) in {elapsed:.1f}s ===")
    print(f"  Total PnL     : ${stats['total_pnl']:>10,.2f}  ({stats['total_return_pct']:+.2f}%)")
    print(f"  Trades        : {stats['total_trades']}")
    print(f"  Win rate      : {stats['win_rate']*100:.1f}%")
    print(f"  Max drawdown  : {stats['max_drawdown_pct']:.2f}%")
    print(f"  Sharpe        : {stats['sharpe_ratio']:.2f}")
    print(f"  LONG  : {len(longs):4d} trades, PnL ${long_pnl:>10,.2f}, "
          f"win {long_wins/len(longs)*100 if longs else 0:.1f}%")
    print(f"  SHORT : {len(shorts):4d} trades, PnL ${short_pnl:>10,.2f}, "
          f"win {short_wins/len(shorts)*100 if shorts else 0:.1f}%")
    return stats, longs, shorts


def main():
    symbols = DEFAULT_SYMBOLS
    print(f"Loading data for {len(symbols)} symbols...")
    data = load_data(symbols)
    print(f"Loaded {len(data)} symbols.")

    base_stats, base_l, base_s = _run(symbols, data, "BASELINE", False)
    filt_stats, filt_l, filt_s = _run(symbols, data, "WITH FILTER", True)

    print()
    print("=== DELTA (filter vs baseline) ===")
    print(f"  PnL           : ${filt_stats['total_pnl'] - base_stats['total_pnl']:+,.2f}")
    print(f"  Return        : {filt_stats['total_return_pct'] - base_stats['total_return_pct']:+.2f}pp")
    print(f"  Trades        : {filt_stats['total_trades'] - base_stats['total_trades']:+d}")
    print(f"  Win rate      : {(filt_stats['win_rate'] - base_stats['win_rate'])*100:+.2f}pp")
    print(f"  Max DD        : {filt_stats['max_drawdown_pct'] - base_stats['max_drawdown_pct']:+.2f}pp")
    print(f"  Sharpe        : {filt_stats['sharpe_ratio'] - base_stats['sharpe_ratio']:+.2f}")
    print(f"  LONG PnL Δ    : ${sum(t.pnl for t in filt_l) - sum(t.pnl for t in base_l):+,.2f} "
          f"({len(filt_l) - len(base_l):+d} trades)")
    print(f"  SHORT PnL Δ   : ${sum(t.pnl for t in filt_s) - sum(t.pnl for t in base_s):+,.2f} "
          f"({len(filt_s) - len(base_s):+d} trades)")


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Dissect phase-filter backtest results: is any edge real, or another
microcap-pump / long-beta artifact?

Reads results/{pf,pf_baseline}_trades.csv (phase-filter variants) and
results/trades.csv (original live strategy) plus data/BTCUSDT_1h.csv for a
buy-and-hold beta benchmark. Prints, per variant:
  - per-symbol PnL + concentration (top-N share, edge with top-3 removed)
  - long vs short split
  - monthly PnL (front/back-loaded => beta suspicion)
  - largest single trade as a share of total PnL
"""
import os
import sys

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULTS = os.path.join(ROOT, "results")
DATA = os.path.join(ROOT, "data")

VARIANTS = [
    ("PHASE FILTER + NEW EXITS", "pf_trades.csv"),
    ("NEW EXITS ONLY (no phase)", "pf_baseline_trades.csv"),
    ("ORIGINAL LIVE STRATEGY",    "trades.csv"),
]


def btc_buy_and_hold():
    p = os.path.join(DATA, "BTCUSDT_1h.csv")
    if not os.path.exists(p):
        return None
    df = pd.read_csv(p).sort_values("open_time")
    first, last = float(df.iloc[0]["close"]), float(df.iloc[-1]["close"])
    span_days = (df.iloc[-1]["open_time"] - df.iloc[0]["open_time"]) / 86_400_000
    return first, last, (last / first - 1) * 100, span_days


def analyze(label, fname):
    path = os.path.join(RESULTS, fname)
    print("\n" + "=" * 64)
    print(f"  {label}   [{fname}]")
    print("=" * 64)
    if not os.path.exists(path):
        print("  (no results file — run not finished or failed)")
        return
    df = pd.read_csv(path)
    if df.empty:
        print("  (zero trades)")
        return

    total = df["pnl"].sum()
    n = len(df)
    wins = df[df["pnl"] > 0]
    print(f"  Trades: {n} | Total PnL: ${total:+,.2f} | "
          f"Win rate: {len(wins)/n*100:.1f}% | "
          f"Distinct symbols: {df['symbol'].nunique()}")

    # ── per-symbol concentration ──────────────────────────────
    by_sym = df.groupby("symbol")["pnl"].agg(["sum", "count"]).sort_values("sum", ascending=False)
    print("\n  Top 5 symbols by PnL:")
    for sym, row in by_sym.head(5).iterrows():
        print(f"    {sym:<14} ${row['sum']:+10,.2f}  ({int(row['count'])} trades)")
    print("  Bottom 3 symbols by PnL:")
    for sym, row in by_sym.tail(3).iterrows():
        print(f"    {sym:<14} ${row['sum']:+10,.2f}  ({int(row['count'])} trades)")

    pos = by_sym[by_sym["sum"] > 0]["sum"].sum()
    if total > 0:
        top1 = by_sym["sum"].iloc[0]
        top3 = by_sym["sum"].head(3).sum()
        print(f"\n  Concentration: top-1 symbol = {top1/total*100:.0f}% of total PnL | "
              f"top-3 = {top3/total*100:.0f}%")
        ex_top3 = total - top3
        print(f"  Edge with top-3 winners REMOVED: ${ex_top3:+,.2f}  "
              f"({'SURVIVES' if ex_top3 > 0 else 'COLLAPSES'})")

    # ── largest single trade ──────────────────────────────────
    big = df.loc[df["pnl"].idxmax()]
    print(f"\n  Largest single trade: {big['symbol']} {big['side']} "
          f"${big['pnl']:+,.2f} ({big['pnl']/total*100:.0f}% of total) "
          f"{big['opened_at']} -> {big['closed_at']}")

    # ── long vs short ─────────────────────────────────────────
    for side in ("LONG", "SHORT"):
        s = df[df["side"] == side]
        if len(s):
            print(f"  {side:<6} {len(s):4d} trades  ${s['pnl'].sum():+10,.2f}  "
                  f"(win {len(s[s['pnl']>0])/len(s)*100:.0f}%)")

    # ── monthly distribution (beta detector) ──────────────────
    df["month"] = pd.to_datetime(df["closed_at"]).dt.to_period("M")
    by_month = df.groupby("month")["pnl"].sum()
    print("\n  Monthly PnL:")
    for m, v in by_month.items():
        bar = "#" * min(40, int(abs(v) / 50))
        print(f"    {m}  ${v:+9,.2f}  {bar}")
    pos_m = (by_month > 0).sum()
    print(f"  Profitable months: {pos_m}/{len(by_month)}")


def main():
    bh = btc_buy_and_hold()
    if bh:
        first, last, ret, days = bh
        print(f"\n  BETA BENCHMARK — BTC buy & hold over {days:.0f} days: "
              f"{first:,.0f} -> {last:,.0f} = {ret:+.1f}%")
    for label, fname in VARIANTS:
        analyze(label, fname)


if __name__ == "__main__":
    main()

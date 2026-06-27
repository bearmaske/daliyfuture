#!/usr/bin/env python3
"""
Phase Filter Backtest

Layered on top of the original Trend Sniper entry signals, adds:

  1. Daily phase gate (BB 20,2):
       UP phase:   daily close > upper BB  →  start;  daily close < middle BB  →  end
       DOWN phase: daily close < lower BB  →  start;  daily close > middle BB  →  end
       LONG only permitted in UP phase; SHORT only permitted in DOWN phase.

  2. First-trade-per-phase filter:
       Only the first LONG per (symbol, UP-phase-id) is executed.
       Only the first SHORT per (symbol, DOWN-phase-id) is executed.
       Counter resets when a new phase begins for that symbol.

  3. New exit logic (replaces ATR-dual stops entirely):
       LONG  exit: 1H close < 1H BB middle(20,2)
                   OR  close  ≤  pre-bar-high × (1 − 3.5%)
       SHORT exit: 1H close > 1H BB middle(20,2)
                   OR  close  ≥  pre-bar-low  × (1 + 3.5%)

       "pre-bar extreme" = max HIGH of completed bars before the current bar (LONG)
                           or min LOW  of completed bars before the current bar (SHORT).
       The current bar's own high/low is NOT used for the 3.5% trigger — this avoids
       same-bar "new high then immediate retrace" ambiguity at 1H resolution.

Entry signals are identical to the live bot (daily bb_middle trend filter + 1H BB
breakout + 24H high/low confirmation). Entry sizing follows atr_dual (same formula
as the live bot: notional = RISK_PER_TRADE_USD / soft_stop_pct, capped at
MAX_NOTIONAL_USD). The 6H middle filter is not applied (same as the existing
backtesting/engine.py).

Usage:
    python -m backtesting.phase_filter_backtest
    python -m backtesting.phase_filter_backtest --no-phase-filter   # new exits only
    python -m backtesting.phase_filter_backtest --symbols BTCUSDT,ETHUSDT
"""

import argparse
import bisect
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import config
from strategy import calculate_bollinger_bands, check_trend_bb_middle
from risk import calculate_atr, compute_stop_distances
from backtesting.engine import apply_slippage, calculate_fee, BacktestTrade
from backtesting.download_data import DEFAULT_SYMBOLS, DATA_DIR
from backtesting.report import calculate_stats, print_report

TZ_CN = timezone(timedelta(hours=8))
DAILY_MS = 86_400_000

DAILY_BB_PERIOD = 20
DAILY_BB_STD = 2.0
H1_BB_PERIOD = config.BB_PERIOD   # 20
H1_BB_STD = config.BB_STD         # 2.0
TRAILING_PCT = 0.035               # 3.5%

RESULTS_DIR = os.path.join(os.path.dirname(__file__), "..", "results")


# ─────────────────────────────────────────────────────────────
# Position
# ─────────────────────────────────────────────────────────────

@dataclass
class PFPosition:
    symbol: str
    side: str          # "LONG" or "SHORT"
    entry_price: float
    quantity: float
    notional: float
    margin: float
    opened_at: str
    opened_ms: int     # open_time (ms) of the entry bar
    phase_id: int
    extreme_price: float = 0.0   # max HIGH (LONG) or min LOW (SHORT) from completed bars

    def __post_init__(self):
        if self.extreme_price == 0.0:
            self.extreme_price = self.entry_price


# ─────────────────────────────────────────────────────────────
# Phase timeline
# ─────────────────────────────────────────────────────────────

def compute_phase_timeline(
    daily_df: pd.DataFrame,
) -> Tuple[List[int], List[Tuple[Optional[str], int]]]:
    """
    Replay daily bars to produce a phase timeline.

    Returns
    -------
    close_times : sorted list of ints
        Millisecond timestamp when each bar's close becomes available
        (open_time + DAILY_MS).
    phases : list of (phase_str, phase_id)
        Phase active immediately after each bar closes.
        phase_str ∈ {"UP", "DOWN", None}.

    Query with query_phase(close_times, phases, h_ts) — O(log n).
    """
    closes = daily_df["close"].astype(float).tolist()
    close_times: List[int] = (daily_df["open_time"].astype(int) + DAILY_MS).tolist()

    phase: Optional[str] = None
    phase_id = 0
    phases: List[Tuple[Optional[str], int]] = []

    for i, close in enumerate(closes):
        if i + 1 < DAILY_BB_PERIOD:
            phases.append((None, 0))
            continue

        # Standard TA: BB at bar i uses bars [i−period+1 .. i] inclusive
        window = closes[i - DAILY_BB_PERIOD + 1: i + 1]
        upper, middle, lower = calculate_bollinger_bands(window, DAILY_BB_PERIOD, DAILY_BB_STD)

        # Phase end check first (so same-bar transitions work cleanly)
        if phase == "UP" and close < middle:
            phase = None
        elif phase == "DOWN" and close > middle:
            phase = None

        # Phase start (only when no phase is currently active)
        if phase is None:
            if close > upper:
                phase = "UP"
                phase_id += 1
            elif close < lower:
                phase = "DOWN"
                phase_id += 1

        phases.append((phase, phase_id))

    return close_times, phases


def query_phase(
    close_times: List[int],
    phases: List[Tuple[Optional[str], int]],
    h_ts: int,
) -> Tuple[Optional[str], int]:
    """
    Return (phase_str, phase_id) active at hourly timestamp h_ts.

    A daily bar with close_time C becomes available when h_ts >= C.
    Binary search → O(log n).
    """
    idx = bisect.bisect_right(close_times, h_ts) - 1
    if idx < 0:
        return None, 0
    return phases[idx]


# ─────────────────────────────────────────────────────────────
# Engine
# ─────────────────────────────────────────────────────────────

class PhaseFilterEngine:
    def __init__(
        self,
        initial_capital: float = config.INITIAL_CAPITAL,
        leverage: int = config.LEVERAGE,
        max_positions: int = config.MAX_POSITIONS,
        trailing_pct: float = TRAILING_PCT,
        apply_phase_filter: bool = True,
    ):
        self.initial_capital = initial_capital
        self.balance = initial_capital
        self.leverage = leverage
        self.max_positions = max_positions
        self.trailing_pct = trailing_pct
        self.apply_phase_filter = apply_phase_filter

        self.positions: List[PFPosition] = []
        self.trades: List[BacktestTrade] = []
        self.equity_curve: List[dict] = []

        # {(symbol, phase_id)} pairs where we already opened a trade this phase
        self._traded_phases: set = set()

        # Counters for diagnostic output
        self.filtered_by_phase: int = 0
        self.filtered_by_first_trade: int = 0

    # ── main loop ────────────────────────────────────────────

    def run(
        self,
        data: Dict[str, Tuple[pd.DataFrame, pd.DataFrame]],
    ) -> Tuple[List[BacktestTrade], List[dict]]:
        """
        Parameters
        ----------
        data : {symbol: (hourly_df, daily_df)}
            hourly_df / daily_df columns: open_time, open, high, low, close, volume, …
        """
        timelines: Dict[str, Tuple[List[int], List]] = {}
        if self.apply_phase_filter:
            for sym, (_, daily_df) in data.items():
                timelines[sym] = compute_phase_timeline(daily_df)

        all_ts: set = set()
        for _, (h, _) in data.items():
            all_ts.update(h["open_time"].astype(int).tolist())
        timeline = sorted(all_ts)

        for ts in timeline:
            # 1. Exit check at the close of bar ts
            self._check_exits(ts, data)

            # 2. Entry check — signal built from bars < ts, execute at bar ts open
            if len(self.positions) < self.max_positions and self.balance > 0:
                for sym, (hourly_df, daily_df) in data.items():
                    if len(self.positions) >= self.max_positions:
                        break
                    if any(p.symbol == sym for p in self.positions):
                        continue

                    if self.apply_phase_filter:
                        ct, ph = timelines[sym]
                        phase, phase_id = query_phase(ct, ph, ts)
                    else:
                        phase, phase_id = "ANY", 0

                    self._check_entry(sym, ts, hourly_df, daily_df, phase, phase_id)

            # 3. Equity snapshot at bar ts close
            self.equity_curve.append({
                "timestamp": ts,
                "equity": self._calc_equity(ts, data),
            })

        if timeline:
            self._close_remaining(timeline[-1], data)

        return self.trades, self.equity_curve

    # ── entry ────────────────────────────────────────────────

    def _check_entry(
        self,
        symbol: str,
        current_ts: int,
        hourly_df: pd.DataFrame,
        daily_df: pd.DataFrame,
        phase: Optional[str],
        phase_id: int,
    ):
        # ── 1H bars strictly before this bar ──────────────────
        h_mask = hourly_df["open_time"].astype(int) < current_ts
        h_closed = hourly_df[h_mask]
        if len(h_closed) < H1_BB_PERIOD + 1:
            return

        hourly_closes = h_closed["close"].astype(float).tolist()

        # ── Daily trend: bb_middle mode (mirrors live default) ─
        d_mask = (daily_df["open_time"].astype(int) + DAILY_MS) <= current_ts
        d_closed = daily_df[d_mask]
        if len(d_closed) < config.SMA_PERIOD:
            return
        daily_closes = d_closed["close"].astype(float).tolist()
        trend = check_trend_bb_middle(daily_closes, config.SMA_PERIOD, config.BB_STD)
        if trend is None:
            return

        # ── 1H Bollinger Bands ─────────────────────────────────
        h_upper, _, h_lower = calculate_bollinger_bands(hourly_closes, H1_BB_PERIOD, H1_BB_STD)
        last_close = hourly_closes[-1]

        # ── 24H high/low confirmation ──────────────────────────
        if len(h_closed) >= 25:
            sig = h_closed.iloc[-1]
            sig_high = float(sig["high"])
            sig_low = float(sig["low"])
            lb = h_closed.iloc[-25:-1]
            is_24h_high = sig_high >= float(lb["high"].max())
            is_24h_low = sig_low <= float(lb["low"].min())
        else:
            is_24h_high = is_24h_low = False

        # ── Signal direction ───────────────────────────────────
        if trend == "LONG" and last_close > h_upper and is_24h_high:
            direction = "LONG"
        elif trend == "SHORT" and last_close < h_lower and is_24h_low:
            direction = "SHORT"
        else:
            return

        # ── Phase gate ─────────────────────────────────────────
        if self.apply_phase_filter:
            if direction == "LONG" and phase != "UP":
                self.filtered_by_phase += 1
                return
            if direction == "SHORT" and phase != "DOWN":
                self.filtered_by_phase += 1
                return
            if (symbol, phase_id) in self._traded_phases:
                self.filtered_by_first_trade += 1
                return

        # ── Entry price: next bar's open + slippage ────────────
        cur_bar = hourly_df[hourly_df["open_time"].astype(int) == current_ts]
        if cur_bar.empty:
            return
        exec_price = float(cur_bar.iloc[0]["open"])
        exec_price = apply_slippage(exec_price, direction, is_entry=True)

        # ── Sizing: atr_dual (same formula as live bot) ────────
        atr = calculate_atr(
            h_closed["high"].astype(float).tolist(),
            h_closed["low"].astype(float).tolist(),
            h_closed["close"].astype(float).tolist(),
            config.ATR_PERIOD,
        )
        soft_pct, _ = compute_stop_distances(atr, exec_price)
        notional = min(config.RISK_PER_TRADE_USD / soft_pct, config.MAX_NOTIONAL_USD)
        margin = notional / self.leverage

        if self.balance < margin:
            return

        quantity = notional / exec_price
        entry_fee = calculate_fee(notional)
        ts_str = datetime.fromtimestamp(current_ts / 1000, tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")

        pos = PFPosition(
            symbol=symbol,
            side=direction,
            entry_price=exec_price,
            quantity=quantity,
            notional=notional,
            margin=margin,
            opened_at=ts_str,
            opened_ms=current_ts,
            phase_id=phase_id,
        )
        self.positions.append(pos)
        self.balance -= margin + entry_fee

        if self.apply_phase_filter:
            self._traded_phases.add((symbol, phase_id))

    # ── exits ────────────────────────────────────────────────

    def _check_exits(self, current_ts: int, data: Dict):
        """Check exit conditions at the close of bar current_ts."""
        to_close = []

        for pos in self.positions:
            if pos.symbol not in data:
                continue
            hourly_df, _ = data[pos.symbol]

            bar = hourly_df[hourly_df["open_time"].astype(int) == current_ts]
            if bar.empty:
                continue
            row = bar.iloc[0]
            bar_close = float(row["close"])
            bar_high = float(row["high"])
            bar_low = float(row["low"])

            # Skip exit on the entry bar — let the position breathe its first bar.
            # Still update extreme so the entry bar's high/low is captured.
            if current_ts <= pos.opened_ms:
                if pos.side == "LONG":
                    pos.extreme_price = max(pos.extreme_price, bar_high)
                else:
                    pos.extreme_price = min(pos.extreme_price, bar_low)
                continue

            # Snapshot extreme BEFORE this bar (for 3.5% trigger; excludes current high/low)
            extreme_before = pos.extreme_price

            triggered = False
            reason = ""

            # ── Exit 1: 1H BB middle crossover ────────────────
            # BB includes the current bar (standard TA: BB at bar N uses last 20 bars
            # up to and including bar N, so the current close is part of the window).
            h_mask = hourly_df["open_time"].astype(int) <= current_ts
            h_upto = hourly_df[h_mask]
            if len(h_upto) >= H1_BB_PERIOD:
                closes_upto = h_upto["close"].astype(float).tolist()
                _, h_mid, _ = calculate_bollinger_bands(closes_upto, H1_BB_PERIOD, H1_BB_STD)
                if pos.side == "LONG" and bar_close < h_mid:
                    triggered, reason = True, "1h_bb_middle"
                elif pos.side == "SHORT" and bar_close > h_mid:
                    triggered, reason = True, "1h_bb_middle"

            # ── Exit 2: 3.5% trailing from pre-bar extreme ────
            # Uses extreme_before (excludes current bar's high/low) to avoid
            # same-bar "new high then immediate retrace" ambiguity.
            if not triggered:
                if pos.side == "LONG" and bar_close <= extreme_before * (1 - self.trailing_pct):
                    triggered, reason = True, "trailing_3.5pct"
                elif pos.side == "SHORT" and bar_close >= extreme_before * (1 + self.trailing_pct):
                    triggered, reason = True, "trailing_3.5pct"

            # Always update extreme with current bar's high/low (after the exit check)
            if pos.side == "LONG":
                pos.extreme_price = max(pos.extreme_price, bar_high)
            else:
                pos.extreme_price = min(pos.extreme_price, bar_low)

            if triggered:
                exit_price = apply_slippage(bar_close, pos.side, is_entry=False)
                to_close.append((pos, exit_price, reason, current_ts))

        for pos, ep, reason, ts in to_close:
            self._close_position(pos, ep, reason, ts)

    def _close_position(self, pos: PFPosition, exit_price: float, reason: str, ts: int):
        n = pos.notional
        if pos.side == "LONG":
            gross = (exit_price - pos.entry_price) / pos.entry_price * n
        else:
            gross = (pos.entry_price - exit_price) / pos.entry_price * n
        exit_fee = calculate_fee(n)
        net_pnl = gross - exit_fee

        ts_str = datetime.fromtimestamp(ts / 1000, tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")
        self.trades.append(BacktestTrade(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            pnl=net_pnl,
            fee=exit_fee * 2,   # entry + exit total (matches existing engine convention)
            opened_at=pos.opened_at,
            closed_at=ts_str,
            exit_reason=reason,
        ))
        self.positions.remove(pos)
        self.balance += pos.margin + net_pnl

    def _close_remaining(self, last_ts: int, data: Dict):
        for pos in list(self.positions):
            if pos.symbol not in data:
                continue
            h, _ = data[pos.symbol]
            if h.empty:
                continue
            last_close = float(h.iloc[-1]["close"])
            ep = apply_slippage(last_close, pos.side, is_entry=False)
            self._close_position(pos, ep, "backtest_end", last_ts)

    def _calc_equity(self, current_ts: int, data: Dict) -> float:
        eq = self.balance
        for pos in self.positions:
            eq += pos.margin
            if pos.symbol not in data:
                continue
            h, _ = data[pos.symbol]
            bar = h[h["open_time"].astype(int) == current_ts]
            if bar.empty:
                continue
            price = float(bar.iloc[0]["close"])
            n = pos.notional
            if pos.side == "LONG":
                eq += (price - pos.entry_price) / pos.entry_price * n
            else:
                eq += (pos.entry_price - price) / pos.entry_price * n
        return eq


# ─────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────

def export_results(
    trades: List[BacktestTrade],
    equity_curve: List[dict],
    prefix: str,
):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    if trades:
        rows = [{
            "symbol": t.symbol, "side": t.side,
            "entry_price": t.entry_price, "exit_price": t.exit_price,
            "quantity": t.quantity, "pnl": round(t.pnl, 4),
            "fee": round(t.fee, 4),
            "opened_at": t.opened_at, "closed_at": t.closed_at,
            "exit_reason": t.exit_reason,
        } for t in trades]
        path = os.path.join(RESULTS_DIR, f"{prefix}_trades.csv")
        pd.DataFrame(rows).to_csv(path, index=False)
        print(f"  Trades  → {path}")
    if equity_curve:
        eq_df = pd.DataFrame(equity_curve)
        eq_df["drawdown"] = eq_df["equity"].cummax() - eq_df["equity"]
        path = os.path.join(RESULTS_DIR, f"{prefix}_equity.csv")
        eq_df.to_csv(path, index=False)
        print(f"  Equity  → {path}")


# ─────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────

def load_data(symbols: List[str]) -> Dict[str, Tuple[pd.DataFrame, pd.DataFrame]]:
    data = {}
    for sym in symbols:
        h_path = os.path.join(DATA_DIR, f"{sym}_1h.csv")
        d_path = os.path.join(DATA_DIR, f"{sym}_1d.csv")
        if not os.path.exists(h_path) or not os.path.exists(d_path):
            print(f"  [SKIP] {sym} — missing CSV (run download_data.py first)")
            continue
        data[sym] = (pd.read_csv(h_path), pd.read_csv(d_path))
    return data


def main():
    parser = argparse.ArgumentParser(description="Phase Filter Backtest")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help="Comma-separated symbol list")
    parser.add_argument("--capital", type=float, default=config.INITIAL_CAPITAL)
    parser.add_argument("--leverage", type=int, default=config.LEVERAGE)
    parser.add_argument("--max-positions", type=int, default=config.MAX_POSITIONS)
    parser.add_argument(
        "--no-phase-filter", action="store_true",
        help="Apply new exits only, without daily-phase or first-trade filters (baseline comparison)",
    )
    args = parser.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",")]
    mode = "NEW EXITS ONLY (no phase filter)" if args.no_phase_filter else "PHASE FILTER + NEW EXITS"

    print("=" * 60)
    print(f"  PHASE FILTER BACKTEST — {mode}")
    print("=" * 60)
    print(f"  Symbols:       {len(symbols)}")
    print(f"  Capital:       ${args.capital:,.0f}")
    print(f"  Leverage:      {args.leverage}x")
    print(f"  Max positions: {args.max_positions}")
    print(f"  Trailing exit: {TRAILING_PCT*100:.1f}% from pre-bar extreme")
    print()

    print("Loading data...")
    data = load_data(symbols)
    if not data:
        print("ERROR: No data loaded. Run: python backtesting/download_data.py")
        sys.exit(1)
    print(f"  Loaded {len(data)} symbols\n")

    engine = PhaseFilterEngine(
        initial_capital=args.capital,
        leverage=args.leverage,
        max_positions=args.max_positions,
        apply_phase_filter=not args.no_phase_filter,
    )

    print("Running backtest...")
    trades, equity_curve = engine.run(data)
    print(f"  Done — {len(trades)} trades\n")

    stats = calculate_stats(trades, equity_curve, args.capital)
    print_report(stats)

    # ── Phase filter summary ──────────────────────────────────
    if engine.apply_phase_filter:
        total_filtered = engine.filtered_by_phase + engine.filtered_by_first_trade
        print(f"\n  Phase filter stats:")
        print(f"    Signals filtered by direction:   {engine.filtered_by_phase}")
        print(f"    Signals filtered by first-trade: {engine.filtered_by_first_trade}")
        print(f"    Total filtered:                  {total_filtered}")
        print(f"    Phase slots used:                {len(engine._traded_phases)}")

    # ── Exit breakdown ────────────────────────────────────────
    exit_counts: dict = {}
    for t in trades:
        exit_counts[t.exit_reason] = exit_counts.get(t.exit_reason, 0) + 1
    if exit_counts:
        print(f"\n  Exit breakdown:")
        total = len(trades)
        for reason, cnt in sorted(exit_counts.items(), key=lambda x: -x[1]):
            print(f"    {reason:<22}  {cnt:5d}  ({cnt/total*100:.1f}%)")

    # ── PnL by direction ─────────────────────────────────────
    long_pnl = sum(t.pnl for t in trades if t.side == "LONG")
    short_pnl = sum(t.pnl for t in trades if t.side == "SHORT")
    long_n = sum(1 for t in trades if t.side == "LONG")
    short_n = sum(1 for t in trades if t.side == "SHORT")
    if trades:
        print(f"\n  PnL by direction:")
        print(f"    LONG  ({long_n:4d} trades):  ${long_pnl:+,.2f}")
        print(f"    SHORT ({short_n:4d} trades):  ${short_pnl:+,.2f}")

    prefix = "pf" if not args.no_phase_filter else "pf_baseline"
    print()
    export_results(trades, equity_curve, prefix)


if __name__ == "__main__":
    main()

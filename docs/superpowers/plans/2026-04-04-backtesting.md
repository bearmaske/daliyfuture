# Backtesting System Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a historical backtesting system that downloads 1 year of Binance kline data for 30 symbols and simulates the existing Trend Sniper strategy with realistic fees, slippage, and capital management.

**Architecture:** Four new modules under `backtesting/` — data downloader, engine, reporter, and CLI entry point. The engine imports pure functions from existing `strategy.py` and `risk.py` to guarantee logic parity with live trading. Data is stored as CSV in `data/`, results output to `results/`.

**Tech Stack:** Python, pandas (already in requirements.txt), python-binance (existing), argparse (stdlib), numpy (existing)

---

## File Structure

| Action | File | Responsibility |
|--------|------|----------------|
| Create | `backtesting/__init__.py` | Package marker |
| Create | `backtesting/download_data.py` | Fetch klines from Binance mainnet, save as CSV |
| Create | `backtesting/engine.py` | Core backtest loop: signal detection, trade simulation, capital tracking |
| Create | `backtesting/report.py` | Calculate stats, print summary, export CSV |
| Create | `backtesting/backtest.py` | CLI entry point, parse args, orchestrate pipeline |
| Create | `tests/test_backtest_engine.py` | Unit tests for engine |
| Create | `tests/test_backtest_report.py` | Unit tests for report |
| Modify | `.gitignore` | Add `data/` and `results/` |

---

### Task 1: Branch, project scaffold, and .gitignore

**Files:**
- Create: `backtesting/__init__.py`
- Modify: `.gitignore`

- [ ] **Step 1: Create feature branch**

```bash
git checkout -b feature/backtesting
```

- [ ] **Step 2: Create backtesting package and directories**

```bash
mkdir -p backtesting data results
touch backtesting/__init__.py
```

- [ ] **Step 3: Update .gitignore**

Append to `.gitignore` (create if not exists):

```
# Backtesting data and results
data/
results/
```

- [ ] **Step 4: Commit scaffold**

```bash
git add backtesting/__init__.py .gitignore
git commit -m "chore: scaffold backtesting package and gitignore data/results"
```

---

### Task 2: Data downloader

**Files:**
- Create: `backtesting/download_data.py`

- [ ] **Step 1: Write download_data.py**

This script fetches 1H and 1D klines for each symbol from Binance mainnet and saves to CSV. Binance returns max 1500 klines per request, so 1 year of 1H data (~8760 bars) needs batched fetching.

```python
"""Download historical klines from Binance mainnet and save as CSV."""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from binance.client import Client

# Add project root to path so we can import config
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

DEFAULT_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
    "DOGEUSDT", "ADAUSDT", "AVAXUSDT", "LINKUSDT", "DOTUSDT",
    "MATICUSDT", "UNIUSDT", "LTCUSDT", "ATOMUSDT", "NEARUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "FILUSDT", "LDOUSDT",
    "INJUSDT", "SUIUSDT", "SEIUSDT", "TIAUSDT", "JUPUSDT",
    "WLDUSDT", "PENDLEUSDT", "STXUSDT", "FETUSDT", "RUNEUSDT",
]

COLUMNS = ["open_time", "open", "high", "low", "close", "volume"]


def fetch_klines_batched(
    client: Client,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    batch_size: int = 1500,
) -> list:
    """Fetch klines in batches of `batch_size`, handling Binance's 1500 limit."""
    all_klines = []
    current_start = start_ms

    while current_start < end_ms:
        try:
            klines = client.futures_klines(
                symbol=symbol,
                interval=interval,
                startTime=current_start,
                endTime=end_ms,
                limit=batch_size,
            )
        except Exception as e:
            print(f"  [WARN] API error for {symbol} {interval}, retrying: {e}")
            time.sleep(5)
            continue

        if not klines:
            break

        all_klines.extend(klines)
        # Next batch starts after the last kline's open_time
        last_open_time = int(klines[-1][0])
        current_start = last_open_time + 1

        if len(klines) < batch_size:
            break

        time.sleep(0.5)  # respect rate limits

    return all_klines


def klines_to_dataframe(klines: list) -> pd.DataFrame:
    """Convert raw Binance kline list to a clean DataFrame."""
    rows = []
    for k in klines:
        rows.append({
            "open_time": int(k[0]),
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
        })
    df = pd.DataFrame(rows, columns=COLUMNS)
    df.drop_duplicates(subset="open_time", inplace=True)
    df.sort_values("open_time", inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


def get_resume_start_ms(filepath: str) -> int | None:
    """If CSV already exists, return the last open_time + 1 for incremental fetch."""
    if not os.path.exists(filepath):
        return None
    try:
        df = pd.read_csv(filepath)
        if df.empty:
            return None
        return int(df["open_time"].iloc[-1]) + 1
    except Exception:
        return None


def download_symbol(
    client: Client, symbol: str, interval: str, start_ms: int, end_ms: int
):
    """Download klines for one symbol+interval, with resume support."""
    os.makedirs(DATA_DIR, exist_ok=True)
    filename = f"{symbol}_{interval}.csv"
    filepath = os.path.join(DATA_DIR, filename)

    resume_ms = get_resume_start_ms(filepath)
    actual_start = resume_ms if resume_ms and resume_ms > start_ms else start_ms

    if actual_start >= end_ms:
        print(f"  {filename}: already up to date, skipping")
        return

    if resume_ms:
        print(f"  {filename}: resuming from {datetime.fromtimestamp(actual_start / 1000, tz=timezone.utc).strftime('%Y-%m-%d')}")

    klines = fetch_klines_batched(client, symbol, interval, actual_start, end_ms)
    if not klines:
        print(f"  {filename}: no new data")
        return

    new_df = klines_to_dataframe(klines)

    # Merge with existing data if resuming
    if resume_ms and os.path.exists(filepath):
        existing_df = pd.read_csv(filepath)
        combined = pd.concat([existing_df, new_df], ignore_index=True)
        combined.drop_duplicates(subset="open_time", inplace=True)
        combined.sort_values("open_time", inplace=True)
        combined.reset_index(drop=True, inplace=True)
        new_df = combined

    new_df.to_csv(filepath, index=False)
    print(f"  {filename}: {len(new_df)} bars saved")


def download_all(symbols: list[str] | None = None, days: int = 365):
    """Download 1H and 1D klines for all symbols."""
    symbols = symbols or DEFAULT_SYMBOLS
    client = Client()

    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp() * 1000)

    print(f"Downloading {len(symbols)} symbols × 2 intervals")
    print(f"Period: {datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')} to {datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d')}")
    print(f"Data dir: {os.path.abspath(DATA_DIR)}")
    print()

    for i, symbol in enumerate(symbols, 1):
        print(f"[{i}/{len(symbols)}] {symbol}")
        download_symbol(client, symbol, Client.KLINE_INTERVAL_1HOUR, start_ms, end_ms)
        download_symbol(client, symbol, Client.KLINE_INTERVAL_1DAY, start_ms, end_ms)
        print()


if __name__ == "__main__":
    download_all()
```

- [ ] **Step 2: Smoke test — run downloader for 1 symbol, 7 days**

```bash
cd /Users/danny/Desktop/code/dabao
python -c "
from backtesting.download_data import download_all
download_all(symbols=['BTCUSDT'], days=7)
"
```

Expected: `data/BTCUSDT_1h.csv` and `data/BTCUSDT_1d.csv` created with data. Print shows bar counts.

- [ ] **Step 3: Commit**

```bash
git add backtesting/download_data.py
git commit -m "feat(backtest): add kline data downloader with batch fetch and resume"
```

---

### Task 3: Backtest engine — core data structures and helpers

**Files:**
- Create: `backtesting/engine.py`
- Create: `tests/test_backtest_engine.py`

- [ ] **Step 1: Write failing tests for engine helpers**

```python
# tests/test_backtest_engine.py
import pytest
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.engine import (
    apply_slippage,
    calculate_fee,
    BacktestPosition,
    BacktestTrade,
)


def test_apply_slippage_long_entry():
    # Long entry: price goes up by slippage
    price = apply_slippage(100.0, side="LONG", is_entry=True, slippage_pct=0.0005)
    assert price == pytest.approx(100.05)


def test_apply_slippage_long_exit():
    # Long exit: price goes down by slippage
    price = apply_slippage(100.0, side="LONG", is_entry=False, slippage_pct=0.0005)
    assert price == pytest.approx(99.95)


def test_apply_slippage_short_entry():
    # Short entry: price goes down by slippage
    price = apply_slippage(100.0, side="SHORT", is_entry=True, slippage_pct=0.0005)
    assert price == pytest.approx(99.95)


def test_apply_slippage_short_exit():
    # Short exit: price goes up by slippage
    price = apply_slippage(100.0, side="SHORT", is_entry=False, slippage_pct=0.0005)
    assert price == pytest.approx(100.05)


def test_calculate_fee():
    # 0.04% taker fee on $2500 notional
    fee = calculate_fee(notional=2500.0, fee_rate=0.0004)
    assert fee == pytest.approx(1.0)


def test_backtest_position_creation():
    pos = BacktestPosition(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=50000.0,
        quantity=0.05,
        opened_at="2025-04-04 12:00:00",
    )
    assert pos.highest_price == 50000.0
    assert pos.lowest_price == 50000.0


def test_backtest_trade_pnl():
    trade = BacktestTrade(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=50000.0,
        exit_price=51000.0,
        quantity=0.05,
        pnl=50.0,
        fee=2.0,
        opened_at="2025-04-04 12:00:00",
        closed_at="2025-04-05 12:00:00",
        exit_reason="atr_stop",
    )
    assert trade.pnl == 50.0
    assert trade.fee == 2.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_backtest_engine.py -v
```

Expected: FAIL — `backtesting.engine` module not found or classes not defined.

- [ ] **Step 3: Write engine.py — data structures and helpers**

```python
# backtesting/engine.py
"""Backtest engine: simulates the Trend Sniper strategy on historical data."""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd

# Add project root to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from strategy import calculate_bollinger_bands, check_trend
from risk import calculate_atr, should_stop_loss

TZ_CN = timezone(timedelta(hours=8))

# --- Fee / slippage constants ---
TAKER_FEE_RATE = 0.0004   # 0.04%
SLIPPAGE_PCT = 0.0005     # 0.05%


@dataclass
class BacktestPosition:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    opened_at: str
    highest_price: float = 0.0
    lowest_price: float = 0.0

    def __post_init__(self):
        if self.highest_price == 0.0:
            self.highest_price = self.entry_price
        if self.lowest_price == 0.0:
            self.lowest_price = self.entry_price


@dataclass
class BacktestTrade:
    symbol: str
    side: str
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    fee: float
    opened_at: str
    closed_at: str
    exit_reason: str


def apply_slippage(price: float, side: str, is_entry: bool, slippage_pct: float = SLIPPAGE_PCT) -> float:
    """Apply slippage: worse price for the trader."""
    if (side == "LONG" and is_entry) or (side == "SHORT" and not is_entry):
        return price * (1 + slippage_pct)
    else:
        return price * (1 - slippage_pct)


def calculate_fee(notional: float, fee_rate: float = TAKER_FEE_RATE) -> float:
    """Calculate trading fee on notional value."""
    return notional * fee_rate
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_backtest_engine.py -v
```

Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backtesting/engine.py tests/test_backtest_engine.py
git commit -m "feat(backtest): add engine data structures and fee/slippage helpers"
```

---

### Task 4: Backtest engine — main simulation loop

**Files:**
- Modify: `backtesting/engine.py`
- Modify: `tests/test_backtest_engine.py`

- [ ] **Step 1: Write failing test for the engine run**

Append to `tests/test_backtest_engine.py`:

```python
from backtesting.engine import BacktestEngine


def _make_kline(open_time_ms, open_p, high, low, close, volume=1000.0):
    """Helper: create a kline row matching Binance format as a dict for DataFrame."""
    return {
        "open_time": open_time_ms,
        "open": open_p,
        "high": high,
        "low": low,
        "close": close,
        "volume": volume,
    }


def test_engine_no_signal_no_trades():
    """Flat data should produce no trades."""
    # 30 days of 1D data, all flat at 100
    base_ms = 1680000000000  # arbitrary start
    day_ms = 86400000
    hour_ms = 3600000

    daily_bars = []
    for i in range(30):
        daily_bars.append(_make_kline(base_ms + i * day_ms, 100, 100, 100, 100))
    daily_df = pd.DataFrame(daily_bars)

    # 30*24 hours of 1H data, all flat at 100
    hourly_bars = []
    for i in range(30 * 24):
        hourly_bars.append(_make_kline(base_ms + i * hour_ms, 100, 100, 100, 100))
    hourly_df = pd.DataFrame(hourly_bars)

    engine = BacktestEngine(
        initial_capital=10000.0,
        position_size=500.0,
        leverage=5,
        max_positions=10,
    )
    trades, equity_curve = engine.run({"TESTUSDT": (hourly_df, daily_df)})

    assert len(trades) == 0
    assert engine.balance == 10000.0


def test_engine_long_signal_opens_and_stops():
    """Rising data should trigger a LONG entry, then an ATR stop on reversal."""
    base_ms = 1680000000000
    day_ms = 86400000
    hour_ms = 3600000

    # Daily: steadily rising for 25 days (100..124) to get LONG trend
    daily_bars = []
    for i in range(25):
        p = 100 + i
        daily_bars.append(_make_kline(base_ms + i * day_ms, p, p + 1, p - 1, p))
    daily_df = pd.DataFrame(daily_bars)

    # Hourly: 25*24=600 bars
    # First 500 bars: gradually rising (will establish BB bands)
    # Bars 500-550: spike above upper band → LONG signal
    # Bars 550-600: sharp drop → ATR stop triggers
    hourly_bars = []
    for i in range(500):
        p = 100 + i * 0.04  # slow rise from 100 to ~120
        hourly_bars.append(_make_kline(base_ms + i * hour_ms, p, p + 0.5, p - 0.5, p))
    for i in range(500, 550):
        p = 120 + (i - 500) * 0.5  # spike from 120 to 145
        hourly_bars.append(_make_kline(base_ms + i * hour_ms, p, p + 1, p - 1, p))
    for i in range(550, 600):
        p = 145 - (i - 550) * 1.5  # drop from 145 to ~70
        hourly_bars.append(_make_kline(base_ms + i * hour_ms, p, p + 1, p - 1, p))
    hourly_df = pd.DataFrame(hourly_bars)

    engine = BacktestEngine(
        initial_capital=10000.0,
        position_size=500.0,
        leverage=5,
        max_positions=10,
    )
    trades, equity_curve = engine.run({"TESTUSDT": (hourly_df, daily_df)})

    # Should have at least 1 trade (opened then stopped out)
    assert len(trades) >= 1
    assert trades[0].side == "LONG"
    assert trades[0].exit_reason in ("atr_stop", "hard_stop")
```

- [ ] **Step 2: Run tests to verify new tests fail**

```bash
python -m pytest tests/test_backtest_engine.py::test_engine_no_signal_no_trades -v
```

Expected: FAIL — `BacktestEngine` not defined.

- [ ] **Step 3: Implement BacktestEngine.run() in engine.py**

Append to `backtesting/engine.py`:

```python
class BacktestEngine:
    """Simulates the Trend Sniper strategy on historical kline data."""

    def __init__(
        self,
        initial_capital: float = 10000.0,
        position_size: float = 500.0,
        leverage: int = 5,
        max_positions: int = 10,
        bb_period: int = 20,
        bb_std: float = 2.0,
        atr_period: int = 14,
        atr_multiplier: float = 2.0,
        max_stop_pct: float = 0.06,
    ):
        self.initial_capital = initial_capital
        self.balance = initial_capital
        self.position_size = position_size
        self.leverage = leverage
        self.max_positions = max_positions
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.atr_period = atr_period
        self.atr_multiplier = atr_multiplier
        self.max_stop_pct = max_stop_pct

        self.positions: list[BacktestPosition] = []
        self.trades: list[BacktestTrade] = []
        self.equity_curve: list[dict] = []

    def run(
        self, data: dict[str, tuple[pd.DataFrame, pd.DataFrame]]
    ) -> tuple[list[BacktestTrade], list[dict]]:
        """Run backtest over all symbols.

        Args:
            data: {symbol: (hourly_df, daily_df)} where each df has columns:
                  open_time, open, high, low, close, volume

        Returns:
            (trades, equity_curve)
        """
        # Build a unified hourly timeline from all symbols
        all_timestamps = set()
        for symbol, (hourly_df, _) in data.items():
            all_timestamps.update(hourly_df["open_time"].tolist())
        timeline = sorted(all_timestamps)

        # Need at least bb_period+1 bars before first signal
        min_hourly_bars = self.bb_period + 1
        min_daily_bars = self.bb_period + 1

        for ts in timeline:
            # 1. Check stop loss for all open positions
            self._check_stops(ts, data)

            # 2. Check entry signals for each symbol
            if len(self.positions) < self.max_positions and self.balance >= self.position_size:
                for symbol, (hourly_df, daily_df) in data.items():
                    if len(self.positions) >= self.max_positions:
                        break
                    if self.balance < self.position_size:
                        break
                    # Skip if already holding this symbol
                    if any(p.symbol == symbol for p in self.positions):
                        continue
                    self._check_signal(symbol, ts, hourly_df, daily_df, min_hourly_bars, min_daily_bars)

            # 3. Record equity
            equity = self._calc_equity(ts, data)
            self.equity_curve.append({"timestamp": ts, "equity": equity})

        # Close any remaining positions at last available price
        self._close_remaining(timeline[-1] if timeline else 0, data)

        return self.trades, self.equity_curve

    def _check_signal(
        self,
        symbol: str,
        current_ts: int,
        hourly_df: pd.DataFrame,
        daily_df: pd.DataFrame,
        min_hourly: int,
        min_daily: int,
    ):
        """Check for entry signal at current timestamp."""
        # Get hourly bars up to (not including) current bar — use closed bars only
        h_mask = hourly_df["open_time"] < current_ts
        h_closed = hourly_df[h_mask]
        if len(h_closed) < min_hourly:
            return

        # Get daily bars with close_time <= current_ts (closed daily bars only)
        # Daily bar open_time + 86400000ms = next day, so bar is closed when open_time + 86400000 <= current_ts
        day_ms = 86400000
        d_mask = (daily_df["open_time"] + day_ms) <= current_ts
        d_closed = daily_df[d_mask]
        if len(d_closed) < min_daily:
            return

        daily_closes = d_closed["close"].tolist()
        hourly_closes = h_closed["close"].tolist()

        # 1. Check daily trend (SMA slope)
        trend = check_trend(daily_closes, self.bb_period)
        if trend is None:
            return

        # 2. Check hourly Bollinger Band breakout
        upper, middle, lower = calculate_bollinger_bands(
            hourly_closes, self.bb_period, self.bb_std
        )
        last_close = hourly_closes[-1]

        signal = False
        if trend == "LONG" and last_close > upper:
            signal = True
        elif trend == "SHORT" and last_close < lower:
            signal = True

        if not signal:
            return

        # 3. Execute at current bar's open price (next bar after signal)
        current_bar = hourly_df[hourly_df["open_time"] == current_ts]
        if current_bar.empty:
            return
        exec_price = float(current_bar.iloc[0]["open"])
        exec_price = apply_slippage(exec_price, trend, is_entry=True)

        notional = self.position_size * self.leverage
        quantity = notional / exec_price
        fee = calculate_fee(notional)

        ts_str = datetime.fromtimestamp(current_ts / 1000, tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")

        pos = BacktestPosition(
            symbol=symbol,
            side=trend,
            entry_price=exec_price,
            quantity=quantity,
            opened_at=ts_str,
        )
        self.positions.append(pos)
        self.balance -= self.position_size
        self.balance -= fee  # deduct entry fee

    def _check_stops(self, current_ts: int, data: dict):
        """Check ATR trailing stop for all open positions."""
        to_close = []

        for pos in self.positions:
            if pos.symbol not in data:
                continue
            hourly_df, _ = data[pos.symbol]

            current_bar = hourly_df[hourly_df["open_time"] == current_ts]
            if current_bar.empty:
                continue

            current_price = float(current_bar.iloc[0]["close"])

            # Update extreme prices
            if current_price > pos.highest_price:
                pos.highest_price = current_price
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price

            # Calculate ATR from closed bars
            h_closed = hourly_df[hourly_df["open_time"] <= current_ts]
            if len(h_closed) < self.atr_period + 1:
                continue

            # Convert to kline format for calculate_atr: [open_time, open, high, low, close, volume]
            kline_list = h_closed.tail(self.atr_period + 2).values.tolist()
            atr = calculate_atr(kline_list, self.atr_period)

            triggered = should_stop_loss(
                side=pos.side,
                highest_price=pos.highest_price,
                lowest_price=pos.lowest_price,
                current_price=current_price,
                atr=atr,
                atr_multiplier=self.atr_multiplier,
                max_stop_pct=self.max_stop_pct,
            )

            if triggered:
                exit_price = apply_slippage(current_price, pos.side, is_entry=False)
                exit_reason = "atr_stop"

                # Check if hard cap triggered
                if pos.side == "LONG":
                    hard_stop = pos.highest_price * (1 - self.max_stop_pct)
                    if current_price <= hard_stop:
                        exit_reason = "hard_stop"
                else:
                    hard_stop = pos.lowest_price * (1 + self.max_stop_pct)
                    if current_price >= hard_stop:
                        exit_reason = "hard_stop"

                to_close.append((pos, exit_price, exit_reason, current_ts))

        for pos, exit_price, exit_reason, ts in to_close:
            self._close_position(pos, exit_price, exit_reason, ts)

    def _close_position(self, pos: BacktestPosition, exit_price: float, reason: str, ts: int):
        """Close a position and record the trade."""
        notional = self.position_size * self.leverage

        if pos.side == "LONG":
            pnl = (exit_price - pos.entry_price) / pos.entry_price * notional
        else:
            pnl = (pos.entry_price - exit_price) / pos.entry_price * notional

        exit_fee = calculate_fee(notional)
        net_pnl = pnl - exit_fee  # entry fee already deducted on open

        ts_str = datetime.fromtimestamp(ts / 1000, tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")

        trade = BacktestTrade(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            pnl=net_pnl,
            fee=calculate_fee(notional) * 2,  # total fee (entry + exit)
            opened_at=pos.opened_at,
            closed_at=ts_str,
            exit_reason=reason,
        )
        self.trades.append(trade)
        self.positions.remove(pos)
        self.balance += self.position_size + net_pnl

    def _close_remaining(self, last_ts: int, data: dict):
        """Force-close any open positions at the last bar's close price."""
        for pos in list(self.positions):
            if pos.symbol not in data:
                continue
            hourly_df, _ = data[pos.symbol]
            if hourly_df.empty:
                continue
            last_close = float(hourly_df.iloc[-1]["close"])
            exit_price = apply_slippage(last_close, pos.side, is_entry=False)
            self._close_position(pos, exit_price, "backtest_end", last_ts)

    def _calc_equity(self, current_ts: int, data: dict) -> float:
        """Calculate total equity = balance + unrealized PnL of open positions."""
        equity = self.balance
        notional = self.position_size * self.leverage

        for pos in self.positions:
            if pos.symbol not in data:
                continue
            hourly_df, _ = data[pos.symbol]
            bar = hourly_df[hourly_df["open_time"] == current_ts]
            if bar.empty:
                continue
            price = float(bar.iloc[0]["close"])

            if pos.side == "LONG":
                unrealized = (price - pos.entry_price) / pos.entry_price * notional
            else:
                unrealized = (pos.entry_price - price) / pos.entry_price * notional
            equity += unrealized

        # Add back position margins
        equity += len(self.positions) * self.position_size
        return equity
```

- [ ] **Step 4: Run all engine tests**

```bash
python -m pytest tests/test_backtest_engine.py -v
```

Expected: All tests PASS (including `test_engine_no_signal_no_trades` and `test_engine_long_signal_opens_and_stops`).

- [ ] **Step 5: Commit**

```bash
git add backtesting/engine.py tests/test_backtest_engine.py
git commit -m "feat(backtest): implement backtest engine with signal detection and trade simulation"
```

---

### Task 5: Report module

**Files:**
- Create: `backtesting/report.py`
- Create: `tests/test_backtest_report.py`

- [ ] **Step 1: Write failing tests for report**

```python
# tests/test_backtest_report.py
import pytest
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from backtesting.engine import BacktestTrade
from backtesting.report import calculate_stats


def _make_trade(pnl, side="LONG", hours_held=24):
    return BacktestTrade(
        symbol="BTCUSDT",
        side=side,
        entry_price=50000.0,
        exit_price=51000.0 if pnl > 0 else 49000.0,
        quantity=0.05,
        pnl=pnl,
        fee=2.0,
        opened_at="2025-04-01 12:00:00",
        closed_at="2025-04-02 12:00:00",
        exit_reason="atr_stop",
    )


def test_stats_basic():
    trades = [_make_trade(100), _make_trade(-50), _make_trade(75)]
    equity = [
        {"timestamp": 0, "equity": 10000},
        {"timestamp": 1, "equity": 10100},
        {"timestamp": 2, "equity": 10050},
        {"timestamp": 3, "equity": 10125},
    ]
    stats = calculate_stats(trades, equity, initial_capital=10000.0)

    assert stats["total_trades"] == 3
    assert stats["total_pnl"] == pytest.approx(125.0)
    assert stats["win_rate"] == pytest.approx(2 / 3)
    assert stats["max_drawdown_pct"] >= 0


def test_stats_no_trades():
    stats = calculate_stats([], [{"timestamp": 0, "equity": 10000}], initial_capital=10000.0)
    assert stats["total_trades"] == 0
    assert stats["total_pnl"] == 0.0
    assert stats["win_rate"] == 0.0


def test_stats_all_losses():
    trades = [_make_trade(-100), _make_trade(-50)]
    equity = [
        {"timestamp": 0, "equity": 10000},
        {"timestamp": 1, "equity": 9900},
        {"timestamp": 2, "equity": 9850},
    ]
    stats = calculate_stats(trades, equity, initial_capital=10000.0)
    assert stats["win_rate"] == 0.0
    assert stats["profit_factor"] == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_backtest_report.py -v
```

Expected: FAIL — `backtesting.report` not found.

- [ ] **Step 3: Implement report.py**

```python
# backtesting/report.py
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

    # Average hold time
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

    # Max drawdown from equity curve
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

    # Annualized return
    if len(equity_curve) >= 2:
        hours = len(equity_curve)
        years = hours / (365.25 * 24)
        if years > 0:
            total_return = stats["total_pnl"] / initial_capital
            stats["annualized_return_pct"] = ((1 + total_return) ** (1 / years) - 1) * 100

    # Sharpe ratio (annualized, hourly returns, rf=0)
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

    # Trades CSV
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

    # Equity curve CSV
    if equity_curve:
        eq_df = pd.DataFrame(equity_curve)
        # Add drawdown column
        peak = eq_df["equity"].cummax()
        eq_df["drawdown"] = peak - eq_df["equity"]
        eq_path = os.path.join(RESULTS_DIR, "equity.csv")
        eq_df.to_csv(eq_path, index=False)
        print(f"Equity curve exported to: {os.path.abspath(eq_path)}")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_backtest_report.py -v
```

Expected: All 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backtesting/report.py tests/test_backtest_report.py
git commit -m "feat(backtest): add report module with stats calculation and CSV export"
```

---

### Task 6: CLI entry point

**Files:**
- Create: `backtesting/backtest.py`

- [ ] **Step 1: Implement backtest.py**

```python
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

    # Load data
    print("Loading data...")
    data = load_data(symbols)
    if not data:
        print("ERROR: No data loaded. Run download_data.py first:")
        print("  python backtesting/download_data.py")
        sys.exit(1)
    print(f"\nLoaded {len(data)} symbols\n")

    # Run engine
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

    # Report
    stats = calculate_stats(trades, equity_curve, args.capital)
    print_report(stats)
    export_csv(trades, equity_curve)


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Commit**

```bash
git add backtesting/backtest.py
git commit -m "feat(backtest): add CLI entry point with argparse"
```

---

### Task 7: Run all tests and full integration smoke test

- [ ] **Step 1: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: All existing tests + new backtest tests PASS.

- [ ] **Step 2: Download data for a few symbols**

```bash
python backtesting/download_data.py
```

This will take a few minutes to download 30 symbols × 2 intervals.

- [ ] **Step 3: Run full backtest**

```bash
python backtesting/backtest.py
```

Expected: Prints loading info, runs backtest, prints stats table, exports `results/trades.csv` and `results/equity.csv`.

- [ ] **Step 4: Verify CSV output**

```bash
head -5 results/trades.csv
head -5 results/equity.csv
```

Expected: CSV headers and data rows present.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat(backtest): complete backtesting system with data download, engine, and reporting"
```

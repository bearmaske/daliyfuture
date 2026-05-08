"""Backtest engine: simulates the Trend Sniper strategy on historical data.

When minute data is provided (`minute_df`), stop-loss checks run at a
configurable minute cadence (1m / 2m / ...) and use intrabar high/low so spikes
trigger correctly. Entry signals still evaluate on closed 1H bars.
Without minute data, stops fall back to 1H close-only checks."""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import config
from strategy import calculate_bollinger_bands, check_trend, check_trend_asymmetric, check_trend_bb_middle, check_trend_rolling
from risk import check_fixed_sl, check_trailing_tp

TZ_CN = timezone(timedelta(hours=8))

TAKER_FEE_RATE = 0.0004   # 0.04%
SLIPPAGE_PCT = 0.0005     # 0.05%


@dataclass
class BacktestPosition:
    symbol: str
    side: str
    entry_price: float
    quantity: float
    opened_at: str
    opened_ms: int = 0
    highest_price: float = 0.0
    lowest_price: float = 0.0
    trailing_activated: bool = False

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
        sma_period: int = 20,
        trailing_activation_pct: float = config.TRAILING_ACTIVATION_PCT,
        trailing_drawdown_pct: float = config.TRAILING_DRAWDOWN_PCT,
        fixed_stop_loss_pct: float = config.FIXED_STOP_LOSS_PCT,
        stop_check_minutes: int = 60,
        trend_timeframe_hours: int = 24,
    ):
        self.initial_capital = initial_capital
        self.balance = initial_capital
        self.position_size = position_size
        self.leverage = leverage
        self.max_positions = max_positions
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.sma_period = sma_period
        self.trailing_activation_pct = trailing_activation_pct
        self.trailing_drawdown_pct = trailing_drawdown_pct
        self.fixed_stop_loss_pct = fixed_stop_loss_pct
        self.stop_check_minutes = stop_check_minutes
        self.trend_timeframe_hours = trend_timeframe_hours
        self._trend_tf_ms = trend_timeframe_hours * 3600_000

        self.positions: list[BacktestPosition] = []
        self.trades: list[BacktestTrade] = []
        self.equity_curve: list[dict] = []

        self._minute_data: dict[str, pd.DataFrame] = {}
        self._hourly_data: dict[str, pd.DataFrame] = {}

    def run(
        self, data: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
        minute_data: dict[str, pd.DataFrame] | None = None,
    ) -> tuple[list[BacktestTrade], list[dict]]:
        """Run backtest over all symbols.

        Args:
            data: {symbol: (hourly_df, daily_df)} where each df has columns:
                  open_time, open, high, low, close, volume
            minute_data: optional {symbol: minute_df} for intra-hour stop checks

        Returns:
            (trades, equity_curve)
        """
        self._minute_data = minute_data or {}
        self._hourly_data = {sym: h for sym, (h, _) in data.items()}

        # Hourly timeline drives entries. Within each hour, stop checks step
        # through minute bars at `stop_check_minutes` stride.
        all_hourly_ts = set()
        for symbol, (hourly_df, _) in data.items():
            all_hourly_ts.update(hourly_df["open_time"].tolist())
        hourly_timeline = sorted(all_hourly_ts)

        min_hourly_bars = self.bb_period + 1
        min_daily_bars = self.sma_period + 1
        HOUR_MS = 3600_000
        MIN_MS = 60_000

        for ts in hourly_timeline:
            # 1. Intrabar stop-loss checks across the preceding hour
            # Run on the minute grid [ts, ts+HOUR_MS) at the configured stride.
            if self._minute_data and self.positions:
                stride_ms = self.stop_check_minutes * MIN_MS
                minute_cursor = ts
                end_of_hour = ts + HOUR_MS
                while minute_cursor < end_of_hour:
                    self._check_stops_minute(minute_cursor)
                    if not self.positions:
                        break
                    minute_cursor += stride_ms

            # 2. Hourly close — run close-only stop for symbols missing minute data
            self._check_stops_hour(ts, data)

            # 3. Check entry signals for each symbol
            if len(self.positions) < self.max_positions and self.balance >= self.position_size:
                for symbol, (hourly_df, daily_df) in data.items():
                    if len(self.positions) >= self.max_positions:
                        break
                    if self.balance < self.position_size:
                        break
                    if any(p.symbol == symbol for p in self.positions):
                        continue
                    if self._symbol_in_cooldown(symbol, ts):
                        continue
                    self._check_signal(symbol, ts, hourly_df, daily_df, min_hourly_bars, min_daily_bars)

            # 4. Record equity
            equity = self._calc_equity(ts, data)
            self.equity_curve.append({"timestamp": ts, "equity": equity})

        # Close any remaining positions at last available price
        self._close_remaining(hourly_timeline[-1] if hourly_timeline else 0, data)

        return self.trades, self.equity_curve

    def _symbol_in_cooldown(self, symbol: str, current_ts: int) -> bool:
        """Mirror state.symbol_cooldown_remaining for backtest.
        Returns True if `symbol` has accumulated >= SYMBOL_LOSS_THRESHOLD
        losing trades in the past SYMBOL_COOLDOWN_WINDOW_HOURS AND that
        threshold was hit within SYMBOL_COOLDOWN_HOURS of `current_ts`.
        """
        if not getattr(config, "SYMBOL_LOSS_THRESHOLD", 0):
            return False
        HOUR_MS = 3600_000
        window_ms = config.SYMBOL_COOLDOWN_WINDOW_HOURS * HOUR_MS
        cooldown_ms = config.SYMBOL_COOLDOWN_HOURS * HOUR_MS
        threshold = config.SYMBOL_LOSS_THRESHOLD
        losses = []
        from datetime import datetime
        for t in self.trades:
            if t.symbol != symbol or t.pnl >= 0:
                continue
            try:
                closed_dt = datetime.strptime(t.closed_at, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
            except ValueError:
                continue
            closed_ms = int(closed_dt.timestamp() * 1000)
            if current_ts - closed_ms <= window_ms:
                losses.append(closed_ms)
        if len(losses) < threshold:
            return False
        losses.sort()
        anchor_ms = losses[threshold - 1]
        return current_ts - anchor_ms < cooldown_ms

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
        h_mask = hourly_df["open_time"] < current_ts
        h_closed = hourly_df[h_mask]
        if len(h_closed) < min_hourly:
            return

        mode = config.TREND_FILTER_MODE
        hourly_closes = h_closed["close"].tolist()

        trend = None
        allow_long = allow_short = True  # only used for asymmetric mode
        if mode == "rolling_sma":
            rolling_need = self.sma_period * 24 + 24
            if len(hourly_closes) < rolling_need:
                return
            trend = check_trend_rolling(
                hourly_closes, period_hours=self.sma_period * 24, step_hours=24
            )
            if trend is None:
                return
        elif mode == "asymmetric":
            rolling_need = self.sma_period * 24 + 24
            if len(hourly_closes) < rolling_need:
                return
            tf_ms = self._trend_tf_ms
            d_mask = (daily_df["open_time"] + tf_ms) <= current_ts
            d_closed = daily_df[d_mask]
            if len(d_closed) < min_daily:
                return
            daily_closes = d_closed["close"].tolist()
            allow_long, allow_short = check_trend_asymmetric(
                daily_closes, hourly_closes, self.sma_period
            )
            if not (allow_long or allow_short):
                return
        elif mode != "disabled":
            tf_ms = self._trend_tf_ms
            d_mask = (daily_df["open_time"] + tf_ms) <= current_ts
            d_closed = daily_df[d_mask]
            if len(d_closed) < min_daily:
                return
            daily_closes = d_closed["close"].tolist()
            if mode == "sma":
                trend = check_trend(daily_closes, self.sma_period)
            elif mode == "bb_middle":
                trend = check_trend_bb_middle(daily_closes, self.sma_period, self.bb_std)
            if trend is None:
                return

        upper, middle, lower = calculate_bollinger_bands(
            hourly_closes, self.bb_period, self.bb_std
        )
        last_close = hourly_closes[-1]

        # 24-bar high/low confirmation: signal bar's high must be 24H high (LONG),
        # or signal bar's low must be 24H low (SHORT).
        if len(h_closed) >= 25:
            signal_bar = h_closed.iloc[-1]
            signal_high = float(signal_bar["high"])
            signal_low = float(signal_bar["low"])
            lookback = h_closed.iloc[-25:-1]
            is_24h_high = signal_high >= lookback["high"].max()
            is_24h_low = signal_low <= lookback["low"].min()
        else:
            is_24h_high = is_24h_low = False

        signal = False
        if mode == "asymmetric":
            if last_close > upper and allow_long and is_24h_high:
                signal = True
                trend = "LONG"
            elif last_close < lower and allow_short and is_24h_low:
                signal = True
                trend = "SHORT"
        elif mode != "disabled":
            if trend == "LONG" and last_close > upper and is_24h_high:
                signal = True
            elif trend == "SHORT" and last_close < lower and is_24h_low:
                signal = True
        else:
            if last_close > upper and is_24h_high:
                signal = True
                trend = "LONG"
            elif last_close < lower and is_24h_low:
                signal = True
                trend = "SHORT"

        if not signal:
            return

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
            opened_ms=current_ts,
        )
        self.positions.append(pos)
        self.balance -= self.position_size
        self.balance -= fee

    def _check_stops_minute(self, minute_ts: int):
        """Stop check at a polling tick using fixed SL and trailing TP."""
        to_close = []
        for pos in self.positions:
            if pos.symbol not in self._minute_data:
                continue
            if minute_ts <= pos.opened_ms:
                continue

            mdf = self._minute_data[pos.symbol]
            bar = mdf[mdf["open_time"] == minute_ts]
            if bar.empty:
                continue
            current_price = float(bar.iloc[0]["close"])

            if current_price > pos.highest_price:
                pos.highest_price = current_price
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price

            extreme_price = pos.highest_price if pos.side == "LONG" else pos.lowest_price

            if check_fixed_sl(pos.side, pos.entry_price, current_price, self.fixed_stop_loss_pct):
                exit_price = apply_slippage(current_price, pos.side, is_entry=False)
                to_close.append((pos, exit_price, "fixed_sl", minute_ts))
                continue

            trail_triggered, newly_activated = check_trailing_tp(
                side=pos.side,
                entry_price=pos.entry_price,
                extreme_price=extreme_price,
                current_price=current_price,
                trailing_activated=pos.trailing_activated,
                activation_pct=self.trailing_activation_pct,
                drawdown_pct=self.trailing_drawdown_pct,
            )
            if newly_activated:
                pos.trailing_activated = True
            if trail_triggered:
                exit_price = apply_slippage(current_price, pos.side, is_entry=False)
                to_close.append((pos, exit_price, "trailing_tp", minute_ts))

        for pos, exit_price, reason, ts in to_close:
            self._close_position(pos, exit_price, reason, ts)

    def _check_stops_hour(self, current_ts: int, data: dict):
        """Hourly-close fallback for symbols without minute data."""
        to_close = []
        for pos in self.positions:
            if pos.symbol in self._minute_data:
                continue  # handled on minute grid
            if pos.symbol not in data:
                continue
            hourly_df, _ = data[pos.symbol]
            bar = hourly_df[hourly_df["open_time"] == current_ts]
            if bar.empty:
                continue
            current_price = float(bar.iloc[0]["close"])
            if current_price > pos.highest_price:
                pos.highest_price = current_price
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price

            extreme_price = pos.highest_price if pos.side == "LONG" else pos.lowest_price

            if check_fixed_sl(pos.side, pos.entry_price, current_price, self.fixed_stop_loss_pct):
                exit_price = apply_slippage(current_price, pos.side, is_entry=False)
                to_close.append((pos, exit_price, "fixed_sl", current_ts))
                continue

            trail_triggered, newly_activated = check_trailing_tp(
                side=pos.side,
                entry_price=pos.entry_price,
                extreme_price=extreme_price,
                current_price=current_price,
                trailing_activated=pos.trailing_activated,
                activation_pct=self.trailing_activation_pct,
                drawdown_pct=self.trailing_drawdown_pct,
            )
            if newly_activated:
                pos.trailing_activated = True
            if trail_triggered:
                exit_price = apply_slippage(current_price, pos.side, is_entry=False)
                to_close.append((pos, exit_price, "trailing_tp", current_ts))

        for pos, exit_price, reason, ts in to_close:
            self._close_position(pos, exit_price, reason, ts)

    def _close_position(self, pos: BacktestPosition, exit_price: float, reason: str, ts: int):
        """Close a position and record the trade."""
        notional = self.position_size * self.leverage

        if pos.side == "LONG":
            pnl = (exit_price - pos.entry_price) / pos.entry_price * notional
        else:
            pnl = (pos.entry_price - exit_price) / pos.entry_price * notional

        exit_fee = calculate_fee(notional)
        net_pnl = pnl - exit_fee

        ts_str = datetime.fromtimestamp(ts / 1000, tz=TZ_CN).strftime("%Y-%m-%d %H:%M:%S")

        trade = BacktestTrade(
            symbol=pos.symbol,
            side=pos.side,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            quantity=pos.quantity,
            pnl=net_pnl,
            fee=calculate_fee(notional) * 2,
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

        equity += len(self.positions) * self.position_size
        return equity

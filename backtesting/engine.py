"""Backtest engine: simulates the Trend Sniper strategy on historical data."""
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from config import config
from strategy import calculate_bollinger_bands, check_trend, check_trend_bb_middle, check_volatility_expanding
from risk import calculate_atr, should_stop_loss

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
        self.sma_period = sma_period
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
        all_timestamps = set()
        for symbol, (hourly_df, _) in data.items():
            all_timestamps.update(hourly_df["open_time"].tolist())
        timeline = sorted(all_timestamps)

        min_hourly_bars = self.bb_period + 1
        min_daily_bars = self.sma_period + 1

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

        # Daily bar is closed when open_time + 86400000ms <= current_ts
        day_ms = 86400000
        d_mask = (daily_df["open_time"] + day_ms) <= current_ts
        d_closed = daily_df[d_mask]
        if len(d_closed) < min_daily:
            return

        daily_closes = d_closed["close"].tolist()
        hourly_closes = h_closed["close"].tolist()

        # 1. Check daily trend if enabled
        trend = None
        mode = config.TREND_FILTER_MODE
        if mode != "disabled":
            if mode == "sma":
                trend = check_trend(daily_closes, self.sma_period)
            elif mode == "bb_middle":
                trend = check_trend_bb_middle(daily_closes, self.sma_period, self.bb_std)
            if trend is None:
                return

        # 2. Check volatility filter (ATR expansion)
        if config.VOL_FILTER_ENABLED:
            h_arr = h_closed[["open_time", "open", "high", "low", "close", "volume"]].values
            expanding, _, _, _ = check_volatility_expanding(
                h_arr, config.VOL_ATR_SHORT, config.VOL_ATR_LONG, config.VOL_ATR_THRESHOLD
            )
            if not expanding:
                return

        # 3. Check hourly Bollinger Band breakout
        upper, middle, lower = calculate_bollinger_bands(
            hourly_closes, self.bb_period, self.bb_std
        )
        last_close = hourly_closes[-1]

        signal = False
        if mode != "disabled":
            if trend == "LONG" and last_close > upper:
                signal = True
            elif trend == "SHORT" and last_close < lower:
                signal = True
        else:
            if last_close > upper:
                signal = True
                trend = "LONG"
            elif last_close < lower:
                signal = True
                trend = "SHORT"

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
        self.balance -= fee

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

            if current_price > pos.highest_price:
                pos.highest_price = current_price
            if current_price < pos.lowest_price:
                pos.lowest_price = current_price

            h_closed = hourly_df[hourly_df["open_time"] <= current_ts]
            if len(h_closed) < self.atr_period + 1:
                continue

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

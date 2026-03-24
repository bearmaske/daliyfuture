import numpy as np
from typing import List, Tuple
from binance.client import Client
from config import config
from exchange import Exchange
from state import StateManager
from notifier import notify, logger


def calculate_bollinger_bands(
    closes: List[float], period: int = 20, std_dev: float = 2.0
) -> Tuple[float, float, float]:
    """Calculate Bollinger Bands from closing prices. Returns (upper, middle, lower)."""
    data = np.array(closes[-period:], dtype=float)
    middle = float(np.mean(data))
    std = float(np.std(data, ddof=0))
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def check_trend(closes: List[float], period: int = 20) -> str:
    """Determine trend from daily closes. Returns 'LONG' or 'SHORT'."""
    sma = float(np.mean(closes[-period:]))
    current_close = closes[-1]
    return "LONG" if current_close > sma else "SHORT"


def check_entry_signal(
    closes: List[float],
    volumes: List[float],
    trend: str,
    period: int = 20,
    std_dev: float = 2.0,
) -> bool:
    """Check if entry signal fires on hourly data."""
    upper, middle, lower = calculate_bollinger_bands(closes, period, std_dev)
    current_close = closes[-1]
    current_volume = volumes[-1]
    avg_volume = float(np.mean(volumes[-period - 1 : -1]))

    if current_volume <= avg_volume:
        return False

    if trend == "LONG" and current_close > upper:
        return True
    if trend == "SHORT" and current_close < lower:
        return True

    return False


def run_strategy(exchange: Exchange, state_mgr: StateManager):
    """Main strategy loop: scan top symbols, check signals, open positions."""
    if state_mgr.position_count >= config.MAX_POSITIONS:
        logger.info("Max positions reached, skipping scan")
        return
    if state_mgr.balance < config.POSITION_SIZE:
        logger.info(f"Insufficient balance: {state_mgr.balance:.2f}")
        return

    top_symbols = exchange.get_top_symbols()
    logger.info(f"Scanning {len(top_symbols)} symbols")

    kline_limit = config.BB_PERIOD + 1

    for symbol in top_symbols:
        if state_mgr.position_count >= config.MAX_POSITIONS:
            break
        if state_mgr.balance < config.POSITION_SIZE:
            break
        if state_mgr.get_position_by_symbol(symbol):
            continue

        try:
            # Daily trend
            daily_klines = exchange.get_klines(
                symbol, Client.KLINE_INTERVAL_1DAY, kline_limit
            )
            daily_closes = [float(k[4]) for k in daily_klines]
            if len(daily_closes) < kline_limit:
                continue
            trend = check_trend(daily_closes, config.BB_PERIOD)

            # Hourly signal
            hourly_klines = exchange.get_klines(
                symbol, Client.KLINE_INTERVAL_1HOUR, kline_limit
            )
            hourly_closes = [float(k[4]) for k in hourly_klines]
            hourly_volumes = [float(k[5]) for k in hourly_klines]
            if len(hourly_closes) < kline_limit:
                continue

            signal = check_entry_signal(
                hourly_closes, hourly_volumes, trend, config.BB_PERIOD, config.BB_STD
            )

            if signal:
                _open_position(exchange, state_mgr, symbol, trend, hourly_closes[-1])

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
            continue


def _open_position(
    exchange: Exchange,
    state_mgr: StateManager,
    symbol: str,
    side: str,
    current_price: float,
):
    """Open a new position."""
    notional = config.POSITION_SIZE * config.LEVERAGE
    raw_qty = notional / current_price
    quantity = exchange.round_quantity(symbol, raw_qty)

    if quantity <= 0:
        logger.warning(f"Quantity too small for {symbol}")
        return

    order_side = "BUY" if side == "LONG" else "SELL"
    try:
        exchange.set_leverage(symbol, config.LEVERAGE)
        order = exchange.place_order(symbol, order_side, quantity)

        state_mgr.add_position(
            symbol=symbol,
            side=side,
            entry_price=current_price,
            quantity=quantity,
        )
        state_mgr.update_balance(-config.POSITION_SIZE)

        notify(
            f"开仓 {side}",
            f"{symbol} | 价格 {current_price:.4f} | 数量 {quantity} | 保证金 ${config.POSITION_SIZE}",
        )
    except Exception as e:
        logger.error(f"Failed to open {side} {symbol}: {e}")

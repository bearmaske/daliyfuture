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
    trend: str,
    period: int = 20,
    std_dev: float = 2.0,
) -> bool:
    """Check if entry signal fires on hourly data."""
    upper, middle, lower = calculate_bollinger_bands(closes, period, std_dev)
    current_close = closes[-1]

    if trend == "LONG" and current_close > upper:
        return True
    if trend == "SHORT" and current_close < lower:
        return True

    return False


def run_strategy(exchange: Exchange, state_mgr: StateManager):
    """Main strategy loop: scan top symbols, check signals, open positions."""
    try:
        exchange.sync_state(state_mgr)
    except Exception as e:
        logger.warning("[策略] 同步 Testnet 失败: %s", e)

    if state_mgr.position_count >= config.MAX_POSITIONS:
        logger.info("Max positions reached, skipping scan")
        return
    if state_mgr.balance < config.POSITION_SIZE:
        logger.info(f"Insufficient balance: {state_mgr.balance:.2f}")
        return

    top_symbols = exchange.get_top_symbols()
    logger.info("=" * 60)
    logger.info("[策略] 开始扫描 %d 个币种 | 余额: $%.2f | 持仓: %d/%d",
                len(top_symbols), state_mgr.balance, state_mgr.position_count, config.MAX_POSITIONS)

    kline_limit = config.BB_PERIOD + 1
    signal_count = 0

    for symbol in top_symbols:
        if state_mgr.position_count >= config.MAX_POSITIONS:
            logger.info("[策略] 已达最大持仓数 %d，停止扫描", config.MAX_POSITIONS)
            break
        if state_mgr.balance < config.POSITION_SIZE:
            logger.info("[策略] 余额不足 $%.2f < $%.2f，停止扫描", state_mgr.balance, config.POSITION_SIZE)
            break
        if state_mgr.get_position_by_symbol(symbol):
            logger.debug("[策略] %s 已持仓，跳过", symbol)
            continue

        try:
            daily_klines = exchange.get_klines(
                symbol, Client.KLINE_INTERVAL_1DAY, kline_limit
            )
            daily_closes = [float(k[4]) for k in daily_klines]
            if len(daily_closes) < kline_limit:
                continue
            trend = check_trend(daily_closes, config.BB_PERIOD)
            _, d_middle, _ = calculate_bollinger_bands(daily_closes, config.BB_PERIOD, config.BB_STD)

            hourly_klines = exchange.get_klines(
                symbol, Client.KLINE_INTERVAL_1HOUR, kline_limit
            )
            hourly_closes = [float(k[4]) for k in hourly_klines]
            if len(hourly_closes) < kline_limit:
                continue

            h_upper, h_middle, h_lower = calculate_bollinger_bands(hourly_closes, config.BB_PERIOD, config.BB_STD)
            current_close = hourly_closes[-1]

            if trend == "LONG" and current_close > h_upper:
                signal = True
            elif trend == "SHORT" and current_close < h_lower:
                signal = True
            else:
                signal = False

            logger.info(
                "[扫描] %s | 趋势: %s | 日线中轨: %.4f | "
                "1H收盘: %.4f | 上轨: %.4f | 下轨: %.4f | 信号: %s",
                symbol, trend, d_middle,
                current_close, h_upper, h_lower,
                "YES" if signal else "-"
            )

            if signal:
                signal_count += 1
                _open_position(exchange, state_mgr, symbol, trend, current_close)

        except Exception as e:
            logger.error("[策略] %s 处理异常: %s", symbol, e)
            continue

    logger.info("[策略] 扫描完成 | 信号数: %d | 持仓: %d/%d | 余额: $%.2f",
                signal_count, state_mgr.position_count, config.MAX_POSITIONS, state_mgr.balance)
    logger.info("=" * 60)


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
        exchange.place_order(symbol, order_side, quantity)

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

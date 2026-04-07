import numpy as np
from typing import List, Optional, Tuple
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


def check_trend(closes: List[float], period: int = 20) -> Optional[str]:
    """Determine trend from daily closes using SMA direction.
    Returns 'LONG', 'SHORT', or None (flat / insufficient data)."""
    if len(closes) < period + 1:
        return None
    sma_now = float(np.mean(closes[-period:]))
    sma_prev = float(np.mean(closes[-period - 1 : -1]))
    current_close = closes[-1]

    if current_close > sma_now and sma_now > sma_prev:
        return "LONG"
    if current_close < sma_now and sma_now < sma_prev:
        return "SHORT"
    return None


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
    """Main strategy loop: scan top symbols, collect signals, open by volume priority."""
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
    volume_map = exchange.get_volume_map(top_symbols)
    logger.info("=" * 60)
    logger.info("[策略] 开始扫描 %d 个币种 | 余额: $%.2f | 持仓: %d/%d",
                len(top_symbols), state_mgr.balance, state_mgr.position_count, config.MAX_POSITIONS)

    # +2 for SMA slope comparison, +1 to discard unclosed candle
    daily_kline_limit = config.BB_PERIOD + 3
    hourly_kline_limit = config.BB_PERIOD + 2  # +1 for BB calc, +1 to discard unclosed candle

    # Phase 1: scan all symbols and collect signals
    signals = []

    for symbol in top_symbols:
        if state_mgr.get_position_by_symbol(symbol):
            logger.debug("[策略] %s 已持仓，跳过", symbol)
            continue

        try:
            daily_klines = exchange.get_klines(
                symbol, Client.KLINE_INTERVAL_1DAY, daily_kline_limit
            )
            daily_closes = [float(k[4]) for k in daily_klines[:-1]]  # drop unclosed candle
            if len(daily_closes) < config.BB_PERIOD + 1:
                continue
            trend = check_trend(daily_closes, config.BB_PERIOD)
            if trend is None:
                continue
            _, d_middle, _ = calculate_bollinger_bands(daily_closes, config.BB_PERIOD, config.BB_STD)

            hourly_klines = exchange.get_klines(
                symbol, Client.KLINE_INTERVAL_1HOUR, hourly_kline_limit
            )
            hourly_closes = [float(k[4]) for k in hourly_klines[:-1]]  # drop unclosed candle
            if len(hourly_closes) < config.BB_PERIOD + 1:
                continue

            h_upper, h_middle, h_lower = calculate_bollinger_bands(hourly_closes, config.BB_PERIOD, config.BB_STD)
            current_close = hourly_closes[-1]
            current_price = exchange.get_price(symbol)

            if trend == "LONG" and current_close > h_upper:
                signal = True
            elif trend == "SHORT" and current_close < h_lower:
                signal = True
            else:
                signal = False

            vol = volume_map.get(symbol, 0.0)
            logger.info(
                "[扫描] %s | 趋势: %s | 现价: %.4f | 日线中轨: %.4f | "
                "1H收盘: %.4f | 上轨: %.4f | 下轨: %.4f | 24h量: %.0f | 信号: %s",
                symbol, trend, current_price, d_middle,
                current_close, h_upper, h_lower, vol,
                "YES" if signal else "-"
            )

            if signal:
                signals.append({
                    "symbol": symbol,
                    "trend": trend,
                    "price": current_price,
                    "volume": vol,
                })

        except Exception as e:
            logger.error("[策略] %s 处理异常: %s", symbol, e)
            continue

    # Phase 2: sort signals by 24h quote volume descending, then open
    signals.sort(key=lambda s: s["volume"], reverse=True)
    available_slots = config.MAX_POSITIONS - state_mgr.position_count

    if signals:
        logger.info("[策略] 收集到 %d 个信号，可用仓位 %d，按交易量排序开仓",
                    len(signals), available_slots)

    opened = 0
    for sig in signals:
        if opened >= available_slots:
            logger.info("[策略] 已用完可用仓位，剩余 %d 个信号未开仓", len(signals) - opened)
            break
        if state_mgr.balance < config.POSITION_SIZE:
            logger.info("[策略] 余额不足 $%.2f < $%.2f，停止开仓", state_mgr.balance, config.POSITION_SIZE)
            break
        _open_position(exchange, state_mgr, sig["symbol"], sig["trend"], sig["price"])
        opened += 1

    logger.info("[策略] 扫描完成 | 信号数: %d | 开仓: %d | 持仓: %d/%d | 余额: $%.2f",
                len(signals), opened, state_mgr.position_count, config.MAX_POSITIONS, state_mgr.balance)
    logger.info("=" * 60)


def _open_position(
    exchange: Exchange,
    state_mgr: StateManager,
    symbol: str,
    side: str,
    current_price: float,
):
    """Open a new position."""
    if state_mgr.get_position_by_symbol(symbol):
        logger.warning("[开仓] %s 已持仓，跳过重复开仓", symbol)
        return

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

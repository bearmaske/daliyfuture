import numpy as np
from typing import List, Optional, Tuple
from binance.client import Client
from binance.exceptions import BinanceAPIException
from config import config
from exchange import Exchange
from state import StateManager
from notifier import notify, logger

# Error codes that mean "Binance itself is refusing to take a new long/short
# position for this symbol right now" — treat as a strong market signal and
# blacklist the symbol for POSITION_RISK_BLACKLIST_HOURS.
POSITION_RISK_ERROR_CODES = {-4106, -4129, -4131, -4411}


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


def check_trend_rolling(hourly_closes: List[float], period_hours: int = 480,
                        step_hours: int = 24) -> Optional[str]:
    """Rolling 20-day SMA trend, evaluated every hour.

    Equivalent to `check_trend(daily_closes, 20)` — same math — but uses the
    last 480 1H closes instead of 20 closed daily bars, so the slope updates
    every hour. Removes the up-to-24-hour lag that check_trend suffers on
    trend-reversal days (SMA only refreshing at UTC midnight).

    slope: mean of last period_hours vs mean shifted back step_hours
    position: last close vs current SMA
    """
    if len(hourly_closes) < period_hours + step_hours:
        return None
    arr = np.asarray(hourly_closes, dtype=float)
    sma_now = float(np.mean(arr[-period_hours:]))
    sma_prev = float(np.mean(arr[-(period_hours + step_hours):-step_hours]))
    current = arr[-1]
    if current > sma_now and sma_now > sma_prev:
        return "LONG"
    if current < sma_now and sma_now < sma_prev:
        return "SHORT"
    return None


def check_trend_asymmetric(
    daily_closes: List[float],
    hourly_closes: List[float],
    sma_period: int = 20,
) -> Tuple[bool, bool]:
    """Asymmetric trend filter: LONG slow, SHORT fast.

    LONG: daily-sma (24h lag) — crypto uptrends are gradual; the lag filters
    noise and keeps us in trends longer.
    SHORT: rolling-sma on 1H bars (1h lag) + daily-sma veto — crashes are
    faster than rallies; catching the break early is worth the extra signals,
    but we don't short when daily trend is clearly LONG.
    Returns (allow_long, allow_short).
    """
    trend_daily = check_trend(daily_closes, sma_period)
    allow_long = trend_daily == "LONG"

    trend_rolling = check_trend_rolling(
        hourly_closes, period_hours=sma_period * 24, step_hours=24
    )
    allow_short = (trend_rolling == "SHORT") and (trend_daily != "LONG")

    return allow_long, allow_short


def check_trend_bb_middle(closes: List[float], period: int = 20, std_dev: float = 2.0) -> Optional[str]:
    """Determine trend by price position relative to daily BB middle.
    Returns 'LONG' if price > middle, 'SHORT' if price < middle, None if insufficient data."""
    if len(closes) < period:
        return None
    _, middle, _ = calculate_bollinger_bands(closes, period, std_dev)
    current_close = closes[-1]
    if current_close > middle:
        return "LONG"
    if current_close < middle:
        return "SHORT"
    return None


def check_volatility_expanding(klines, short_period: int = 7, long_period: int = 28, threshold: float = 1.0) -> tuple:
    """Check if volatility is expanding by comparing short vs long ATR.
    Returns (is_expanding, short_atr, long_atr, ratio).
    klines: list/array of [open_time, open, high, low, close, volume, ...] or ndarray."""
    arr = np.asarray(klines, dtype=float) if not isinstance(klines, np.ndarray) else klines
    min_bars = long_period + 1
    if len(arr) < min_bars:
        return False, 0.0, 0.0, 0.0  # not enough data, skip entry

    highs = arr[1:, 2]
    lows = arr[1:, 3]
    prev_closes = arr[:-1, 4]
    tr = np.maximum(highs - lows, np.maximum(np.abs(highs - prev_closes), np.abs(lows - prev_closes)))

    short_atr = float(np.mean(tr[-short_period:]))
    long_atr = float(np.mean(tr[-long_period:]))
    ratio = short_atr / long_atr if long_atr > 0 else 0.0
    return ratio >= threshold, short_atr, long_atr, ratio


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
    # Check cooldown period (circuit breaker)
    if state_mgr.is_in_cooldown():
        remaining = state_mgr.cooldown_remaining()
        logger.info("[策略] 冷静期中，暂停开仓 | 剩余: %s", remaining)
        return

    try:
        exchange.sync_state(state_mgr)
    except Exception as e:
        logger.warning("[策略] 同步账户失败: %s", e)

    if state_mgr.position_count >= config.MAX_POSITIONS:
        logger.info("Max positions reached, skipping scan")
        return
    if state_mgr.balance < config.POSITION_SIZE:
        logger.info(f"Insufficient balance: {state_mgr.balance:.2f}")
        return

    top_symbols = exchange.get_top_symbols()
    volume_map = exchange.get_volume_map(top_symbols)
    logger.info("=" * 60)
    logger.info("[策略] 开始扫描 %d 个币种 | 余额: $%.2f | 持仓: %d/%d | 过滤模式: %s",
                len(top_symbols), state_mgr.balance, state_mgr.position_count, config.MAX_POSITIONS,
                config.TREND_FILTER_MODE)

    skip_counts = {
        "已持仓": 0,
        "币种冷却": 0,
        "风控黑名单": 0,
        "日线数据不足": 0,
        "无明确趋势": 0,
        "小时线数据不足": 0,
        "24H高低不满足": 0,
        "无突破": 0,
        "异常": 0,
    }

    # +2 for SMA slope comparison, +1 to discard unclosed candle
    daily_kline_limit = config.SMA_PERIOD + 3
    # Rolling SMA needs SMA_PERIOD*24 + slope-step 24h + unclosed bar
    rolling_hours_needed = config.SMA_PERIOD * 24 + 24
    # Need: BB_PERIOD closed bars + 24 lookback bars + 1 signal bar + 1 unclosed
    hourly_bars_needed = max(config.BB_PERIOD, 25)  # 25 = 24 lookback + 1 signal bar
    if config.TREND_FILTER_MODE == "rolling_sma":
        hourly_bars_needed = max(hourly_bars_needed, rolling_hours_needed)
    hourly_kline_limit = hourly_bars_needed + 1  # +1 to discard unclosed candle

    # Phase 1: scan all symbols and collect signals
    signals = []

    for symbol in top_symbols:
        if state_mgr.get_position_by_symbol(symbol):
            logger.info("[跳过] %s | 原因: 已持仓", symbol)
            skip_counts["已持仓"] += 1
            continue

        blacklisted = state_mgr.symbol_blacklist_remaining(symbol)
        if blacklisted:
            remaining, reason = blacklisted
            logger.info("[跳过] %s | 原因: 风控黑名单 (%s, 剩余 %s)",
                        symbol, reason, remaining)
            skip_counts["风控黑名单"] += 1
            continue

        cooldown_left = state_mgr.symbol_cooldown_remaining(
            symbol,
            loss_threshold=config.SYMBOL_LOSS_THRESHOLD,
            window_hours=config.SYMBOL_COOLDOWN_WINDOW_HOURS,
            cooldown_hours=config.SYMBOL_COOLDOWN_HOURS,
        )
        if cooldown_left:
            logger.info("[跳过] %s | 原因: 币种冷却中 (近%dh内%d+次亏损, 剩余 %s)",
                        symbol, config.SYMBOL_COOLDOWN_WINDOW_HOURS,
                        config.SYMBOL_LOSS_THRESHOLD, cooldown_left)
            skip_counts["币种冷却"] += 1
            continue

        try:
            trend = None
            d_middle = 0.0
            mode = config.TREND_FILTER_MODE

            # Fetch hourly bars first — rolling_sma mode needs ~506 of them
            hourly_klines = exchange.get_klines(
                symbol, Client.KLINE_INTERVAL_1HOUR, hourly_kline_limit
            )
            hourly_closes = [float(k[4]) for k in hourly_klines[:-1]]  # drop unclosed candle
            if len(hourly_closes) < config.BB_PERIOD + 1:
                logger.info("[跳过] %s | 原因: 小时线数据不足 (%d/%d)", symbol, len(hourly_closes), config.BB_PERIOD + 1)
                skip_counts["小时线数据不足"] += 1
                continue

            if mode != "disabled":
                if mode == "rolling_sma":
                    if len(hourly_closes) < rolling_hours_needed:
                        logger.info("[跳过] %s | 原因: 小时线数据不足(滚动SMA需 %d/%d)",
                                    symbol, len(hourly_closes), rolling_hours_needed)
                        skip_counts["小时线数据不足"] += 1
                        continue
                    trend = check_trend_rolling(
                        hourly_closes, period_hours=config.SMA_PERIOD * 24, step_hours=24
                    )
                    if trend is None:
                        window = np.asarray(hourly_closes[-config.SMA_PERIOD * 24:], dtype=float)
                        prev_window = np.asarray(
                            hourly_closes[-(config.SMA_PERIOD * 24 + 24):-24], dtype=float
                        )
                        sma_now = float(window.mean())
                        sma_prev = float(prev_window.mean())
                        slope = "↑" if sma_now > sma_prev else ("↓" if sma_now < sma_prev else "→")
                        rel = "↑" if hourly_closes[-1] > sma_now else "↓"
                        logger.info("[跳过] %s | 原因: 无明确趋势(滚动SMA斜率%s, 价格%sSMA)",
                                    symbol, slope, rel)
                        skip_counts["无明确趋势"] += 1
                        continue
                    d_middle = float(np.mean(hourly_closes[-config.SMA_PERIOD * 24:]))
                else:
                    daily_klines = exchange.get_klines(
                        symbol, Client.KLINE_INTERVAL_1DAY, daily_kline_limit
                    )
                    daily_closes = [float(k[4]) for k in daily_klines[:-1]]  # drop unclosed
                    if len(daily_closes) < config.SMA_PERIOD + 1:
                        logger.info("[跳过] %s | 原因: 日线数据不足 (%d/%d)", symbol, len(daily_closes), config.SMA_PERIOD + 1)
                        skip_counts["日线数据不足"] += 1
                        continue
                    if mode == "sma":
                        trend = check_trend(daily_closes, config.SMA_PERIOD)
                    elif mode == "bb_middle":
                        trend = check_trend_bb_middle(daily_closes, config.SMA_PERIOD, config.BB_STD)
                    if trend is None:
                        sma_now = float(np.mean(daily_closes[-config.SMA_PERIOD:]))
                        sma_prev = float(np.mean(daily_closes[-config.SMA_PERIOD - 1:-1]))
                        slope = "↑" if sma_now > sma_prev else ("↓" if sma_now < sma_prev else "→")
                        rel = "↑" if daily_closes[-1] > sma_now else "↓"
                        logger.info("[跳过] %s | 原因: 无明确趋势 (SMA斜率%s, 价格%sSMA)", symbol, slope, rel)
                        skip_counts["无明确趋势"] += 1
                        continue
                    _, d_middle, _ = calculate_bollinger_bands(daily_closes, config.SMA_PERIOD, config.BB_STD)

            h_upper, h_middle, h_lower = calculate_bollinger_bands(hourly_closes, config.BB_PERIOD, config.BB_STD)
            current_close = hourly_closes[-1]
            current_price = exchange.get_price(symbol)

            # 24-bar high/low breakout confirmation
            # hourly_klines[-1] = unclosed, [-2] = signal bar, [-26:-2] = 24 prior bars
            signal_bar = hourly_klines[-2]
            signal_high = float(signal_bar[2])
            signal_low = float(signal_bar[3])
            lookback_klines = hourly_klines[-26:-2]
            if len(lookback_klines) >= 24:
                lookback_highs = [float(k[2]) for k in lookback_klines]
                lookback_lows = [float(k[3]) for k in lookback_klines]
                is_24h_high = signal_high >= max(lookback_highs)
                is_24h_low = signal_low <= min(lookback_lows)
            else:
                is_24h_high = is_24h_low = False

            if mode != "disabled":
                if trend == "LONG" and current_close > h_upper and is_24h_high:
                    signal = True
                elif trend == "SHORT" and current_close < h_lower and is_24h_low:
                    signal = True
                else:
                    signal = False
            else:
                if current_close > h_upper and is_24h_high:
                    signal = True
                    trend = "LONG"
                elif current_close < h_lower and is_24h_low:
                    signal = True
                    trend = "SHORT"
                else:
                    signal = False

            vol = volume_map.get(symbol, 0.0)
            bb_width_pct = (h_upper - h_lower) / h_middle * 100 if h_middle else 0
            close_to_upper_pct = (current_close - h_middle) / (h_upper - h_middle) * 100 if h_upper > h_middle else 0
            signal_tag = ">>> 信号 <<<" if signal else "-"
            logger.info(
                "[扫描] %s | 趋势: %s | 现价: %.4f | 日线中轨: %.4f | "
                "1H收盘: %.4f | BB上/中/下: %.4f/%.4f/%.4f | 带宽 %.2f%% | 位置 %+.0f%% | "
                "24H高: %.4f(%s) | 24H低: %.4f(%s) | 24h量: $%.1fM | %s",
                symbol, trend or "-", current_price, d_middle,
                current_close, h_upper, h_middle, h_lower,
                bb_width_pct, close_to_upper_pct,
                signal_high, "✓" if is_24h_high else "✗",
                signal_low, "✓" if is_24h_low else "✗",
                vol / 1e6,
                signal_tag,
            )

            if not signal:
                # Distinguish BB break without 24H confirmation vs no BB break
                if (trend == "LONG" and current_close > h_upper) or (trend == "SHORT" and current_close < h_lower):
                    skip_counts["24H高低不满足"] += 1
                else:
                    skip_counts["无突破"] += 1
                continue

            signals.append({
                "symbol": symbol,
                "trend": trend,
                "price": current_price,
                "volume": vol,
            })

        except Exception as e:
            logger.error("[策略] %s 处理异常: %s", symbol, e)
            skip_counts["异常"] += 1
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

    skip_summary = " | ".join(f"{k} {v}" for k, v in skip_counts.items() if v > 0)
    if skip_summary:
        logger.info("[策略] 跳过汇总: %s", skip_summary)
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
        order = exchange.place_order(symbol, order_side, quantity, position_side=side)

        # Store open order ID; commission will be queried at close time
        # (trade fills have propagation delay on Binance, not available immediately)
        open_order_id = order.get("orderId")

        # Use Binance-reported fill price and executed qty — market orders can
        # slip from the pre-trade ticker, and recording the ticker here caused
        # PnL reports to diverge from actual account PnL.
        fill_price, executed_qty = exchange.get_order_fill(symbol, open_order_id, current_price)
        if executed_qty <= 0:
            executed_qty = quantity
        slippage_pct = (fill_price - current_price) / current_price * 100 if current_price else 0

        pos = state_mgr.add_position(
            symbol=symbol,
            side=side,
            entry_price=fill_price,
            quantity=executed_qty,
            open_order_id=open_order_id,
        )
        state_mgr.update_balance(-config.POSITION_SIZE)

        close_side = "SELL" if side == "LONG" else "BUY"

        # Fixed stop loss — STOP_MARKET at entry ± 2%
        try:
            raw_sl = (
                fill_price * (1 - config.FIXED_STOP_LOSS_PCT) if side == "LONG"
                else fill_price * (1 + config.FIXED_STOP_LOSS_PCT)
            )
            sl_price = exchange.round_price(symbol, raw_sl)
            if (side == "LONG" and sl_price >= fill_price) or (side == "SHORT" and sl_price <= fill_price):
                logger.warning("[开仓] %s 止损价 %.8f 精度不足（与入场价相同），依赖本地轮询兜底",
                               symbol, sl_price)
            else:
                sl_order = exchange.place_stop_order(
                    symbol, close_side, executed_qty, sl_price, position_side=side
                )
                state_mgr.set_stop_order_id(pos["id"], sl_order.get("orderId"))
                logger.info("[开仓] %s 止损单 orderId=%s 止损价 %.8f", symbol, sl_order.get("orderId"), sl_price)
        except Exception as e:
            logger.error("[开仓] %s 止损单下单失败: %s | 将由本地轮询兜底", symbol, e)

        # 移动止盈单不在开仓时挂，等浮盈达到 3% 时由止损检查任务用当时最高价挂单

        actual_notional = fill_price * executed_qty
        logger.info(
            "[开仓] %s %s | 信号价 %.4f → 成交价 %.4f (滑点 %+.3f%%) | 目标量 %g → 实际量 %g | "
            "实际名义 $%.2f | 保证金 $%.2f | 杠杆 %dx | orderId=%s | 余额 $%.2f",
            symbol, side, current_price, fill_price, slippage_pct,
            quantity, executed_qty, actual_notional,
            config.POSITION_SIZE, config.LEVERAGE, open_order_id, state_mgr.balance,
        )

        # Fetch funding rate info for notification
        funding_msg = ""
        try:
            fi = exchange.get_funding_info(symbol)
            rate_sign = "+" if fi["rate"] >= 0 else ""
            # Positive rate: longs pay shorts; Negative rate: shorts pay longs
            if side == "LONG":
                pay_label = "付出" if fi["rate"] > 0 else "收取"
            else:
                pay_label = "收取" if fi["rate"] > 0 else "付出"
            funding_msg = f"\n资金费率: {rate_sign}{fi['rate_pct']:.4f}% ({pay_label}) | 下次收取: {fi['next_time']}"
        except Exception:
            pass

        sl_price = (
            fill_price * (1 - config.FIXED_STOP_LOSS_PCT) if side == "LONG"
            else fill_price * (1 + config.FIXED_STOP_LOSS_PCT)
        )
        notify(
            f"开仓 {side}",
            f"{symbol} | 成交价 {fill_price:.4f} | 数量 {executed_qty:g} | "
            f"保证金 ${config.POSITION_SIZE}\n"
            f"固定止损: {sl_price:.4f} ({config.FIXED_STOP_LOSS_PCT*100:.0f}%){funding_msg}",
        )
    except BinanceAPIException as e:
        if getattr(e, "code", None) in POSITION_RISK_ERROR_CODES:
            state_mgr.add_symbol_blacklist(
                symbol,
                reason=f"Binance风控拒单({e.code})",
                hours=config.POSITION_RISK_BLACKLIST_HOURS,
            )
            logger.error(
                "[开仓] %s 被 Binance 风控拒单 (code=%s): %s | 已加入 %dh 黑名单",
                symbol, e.code, e.message, config.POSITION_RISK_BLACKLIST_HOURS,
            )
            notify(
                "币种加入黑名单",
                f"{symbol} {side} 被 Binance 风控拒单 (code={e.code})\n"
                f"已加入 {config.POSITION_RISK_BLACKLIST_HOURS}h 黑名单",
            )
        else:
            logger.error(f"Failed to open {side} {symbol}: {e}")
    except Exception as e:
        logger.error(f"Failed to open {side} {symbol}: {e}")

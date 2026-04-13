import numpy as np
from typing import List
from config import config
from exchange import Exchange
from state import StateManager
from notifier import notify, logger
from binance.client import Client


def calculate_pnl(side: str, entry_price: float, exit_price: float) -> float:
    """Calculate PnL for a position."""
    if side == "LONG":
        return (exit_price - entry_price) / entry_price * config.POSITION_SIZE * config.LEVERAGE
    else:
        return (entry_price - exit_price) / entry_price * config.POSITION_SIZE * config.LEVERAGE


def calculate_atr(klines: list, period: int = 14) -> float:
    """Calculate Average True Range from klines.
    Each kline: [open_time, open, high, low, close, volume, ...]."""
    if len(klines) < period + 1:
        return 0.0
    true_ranges = []
    for i in range(1, len(klines)):
        high = float(klines[i][2])
        low = float(klines[i][3])
        prev_close = float(klines[i - 1][4])
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)
    return float(np.mean(true_ranges[-period:]))


def should_stop_loss(
    side: str,
    highest_price: float,
    lowest_price: float,
    current_price: float,
    atr: float,
    atr_multiplier: float,
    max_stop_pct: float,
) -> bool:
    """Check if ATR trailing stop should trigger, with a hard cap."""
    if side == "LONG":
        atr_stop = highest_price - atr_multiplier * atr
        hard_stop = highest_price * (1 - max_stop_pct)
        stop_price = max(atr_stop, hard_stop)  # use the tighter one
        return current_price <= stop_price
    else:
        atr_stop = lowest_price + atr_multiplier * atr
        hard_stop = lowest_price * (1 + max_stop_pct)
        stop_price = min(atr_stop, hard_stop)  # use the tighter one
        return current_price >= stop_price


def check_drawdown(exchange: Exchange, state_mgr: StateManager) -> bool:
    """Check if total assets have dropped beyond MAX_DRAWDOWN_PCT.
    If triggered, force-close all positions and enter cooldown.
    Returns True if circuit breaker was triggered."""
    if state_mgr.is_in_cooldown():
        return False  # already in cooldown, positions already closed

    try:
        summary = exchange.get_account_summary()
        total_assets = summary["total_margin_balance"]
    except Exception as e:
        logger.warning("[熔断] 获取账户数据失败: %s", e)
        return False

    drawdown_pct = (config.INITIAL_CAPITAL - total_assets) / config.INITIAL_CAPITAL
    threshold = config.MAX_DRAWDOWN_PCT

    if drawdown_pct < threshold:
        return False

    # Circuit breaker triggered!
    logger.warning("[熔断] 总资产 $%.2f, 回撤 %.1f%% 超过阈值 %.0f%%, 触发强制平仓!",
                   total_assets, drawdown_pct * 100, threshold * 100)

    positions = list(state_mgr.state.get("positions", []))
    closed_count = 0
    for pos in positions:
        try:
            current_price = exchange.get_price(pos["symbol"])
            _close_position(exchange, state_mgr, pos, current_price, "熔断强平")
            closed_count += 1
        except Exception as e:
            logger.error("[熔断] 平仓 %s 失败: %s", pos["symbol"], e)

    state_mgr.set_cooldown(config.COOLDOWN_HOURS)

    loss_pct = drawdown_pct * 100
    notify(
        "⚠ 熔断触发 — 全部平仓",
        f"总资产: ${total_assets:.2f} | 亏损: -{loss_pct:.1f}%\n"
        f"已平仓 {closed_count}/{len(positions)} 个仓位\n"
        f"进入冷静期 {config.COOLDOWN_HOURS} 小时，期间暂停开仓",
    )
    return True


def check_stop_loss(exchange: Exchange, state_mgr: StateManager):
    """Check all open positions for ATR trailing stop triggers."""
    # Check global drawdown circuit breaker first
    if check_drawdown(exchange, state_mgr):
        return  # all positions closed, skip individual checks

    positions = list(state_mgr.state.get("positions", []))
    if not positions:
        return

    logger.info("[止损] 检查 %d 个持仓", len(positions))

    for pos in positions:
        try:
            current_price = exchange.get_price(pos["symbol"])
            state_mgr.update_extreme_price(pos["id"], current_price)

            # Fetch 1H klines for ATR calculation
            atr_kline_limit = config.ATR_PERIOD + 2
            hourly_klines = exchange.get_klines(
                pos["symbol"], Client.KLINE_INTERVAL_1HOUR, atr_kline_limit
            )
            atr = calculate_atr(hourly_klines, config.ATR_PERIOD)

            if pos["side"] == "LONG":
                atr_stop = pos["highest_price"] - config.ATR_MULTIPLIER * atr
                hard_stop = pos["highest_price"] * (1 - config.MAX_STOP_LOSS)
                stop_price = max(atr_stop, hard_stop)
                drawdown_pct = (pos["highest_price"] - current_price) / pos["highest_price"] * 100
                label = "回撤"
                extreme_label = "最高"
                extreme_price = pos["highest_price"]
            else:
                atr_stop = pos["lowest_price"] + config.ATR_MULTIPLIER * atr
                hard_stop = pos["lowest_price"] * (1 + config.MAX_STOP_LOSS)
                stop_price = min(atr_stop, hard_stop)
                drawdown_pct = (current_price - pos["lowest_price"]) / pos["lowest_price"] * 100
                label = "反弹"
                extreme_label = "最低"
                extreme_price = pos["lowest_price"]

            triggered = should_stop_loss(
                side=pos["side"],
                highest_price=pos["highest_price"],
                lowest_price=pos["lowest_price"],
                current_price=current_price,
                atr=atr,
                atr_multiplier=config.ATR_MULTIPLIER,
                max_stop_pct=config.MAX_STOP_LOSS,
            )

            status = "触发止损!" if triggered else "安全"

            logger.info(
                "[止损] %s %s | 入场: %.4f | %s: %.4f | 现价: %.4f | "
                "ATR: %.4f | 止损线: %.4f | %s: %.2f%% | %s",
                pos["symbol"], pos["side"],
                pos["entry_price"],
                extreme_label, extreme_price,
                current_price,
                atr, stop_price,
                label, drawdown_pct,
                status,
            )

            if triggered:
                _close_position(exchange, state_mgr, pos, current_price, "ATR移动止损")

        except Exception as e:
            logger.error("[止损] %s 检查异常: %s", pos["symbol"], e)


def _close_position(
    exchange: Exchange,
    state_mgr: StateManager,
    pos: dict,
    exit_price: float,
    reason: str = "ATR移动止损",
):
    """Close a position via market order."""
    close_side = "SELL" if pos["side"] == "LONG" else "BUY"

    try:
        exchange.place_order(pos["symbol"], close_side, pos["quantity"])

        pnl = calculate_pnl(pos["side"], pos["entry_price"], exit_price)

        state_mgr.remove_position(pos["id"])
        state_mgr.add_trade_history(
            symbol=pos["symbol"],
            side=pos["side"],
            entry_price=pos["entry_price"],
            exit_price=exit_price,
            quantity=pos["quantity"],
            pnl=pnl,
            opened_at=pos["opened_at"],
        )
        state_mgr.update_balance(config.POSITION_SIZE + pnl)

        notify(
            f"平仓 {pos['side']} ({reason})",
            f"{pos['symbol']} | 入场 {pos['entry_price']:.4f} | 出场 {exit_price:.4f} | PnL ${pnl:.2f}",
        )
    except Exception as e:
        logger.error(f"Failed to close {pos['symbol']}: {e}")

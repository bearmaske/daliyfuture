from datetime import datetime, timezone, timedelta
from typing import List

import numpy as np

from config import config
from exchange import Exchange
from state import StateManager
from notifier import notify, logger

TZ_CN = timezone(timedelta(hours=8))


def _position_age(opened_at: str) -> str:
    """Return human-readable age of a position, e.g. '3h12m' or '45m'."""
    if not opened_at:
        return "?"
    fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")
    dt = None
    for f in fmts:
        try:
            dt = datetime.strptime(opened_at, f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_CN)
            break
        except ValueError:
            continue
    if dt is None:
        return "?"
    seconds = int((datetime.now(TZ_CN) - dt).total_seconds())
    if seconds < 0:
        return "0m"
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days > 0:
        return f"{days}d{hours}h"
    if hours > 0:
        return f"{hours}h{minutes}m"
    return f"{minutes}m"


def calculate_pnl(side: str, entry_price: float, exit_price: float, quantity: float = None) -> float:
    """Calculate PnL. When quantity is given, uses qty × (exit − entry) which
    matches Binance's exchange-side accounting. Without quantity, falls back to
    the notional formula (used for open-position unrealized previews where the
    displayed quantity hasn't been reconciled yet)."""
    if quantity is not None and quantity > 0:
        if side == "LONG":
            return (exit_price - entry_price) * quantity
        return (entry_price - exit_price) * quantity
    if side == "LONG":
        return (exit_price - entry_price) / entry_price * config.POSITION_SIZE * config.LEVERAGE
    return (entry_price - exit_price) / entry_price * config.POSITION_SIZE * config.LEVERAGE


def check_fixed_sl(side: str, entry_price: float, current_price: float, sl_pct: float) -> bool:
    """Fixed stop loss: close if price moves sl_pct against entry."""
    if side == "LONG":
        return current_price <= entry_price * (1 - sl_pct)
    return current_price >= entry_price * (1 + sl_pct)


def check_trailing_tp(
    side: str,
    entry_price: float,
    extreme_price: float,
    current_price: float,
    trailing_activated: bool,
    activation_pct: float,
    drawdown_pct: float,
) -> tuple[bool, bool]:
    """Activation-based trailing TP.

    Returns (triggered, newly_activated).
    - Activates when floating profit >= activation_pct from entry.
    - After activation, exits when price retraces drawdown_pct from extreme.
    """
    if side == "LONG":
        profit_pct = (current_price - entry_price) / entry_price
        newly_activated = (not trailing_activated) and profit_pct >= activation_pct
        if trailing_activated or newly_activated:
            return current_price <= extreme_price * (1 - drawdown_pct), newly_activated
    else:
        profit_pct = (entry_price - current_price) / entry_price
        newly_activated = (not trailing_activated) and profit_pct >= activation_pct
        if trailing_activated or newly_activated:
            return current_price >= extreme_price * (1 + drawdown_pct), newly_activated
    return False, False


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


def _check_exchange_order(exchange, state_mgr, pos, order_id, reason, other_order_id_key):
    """Query an exchange order. Returns True if position was closed (FILLED).
    Handles CANCELED/EXPIRED by re-placing. Returns False to continue normal checks."""
    try:
        order_info = exchange.get_order_status(pos["symbol"], order_id)
        status = order_info["status"]

        if status == "FILLED":
            fill_price = order_info["avgPrice"] or 0
            executed_qty = order_info["executedQty"] or pos["quantity"]
            logger.info("[止损] %s %s | 交易所%s单已触发 @ %.4f | orderId=%s",
                        pos["symbol"], pos["side"], reason, fill_price, order_id)
            # OCO: cancel the other order
            other_id = pos.get(other_order_id_key)
            if other_id:
                exchange.cancel_order(pos["symbol"], other_id)
            _record_position_close(
                state_mgr, pos, fill_price, executed_qty,
                close_order_id=order_id, reason=reason,
            )
            _notify_close(pos, fill_price, executed_qty, reason)
            return True

        if status in ("CANCELED", "EXPIRED"):
            logger.warning("[止损] %s %s单 %s 状态 %s，重新挂单",
                           pos["symbol"], reason, order_id, status)
            if reason == "固定止损":
                _replace_stop_order(exchange, state_mgr, pos)
            else:
                _replace_trailing_order(exchange, state_mgr, pos)

    except Exception as e:
        logger.warning("[止损] %s 查询%s单失败: %s", pos["symbol"], reason, e)

    return False


def check_stop_loss(exchange: Exchange, state_mgr: StateManager):
    """Check all open positions.

    Primary path: query exchange orders (STOP_MARKET + TRAILING_STOP_MARKET).
    OCO logic is manual — when one fires we cancel the other.
    Local fallback runs only when exchange order is missing or query fails.
    """
    if check_drawdown(exchange, state_mgr):
        return

    positions = list(state_mgr.state.get("positions", []))
    if not positions:
        return

    logger.info("[止损] 检查 %d 个持仓", len(positions))

    for pos in positions:
        try:
            current_price = exchange.get_price(pos["symbol"])
            state_mgr.update_extreme_price(pos["id"], current_price)

            stop_order_id = pos.get("stop_order_id")
            trailing_order_id = pos.get("trailing_order_id")
            trailing_activated = pos.get("trailing_activated", False)

            if pos["side"] == "LONG":
                extreme_price = pos["highest_price"]
                profit_pct = (current_price - pos["entry_price"]) / pos["entry_price"] * 100
                extreme_label = "最高"
            else:
                extreme_price = pos["lowest_price"]
                profit_pct = (pos["entry_price"] - current_price) / pos["entry_price"] * 100
                extreme_label = "最低"

            fixed_sl_price = (
                pos["entry_price"] * (1 - config.FIXED_STOP_LOSS_PCT)
                if pos["side"] == "LONG"
                else pos["entry_price"] * (1 + config.FIXED_STOP_LOSS_PCT)
            )
            trail_stop_price = (
                extreme_price * (1 - config.TRAILING_DRAWDOWN_PCT)
                if pos["side"] == "LONG"
                else extreme_price * (1 + config.TRAILING_DRAWDOWN_PCT)
            )

            # --- Fixed SL (exchange) ---
            if stop_order_id:
                if _check_exchange_order(exchange, state_mgr, pos,
                                         stop_order_id, "固定止损", "trailing_order_id"):
                    continue
            else:
                _replace_stop_order(exchange, state_mgr, pos)
                if check_fixed_sl(pos["side"], pos["entry_price"], current_price,
                                  config.FIXED_STOP_LOSS_PCT):
                    _close_position(exchange, state_mgr, pos, current_price, "固定止损(兜底)")
                    continue

            # --- Trailing TP (exchange, with local fallback) ---
            if trailing_order_id:
                if _check_exchange_order(exchange, state_mgr, pos,
                                         trailing_order_id, "移动止盈", "stop_order_id"):
                    continue
                # Exchange order alive — no local check needed
                trailing_label = "交易所挂单中"
            else:
                # No exchange trailing order → local fallback
                _replace_trailing_order(exchange, state_mgr, pos)
                trailing_triggered, newly_activated = check_trailing_tp(
                    side=pos["side"],
                    entry_price=pos["entry_price"],
                    extreme_price=extreme_price,
                    current_price=current_price,
                    trailing_activated=trailing_activated,
                    activation_pct=config.TRAILING_ACTIVATION_PCT,
                    drawdown_pct=config.TRAILING_DRAWDOWN_PCT,
                )
                if newly_activated and not trailing_activated:
                    state_mgr.set_trailing_activated(pos["id"])
                    trailing_activated = True
                    logger.info("[止损] %s %s | 移动止盈本地激活 | 浮盈 %+.2f%%",
                                pos["symbol"], pos["side"], profit_pct)
                if trailing_triggered:
                    _close_position(exchange, state_mgr, pos, current_price, "移动止盈(兜底)")
                    continue
                trailing_label = (
                    "本地激活" if trailing_activated
                    else f"本地待激活(需浮盈≥{config.TRAILING_ACTIVATION_PCT*100:.0f}%)"
                )

            unrealized = calculate_pnl(pos["side"], pos["entry_price"], current_price, pos.get("quantity"))
            unrealized_pct = unrealized / config.POSITION_SIZE * 100
            age = _position_age(pos.get("opened_at"))

            logger.info(
                "[止损] %s %s | 持仓 %s | 入场: %.4f | %s: %.4f | 现价: %.4f | "
                "PnL: $%+.2f (%+.1f%%) | 固定止损: %.4f | 移动止盈: %s (线 %.4f) | 安全",
                pos["symbol"], pos["side"], age,
                pos["entry_price"],
                extreme_label, extreme_price,
                current_price,
                unrealized, unrealized_pct,
                fixed_sl_price,
                trailing_label,
                trail_stop_price,
            )

        except Exception as e:
            logger.error("[止损] %s 检查异常: %s", pos["symbol"], e)


def _replace_stop_order(exchange: Exchange, state_mgr, pos: dict):
    """Re-place the STOP_MARKET fixed SL order."""
    try:
        sl_price = exchange.round_price(
            pos["symbol"],
            pos["entry_price"] * (1 - config.FIXED_STOP_LOSS_PCT) if pos["side"] == "LONG"
            else pos["entry_price"] * (1 + config.FIXED_STOP_LOSS_PCT),
        )
        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        sl_order = exchange.place_stop_order(
            pos["symbol"], close_side, pos["quantity"], sl_price, position_side=pos["side"]
        )
        new_id = sl_order.get("orderId")
        state_mgr.set_stop_order_id(pos["id"], new_id)
        logger.info("[止损] %s 新止损单 orderId=%s 止损价 %.4f", pos["symbol"], new_id, sl_price)
    except Exception as e:
        logger.error("[止损] %s 重新挂止损单失败: %s", pos["symbol"], e)


def _replace_trailing_order(exchange: Exchange, state_mgr, pos: dict):
    """Re-place the TRAILING_STOP_MARKET order. If activationPrice is already
    past current price (Binance error -2021), clear the order ID so local
    trailing logic takes over as fallback."""
    try:
        activation_price = exchange.round_price(
            pos["symbol"],
            pos["entry_price"] * (1 + config.TRAILING_ACTIVATION_PCT) if pos["side"] == "LONG"
            else pos["entry_price"] * (1 - config.TRAILING_ACTIVATION_PCT),
        )
        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        tp_order = exchange.place_trailing_stop_order(
            pos["symbol"], close_side, pos["quantity"],
            activation_price=activation_price,
            callback_rate=config.TRAILING_DRAWDOWN_PCT * 100,
            position_side=pos["side"],
        )
        new_id = tp_order.get("orderId")
        state_mgr.set_trailing_order_id(pos["id"], new_id)
        logger.info("[止损] %s 新移动止盈单 orderId=%s 激活价 %.4f",
                    pos["symbol"], new_id, activation_price)
    except Exception as e:
        # -2021: activation price already past current price; fall back to local logic
        logger.warning("[止损] %s 重新挂移动止盈单失败(%s)，切换本地兜底", pos["symbol"], e)
        state_mgr.set_trailing_order_id(pos["id"], None)


def _record_position_close(
    state_mgr,
    pos: dict,
    exit_price: float,
    executed_qty: float,
    close_order_id,
    reason: str,
):
    """Record a position close that was already executed on the exchange
    (e.g. STOP_MARKET order filled). Does NOT place a new order."""
    raw_pnl = calculate_pnl(pos["side"], pos["entry_price"], exit_price, executed_qty)

    state_mgr.remove_position(pos["id"])
    state_mgr.add_trade_history(
        symbol=pos["symbol"],
        side=pos["side"],
        entry_price=pos["entry_price"],
        exit_price=exit_price,
        quantity=executed_qty,
        pnl=raw_pnl,
        commission=None,
        open_order_id=pos.get("open_order_id"),
        close_order_id=close_order_id,
        opened_at=pos["opened_at"],
    )
    state_mgr.update_balance(config.POSITION_SIZE + raw_pnl)

    pnl_pct = raw_pnl / config.POSITION_SIZE * 100
    age = _position_age(pos.get("opened_at"))
    logger.info(
        "[平仓] %s %s (%s) | 持仓 %s | 入场 %.4f → 成交价 %.4f | "
        "数量 %g | PnL $%+.2f (%+.1f%%) | orderId=%s | 余额 $%.2f",
        pos["symbol"], pos["side"], reason, age,
        pos["entry_price"], exit_price,
        executed_qty, raw_pnl, pnl_pct, close_order_id, state_mgr.balance,
    )


def _notify_close(pos: dict, exit_price: float, executed_qty: float, reason: str):
    raw_pnl = calculate_pnl(pos["side"], pos["entry_price"], exit_price, executed_qty)
    notify(
        f"平仓 {pos['side']} ({reason})",
        f"{pos['symbol']} | 入场 {pos['entry_price']:.4f} | 出场 {exit_price:.4f} | "
        f"数量 {executed_qty:g} | PnL ${raw_pnl:.2f} (手续费待结算)",
    )


def _close_position(
    exchange: Exchange,
    state_mgr: StateManager,
    pos: dict,
    exit_price: float,
    reason: str = "移动止盈",
):
    """Close a position via market order. Cancels the exchange stop order first."""
    # Cancel both exchange orders before placing close market order (OCO cleanup)
    for oid_key in ("stop_order_id", "trailing_order_id"):
        oid = pos.get(oid_key)
        if oid:
            exchange.cancel_order(pos["symbol"], oid)

    close_side = "SELL" if pos["side"] == "LONG" else "BUY"

    try:
        order = exchange.place_order(pos["symbol"], close_side, pos["quantity"], position_side=pos["side"])

        open_order_id = pos.get("open_order_id")
        close_order_id = order.get("orderId")

        actual_exit, executed_qty = exchange.get_order_fill(pos["symbol"], close_order_id, exit_price)
        if executed_qty <= 0:
            executed_qty = pos["quantity"]
        slippage_pct = (actual_exit - exit_price) / exit_price * 100 if exit_price else 0

        _record_position_close(state_mgr, pos, actual_exit, executed_qty, close_order_id, reason)

        pnl_pct = calculate_pnl(pos["side"], pos["entry_price"], actual_exit, executed_qty) / config.POSITION_SIZE * 100
        age = _position_age(pos.get("opened_at"))
        logger.info(
            "[平仓] %s %s (%s) | 持仓 %s | 信号价 %.4f → 成交价 %.4f (滑点 %+.3f%%) | "
            "数量 %g | PnL %+.1f%% | orderId=%s",
            pos["symbol"], pos["side"], reason, age,
            exit_price, actual_exit, slippage_pct,
            executed_qty, pnl_pct, close_order_id,
        )

        _notify_close(pos, actual_exit, executed_qty, reason)
    except Exception as e:
        logger.error(f"Failed to close {pos['symbol']}: {e}")

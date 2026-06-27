from datetime import datetime, timezone, timedelta
from typing import List

import numpy as np

from config import config
from exchange import Exchange
from state import StateManager
from notifier import notify, logger

TZ_CN = timezone(timedelta(hours=8))


def _parse_position_dt(value: str):
    """Parse opened_at in any historical format. Returns aware datetime or None."""
    if not value:
        return None
    fmts = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")
    for f in fmts:
        try:
            dt = datetime.strptime(value, f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ_CN)
            return dt
        except ValueError:
            continue
    return None


def _position_age(opened_at: str) -> str:
    """Return human-readable age of a position, e.g. '3h12m' or '45m'."""
    dt = _parse_position_dt(opened_at)
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


def _pos_hard_stop_pct(pos: dict) -> float:
    """硬止损距离：仓位字段优先，存量仓位回退 FIXED_STOP_LOSS_PCT。"""
    return pos.get("hard_stop_pct") or config.FIXED_STOP_LOSS_PCT


def _pos_margin(pos: dict) -> float:
    """本笔保证金：仓位字段优先，存量仓位回退 POSITION_SIZE。"""
    return pos.get("position_size") or config.POSITION_SIZE


def calculate_pnl(side: str, entry_price: float, exit_price: float,
                  quantity: float = None, position_size: float = None) -> float:
    """Calculate PnL. When quantity is given, uses qty × (exit − entry) which
    matches Binance's exchange-side accounting. Without quantity, falls back to
    the notional formula (position_size × leverage; 等风险缩仓后各笔不同)."""
    if quantity is not None and quantity > 0:
        if side == "LONG":
            return (exit_price - entry_price) * quantity
        return (entry_price - exit_price) * quantity
    size = position_size or config.POSITION_SIZE
    if side == "LONG":
        return (exit_price - entry_price) / entry_price * size * config.LEVERAGE
    return (entry_price - exit_price) / entry_price * size * config.LEVERAGE


def calculate_atr(highs: List[float], lows: List[float], closes: List[float],
                  period: int = 14) -> float:
    """Wilder ATR。数据不足（< period+1 根）或长度不一致时返回 0.0。
    调用方负责丢掉最后一根未收盘 K 线（项目惯例）。"""
    n = len(closes)
    if period <= 0 or n < period + 1 or len(highs) != n or len(lows) != n:
        return 0.0
    trs = []
    for i in range(1, n):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        trs.append(tr)
    atr = sum(trs[:period]) / period
    for tr in trs[period:]:
        atr = (atr * (period - 1) + tr) / period
    return atr


def compute_stop_distances(atr: float, entry_price: float) -> tuple[float, float]:
    """软/硬止损距离（占入场价的比例）。
    软 = clamp(SOFT_STOP_ATR_MULT × ATR / 价格, floor=SOFT_STOP_FLOOR_PCT, cap=HARD_STOP_CAP_PCT)
    硬 = min(HARD_STOP_MULT × 软, HARD_STOP_CAP_PCT)。软 ≤ 硬 恒成立。
    ATR 缺失（=0）时退化为 floor。"""
    if atr <= 0 or entry_price <= 0:
        soft = config.SOFT_STOP_FLOOR_PCT
    else:
        soft = max(config.SOFT_STOP_FLOOR_PCT,
                   config.SOFT_STOP_ATR_MULT * atr / entry_price)
    soft = min(soft, config.HARD_STOP_CAP_PCT)
    hard = min(config.HARD_STOP_MULT * soft, config.HARD_STOP_CAP_PCT)
    return soft, hard


def compute_position_size(soft_stop_pct: float) -> tuple[float, float]:
    """等风险仓位：名义 = RISK_PER_TRADE_USD / 软止损%，封顶 MAX_NOTIONAL_USD。
    返回 (名义, 保证金)。"""
    notional = min(config.RISK_PER_TRADE_USD / soft_stop_pct, config.MAX_NOTIONAL_USD)
    return notional, notional / config.LEVERAGE


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


def check_phase_exit(
    side: str,
    bar_close: float,
    bb_middle: float,
    pre_bar_extreme: float,
    trailing_pct: float,
):
    """Phase-mode 1H-close exit. BB-middle cross takes precedence over the
    3.5% confirmed-extreme retrace. Mirrors phase_filter_backtest._check_exits.
    Returns "1h_bb_middle", "trailing_3.5pct", or None.
    """
    if side == "LONG":
        if bar_close < bb_middle:
            return "1h_bb_middle"
        if bar_close <= pre_bar_extreme * (1 - trailing_pct):
            return "trailing_3.5pct"
    else:
        if bar_close > bb_middle:
            return "1h_bb_middle"
        if bar_close >= pre_bar_extreme * (1 + trailing_pct):
            return "trailing_3.5pct"
    return None


def compute_phase_exit_inputs(
    closed_klines: list,
    opened_ms: int,
    entry_price: float,
    bb_period: int = 20,
):
    """From CLOSED 1H klines, derive the inputs for check_phase_exit on the
    just-closed bar. Returns (bar_close, bb_middle, pre_high, pre_low) or None.

    closed_klines: list of [open_time, open, high, low, close, ...] (unclosed
                   candle already dropped by caller), ascending by open_time.
    pre_high/pre_low EXCLUDE the entry bar (open_time == opened_ms) and the
    just-closed bar — matching the reference engine's exit-before-entry order.
    Returns None while only the entry bar has closed (breathe one bar) or when
    there are fewer than bb_period closed bars.
    """
    if len(closed_klines) < bb_period:
        return None
    last = closed_klines[-1]
    last_ot = int(last[0])
    if last_ot <= int(opened_ms):
        return None  # still on/at the entry bar — breathe

    closes = [float(k[4]) for k in closed_klines]
    bb_middle = float(np.mean(closes[-bb_period:]))  # BB middle band == SMA(period)
    bar_close = float(last[4])

    pre_high = entry_price
    pre_low = entry_price
    for k in closed_klines[:-1]:                 # exclude just-closed bar
        ot = int(k[0])
        if ot <= int(opened_ms):                 # exclude entry bar and earlier
            continue
        pre_high = max(pre_high, float(k[2]))
        pre_low = min(pre_low, float(k[3]))
    return bar_close, bb_middle, pre_high, pre_low


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


def _check_exchange_order(exchange, state_mgr, pos, order_id, reason,
                          other_order_id_key, extreme_price: float = 0.0):
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
                _place_trailing_order(exchange, state_mgr, pos, extreme_price)

    except Exception as e:
        logger.warning("[止损] %s 查询%s单失败: %s", pos["symbol"], reason, e)

    return False


def _sync_positions_with_exchange(exchange: Exchange, state_mgr: StateManager):
    """Compare local positions against exchange. For any local position that no
    longer exists on the exchange, query its algo stop order to get the fill
    price and record the close. This catches stop-loss triggers that happened
    while the bot was offline or missed during polling."""
    try:
        exchange_positions = exchange.get_open_positions()
    except Exception as e:
        logger.warning("[同步] 获取交易所持仓失败，跳过同步: %s", e)
        return

    exchange_symbols = {p["symbol"] for p in exchange_positions}
    local_positions = list(state_mgr.state.get("positions", []))

    for pos in local_positions:
        if pos["symbol"] in exchange_symbols:
            continue  # still open, no action needed

        # Position exists locally but not on exchange — it was closed
        logger.info("[同步] %s %s 已在交易所平仓，同步本地状态", pos["symbol"], pos["side"])

        fill_price = 0.0
        executed_qty = pos["quantity"]
        close_order_id = None
        reason = "交易所止损(同步)"

        # Try to get fill price from the algo stop order
        for order_key, order_reason in [("stop_order_id", "固定止损"), ("trailing_order_id", "移动止盈")]:
            order_id = pos.get(order_key)
            if not order_id:
                continue
            try:
                info = exchange.get_order_status(pos["symbol"], order_id)
                if info["status"] == "FILLED" and info["avgPrice"] > 0:
                    fill_price = info["avgPrice"]
                    executed_qty = info["executedQty"] or executed_qty
                    close_order_id = order_id
                    reason = order_reason
                    # Cancel the other order
                    other_key = "trailing_order_id" if order_key == "stop_order_id" else "stop_order_id"
                    other_id = pos.get(other_key)
                    if other_id:
                        exchange.cancel_order(pos["symbol"], other_id)
                    break
            except Exception as e:
                logger.debug("[同步] 查询 %s 订单 %s 失败: %s", order_reason, order_id, e)

        if fill_price <= 0:
            # Couldn't get fill price from algo order — use last known price
            try:
                fill_price = exchange.get_price(pos["symbol"])
            except Exception:
                fill_price = pos["entry_price"]
            logger.warning("[同步] %s 无法获取成交价，用当前价 %.4f 估算", pos["symbol"], fill_price)

        _record_position_close(state_mgr, pos, fill_price, executed_qty, close_order_id, reason)
        _notify_close(pos, fill_price, executed_qty, reason)


def _check_soft_stops(exchange: Exchange, state_mgr: StateManager, now: datetime = None):
    """软止损：每个整点后的第一个风控 tick，用最近一根已收盘 1H 的收盘价确认。
    收盘价越过软止损线 → 市价平仓。仅 atr_dual 模式、仅带 soft_stop_pct 的仓位。
    本小时内开的仓跳过（第一次确认等下个整点 → 至少扛过第一根 K 线）。
    K 线未滚动竞态保护：若 kl[-2] 的 open_time 早于上上整点，说明 Binance 还没推新 bar；
    恢复 hour_key 让下一个 tick 重试本小时。"""
    if config.STOP_MODE != "atr_dual":
        return
    now = now or datetime.now(TZ_CN)
    hour_key = now.strftime("%Y-%m-%d %H")
    if state_mgr.last_soft_check_hour == hour_key:
        return
    prev_key = state_mgr.last_soft_check_hour
    state_mgr.set_last_soft_check_hour(hour_key)

    hour_start = now.replace(minute=0, second=0, microsecond=0)
    hour_start_ms = int(hour_start.timestamp() * 1000)
    HOUR_MS = 3_600_000
    stale = False
    for pos in list(state_mgr.state.get("positions", [])):
        soft_pct = pos.get("soft_stop_pct")
        if not soft_pct:
            continue  # 存量仓位（旧逻辑）
        opened = _parse_position_dt(pos.get("opened_at"))
        if opened is None or opened >= hour_start:
            continue
        try:
            kl = exchange.get_klines(pos["symbol"], "1h", 2)
            bar_open_ms = int(kl[-2][0])
            if bar_open_ms < hour_start_ms - HOUR_MS:
                stale = True  # K线尚未滚动，整小时稍后重试
                continue
            bar_close = float(kl[-2][4])  # 最近一根已收盘 1H 的收盘价
        except Exception as e:
            logger.warning("[软止损] %s 拉K线失败,本小时跳过: %s", pos["symbol"], e)
            continue
        if check_fixed_sl(pos["side"], pos["entry_price"], bar_close, soft_pct):
            logger.info("[软止损] %s %s | 1H收盘 %.4f 越过软止损线 (入场 %.4f, %.2f%%) | 平仓",
                        pos["symbol"], pos["side"], bar_close,
                        pos["entry_price"], soft_pct * 100)
            _close_position(exchange, state_mgr, pos, bar_close, "软止损(1H收盘)")

    if stale:
        # 有 symbol 的刚收盘 K 线还没出来：恢复上一个 hour key，下个 tick 重试本小时
        state_mgr.set_last_soft_check_hour(prev_key or "")


def check_stop_loss(exchange: Exchange, state_mgr: StateManager):
    """Check all open positions.

    Primary path: query exchange orders (STOP_MARKET + TRAILING_STOP_MARKET).
    OCO logic is manual — when one fires we cancel the other.
    Local fallback runs only when exchange order is missing or query fails.
    """
    if check_drawdown(exchange, state_mgr):
        return

    # Sync local state with exchange first — catches stops triggered while offline
    _sync_positions_with_exchange(exchange, state_mgr)
    _check_soft_stops(exchange, state_mgr)

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

            hard_pct = _pos_hard_stop_pct(pos)
            fixed_sl_price = (
                pos["entry_price"] * (1 - hard_pct)
                if pos["side"] == "LONG"
                else pos["entry_price"] * (1 + hard_pct)
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
                                  hard_pct):
                    _close_position(exchange, state_mgr, pos, current_price, "固定止损(兜底)")
                    continue

            # --- Trailing TP ---
            # Activation threshold: extreme_price >= entry * (1 + 3%)
            activation_reached = (
                pos["highest_price"] >= pos["entry_price"] * (1 + config.TRAILING_ACTIVATION_PCT)
                if pos["side"] == "LONG"
                else pos["lowest_price"] <= pos["entry_price"] * (1 - config.TRAILING_ACTIVATION_PCT)
            )

            if trailing_order_id:
                # Exchange order alive — query status (OCO)
                if _check_exchange_order(exchange, state_mgr, pos,
                                         trailing_order_id, "移动止盈", "stop_order_id",
                                         extreme_price=extreme_price):
                    continue
                trailing_label = "交易所挂单中"
            elif activation_reached:
                # Threshold reached but no order — place now using current extreme_price
                _place_trailing_order(exchange, state_mgr, pos, extreme_price)
                trailing_order_id = pos.get("trailing_order_id")
                trailing_label = "移动止盈已激活" if trailing_order_id else "激活-挂单失败(本地兜底)"
                trail_stop = (
                    extreme_price * (1 - config.TRAILING_DRAWDOWN_PCT)
                    if pos["side"] == "LONG"
                    else extreme_price * (1 + config.TRAILING_DRAWDOWN_PCT)
                )
                act_profit_pct = (
                    (current_price - pos["entry_price"]) / pos["entry_price"] * 100
                    if pos["side"] == "LONG"
                    else (pos["entry_price"] - current_price) / pos["entry_price"] * 100
                )
                notify(
                    f"移动止盈已激活 {pos['side']}",
                    f"{pos['symbol']} | 浮盈 +{act_profit_pct:.1f}% | 现价 {current_price:.4f}\n"
                    f"激活价 {extreme_price:.4f} | 止盈线 {trail_stop:.4f} (回撤 {config.TRAILING_DRAWDOWN_PCT*100:.0f}%)",
                )
                if not trailing_order_id:
                    # Placement failed — local fallback
                    trailing_triggered, _ = check_trailing_tp(
                        side=pos["side"],
                        entry_price=pos["entry_price"],
                        extreme_price=extreme_price,
                        current_price=current_price,
                        trailing_activated=True,
                        activation_pct=config.TRAILING_ACTIVATION_PCT,
                        drawdown_pct=config.TRAILING_DRAWDOWN_PCT,
                    )
                    if trailing_triggered:
                        _close_position(exchange, state_mgr, pos, current_price, "移动止盈(兜底)")
                        continue
            else:
                trailing_label = f"待激活(需浮盈≥{config.TRAILING_ACTIVATION_PCT*100:.0f}%)"

            unrealized = calculate_pnl(pos["side"], pos["entry_price"], current_price,
                                       pos.get("quantity"), position_size=_pos_margin(pos))
            unrealized_pct = unrealized / _pos_margin(pos) * 100
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
        hard_pct = _pos_hard_stop_pct(pos)
        raw_sl = (
            pos["entry_price"] * (1 - hard_pct) if pos["side"] == "LONG"
            else pos["entry_price"] * (1 + hard_pct)
        )
        sl_price = exchange.round_stop_price(pos["symbol"], raw_sl, pos["side"])

        # Guard: if tick is so large that even floor/ceil gives an invalid price
        if pos["side"] == "LONG" and sl_price <= 0:
            logger.warning("[止损] %s 止损价为 0（tick 过大），依赖本地轮询兜底", pos["symbol"])
            return
        if pos["side"] == "SHORT" and sl_price <= 0:
            logger.warning("[止损] %s 止损价为 0（tick 过大），依赖本地轮询兜底", pos["symbol"])
            return

        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        sl_order = exchange.place_stop_order(
            pos["symbol"], close_side, pos["quantity"], sl_price, position_side=pos["side"]
        )
        new_id = sl_order.get("orderId")
        state_mgr.set_stop_order_id(pos["id"], new_id)
        logger.info("[止损] %s 新止损单 orderId=%s 止损价 %.8f", pos["symbol"], new_id, sl_price)
    except Exception as e:
        logger.error("[止损] %s 重新挂止损单失败: %s", pos["symbol"], e)


def _place_trailing_order(exchange: Exchange, state_mgr, pos: dict, extreme_price: float):
    """Place (or re-place) TRAILING_STOP_MARKET using current extreme price as activationPrice.

    activationPrice = extreme_price (highest for LONG, lowest for SHORT).
    Since we only call this once profit >= 3%, extreme_price is at or near
    current price, so the trailing activates immediately and trails from there.
    """
    try:
        activation_price = exchange.round_price(pos["symbol"], extreme_price)
        close_side = "SELL" if pos["side"] == "LONG" else "BUY"
        tp_order = exchange.place_trailing_stop_order(
            pos["symbol"], close_side, pos["quantity"],
            activation_price=activation_price,
            callback_rate=config.TRAILING_DRAWDOWN_PCT * 100,
            position_side=pos["side"],
        )
        new_id = tp_order.get("orderId")
        state_mgr.set_trailing_order_id(pos["id"], new_id)
        logger.info("[止损] %s 移动止盈单 orderId=%s 激活价 %.4f 回调 %.1f%%",
                    pos["symbol"], new_id, activation_price, config.TRAILING_DRAWDOWN_PCT * 100)
    except Exception as e:
        logger.warning("[止损] %s 挂移动止盈单失败: %s | 本地兜底", pos["symbol"], e)
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
    state_mgr.update_balance(_pos_margin(pos) + raw_pnl)

    pnl_pct = raw_pnl / _pos_margin(pos) * 100
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

        pnl_pct = calculate_pnl(pos["side"], pos["entry_price"], actual_exit, executed_qty) / _pos_margin(pos) * 100
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

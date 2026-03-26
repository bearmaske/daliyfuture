import numpy as np
from binance.client import Client
from config import config
from exchange import Exchange
from state import StateManager
from notifier import notify, logger


def should_stop_loss(
    side: str,
    highest_price: float,
    lowest_price: float,
    current_price: float,
    long_stop: float,
    short_stop: float,
) -> bool:
    """Check if trailing stop loss should trigger."""
    if side == "LONG":
        drawdown = (highest_price - current_price) / highest_price
        return drawdown >= long_stop
    else:  # SHORT
        rebound = (current_price - lowest_price) / lowest_price
        return rebound >= short_stop


def should_close_at_middle_band(
    side: str,
    current_price: float,
    bb_middle: float,
) -> bool:
    """Check if price has reverted to the 1H Bollinger middle band."""
    if side == "LONG" and current_price <= bb_middle:
        return True
    if side == "SHORT" and current_price >= bb_middle:
        return True
    return False


def check_stop_loss(exchange: Exchange, state_mgr: StateManager):
    """Check all open positions for stop loss and middle band exit triggers."""
    positions = list(state_mgr.state.get("positions", []))
    if not positions:
        logger.debug("[止损] 无持仓，跳过检查")
        return

    logger.info("[止损] 检查 %d 个持仓", len(positions))

    for pos in positions:
        try:
            current_price = exchange.get_price(pos["symbol"])
            state_mgr.update_extreme_price(pos["id"], current_price)

            updated_pos = state_mgr.get_position_by_id(pos["id"])
            if updated_pos is None:
                continue

            # Calculate drawdown/rebound percentage
            if updated_pos["side"] == "LONG":
                pct = (updated_pos["highest_price"] - current_price) / updated_pos["highest_price"] * 100
                threshold = config.LONG_TRAILING_STOP * 100
                label = "回撤"
                extreme_label = "最高"
                extreme_price = updated_pos["highest_price"]
            else:
                pct = (current_price - updated_pos["lowest_price"]) / updated_pos["lowest_price"] * 100
                threshold = config.SHORT_TRAILING_STOP * 100
                label = "反弹"
                extreme_label = "最低"
                extreme_price = updated_pos["lowest_price"]

            # Check trailing stop loss
            triggered = should_stop_loss(
                side=updated_pos["side"],
                highest_price=updated_pos["highest_price"],
                lowest_price=updated_pos["lowest_price"],
                current_price=current_price,
                long_stop=config.LONG_TRAILING_STOP,
                short_stop=config.SHORT_TRAILING_STOP,
            )

            # Check middle band exit
            bb_middle = None
            mid_band_exit = False
            try:
                kline_limit = config.BB_PERIOD + 1
                hourly_klines = exchange.get_klines(
                    pos["symbol"], Client.KLINE_INTERVAL_1HOUR, kline_limit
                )
                hourly_closes = [float(k[4]) for k in hourly_klines]
                if len(hourly_closes) >= kline_limit:
                    data = np.array(hourly_closes[-config.BB_PERIOD:], dtype=float)
                    bb_middle = float(np.mean(data))
                    mid_band_exit = should_close_at_middle_band(
                        updated_pos["side"], current_price, bb_middle
                    )
            except Exception as e:
                logger.warning("[止损] %s 获取布林中轨失败: %s", pos["symbol"], e)

            # Log details
            mid_info = ""
            if bb_middle is not None:
                mid_info = f" | 1H中轨: {bb_middle:.4f}"

            status = "安全"
            if triggered:
                status = "触发止损!"
            elif mid_band_exit:
                status = "回归中轨平仓!"

            logger.info(
                "[止损] %s %s | 入场: %.4f | %s: %.4f | 现价: %.4f | %s: %.2f%% / %.1f%%%s | %s",
                updated_pos["symbol"], updated_pos["side"],
                updated_pos["entry_price"],
                extreme_label, extreme_price,
                current_price,
                label, pct, threshold,
                mid_info, status
            )

            if triggered:
                _close_position(exchange, state_mgr, updated_pos, current_price, "移动止损")
            elif mid_band_exit:
                _close_position(exchange, state_mgr, updated_pos, current_price, "回归中轨")

        except Exception as e:
            logger.error("[止损] %s 检查异常: %s", pos["symbol"], e)


def _close_position(
    exchange: Exchange,
    state_mgr: StateManager,
    pos: dict,
    exit_price: float,
    reason: str = "移动止损",
):
    """Close a position via market order."""
    close_side = "SELL" if pos["side"] == "LONG" else "BUY"

    try:
        exchange.place_order(pos["symbol"], close_side, pos["quantity"])

        if pos["side"] == "LONG":
            pnl = (exit_price - pos["entry_price"]) / pos["entry_price"] * config.POSITION_SIZE * config.LEVERAGE
        else:
            pnl = (pos["entry_price"] - exit_price) / pos["entry_price"] * config.POSITION_SIZE * config.LEVERAGE

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

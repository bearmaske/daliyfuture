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


def check_stop_loss(exchange: Exchange, state_mgr: StateManager):
    """Check all open positions for stop loss triggers."""
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

            triggered = should_stop_loss(
                side=updated_pos["side"],
                highest_price=updated_pos["highest_price"],
                lowest_price=updated_pos["lowest_price"],
                current_price=current_price,
                long_stop=config.LONG_TRAILING_STOP,
                short_stop=config.SHORT_TRAILING_STOP,
            )

            logger.info(
                "[止损] %s %s | 入场: %.4f | %s: %.4f | 现价: %.4f | %s: %.2f%% / %.1f%% | %s",
                updated_pos["symbol"], updated_pos["side"],
                updated_pos["entry_price"],
                extreme_label, extreme_price,
                current_price,
                label, pct, threshold,
                "触发止损!" if triggered else "安全"
            )

            if triggered:
                _close_position(exchange, state_mgr, updated_pos, current_price)

        except Exception as e:
            logger.error("[止损] %s 检查异常: %s", pos["symbol"], e)


def _close_position(
    exchange: Exchange,
    state_mgr: StateManager,
    pos: dict,
    exit_price: float,
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
            f"平仓 {pos['side']}",
            f"{pos['symbol']} | 入场 {pos['entry_price']:.4f} | 出场 {exit_price:.4f} | PnL ${pnl:.2f}",
        )
    except Exception as e:
        logger.error(f"Failed to close {pos['symbol']}: {e}")

import signal
import sys
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR
from config import config
from exchange import Exchange
from state import StateManager
from strategy import run_strategy
from risk import check_stop_loss, calculate_pnl
from notifier import notify, logger


def main():
    logger.info("=" * 60)
    logger.info("Starting DualTrend Bollinger Strategy Bot")
    logger.info("=" * 60)

    logger.info("[配置] 初始资金: $%.2f | 单仓: $%.2f | 最大持仓: %d",
                config.INITIAL_CAPITAL, config.POSITION_SIZE, config.MAX_POSITIONS)
    logger.info("[配置] 杠杆: %dx | ATR止损: %d周期 × %.1f倍 | 兜底: %.1f%%",
                config.LEVERAGE, config.ATR_PERIOD, config.ATR_MULTIPLIER, config.MAX_STOP_LOSS * 100)
    logger.info("[配置] 布林带: SMA%d ± %.1fσ | 扫描前 %d 大成交量币种",
                config.BB_PERIOD, config.BB_STD, config.TOP_SYMBOLS_COUNT)

    channels = ["日志: ON"]
    channels.append(f"Telegram: {'ON' if config.TELEGRAM_ENABLED else 'OFF'}")
    channels.append(f"Bark: {'ON' if config.BARK_ENABLED else 'OFF'}")
    logger.info("[通知] %s", " | ".join(channels))

    exchange = Exchange()
    state_mgr = StateManager(
        config.STATE_FILE, config.STATE_BACKUP_FILE, config.INITIAL_CAPITAL
    )
    state_mgr.load()

    logger.info("[同步] 正在从 Testnet 同步账户数据...")
    try:
        exchange.sync_state(state_mgr)
    except Exception as e:
        logger.warning("[同步] 同步失败，使用本地数据: %s", e)

    logger.info("[状态] 余额: $%.2f | 当前持仓: %d",
                state_mgr.balance, state_mgr.position_count)

    scheduler = BlockingScheduler()

    def job_error_listener(event):
        logger.error(f"Job {event.job_id} failed: {event.exception}")
        notify("Job 异常", f"Job {event.job_id} 执行失败: {event.exception}")

    scheduler.add_listener(job_error_listener, EVENT_JOB_ERROR)

    scheduler.add_job(
        run_strategy,
        "cron",
        minute=1,
        args=[exchange, state_mgr],
        id="strategy",
        max_instances=1,
        misfire_grace_time=60,
        coalesce=True,
    )

    scheduler.add_job(
        check_stop_loss,
        "interval",
        minutes=config.RISK_CHECK_INTERVAL_MINUTES,
        args=[exchange, state_mgr],
        id="risk",
        max_instances=1,
        misfire_grace_time=60,
    )

    scheduler.add_job(
        _heartbeat,
        "interval",
        hours=config.HEARTBEAT_INTERVAL_HOURS,
        args=[exchange, state_mgr],
        id="heartbeat",
    )

    def shutdown(signum, frame):
        logger.info("Shutting down...")
        scheduler.shutdown(wait=False)
        state_mgr.save()
        notify("Bot 停止", "DualTrend Bollinger Strategy Bot 已停止")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("[调度] 策略检查: 每小时 :01 | 止损监控: 每 %d 分钟 | 心跳: 每 %d 小时",
                config.RISK_CHECK_INTERVAL_MINUTES, config.HEARTBEAT_INTERVAL_HOURS)
    logger.info("=" * 60)

    notify("Bot 启动", f"余额: ${state_mgr.balance:.2f} | 持仓: {state_mgr.position_count}")

    # run_strategy already syncs with Testnet internally, no need to sync again
    logger.info("[启动] 立即执行首次策略扫描...")
    run_strategy(exchange, state_mgr)
    check_stop_loss(exchange, state_mgr)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass


def _heartbeat(exchange: Exchange, state_mgr: StateManager):
    try:
        exchange.sync_state(state_mgr)
    except Exception as e:
        logger.warning("[心跳] 同步失败: %s", e)

    positions = state_mgr.state.get("positions", [])
    history = state_mgr.state.get("trade_history", [])

    # --- 账户概览 ---
    try:
        summary = exchange.get_account_summary()
        total_wallet_balance = summary["total_wallet_balance"]
        total_unrealized_pnl = summary["total_unrealized_pnl"]
        total_assets = summary["total_margin_balance"]
        available_balance = summary["available_balance"]
    except Exception as e:
        logger.warning("[心跳] 获取账户概览失败: %s", e)
        total_wallet_balance = state_mgr.balance
        total_unrealized_pnl = 0.0
        total_assets = state_mgr.balance
        available_balance = state_mgr.balance

    profit = total_assets - config.INITIAL_CAPITAL
    profit_pct = (profit / config.INITIAL_CAPITAL * 100) if config.INITIAL_CAPITAL else 0
    profit_sign = "+" if profit >= 0 else ""

    # --- 历史统计 ---
    total_closed_pnl = 0.0
    win_count = 0
    lose_count = 0
    for t in history:
        pnl = t.get("pnl", 0)
        total_closed_pnl += pnl
        if pnl > 0:
            win_count += 1
        else:
            lose_count += 1
    win_rate = (win_count / len(history) * 100) if history else 0

    lines = []
    lines.append("--- 资产概览 ---")
    lines.append(f"总资产: ${total_assets:.2f} | 盈利: {profit_sign}${profit:.2f} ({profit_sign}{profit_pct:.2f}%)")
    lines.append(f"钱包余额: ${total_wallet_balance:.2f} | 可用余额: ${available_balance:.2f}")
    unrealized_sign = "+" if total_unrealized_pnl >= 0 else ""
    lines.append(f"持仓未实现PnL: {unrealized_sign}${total_unrealized_pnl:.2f}")
    lines.append(f"初始资金: ${config.INITIAL_CAPITAL:.2f}")

    lines.append("--- 交易统计 ---")
    lines.append(f"持仓: {len(positions)}/{config.MAX_POSITIONS}")
    lines.append(f"已平仓: {len(history)} 笔 | 胜率: {win_rate:.0f}% ({win_count}胜/{lose_count}负)")
    closed_pnl_sign = "+" if total_closed_pnl >= 0 else ""
    lines.append(f"累计已实现PnL: {closed_pnl_sign}${total_closed_pnl:.2f}")

    if positions:
        lines.append("--- 当前持仓 ---")
        for pos in positions:
            try:
                current_price = exchange.get_price(pos["symbol"])
                pnl = calculate_pnl(pos["side"], pos["entry_price"], current_price)
                pnl_pct = pnl / config.POSITION_SIZE * 100
                sign = "+" if pnl >= 0 else ""
                lines.append(
                    f"{pos['symbol']} {pos['side']} | 入场: {pos['entry_price']:.4f} | "
                    f"现价: {current_price:.4f} | {sign}${pnl:.2f} ({sign}{pnl_pct:.1f}%)"
                )
            except Exception:
                lines.append(f"{pos['symbol']} {pos['side']} | 获取价格失败")
    else:
        lines.append("当前无持仓")

    lines.append("--- 策略状态 ---")
    if state_mgr.is_in_cooldown():
        remaining = state_mgr.cooldown_remaining()
        lines.append(f"⚠ 冷静期中，暂停开仓 | 剩余: {remaining}")
    else:
        lines.append("策略运行正常")

    notify("策略执行汇报", "\n".join(lines))


if __name__ == "__main__":
    main()

import signal
import sys
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR
from config import config
from exchange import Exchange
from state import StateManager
from strategy import run_strategy, _sync_with_testnet
from risk import check_stop_loss
from notifier import notify, logger


def main():
    logger.info("=" * 60)
    logger.info("Starting DualTrend Bollinger Strategy Bot")
    logger.info("=" * 60)

    # Print config summary
    logger.info("[配置] 初始资金: $%.2f | 单仓: $%.2f | 最大持仓: %d",
                config.INITIAL_CAPITAL, config.POSITION_SIZE, config.MAX_POSITIONS)
    logger.info("[配置] 杠杆: %dx | 多单止损: %.1f%% | 空单止损: %.1f%%",
                config.LEVERAGE, config.LONG_TRAILING_STOP * 100, config.SHORT_TRAILING_STOP * 100)
    logger.info("[配置] 布林带: SMA%d ± %.1fσ | 扫描前 %d 大成交量币种",
                config.BB_PERIOD, config.BB_STD, config.TOP_SYMBOLS_COUNT)

    # Print notification channel status
    channels = ["日志: ON"]
    if config.TELEGRAM_ENABLED:
        channels.append("Telegram: ON")
    else:
        channels.append("Telegram: OFF")
    if config.BARK_ENABLED:
        channels.append("Bark: ON")
    else:
        channels.append("Bark: OFF")
    logger.info("[通知] %s", " | ".join(channels))

    # Initialize components
    exchange = Exchange()
    state_mgr = StateManager(
        config.STATE_FILE, config.STATE_BACKUP_FILE, config.INITIAL_CAPITAL
    )
    state_mgr.load()

    # Sync with Testnet account (source of truth)
    logger.info("[同步] 正在从 Testnet 同步账户数据...")
    try:
        _sync_with_testnet(exchange, state_mgr)
    except Exception as e:
        logger.warning("[同步] 同步失败，使用本地数据: %s", e)

    logger.info("[状态] 余额: $%.2f | 当前持仓: %d",
                state_mgr.balance, state_mgr.position_count)

    # Scheduler
    scheduler = BlockingScheduler()

    # Log job errors
    def job_error_listener(event):
        logger.error(f"Job {event.job_id} failed: {event.exception}")
        notify("Job 异常", f"Job {event.job_id} 执行失败: {event.exception}")

    scheduler.add_listener(job_error_listener, EVENT_JOB_ERROR)

    # Strategy check: every hour at :01
    scheduler.add_job(
        run_strategy,
        "cron",
        minute=1,
        args=[exchange, state_mgr],
        id="strategy",
        max_instances=1,
        misfire_grace_time=300,
    )

    # Stop loss check: every 5 minutes
    scheduler.add_job(
        check_stop_loss,
        "interval",
        minutes=config.RISK_CHECK_INTERVAL_MINUTES,
        args=[exchange, state_mgr],
        id="risk",
        max_instances=1,
        misfire_grace_time=60,
    )

    # Heartbeat: every 6 hours
    scheduler.add_job(
        _heartbeat,
        "interval",
        hours=config.HEARTBEAT_INTERVAL_HOURS,
        args=[exchange, state_mgr],
        id="heartbeat",
    )

    # Graceful shutdown
    def shutdown(signum, frame):
        logger.info("Shutting down...")
        scheduler.shutdown(wait=False)
        state_mgr.save()
        notify("Bot 停止", "DualTrend Bollinger Strategy Bot 已停止")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Print schedule info
    logger.info("[调度] 策略检查: 每小时 :01 | 止损监控: 每 %d 分钟 | 心跳: 每 %d 小时",
                config.RISK_CHECK_INTERVAL_MINUTES, config.HEARTBEAT_INTERVAL_HOURS)
    logger.info("=" * 60)

    notify("Bot 启动", f"余额: ${state_mgr.balance:.2f} | 持仓: {state_mgr.position_count}")

    # Run strategy and stop loss check immediately on startup
    logger.info("[启动] 立即执行首次策略扫描...")
    run_strategy(exchange, state_mgr)
    check_stop_loss(exchange, state_mgr)

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass


def _heartbeat(exchange: Exchange, state_mgr: StateManager):
    # Sync before reporting
    try:
        _sync_with_testnet(exchange, state_mgr)
    except Exception as e:
        logger.warning("[心跳] 同步失败: %s", e)

    positions = state_mgr.state.get("positions", [])
    history = state_mgr.state.get("trade_history", [])

    # Account summary
    balance = state_mgr.balance
    total_closed_pnl = sum(t.get("pnl", 0) for t in history)
    win_count = sum(1 for t in history if t.get("pnl", 0) > 0)
    lose_count = sum(1 for t in history if t.get("pnl", 0) <= 0)
    win_rate = (win_count / len(history) * 100) if history else 0

    lines = []
    lines.append(f"余额: ${balance:.2f} | 持仓: {len(positions)}/{config.MAX_POSITIONS}")
    lines.append(f"已平仓: {len(history)} 笔 | 胜率: {win_rate:.0f}% ({win_count}胜/{lose_count}负)")
    lines.append(f"累计已实现PnL: ${total_closed_pnl:.2f}")

    # Current positions with unrealized PnL
    if positions:
        lines.append("--- 当前持仓 ---")
        total_unrealized = 0
        for pos in positions:
            try:
                current_price = exchange.get_price(pos["symbol"])
                if pos["side"] == "LONG":
                    pnl = (current_price - pos["entry_price"]) / pos["entry_price"] * config.POSITION_SIZE * config.LEVERAGE
                else:
                    pnl = (pos["entry_price"] - current_price) / pos["entry_price"] * config.POSITION_SIZE * config.LEVERAGE
                total_unrealized += pnl
                sign = "+" if pnl >= 0 else ""
                lines.append(
                    f"{pos['symbol']} {pos['side']} | 入场: {pos['entry_price']:.4f} | "
                    f"现价: {current_price:.4f} | {sign}${pnl:.2f}"
                )
            except Exception as e:
                lines.append(f"{pos['symbol']} {pos['side']} | 获取价格失败")
        sign = "+" if total_unrealized >= 0 else ""
        lines.append(f"未实现PnL合计: {sign}${total_unrealized:.2f}")
    else:
        lines.append("当前无持仓")

    # Strategy status
    lines.append("--- 策略状态 ---")
    lines.append("策略运行正常")

    notify("策略执行汇报", "\n".join(lines))


if __name__ == "__main__":
    main()

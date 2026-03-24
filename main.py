import signal
import sys
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR
from config import config
from exchange import Exchange
from state import StateManager
from strategy import run_strategy
from risk import check_stop_loss
from notifier import notify, logger


def main():
    logger.info("Starting DualTrend Bollinger Strategy Bot")

    # Initialize components
    exchange = Exchange()
    state_mgr = StateManager(
        config.STATE_FILE, config.STATE_BACKUP_FILE, config.INITIAL_CAPITAL
    )
    state_mgr.load()

    logger.info(
        f"State loaded: balance={state_mgr.balance:.2f}, "
        f"positions={state_mgr.position_count}"
    )

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
        args=[state_mgr],
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

    notify("Bot 启动", f"余额: ${state_mgr.balance:.2f} | 持仓: {state_mgr.position_count}")

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass


def _heartbeat(state_mgr: StateManager):
    positions = state_mgr.state.get("positions", [])
    history = state_mgr.state.get("trade_history", [])
    total_pnl = sum(t.get("pnl", 0) for t in history)
    notify(
        "心跳",
        f"余额: ${state_mgr.balance:.2f} | 持仓: {len(positions)} | 累计PnL: ${total_pnl:.2f}",
    )


if __name__ == "__main__":
    main()

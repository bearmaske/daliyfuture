import os
import signal
import sys
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.events import EVENT_JOB_ERROR
from datetime import datetime, timezone, timedelta

from config import config
from exchange import Exchange
from state import StateManager, get_runtime
from strategy import run_strategy
from risk import check_stop_loss, calculate_pnl
from watcher import MarkPriceWatcher
from notifier import notify, logger

TZ_CN = timezone(timedelta(hours=8))

_PID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"bot_{config.TRADING_MODE}.pid")


def _acquire_pid_lock():
    """Prevent two instances of the same TRADING_MODE from running simultaneously."""
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as f:
                old_pid = int(f.read().strip())
            # Check if that process is still alive
            os.kill(old_pid, 0)
            logger.error(
                "另一个 %s 实例已在运行 (PID %d)，拒绝启动。如需强制重启请先删除 %s",
                config.TRADING_MODE, old_pid, _PID_FILE,
            )
            sys.exit(1)
        except (ProcessLookupError, ValueError):
            pass  # stale PID file — previous process is gone
    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _release_pid_lock():
    try:
        os.remove(_PID_FILE)
    except OSError:
        pass


def main():
    _acquire_pid_lock()
    logger.info("=" * 60)
    mode_label = "实盘 LIVE" if config.is_live else "模拟盘 PAPER"
    logger.info("Starting DualTrend Bollinger Strategy Bot [%s]", mode_label)
    logger.info("=" * 60)

    logger.info("[配置] 交易模式: %s", mode_label)
    logger.info("[配置] 初始资金: $%.2f | 单仓: $%.2f | 最大持仓: %d",
                config.INITIAL_CAPITAL, config.POSITION_SIZE, config.MAX_POSITIONS)
    logger.info("[配置] 杠杆: %dx | 固定止损: %.1f%% | 移动止盈: 激活≥%.1f%% 回撤≥%.1f%%",
                config.LEVERAGE,
                config.FIXED_STOP_LOSS_PCT * 100,
                config.TRAILING_ACTIVATION_PCT * 100,
                config.TRAILING_DRAWDOWN_PCT * 100)
    logger.info("[配置] 布林带: SMA%d ± %.1fσ | 扫描前 %d 大成交量币种",
                config.BB_PERIOD, config.BB_STD, config.TOP_SYMBOLS_COUNT)

    channels = ["日志: ON"]
    if config.is_live:
        channels.append(f"Bark: {'ON' if config.BARK_ENABLED else 'OFF'} (实盘通道)")
    else:
        channels.append(f"PushDeer: {'ON' if config.PUSHDEER_ENABLED else 'OFF'} (模拟通道)")
        channels.append(f"Telegram: {'ON' if config.TELEGRAM_ENABLED else 'OFF'} (模拟通道)")
    logger.info("[通知] %s", " | ".join(channels))

    exchange = Exchange()
    state_mgr = StateManager(
        config.STATE_FILE, config.STATE_BACKUP_FILE, config.INITIAL_CAPITAL
    )
    state_mgr.load()

    watcher = MarkPriceWatcher(exchange, state_mgr)
    watcher.start()

    sync_label = "实盘" if config.is_live else "Testnet"
    logger.info("[同步] 正在从 %s 同步账户数据...", sync_label)
    try:
        exchange.sync_state(state_mgr)
    except Exception as e:
        logger.warning("[同步] 同步失败，使用本地数据: %s", e)

    logger.info("[状态] 余额: $%.2f | 当前持仓: %d | 运行时长: %s",
                state_mgr.balance, state_mgr.position_count, get_runtime())

    scheduler = BlockingScheduler()

    def job_error_listener(event):
        logger.error(f"Job {event.job_id} failed: {event.exception}")
        notify("Job 异常", f"Job {event.job_id} 执行失败: {event.exception}")

    scheduler.add_listener(job_error_listener, EVENT_JOB_ERROR)

    def strategy_job():
        try:
            summary = exchange.get_account_summary()
            _check_daily_drawdown(summary["total_margin_balance"], state_mgr)
        except Exception as e:
            logger.warning("[风控] 日内跌幅检查失败: %s", e)
        run_strategy(exchange, state_mgr)
        watcher.update_subscriptions()

    def risk_job():
        check_stop_loss(exchange, state_mgr)
        watcher.update_subscriptions()

    scheduler.add_job(
        strategy_job,
        "cron",
        minute=1,
        id="strategy",
        max_instances=1,
        misfire_grace_time=60,
        coalesce=True,
    )

    scheduler.add_job(
        risk_job,
        "interval",
        seconds=config.RISK_CHECK_INTERVAL_SECONDS,
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

    scheduler.add_job(
        _rebalance_position_size,
        "cron",
        hour=0,
        minute=0,
        timezone=TZ_CN,
        args=[exchange, state_mgr],
        id="rebalance",
        max_instances=1,
        misfire_grace_time=300,
    )

    def shutdown(signum, frame):
        logger.info("Shutting down...")
        scheduler.shutdown(wait=False)
        watcher.stop()
        state_mgr.save()
        _release_pid_lock()
        notify("Bot 停止", "DualTrend Bollinger Strategy Bot 已停止")
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    # Calculate next strategy scan time (next :01)
    now = datetime.now(TZ_CN)
    if now.minute == 0 and now.second < 60:
        next_scan = now.replace(minute=1, second=0, microsecond=0)
    elif now.minute >= 1:
        from datetime import timedelta as _td
        next_scan = (now + _td(hours=1)).replace(minute=1, second=0, microsecond=0)
    else:
        next_scan = now.replace(minute=1, second=0, microsecond=0)
    wait_minutes = int((next_scan - now).total_seconds() / 60)
    wait_seconds = int((next_scan - now).total_seconds() % 60)

    logger.info("[调度] 策略检查: 每小时 :01 | 止损监控: 每 %d 秒 | 心跳: 每 %d 小时 | 调仓: 每日 00:00",
                config.RISK_CHECK_INTERVAL_SECONDS, config.HEARTBEAT_INTERVAL_HOURS)
    logger.info("[调度] 首次策略扫描: %s (约 %d 分 %d 秒后)",
                next_scan.strftime("%H:%M"), wait_minutes, wait_seconds)
    logger.info("=" * 60)

    notify("Bot 启动", f"[{mode_label}] 余额: ${state_mgr.balance:.2f} | 持仓: {state_mgr.position_count}\n"
                       f"首次策略扫描: {next_scan.strftime('%H:%M')} (约 {wait_minutes} 分钟后)")

    check_stop_loss(exchange, state_mgr)
    watcher.update_subscriptions()

    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass


def _check_daily_drawdown(total_assets: float, state_mgr: StateManager) -> bool:
    """对比当日 00:00 快照，若跌幅 ≥ 20% 则触发冷静期。返回是否触发。"""
    snapshot = state_mgr.get_daily_balance_snapshot()
    if snapshot <= 0:
        return False
    daily_drawdown = (snapshot - total_assets) / snapshot
    logger.info("[风控] 当日快照: $%.2f | 当前总资产: $%.2f | 日内变动: %.2f%%",
                snapshot, total_assets, -daily_drawdown * 100)
    if daily_drawdown >= 0.20:
        state_mgr.set_cooldown(hours=config.COOLDOWN_HOURS)
        logger.warning("[风控] 日内跌幅 %.2f%% ≥ 20%%，触发冷静期 %dh",
                       daily_drawdown * 100, config.COOLDOWN_HOURS)
        notify("⚠ 日内亏损熔断",
               f"日内跌幅: -{daily_drawdown * 100:.2f}%\n"
               f"当日快照: ${snapshot:.2f} → 当前: ${total_assets:.2f}\n"
               f"触发 {config.COOLDOWN_HOURS}h 冷静期，暂停开仓")
        return True
    return False


def _rebalance_position_size(exchange: Exchange, state_mgr: StateManager):
    """每日凌晨 00:00：① 保存当日余额快照；② 动态调整单仓金额。"""
    try:
        summary = exchange.get_account_summary()
        total_assets = summary["total_margin_balance"]
    except Exception as e:
        logger.warning("[调仓] 获取账户资产失败，跳过调整: %s", e)
        return

    # 保存今日 00:00 快照（供每小时扫描比对）
    state_mgr.set_daily_balance_snapshot(total_assets)
    logger.info("[调仓] 已记录今日 00:00 快照: $%.2f", total_assets)

    # --- 单仓金额调整 ---
    new_size = int(total_assets * 0.05 / 10) * 10
    if new_size <= 0:
        logger.warning("[调仓] 计算结果 ≤ 0 (总资产 $%.2f)，跳过调整", total_assets)
        return

    old_size = config.POSITION_SIZE
    config.POSITION_SIZE = float(new_size)
    logger.info("[调仓] 总资产: $%.2f | 单仓: $%.0f → $%.0f (5%%)", total_assets, old_size, new_size)
    notify("单仓金额更新", f"总资产: ${total_assets:.2f}\n单仓金额: ${old_size:.0f} → ${new_size:.0f} (5%)")


def _backfill_commission(exchange: Exchange, state_mgr: StateManager, history: list):
    """Backfill commission for trades that don't have it yet.
    Binance trade fills have propagation delay, so we query later."""
    updated = False
    for trade in history:
        if trade.get("commission") is not None:
            continue
        symbol = trade["symbol"]
        total_commission = 0.0
        for label, oid in [("开仓", trade.get("open_order_id")), ("平仓", trade.get("close_order_id"))]:
            if oid:
                try:
                    c = exchange.get_order_commission(symbol, oid)
                    total_commission += c
                except Exception as e:
                    logger.warning("[手续费补查] %s %s (orderId=%s) 失败: %s", symbol, label, oid, e)
        trade["commission"] = total_commission
        trade["pnl"] = trade["pnl"] - total_commission
        updated = True
        logger.info("[手续费补查] %s | 手续费: $%.4f | 净PnL: $%.2f", symbol, total_commission, trade["pnl"])
    if updated:
        state_mgr.save()


def _heartbeat(exchange: Exchange, state_mgr: StateManager):
    try:
        exchange.sync_state(state_mgr)
    except Exception as e:
        logger.warning("[心跳] 同步失败: %s", e)

    positions = state_mgr.state.get("positions", [])
    history = state_mgr.state.get("trade_history", [])

    # --- 补查手续费 ---
    _backfill_commission(exchange, state_mgr, history)

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
    total_commission = 0.0
    win_count = 0
    lose_count = 0
    for t in history:
        pnl = t.get("pnl", 0)
        total_closed_pnl += pnl
        total_commission += t.get("commission") or 0
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
    snapshot = state_mgr.get_daily_balance_snapshot()
    snapshot_str = f"${snapshot:.2f}" if snapshot > 0 else "未记录"
    daily_chg = f"{(total_assets - snapshot) / snapshot * 100:+.2f}%" if snapshot > 0 else "N/A"
    lines.append(f"单仓金额: ${config.POSITION_SIZE:.0f} (总资产5%) | 今日快照: {snapshot_str} | 日内变动: {daily_chg}")

    mode_label = "实盘" if config.is_live else "模拟盘"
    lines.append(f"--- 交易统计 ({mode_label}) ---")
    lines.append(f"运行时长: {get_runtime()} (自 {config.STRATEGY_START_TIME})")
    lines.append(f"持仓: {len(positions)}/{config.MAX_POSITIONS}")
    lines.append(f"已平仓: {len(history)} 笔 | 胜率: {win_rate:.0f}% ({win_count}胜/{lose_count}负)")
    closed_pnl_sign = "+" if total_closed_pnl >= 0 else ""
    lines.append(f"累计已实现PnL: {closed_pnl_sign}${total_closed_pnl:.2f} | 累计手续费(重建): ${total_commission:.4f}")

    # Platform-reported commission via GET /fapi/v1/income?incomeType=COMMISSION
    try:
        start_dt = datetime.strptime(config.STRATEGY_START_TIME, "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ_CN)
        platform_commission = exchange.get_total_commission_since(int(start_dt.timestamp() * 1000))
        lines.append(f"平台手续费(/income): ${platform_commission:.4f}")
    except Exception as e:
        logger.warning("[心跳] 获取平台手续费失败: %s", e)

    if positions:
        lines.append("--- 当前持仓 ---")
        for pos in positions:
            try:
                current_price = exchange.get_price(pos["symbol"])
                pnl = calculate_pnl(pos["side"], pos["entry_price"], current_price, pos.get("quantity"))
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

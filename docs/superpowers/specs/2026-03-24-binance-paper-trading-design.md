# Binance Testnet 模拟盘交易策略 — 设计文档

**策略名称：** 双面布林趋势策略（DualTrend Bollinger Strategy）
**版本：** v1.0
**日期：** 2026-03-24
**状态：** 设计完成，待实现

---

## 1. 概述

基于布林带的加密货币 Futures 量化交易策略，运行在 Binance Testnet 上。日线布林带判断趋势方向，小时线捕捉突破入场，移动止损锁定利润。

## 2. 技术选型

| 项目 | 选择 |
|------|------|
| 连接方式 | Binance Testnet API |
| 语言 | Python |
| 运行方式 | 长驻进程（APScheduler 调度） |
| 通知 | 日志 + Telegram + Bark |
| 状态存储 | JSON 文件 |
| 架构 | 模块化 |

## 3. 项目结构

```
dabao/
├── main.py              # 入口，启动 APScheduler 调度器
├── config.py            # 所有参数 + 从 .env 加载密钥
├── exchange.py          # Binance Testnet API 封装
├── strategy.py          # 双面布林策略逻辑
├── risk.py              # 止损监控（每5分钟）
├── notifier.py          # 日志 + Telegram + Bark 通知
├── state.py             # JSON 状态管理
├── .env                 # 密钥（不提交 git）
├── .gitignore
├── state.json           # 运行时状态（自动生成）
└── requirements.txt
```

## 4. 核心依赖

- `python-binance` — Binance API 客户端（支持 Testnet）
- `APScheduler` — 定时任务调度
- `python-dotenv` — 环境变量加载
- `python-telegram-bot` — Telegram 通知
- `pandas` + `numpy` — 布林带计算

## 5. 模块职责

| 模块 | 职责 | 对外接口 |
|------|------|----------|
| `config.py` | 集中管理参数，加载 .env | `Config` 数据类 |
| `exchange.py` | 封装 Binance Testnet API | `get_top_symbols()`, `get_klines()`, `place_order()`, `get_price()`, `get_account()` |
| `strategy.py` | 布林带计算、趋势判断、入场信号 | `check_signals()` — 返回交易动作列表 |
| `risk.py` | 遍历持仓，检查移动止损条件 | `check_stop_loss()` — 返回需平仓列表 |
| `state.py` | JSON 读写 | `load()`, `save()`, `add_position()`, `remove_position()` |
| `notifier.py` | 日志 + Telegram + Bark 推送 | `notify(event, data)` |
| `main.py` | 启动调度器，注册定时任务 | 程序入口 |

## 6. 数据流

```
main.py (调度器)
  │
  ├── 每1小时 ──→ strategy.py (策略检查)
  │                  │
  │                  ├── exchange.py (获取前50大成交量币种)
  │                  ├── exchange.py (拉取日线+小时线K线)
  │                  ├── 计算布林带，判断趋势+入场信号
  │                  ├── 满足条件 → exchange.py (Testnet下单)
  │                  ├── state.py (更新持仓记录)
  │                  └── notifier.py (通知开仓)
  │
  └── 每5分钟 ──→ risk.py (止损监控)
                     │
                     ├── exchange.py (获取当前价格)
                     ├── state.py (读取持仓，计算回撤)
                     ├── 触发止损 → exchange.py (平仓)
                     ├── state.py (更新状态)
                     └── notifier.py (通知平仓)
```

## 7. 策略逻辑

### 7.1 交易标的选择

- 通过 Binance API 获取 USDT 永续合约 24h 行情
- 按 quote volume（USDT 计价成交额）降序，取前 50 名
- 过滤掉稳定币对（BUSDUSDT、USDCUSDT 等）
- 同一 symbol 不允许重复开仓（已持仓的 symbol 跳过）

### 7.2 趋势判断（日线）

- 拉取最近 `BB_PERIOD + 1` 根日线 K 线（默认 21 根）
- 计算 SMA20 作为布林带中轨
- 收盘价 > 中轨 → 多头趋势，只允许做多
- 收盘价 < 中轨 → 空头趋势，只允许做空

### 7.3 入场信号（小时线）

- 拉取最近 `BB_PERIOD + 1` 根小时线 K 线（默认 21 根）
- 计算布林带：中轨 = SMA20，上轨 = 中轨 + 2σ，下轨 = 中轨 - 2σ
- 做多条件：多头趋势 + 收盘价突破上轨 + 成交量 > 前20根均量
- 做空条件：空头趋势 + 收盘价跌破下轨 + 成交量 > 前20根均量
- 额外检查：持仓数 < 10 且可用资金 ≥ $500

### 7.4 下单逻辑

- 使用**市价单**（Market Order），确保突破信号及时成交
- 每次开仓金额固定 $500（保证金），杠杆 5x，实际控制 $2500 合约价值
- 数量计算：`quantity = notional / current_price`，按 Binance 交易对的 `stepSize` 向下取整
- 开仓后 `balance` 扣减 $500 保证金
- 平仓后 `balance` 加回保证金 + PnL
- 记录开仓价格作为止损基准

### 7.5 止损逻辑（每5分钟）

- 多单：当前价格相对最高价回撤 ≥ 3% → 平仓（百分比基于标的价格，非保证金）
- 空单：当前价格相对最低价反弹 ≥ 5% → 平仓（百分比基于标的价格，非保证金）
- 每次检查时更新最高价/最低价（移动止损）
- 趋势反转不主动平仓，仅依赖移动止损出场

### 7.6 调度细节

- 策略检查在每小时的 **:01** 执行，确保小时线 K 线已收盘
- 止损检查每 5 分钟执行
- 两个任务均设置 `max_instances=1`，防止重叠执行
- state.json 读写通过内存锁（`threading.Lock`）保护，防止竞态
- K 线数据从 **Binance 主网** 获取（Testnet K 线数据质量差），下单通过 **Testnet** 执行

### 7.7 进程生命周期

- 启动时加载 state.json，若不存在则初始化默认状态
- 支持 SIGTERM/SIGINT 优雅退出：保存状态后再退出
- 每 6 小时发送一次心跳通知，确认进程存活

## 8. 参数配置

| 参数 | 默认值 | 说明 |
|------|--------|------|
| INITIAL_CAPITAL | 10000 | 初始资金（USDT） |
| POSITION_SIZE | 500 | 单仓金额 |
| MAX_POSITIONS | 10 | 最大持仓数 |
| LEVERAGE | 5 | 杠杆倍数 |
| LONG_TRAILING_STOP | 0.03 | 多单移动止损回撤比例 |
| SHORT_TRAILING_STOP | 0.05 | 空单移动止损反弹比例 |
| BB_PERIOD | 20 | 布林带周期 |
| BB_STD | 2 | 布林带标准差倍数 |
| TOP_SYMBOLS_COUNT | 50 | 扫描成交量前N币种 |
| STRATEGY_INTERVAL_HOURS | 1 | 策略检查间隔（小时） |
| RISK_CHECK_INTERVAL_MINUTES | 5 | 止损检查间隔（分钟） |

## 9. 状态存储（state.json）

```json
{
  "balance": 10000,
  "positions": [
    {
      "id": "uuid",
      "symbol": "BTCUSDT",
      "side": "LONG",
      "entry_price": 65000,
      "quantity": 0.0077,
      "highest_price": 67000,
      "lowest_price": null,
      "opened_at": "2026-03-24T10:00:00Z"
    }
  ],
  "trade_history": [
    {
      "id": "uuid",
      "symbol": "BTCUSDT",
      "side": "LONG",
      "entry_price": 65000,
      "exit_price": 66500,
      "pnl": 17.7,
      "opened_at": "...",
      "closed_at": "..."
    }
  ]
}
```

## 10. 通知

三通道并行发送，任一通道失败不影响其他：

- **日志** — Python `logging`，输出到终端 + `binance_paper_trading.log` 文件
- **Telegram** — 通过 `python-telegram-bot` 发送
- **Bark** — HTTP GET `{BARK_URL}/<title>/<body>`（`BARK_URL` 已包含 server + key）

日志格式：`[2026-03-24 10:00:00] [INFO] BTCUSDT | 多头趋势 | 突破上轨 65200 | 开仓 $500`

## 11. 错误处理

- API 调用失败 → 重试 3 次（间隔 5 秒），失败后记录日志 + 通知告警，跳过本轮
- 下单失败 → 不更新 state，记录错误，下一轮重新评估
- state.json 读写失败 → 写入前备份为 `state.backup.json`，读取失败时尝试恢复
- 调度器异常 → 捕获异常不退出进程，记录日志继续运行

## 12. 环境变量（.env）

```
BINANCE_TESTNET_API_KEY=你的key
BINANCE_TESTNET_API_SECRET=你的secret
TELEGRAM_BOT_TOKEN=你的token
TELEGRAM_CHAT_ID=你的chat_id
BARK_URL=https://api.day.app/你的key
```

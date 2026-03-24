# 双面布林趋势策略 — Binance Testnet 模拟盘交易 Bot

基于布林带的加密货币 USDT 永续合约量化交易策略，运行在 Binance Testnet（测试网）上。

## 策略简介

- **日线布林带**判断趋势方向（价格 > 中轨做多，< 中轨做空）
- **小时线布林带**捕捉突破入场（突破上/下轨 + 成交量放大）
- **移动止损**锁定利润（多单回撤 3%，空单反弹 5%）
- 每小时自动扫描成交量前 50 的 USDT 永续合约

## 快速开始

### 1. 安装依赖

```bash
cd dabao
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
```

编辑 `.env`，填入你的密钥：

```bash
# 必填 — Binance Testnet API 密钥
# 申请地址：https://testnet.binancefuture.com
BINANCE_TESTNET_API_KEY=你的key
BINANCE_TESTNET_API_SECRET=你的secret

# 可选 — Telegram 通知
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=你的bot_token
TELEGRAM_CHAT_ID=你的chat_id

# 可选 — Bark 通知（iOS 推送）
BARK_ENABLED=false
BARK_URL=https://api.day.app/你的key
```

> **Telegram 和 Bark 默认关闭。** 需要哪个就把对应的 `_ENABLED` 改为 `true` 并填入密钥。

### 3. 启动 Bot

```bash
source .venv/bin/activate
python main.py
```

Bot 启动后会持续运行：
- 每小时 :01 扫描市场并检查入场信号
- 每 5 分钟检查持仓止损
- 每 6 小时发送心跳通知（如果启用了通知）

### 4. 停止 Bot

按 `Ctrl+C`，Bot 会保存状态后优雅退出。

## 运行测试

```bash
source .venv/bin/activate
python -m pytest tests/ -v
```

## 项目结构

```
dabao/
├── main.py          # 入口，APScheduler 调度
├── config.py        # 参数配置 + 环境变量加载
├── exchange.py      # Binance API 封装（主网行情 + 测试网下单）
├── strategy.py      # 布林带策略（趋势判断 + 入场信号）
├── risk.py          # 移动止损监控
├── notifier.py      # 日志 + Telegram + Bark 通知
├── state.py         # JSON 状态持久化
├── tests/           # 单元测试
├── .env.example     # 环境变量模板
├── requirements.txt # Python 依赖
└── state.json       # 运行时状态（自动生成）
```

## 策略参数

所有参数在 `config.py` 中定义，可直接修改：

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `INITIAL_CAPITAL` | 10000 | 初始资金（USDT） |
| `POSITION_SIZE` | 500 | 单仓保证金（USDT） |
| `MAX_POSITIONS` | 10 | 最大同时持仓数 |
| `LEVERAGE` | 5 | 杠杆倍数 |
| `LONG_TRAILING_STOP` | 0.03 | 多单移动止损（从最高价回撤 3%） |
| `SHORT_TRAILING_STOP` | 0.05 | 空单移动止损（从最低价反弹 5%） |
| `BB_PERIOD` | 20 | 布林带周期 |
| `BB_STD` | 2.0 | 布林带标准差倍数 |
| `TOP_SYMBOLS_COUNT` | 50 | 扫描成交量前 N 币种 |

## 数据说明

- **行情数据**从 Binance **主网**获取（数据质量更好）
- **下单执行**通过 Binance **Testnet** 进行（不涉及真实资金）
- 同一币种不会重复开仓
- 持仓状态保存在 `state.json`，重启后自动恢复

## 日志

- 终端实时输出
- 同时写入 `binance_paper_trading.log` 文件
- 格式：`[2026-03-24 10:01:00] [INFO] 开仓 LONG | BTCUSDT | 价格 70969.0 | 数量 0.035 | 保证金 $500`

## 注意事项

- 这是**模拟盘**，不涉及真实资金
- Binance Testnet API Key 需要在 [testnet.binancefuture.com](https://testnet.binancefuture.com) 单独申请
- Testnet 的撮合引擎和主网独立，订单簿可能较薄
- 建议用 `screen` 或 `tmux` 在后台运行 Bot

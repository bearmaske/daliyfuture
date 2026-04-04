# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A Binance Testnet crypto trading bot ("Trend Sniper") that trades USDT perpetual futures using a dual-timeframe Bollinger Band strategy. It runs on paper money only — orders execute on Testnet, market data comes from mainnet.

## Commands

```bash
# Run the bot
source .venv/bin/activate
python main.py

# Run all tests
python -m pytest tests/ -v

# Run a single test file
python -m pytest tests/test_strategy.py -v

# Install dependencies
pip install -r requirements.txt
```

## Architecture

The bot uses APScheduler with three jobs:
- **Strategy scan** (`strategy.py:run_strategy`) — runs at :01 every hour. Scans top N symbols by volume, checks daily trend (SMA direction) then hourly Bollinger Band breakout + volume confirmation for entry signals.
- **Risk check** (`risk.py:check_stop_loss`) — runs every 2 minutes. Two exit triggers: trailing stop loss (3% long / 5% short drawdown from extreme) and price reverting to 1H Bollinger middle band.
- **Heartbeat** (`main.py:_heartbeat`) — runs every 6 hours. Sends a portfolio summary notification.

**Dual-client design** in `exchange.py`: `data_client` hits mainnet (no auth, better data quality) for klines/prices; `testnet_client` (lazy-init, needs API keys) handles order placement and account queries.

**State management** (`state.py`): JSON file persistence with backup, thread-safe via `threading.Lock`. Positions track highest/lowest price for trailing stops. `sync_positions()` reconciles local state against Testnet — remote is source of truth for balance and positions.

**Notifications** (`notifier.py`): Logs to console + file, optionally pushes to Telegram and/or Bark (iOS). All notification failures are non-fatal.

## Configuration

All strategy parameters are constants in the `Config` dataclass (`config.py`). API keys and notification settings come from `.env` (loaded via python-dotenv). See `.env.example` for the template.

## Key Conventions

- Timestamps use UTC+8 throughout (see `state.py:TZ_CN`)
- All log messages use Chinese labels (e.g. `[策略]`, `[止损]`, `[同步]`)
- Notification titles/bodies are in Chinese
- Position PnL is calculated as percentage move × position_size × leverage (not based on quantity)

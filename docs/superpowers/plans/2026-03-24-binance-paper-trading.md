# Binance Testnet Paper Trading Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a long-running Python bot that trades Binance USDT Futures on Testnet using a dual Bollinger Band trend strategy with trailing stop loss.

**Architecture:** Modular Python application with 7 files. APScheduler runs two jobs: hourly strategy check (at :01) and 5-minute stop loss monitor. Mainnet client for market data, Testnet client for order execution. State persisted in JSON.

**Tech Stack:** Python 3.10+, python-binance, APScheduler, python-dotenv, python-telegram-bot, pandas, numpy

**Spec:** `docs/superpowers/specs/2026-03-24-binance-paper-trading-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `config.py` | Load .env, expose all parameters as a dataclass |
| `state.py` | Thread-safe JSON read/write, position CRUD |
| `exchange.py` | Two Binance clients (mainnet data + testnet orders), API wrappers |
| `notifier.py` | Logging setup + Telegram + Bark notification |
| `strategy.py` | Bollinger Band calculation, trend + entry signal detection |
| `risk.py` | Trailing stop loss check, position close logic |
| `main.py` | APScheduler setup, signal handlers, orchestration |
| `tests/test_state.py` | State module tests |
| `tests/test_strategy.py` | Strategy logic tests |
| `tests/test_risk.py` | Risk module tests |
| `requirements.txt` | Dependencies |
| `.env.example` | Template for environment variables |
| `.gitignore` | Ignore .env, state.json, logs, __pycache__ |

---

### Task 1: Project Setup

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`
- Create: `.gitignore`

- [ ] **Step 1: Create requirements.txt**

```
python-binance==1.0.19
APScheduler==3.10.4
python-dotenv==1.0.1
python-telegram-bot==21.5
pandas==2.2.2
numpy==1.26.4
pytest==8.3.3
```

- [ ] **Step 2: Create .env.example**

```
BINANCE_TESTNET_API_KEY=your_testnet_api_key
BINANCE_TESTNET_API_SECRET=your_testnet_api_secret
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
BARK_URL=https://api.day.app/your_bark_key
```

- [ ] **Step 3: Create .gitignore**

```
.env
state.json
state.backup.json
binance_paper_trading.log
__pycache__/
*.pyc
.venv/
```

- [ ] **Step 4: Create tests/__init__.py**

```python
# tests/__init__.py
```

- [ ] **Step 5: Install dependencies**

Run: `cd /Users/danny/Desktop/code/dabao && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`
Expected: All packages install successfully.

- [ ] **Step 6: Commit**

```bash
git init
git add requirements.txt .env.example .gitignore tests/__init__.py
git commit -m "chore: project setup with dependencies"
```

---

### Task 2: Config Module

**Files:**
- Create: `config.py`

- [ ] **Step 1: Write config.py**

```python
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # API Keys
    BINANCE_TESTNET_API_KEY: str = os.getenv("BINANCE_TESTNET_API_KEY", "")
    BINANCE_TESTNET_API_SECRET: str = os.getenv("BINANCE_TESTNET_API_SECRET", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    BARK_URL: str = os.getenv("BARK_URL", "")

    # Capital & Position
    INITIAL_CAPITAL: float = 10000.0
    POSITION_SIZE: float = 500.0
    MAX_POSITIONS: int = 10
    LEVERAGE: int = 5

    # Stop Loss
    LONG_TRAILING_STOP: float = 0.03
    SHORT_TRAILING_STOP: float = 0.05

    # Bollinger Bands
    BB_PERIOD: int = 20
    BB_STD: float = 2.0

    # Scanning
    TOP_SYMBOLS_COUNT: int = 50
    STABLECOIN_FILTER: list = None

    # Scheduling
    STRATEGY_INTERVAL_HOURS: int = 1
    RISK_CHECK_INTERVAL_MINUTES: int = 5
    HEARTBEAT_INTERVAL_HOURS: int = 6

    # Files
    STATE_FILE: str = "state.json"
    STATE_BACKUP_FILE: str = "state.backup.json"
    LOG_FILE: str = "binance_paper_trading.log"

    def __post_init__(self):
        if self.STABLECOIN_FILTER is None:
            self.STABLECOIN_FILTER = [
                "BUSDUSDT", "USDCUSDT", "TUSDUSDT", "DAIUSDT", "FDUSDUSDT"
            ]


config = Config()
```

- [ ] **Step 2: Smoke test**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -c "from config import config; print(config.POSITION_SIZE, config.BB_PERIOD)"`
Expected: `500.0 20`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add config module with all strategy parameters"
```

---

### Task 3: State Module

**Files:**
- Create: `state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_state.py
import json
import os
import tempfile
import pytest
from state import StateManager


@pytest.fixture
def state_mgr(tmp_path):
    path = tmp_path / "state.json"
    backup = tmp_path / "state.backup.json"
    return StateManager(str(path), str(backup), initial_capital=10000.0)


def test_init_creates_default_state(state_mgr):
    state = state_mgr.load()
    assert state["balance"] == 10000.0
    assert state["positions"] == []
    assert state["trade_history"] == []


def test_add_position(state_mgr):
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="BTCUSDT",
        side="LONG",
        entry_price=65000.0,
        quantity=0.0077,
    )
    assert pos["symbol"] == "BTCUSDT"
    assert pos["side"] == "LONG"
    assert pos["highest_price"] == 65000.0
    assert pos["lowest_price"] == 65000.0
    assert len(state_mgr.state["positions"]) == 1


def test_remove_position(state_mgr):
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="BTCUSDT", side="LONG", entry_price=65000.0, quantity=0.0077
    )
    removed = state_mgr.remove_position(pos["id"])
    assert removed is not None
    assert len(state_mgr.state["positions"]) == 0


def test_save_creates_backup(state_mgr, tmp_path):
    state_mgr.load()
    state_mgr.save()
    # Second save should create backup
    state_mgr.save()
    backup_path = tmp_path / "state.backup.json"
    assert backup_path.exists()


def test_get_position_by_symbol(state_mgr):
    state_mgr.load()
    state_mgr.add_position(
        symbol="BTCUSDT", side="LONG", entry_price=65000.0, quantity=0.0077
    )
    found = state_mgr.get_position_by_symbol("BTCUSDT")
    assert found is not None
    assert found["symbol"] == "BTCUSDT"
    assert state_mgr.get_position_by_symbol("ETHUSDT") is None


def test_update_extreme_price(state_mgr):
    state_mgr.load()
    pos = state_mgr.add_position(
        symbol="BTCUSDT", side="LONG", entry_price=65000.0, quantity=0.0077
    )
    state_mgr.update_extreme_price(pos["id"], current_price=67000.0)
    updated = state_mgr.get_position_by_id(pos["id"])
    assert updated["highest_price"] == 67000.0

    # Price lower than highest should not update highest
    state_mgr.update_extreme_price(pos["id"], current_price=66000.0)
    updated = state_mgr.get_position_by_id(pos["id"])
    assert updated["highest_price"] == 67000.0


def test_thread_safety(state_mgr):
    """Verify that the lock exists and state operations are guarded."""
    import threading
    state_mgr.load()
    assert hasattr(state_mgr, "_lock")
    assert isinstance(state_mgr._lock, type(threading.Lock()))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -m pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'state'`

- [ ] **Step 3: Write state.py implementation**

```python
import json
import os
import shutil
import threading
import uuid
from datetime import datetime, timezone


class StateManager:
    def __init__(self, state_file: str, backup_file: str, initial_capital: float):
        self.state_file = state_file
        self.backup_file = backup_file
        self.initial_capital = initial_capital
        self._lock = threading.Lock()
        self.state = None

    def load(self) -> dict:
        with self._lock:
            if os.path.exists(self.state_file):
                try:
                    with open(self.state_file, "r") as f:
                        self.state = json.load(f)
                except (json.JSONDecodeError, IOError):
                    if os.path.exists(self.backup_file):
                        with open(self.backup_file, "r") as f:
                            self.state = json.load(f)
                    else:
                        self.state = self._default_state()
            else:
                self.state = self._default_state()
            return self.state

    def save(self):
        with self._lock:
            if os.path.exists(self.state_file):
                shutil.copy2(self.state_file, self.backup_file)
            with open(self.state_file, "w") as f:
                json.dump(self.state, f, indent=2, default=str)

    def add_position(
        self, symbol: str, side: str, entry_price: float, quantity: float
    ) -> dict:
        with self._lock:
            pos = {
                "id": str(uuid.uuid4()),
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "quantity": quantity,
                "highest_price": entry_price,
                "lowest_price": entry_price,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state["positions"].append(pos)
        self.save()
        return pos

    def remove_position(self, position_id: str) -> dict | None:
        with self._lock:
            for i, pos in enumerate(self.state["positions"]):
                if pos["id"] == position_id:
                    removed = self.state["positions"].pop(i)
                    break
            else:
                return None
        self.save()
        return removed

    def get_position_by_symbol(self, symbol: str) -> dict | None:
        with self._lock:
            for pos in self.state["positions"]:
                if pos["symbol"] == symbol:
                    return pos
        return None

    def get_position_by_id(self, position_id: str) -> dict | None:
        with self._lock:
            for pos in self.state["positions"]:
                if pos["id"] == position_id:
                    return pos
        return None

    def update_extreme_price(self, position_id: str, current_price: float):
        with self._lock:
            for pos in self.state["positions"]:
                if pos["id"] == position_id:
                    if current_price > pos["highest_price"]:
                        pos["highest_price"] = current_price
                    if current_price < pos["lowest_price"]:
                        pos["lowest_price"] = current_price
                    break
        self.save()

    def add_trade_history(
        self,
        symbol: str,
        side: str,
        entry_price: float,
        exit_price: float,
        quantity: float,
        pnl: float,
        opened_at: str,
    ):
        with self._lock:
            trade = {
                "id": str(uuid.uuid4()),
                "symbol": symbol,
                "side": side,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "quantity": quantity,
                "pnl": pnl,
                "opened_at": opened_at,
                "closed_at": datetime.now(timezone.utc).isoformat(),
            }
            self.state["trade_history"].append(trade)
        self.save()
        return trade

    def update_balance(self, amount: float):
        with self._lock:
            self.state["balance"] += amount
        self.save()

    @property
    def position_count(self) -> int:
        return len(self.state["positions"])

    @property
    def balance(self) -> float:
        return self.state["balance"]

    def _default_state(self) -> dict:
        return {
            "balance": self.initial_capital,
            "positions": [],
            "trade_history": [],
        }
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -m pytest tests/test_state.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add state.py tests/test_state.py
git commit -m "feat: add state module with JSON persistence and thread safety"
```

---

### Task 4: Notifier Module

**Files:**
- Create: `notifier.py`

- [ ] **Step 1: Write notifier.py**

```python
import logging
import urllib.request
import urllib.parse
from config import config

# Setup logging
logger = logging.getLogger("dabao")
logger.setLevel(logging.INFO)

# Console handler
console_handler = logging.StreamHandler()
console_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(console_handler)

# File handler
file_handler = logging.FileHandler(config.LOG_FILE)
file_handler.setFormatter(
    logging.Formatter("[%(asctime)s] [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
)
logger.addHandler(file_handler)


def notify(title: str, message: str):
    """Send notification via all channels. Failures are logged but don't propagate."""
    logger.info(f"{title} | {message}")
    _send_telegram(title, message)
    _send_bark(title, message)


def _send_telegram(title: str, message: str):
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return
    try:
        from telegram import Bot
        import asyncio

        async def _send():
            bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
            await bot.send_message(
                chat_id=config.TELEGRAM_CHAT_ID,
                text=f"*{title}*\n{message}",
                parse_mode="Markdown",
            )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_send())
        except RuntimeError:
            asyncio.run(_send())
    except Exception as e:
        logger.warning(f"Telegram notification failed: {e}")


def _send_bark(title: str, message: str):
    if not config.BARK_URL:
        return
    try:
        encoded_title = urllib.parse.quote(title)
        encoded_message = urllib.parse.quote(message)
        url = f"{config.BARK_URL}/{encoded_title}/{encoded_message}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            pass
    except Exception as e:
        logger.warning(f"Bark notification failed: {e}")
```

- [ ] **Step 2: Smoke test logging**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -c "from notifier import notify, logger; logger.info('test log'); print('OK')"`
Expected: `OK` and log line printed to console.

- [ ] **Step 3: Commit**

```bash
git add notifier.py
git commit -m "feat: add notifier module with logging, Telegram, and Bark"
```

---

### Task 5: Exchange Module

**Files:**
- Create: `exchange.py`

- [ ] **Step 1: Write exchange.py**

```python
import math
import time
from binance.client import Client
from notifier import logger
from config import config


class Exchange:
    def __init__(self):
        # Mainnet client for market data (better kline quality)
        self.data_client = Client()
        # Testnet client lazy-initialized (needs API keys)
        self._testnet_client = None
        self._symbol_filters = {}
        self._filters_loaded = False

    @property
    def testnet_client(self) -> Client:
        if self._testnet_client is None:
            self._testnet_client = Client(
                api_key=config.BINANCE_TESTNET_API_KEY,
                api_secret=config.BINANCE_TESTNET_API_SECRET,
                testnet=True,
            )
        return self._testnet_client

    def get_top_symbols(self, limit: int = None) -> list[str]:
        """Get top N USDT perpetual futures by quote volume."""
        limit = limit or config.TOP_SYMBOLS_COUNT
        tickers = self._retry(lambda: self.data_client.futures_ticker())
        # Filter USDT pairs, exclude stablecoins
        usdt_tickers = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and t["symbol"] not in config.STABLECOIN_FILTER
        ]
        # Sort by quote asset volume descending
        usdt_tickers.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
        return [t["symbol"] for t in usdt_tickers[:limit]]

    def get_klines(self, symbol: str, interval: str, limit: int) -> list[list]:
        """Get klines from mainnet. Returns list of [open_time, open, high, low, close, volume, ...]."""
        return self._retry(
            lambda: self.data_client.futures_klines(
                symbol=symbol, interval=interval, limit=limit
            )
        )

    def get_price(self, symbol: str) -> float:
        """Get current price from mainnet."""
        ticker = self._retry(
            lambda: self.data_client.futures_symbol_ticker(symbol=symbol)
        )
        return float(ticker["price"])

    def get_step_size(self, symbol: str) -> float:
        """Get lot step size for quantity rounding."""
        if not self._filters_loaded:
            info = self._retry(lambda: self.data_client.futures_exchange_info())
            for s in info["symbols"]:
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        self._symbol_filters[s["symbol"]] = float(f["stepSize"])
            self._filters_loaded = True
        return self._symbol_filters.get(symbol, 0.001)

    def round_quantity(self, symbol: str, quantity: float) -> float:
        """Round quantity down to valid step size."""
        step = self.get_step_size(symbol)
        precision = int(round(-math.log10(step)))
        return math.floor(quantity * 10**precision) / 10**precision

    def place_order(self, symbol: str, side: str, quantity: float) -> dict:
        """Place a market order on testnet."""
        logger.info(f"Placing {side} order: {symbol} qty={quantity}")
        return self._retry(
            lambda: self.testnet_client.futures_create_order(
                symbol=symbol,
                side=side,
                type="MARKET",
                quantity=quantity,
            )
        )

    def set_leverage(self, symbol: str, leverage: int):
        """Set leverage for a symbol on testnet."""
        try:
            self.testnet_client.futures_change_leverage(
                symbol=symbol, leverage=leverage
            )
        except Exception as e:
            # May fail if already set, ignore
            logger.debug(f"Set leverage {symbol} {leverage}x: {e}")

    def _retry(self, func, retries: int = 3, delay: int = 5):
        for i in range(retries):
            try:
                return func()
            except Exception as e:
                if i < retries - 1:
                    logger.warning(f"API call failed (attempt {i+1}/{retries}): {e}")
                    time.sleep(delay)
                else:
                    raise
```

- [ ] **Step 2: Smoke test (mainnet data only, no keys needed)**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -c "
from binance.client import Client
c = Client()
tickers = c.futures_ticker()
usdt = [t for t in tickers if t['symbol'].endswith('USDT')]
usdt.sort(key=lambda x: float(x['quoteVolume']), reverse=True)
for t in usdt[:5]:
    print(t['symbol'], t['quoteVolume'])
"`
Expected: Top 5 USDT futures symbols printed with volumes.

- [ ] **Step 3: Commit**

```bash
git add exchange.py
git commit -m "feat: add exchange module with dual client (mainnet data + testnet orders)"
```

---

### Task 6: Strategy Module

**Files:**
- Create: `strategy.py`
- Create: `tests/test_strategy.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_strategy.py
import numpy as np
import pytest
from strategy import calculate_bollinger_bands, check_trend, check_entry_signal


def test_bollinger_bands_calculation():
    # 21 closing prices
    closes = [100.0] * 20 + [110.0]
    upper, middle, lower = calculate_bollinger_bands(closes, period=20, std_dev=2)
    # SMA of first 20 = 100, last value uses 20 most recent (indices 1-20)
    assert middle is not None
    assert upper > middle
    assert lower < middle


def test_trend_bullish():
    # Close above middle band
    closes = [100.0] * 20 + [120.0]
    trend = check_trend(closes, period=20)
    assert trend == "LONG"


def test_trend_bearish():
    # Close below middle band
    closes = [100.0] * 20 + [80.0]
    trend = check_trend(closes, period=20)
    assert trend == "SHORT"


def test_entry_signal_long():
    # Price breaks above upper band + volume confirms
    closes = [100.0] * 20 + [115.0]
    volumes = [1000.0] * 20 + [2000.0]
    signal = check_entry_signal(closes, volumes, trend="LONG", period=20, std_dev=2)
    assert signal is True


def test_entry_signal_no_volume():
    # Price breaks above upper band but volume is low
    closes = [100.0] * 20 + [115.0]
    volumes = [1000.0] * 20 + [500.0]  # Below average
    signal = check_entry_signal(closes, volumes, trend="LONG", period=20, std_dev=2)
    assert signal is False


def test_entry_signal_wrong_trend():
    # Price breaks above upper band but trend is SHORT
    closes = [100.0] * 20 + [115.0]
    volumes = [1000.0] * 20 + [2000.0]
    signal = check_entry_signal(closes, volumes, trend="SHORT", period=20, std_dev=2)
    assert signal is False


def test_entry_signal_short():
    # Price breaks below lower band + volume confirms
    closes = [100.0] * 20 + [85.0]
    volumes = [1000.0] * 20 + [2000.0]
    signal = check_entry_signal(closes, volumes, trend="SHORT", period=20, std_dev=2)
    assert signal is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -m pytest tests/test_strategy.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'strategy'`

- [ ] **Step 3: Write strategy.py**

```python
import numpy as np
from binance.client import Client
from config import config
from exchange import Exchange
from state import StateManager
from notifier import notify, logger


def calculate_bollinger_bands(
    closes: list[float], period: int = 20, std_dev: float = 2.0
) -> tuple[float, float, float]:
    """Calculate Bollinger Bands from closing prices. Returns (upper, middle, lower)."""
    data = np.array(closes[-period:], dtype=float)
    middle = float(np.mean(data))
    std = float(np.std(data, ddof=0))
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def check_trend(closes: list[float], period: int = 20) -> str:
    """Determine trend from daily closes. Returns 'LONG' or 'SHORT'."""
    sma = float(np.mean(closes[-period:]))
    current_close = closes[-1]
    return "LONG" if current_close > sma else "SHORT"


def check_entry_signal(
    closes: list[float],
    volumes: list[float],
    trend: str,
    period: int = 20,
    std_dev: float = 2.0,
) -> bool:
    """Check if entry signal fires on hourly data."""
    upper, middle, lower = calculate_bollinger_bands(closes, period, std_dev)
    current_close = closes[-1]
    current_volume = volumes[-1]
    avg_volume = float(np.mean(volumes[-period - 1 : -1]))

    if current_volume <= avg_volume:
        return False

    if trend == "LONG" and current_close > upper:
        return True
    if trend == "SHORT" and current_close < lower:
        return True

    return False


def run_strategy(exchange: Exchange, state_mgr: StateManager):
    """Main strategy loop: scan top symbols, check signals, open positions."""
    if state_mgr.position_count >= config.MAX_POSITIONS:
        logger.info("Max positions reached, skipping scan")
        return
    if state_mgr.balance < config.POSITION_SIZE:
        logger.info(f"Insufficient balance: {state_mgr.balance:.2f}")
        return

    top_symbols = exchange.get_top_symbols()
    logger.info(f"Scanning {len(top_symbols)} symbols")

    kline_limit = config.BB_PERIOD + 1

    for symbol in top_symbols:
        if state_mgr.position_count >= config.MAX_POSITIONS:
            break
        if state_mgr.balance < config.POSITION_SIZE:
            break
        if state_mgr.get_position_by_symbol(symbol):
            continue

        try:
            # Daily trend
            daily_klines = exchange.get_klines(
                symbol, Client.KLINE_INTERVAL_1DAY, kline_limit
            )
            daily_closes = [float(k[4]) for k in daily_klines]
            if len(daily_closes) < kline_limit:
                continue
            trend = check_trend(daily_closes, config.BB_PERIOD)

            # Hourly signal
            hourly_klines = exchange.get_klines(
                symbol, Client.KLINE_INTERVAL_1HOUR, kline_limit
            )
            hourly_closes = [float(k[4]) for k in hourly_klines]
            hourly_volumes = [float(k[5]) for k in hourly_klines]
            if len(hourly_closes) < kline_limit:
                continue

            signal = check_entry_signal(
                hourly_closes, hourly_volumes, trend, config.BB_PERIOD, config.BB_STD
            )

            if signal:
                _open_position(exchange, state_mgr, symbol, trend, hourly_closes[-1])

        except Exception as e:
            logger.error(f"Error processing {symbol}: {e}")
            continue


def _open_position(
    exchange: Exchange,
    state_mgr: StateManager,
    symbol: str,
    side: str,
    current_price: float,
):
    """Open a new position."""
    notional = config.POSITION_SIZE * config.LEVERAGE
    raw_qty = notional / current_price
    quantity = exchange.round_quantity(symbol, raw_qty)

    if quantity <= 0:
        logger.warning(f"Quantity too small for {symbol}")
        return

    order_side = "BUY" if side == "LONG" else "SELL"
    try:
        exchange.set_leverage(symbol, config.LEVERAGE)
        order = exchange.place_order(symbol, order_side, quantity)

        state_mgr.add_position(
            symbol=symbol,
            side=side,
            entry_price=current_price,
            quantity=quantity,
        )
        state_mgr.update_balance(-config.POSITION_SIZE)

        notify(
            f"开仓 {side}",
            f"{symbol} | 价格 {current_price:.4f} | 数量 {quantity} | 保证金 ${config.POSITION_SIZE}",
        )
    except Exception as e:
        logger.error(f"Failed to open {side} {symbol}: {e}")
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -m pytest tests/test_strategy.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add strategy.py tests/test_strategy.py
git commit -m "feat: add strategy module with Bollinger Band signals"
```

---

### Task 7: Risk Module

**Files:**
- Create: `risk.py`
- Create: `tests/test_risk.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_risk.py
import pytest
from risk import should_stop_loss


def test_long_stop_loss_triggered():
    # Price dropped 3% from highest
    result = should_stop_loss(
        side="LONG",
        highest_price=100.0,
        lowest_price=90.0,
        current_price=96.5,  # 3.5% drop from highest
        long_stop=0.03,
        short_stop=0.05,
    )
    assert result is True


def test_long_stop_loss_not_triggered():
    result = should_stop_loss(
        side="LONG",
        highest_price=100.0,
        lowest_price=90.0,
        current_price=98.0,  # 2% drop, under threshold
        long_stop=0.03,
        short_stop=0.05,
    )
    assert result is False


def test_short_stop_loss_triggered():
    # Price rebounded 5% from lowest
    result = should_stop_loss(
        side="SHORT",
        highest_price=110.0,
        lowest_price=100.0,
        current_price=105.5,  # 5.5% rebound from lowest
        long_stop=0.03,
        short_stop=0.05,
    )
    assert result is True


def test_short_stop_loss_not_triggered():
    result = should_stop_loss(
        side="SHORT",
        highest_price=110.0,
        lowest_price=100.0,
        current_price=103.0,  # 3% rebound, under threshold
        long_stop=0.03,
        short_stop=0.05,
    )
    assert result is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -m pytest tests/test_risk.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'risk'`

- [ ] **Step 3: Write risk.py**

```python
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
        return

    for pos in positions:
        try:
            current_price = exchange.get_price(pos["symbol"])

            # Update extreme prices
            state_mgr.update_extreme_price(pos["id"], current_price)

            # Re-read updated position
            updated_pos = state_mgr.get_position_by_id(pos["id"])
            if updated_pos is None:
                continue

            triggered = should_stop_loss(
                side=updated_pos["side"],
                highest_price=updated_pos["highest_price"],
                lowest_price=updated_pos["lowest_price"],
                current_price=current_price,
                long_stop=config.LONG_TRAILING_STOP,
                short_stop=config.SHORT_TRAILING_STOP,
            )

            if triggered:
                _close_position(exchange, state_mgr, updated_pos, current_price)

        except Exception as e:
            logger.error(f"Error checking stop loss for {pos['symbol']}: {e}")


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

        # Calculate PnL
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
```

- [ ] **Step 4: Run tests**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -m pytest tests/test_risk.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add risk.py tests/test_risk.py
git commit -m "feat: add risk module with trailing stop loss"
```

---

### Task 8: Main Entry Point

**Files:**
- Create: `main.py`

- [ ] **Step 1: Write main.py**

```python
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
```

- [ ] **Step 2: Verify imports work**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -c "import main; print('imports OK')"`
Expected: `imports OK` (will show log output too).

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add main entry point with APScheduler orchestration"
```

---

### Task 9: Integration Smoke Test

- [ ] **Step 1: Run all unit tests**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -m pytest tests/ -v`
Expected: All tests PASS.

- [ ] **Step 2: Test mainnet data fetch end-to-end**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && python -c "
from exchange import Exchange
ex = Exchange()
symbols = ex.get_top_symbols(5)
print('Top 5:', symbols)
for s in symbols[:2]:
    price = ex.get_price(s)
    step = ex.get_step_size(s)
    print(f'{s}: price={price}, step={step}')
"`
Expected: Top 5 symbols and their prices printed.

- [ ] **Step 3: Verify .env is set up**

Ensure user has created `.env` with real Testnet keys before running the bot.

- [ ] **Step 4: Test bot startup (brief run)**

Run: `cd /Users/danny/Desktop/code/dabao && source .venv/bin/activate && timeout 10 python main.py || true`
Expected: Bot starts, prints startup log, heartbeat info, then exits after 10s timeout.

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: complete DualTrend Bollinger Strategy paper trading bot"
```

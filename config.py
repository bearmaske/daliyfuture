import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Trading Mode: "paper" (testnet) or "live" (mainnet)
    TRADING_MODE: str = os.getenv("TRADING_MODE", "paper")

    # API Keys
    BINANCE_TESTNET_API_KEY: str = os.getenv("BINANCE_TESTNET_API_KEY", "")
    BINANCE_TESTNET_API_SECRET: str = os.getenv("BINANCE_TESTNET_API_SECRET", "")
    BINANCE_LIVE_API_KEY: str = os.getenv("BINANCE_LIVE_API_KEY", "")
    BINANCE_LIVE_API_SECRET: str = os.getenv("BINANCE_LIVE_API_SECRET", "")
    TELEGRAM_ENABLED: bool = os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")
    BARK_ENABLED: bool = os.getenv("BARK_ENABLED", "false").lower() == "true"
    BARK_URLS: list = None
    PUSHDEER_ENABLED: bool = os.getenv("PUSHDEER_ENABLED", "false").lower() == "true"
    PUSHDEER_KEYS: list = None

    # Capital & Position
    INITIAL_CAPITAL: float = 10000.0
    POSITION_SIZE: float = 500.0
    MAX_POSITIONS: int = 10
    LEVERAGE: int = 5

    # ATR Trailing Stop
    ATR_PERIOD: int = 14
    ATR_MULTIPLIER: float = 2.0
    MAX_STOP_LOSS: float = 0.06  # hard cap: 6% regardless of ATR

    # Global Drawdown Circuit Breaker
    MAX_DRAWDOWN_PCT: float = 0.15  # force-close all if total assets drop 15% from initial
    COOLDOWN_HOURS: int = 24        # cooldown period after circuit breaker triggers

    # Trend Filter: "sma" = SMA slope, "bb_middle" = price vs daily BB middle, "disabled" = no filter
    TREND_FILTER_MODE: str = "sma"
    SMA_PERIOD: int = 20  # SMA period for daily trend check (independent of BB_PERIOD)

    # Volatility Filter: skip entry when ATR is contracting (low-vol regime)
    VOL_FILTER_ENABLED: bool = True
    VOL_ATR_SHORT: int = 7    # short-term ATR window (recent volatility)
    VOL_ATR_LONG: int = 28    # long-term ATR window (baseline volatility)
    VOL_ATR_THRESHOLD: float = 1.2  # short/long ratio must be >= this to allow entry

    # Bollinger Bands
    BB_PERIOD: int = 20
    BB_STD: float = 2.0

    # Scanning
    TOP_SYMBOLS_COUNT: int = 50
    STABLECOIN_FILTER: list = None
    # Skip tokenized stock + pre-IPO perpetuals (different dynamics, follow equity market)
    EXCLUDE_EQUITY_PERPS: bool = True
    # Skip crypto majors (too efficient, edge is in mid-cap momentum). Edit freely.
    EXCLUDE_TOP10_SYMBOLS: list = None

    # Scheduling
    STRATEGY_INTERVAL_HOURS: int = 1
    RISK_CHECK_INTERVAL_MINUTES: int = 1
    HEARTBEAT_INTERVAL_HOURS: int = 6

    # Strategy inception date (UTC+8) — drives "运行时长" in heartbeat
    STRATEGY_START_TIME: str = "2026-04-13 00:00:00"

    # Files
    STATE_FILE: str = "state.json"
    STATE_BACKUP_FILE: str = "state.backup.json"

    def __post_init__(self):
        if self.STABLECOIN_FILTER is None:
            self.STABLECOIN_FILTER = [
                "BUSDUSDT", "USDCUSDT", "TUSDUSDT", "DAIUSDT", "FDUSDUSDT"
            ]
        if self.EXCLUDE_TOP10_SYMBOLS is None:
            self.EXCLUDE_TOP10_SYMBOLS = [
                "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT",
                "DOGEUSDT", "ADAUSDT", "TRXUSDT", "TONUSDT", "AVAXUSDT",
            ]
        # Support multiple Bark URLs, comma-separated
        if self.BARK_URLS is None:
            raw = os.getenv("BARK_URLS", "")
            self.BARK_URLS = [u.strip() for u in raw.split(",") if u.strip()]
        # Support multiple PushDeer keys, comma-separated
        if self.PUSHDEER_KEYS is None:
            raw = os.getenv("PUSHDEER_KEYS", "")
            self.PUSHDEER_KEYS = [k.strip() for k in raw.split(",") if k.strip()]

    @property
    def is_live(self) -> bool:
        return self.TRADING_MODE.lower() == "live"


config = Config()

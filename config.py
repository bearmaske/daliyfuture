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

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv(override=True)


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
    INITIAL_CAPITAL: float = 8084.0
    POSITION_SIZE: float = 400.0
    MAX_POSITIONS: int = 10
    LEVERAGE: int = 5

    # Trailing TP + Fixed SL
    TRAILING_ACTIVATION_PCT: float = 0.03   # activate trailing when profit >= 3%
    TRAILING_DRAWDOWN_PCT: float = 0.015    # exit when price retraces 1.5% from extreme
    FIXED_STOP_LOSS_PCT: float = 0.02       # fixed stop loss 2% from entry

    # Global Drawdown Circuit Breaker
    MAX_DRAWDOWN_PCT: float = 0.20  # force-close all if total assets drop 20% from initial
    COOLDOWN_HOURS: int = 24        # cooldown period after circuit breaker triggers
    # Per-symbol cooldown: a symbol that loses this many trades within
    # SYMBOL_COOLDOWN_WINDOW_HOURS enters a SYMBOL_COOLDOWN_HOURS blacklist.
    SYMBOL_LOSS_THRESHOLD: int = 2
    SYMBOL_COOLDOWN_WINDOW_HOURS: int = 24
    SYMBOL_COOLDOWN_HOURS: int = 24
    # Binance position-risk blacklist: if open-order is rejected with a
    # position-risk error code (e.g. -4106), blacklist the symbol this long.
    POSITION_RISK_BLACKLIST_HOURS: int = 24

    # Trend Filter: "sma" = SMA slope, "bb_middle" = price vs daily BB middle, "disabled" = no filter
    TREND_FILTER_MODE: str = "bb_middle"
    SMA_PERIOD: int = 20  # SMA period for daily trend check (independent of BB_PERIOD)

    # Bollinger Bands
    BB_PERIOD: int = 20
    BB_STD: float = 2.0

    # 6H BB middle filter: require current 1H close on the trend-corresponding
    # side of the 6H BB middle (LONG above, SHORT below). Layered on top of
    # daily trend + 1H BB breakout + 24H high/low confirmation.
    H6_MIDDLE_FILTER_ENABLED: bool = True

    # BNB fee burn — pay USDⓈ-M Futures fees in BNB for 10% discount.
    # When True, bot syncs the Binance-side toggle to ON at startup. When False,
    # bot does NOT touch the account setting (leaves whatever you set manually).
    # NOTE: bot does NOT auto-buy/transfer BNB. You must keep BNB in the futures
    # wallet manually, otherwise Binance falls back to USDT (no discount).
    BNB_FEE_BURN_ENABLED: bool = False
    BNB_BALANCE_MIN_ALERT: float = 0.05  # heartbeat warns when BNB futures balance below this

    # Scanning
    TOP_SYMBOLS_COUNT: int = 50
    STABLECOIN_FILTER: list = None
    # Permanent symbol blacklist: always excluded from the scan pool regardless of volume.
    SYMBOL_BLACKLIST: list = None
    # Skip non-crypto perpetuals: tokenized stocks (EQUITY), pre-IPO (PREMARKET),
    # commodities (COMMODITY: 黄金/白银/原油/天然气/铜/钯/铂), indices (INDEX: BTCDOM 等).
    # 这些跟随传统市场或 basket 行情，与币市动量策略不兼容。
    EXCLUDE_EQUITY_PERPS: bool = True
    # Skip crypto majors (too efficient, edge is in mid-cap momentum). Edit freely.
    EXCLUDE_TOP10_SYMBOLS: list = None
    # Minimum 24h quote volume (USDT). Protects against thin-liquidity coins.
    MIN_QUOTE_VOLUME_24H: float = 50_000_000.0
    # Sustained-liquidity filter: the 24h floor above is a *point-in-time* gate —
    # a thin coin that pumps for one day spikes over it, gets traded, then reverts
    # to thin (实盘验证:这类细币尾巴净亏 −$1,212). This requires the symbol's
    # MEDIAN daily quote volume over the last N closed days to clear a floor too.
    # Mode: "off" = disabled | "observe" = log what it WOULD drop, don't drop |
    #       "enforce" = actually drop. Default "observe" for a safe forward look.
    SUSTAINED_VOLUME_FILTER_MODE: str = "observe"
    SUSTAINED_VOLUME_LOOKBACK_DAYS: int = 7
    MIN_SUSTAINED_QUOTE_VOLUME: float = 50_000_000.0
    # Additional "spike pool": include coins whose most recent closed 1H quote
    # volume is >= MIN_1H_QUOTE_VOLUME, even if they miss the 24h top-N list.
    # Designed to catch 爆涨暴跌 that hasn't shown up in the 24h average yet.
    ENABLE_1H_SPIKE_POOL: bool = False
    MIN_1H_QUOTE_VOLUME: float = 10_000_000.0

    # Scheduling
    STRATEGY_INTERVAL_HOURS: int = 1
    RISK_CHECK_INTERVAL_SECONDS: int = 60
    HEARTBEAT_INTERVAL_HOURS: int = 4

    # Strategy inception date (UTC+8) — drives "运行时长" in heartbeat
    STRATEGY_START_TIME: str = "2026-05-06 21:00:00"

    # Files
    STATE_FILE: str = "state.json"
    STATE_BACKUP_FILE: str = "state.backup.json"

    def __post_init__(self):
        if self.STABLECOIN_FILTER is None:
            self.STABLECOIN_FILTER = [
                "BUSDUSDT", "USDCUSDT", "TUSDUSDT", "DAIUSDT", "FDUSDUSDT"
            ]
        if self.SYMBOL_BLACKLIST is None:
            self.SYMBOL_BLACKLIST = [
                "MEGAUSDT",
                "PAXGUSDT",  # Paxos Gold 代币，underlyingType=COIN 但跟随黄金价格
            ]
        if self.EXCLUDE_TOP10_SYMBOLS is None:
            # Empty by default — top 10 majors are included in the scan pool
            self.EXCLUDE_TOP10_SYMBOLS = []
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

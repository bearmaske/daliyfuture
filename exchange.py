import math
import time
from typing import Optional, List
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

    def get_top_symbols(self, limit: Optional[int] = None) -> List[str]:
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

    def get_klines(self, symbol: str, interval: str, limit: int) -> list:
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

    def get_account_balance(self) -> float:
        """Get available USDT balance from testnet account."""
        account = self._retry(lambda: self.testnet_client.futures_account())
        for asset in account.get("assets", []):
            if asset["asset"] == "USDT":
                return float(asset["availableBalance"])
        return 0.0

    def get_open_positions(self) -> list:
        """Get all open positions from testnet account.
        Returns list of dicts with symbol, side, entry_price, quantity."""
        positions = self._retry(
            lambda: self.testnet_client.futures_position_information()
        )
        open_positions = []
        for p in positions:
            qty = float(p["positionAmt"])
            if qty == 0:
                continue
            open_positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if qty > 0 else "SHORT",
                "entry_price": float(p["entryPrice"]),
                "quantity": abs(qty),
                "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
            })
        return open_positions

    def sync_state(self, state_mgr) -> None:
        """Sync local state with actual Testnet account."""
        remote_balance = self.get_account_balance()
        remote_positions = self.get_open_positions()

        added, removed = state_mgr.sync_positions(remote_positions, remote_balance)

        if added:
            logger.info("[同步] 新增本地持仓: %s", ", ".join(added))
        if removed:
            logger.info("[同步] 移除本地持仓: %s", ", ".join(removed))

        logger.info("[同步] Testnet 余额: $%.2f | 持仓: %d",
                    remote_balance, len(remote_positions))

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

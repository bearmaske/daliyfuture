import math
import time
from typing import Optional, List
from binance.client import Client
from notifier import logger
from config import config


class Exchange:
    def __init__(self):
        # Mainnet client for market data (always mainnet for kline quality)
        self.data_client = Client()
        # Trading client lazy-initialized (testnet or mainnet depending on mode)
        self._trading_client = None
        self._symbol_filters = {}
        self._filters_loaded = False

    @property
    def trading_client(self) -> Client:
        """Trading client: testnet in paper mode, mainnet with auth in live mode."""
        if self._trading_client is None:
            if config.is_live:
                self._trading_client = Client(
                    api_key=config.BINANCE_LIVE_API_KEY,
                    api_secret=config.BINANCE_LIVE_API_SECRET,
                )
            else:
                self._trading_client = Client(
                    api_key=config.BINANCE_TESTNET_API_KEY,
                    api_secret=config.BINANCE_TESTNET_API_SECRET,
                    testnet=True,
                )
        return self._trading_client

    @property
    def testnet_client(self) -> Client:
        """Backward-compatible alias."""
        return self.trading_client

    def get_top_symbols(self, limit: Optional[int] = None) -> List[str]:
        """Get top N USDT perpetual futures by quote volume."""
        limit = limit or config.TOP_SYMBOLS_COUNT
        tickers = self._retry(lambda: self.data_client.futures_ticker())
        # Filter USDT pairs, exclude stablecoins, exclude non-ASCII
        # (python-binance mis-encodes non-ASCII symbols → APIError -1022 signature mismatch)
        skipped_non_ascii = []
        usdt_tickers = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT") or sym in config.STABLECOIN_FILTER:
                continue
            if not sym.isascii():
                skipped_non_ascii.append(sym)
                continue
            usdt_tickers.append(t)
        if skipped_non_ascii:
            logger.info("[扫描] 跳过非ASCII符号: %s", ", ".join(skipped_non_ascii))
        # Sort by quote asset volume descending
        usdt_tickers.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
        return [t["symbol"] for t in usdt_tickers[:limit]]

    def get_volume_map(self, symbols: List[str]) -> dict:
        """Get 24h quote volume for given symbols. Returns {symbol: quoteVolume}."""
        tickers = self._retry(lambda: self.data_client.futures_ticker())
        symbol_set = set(symbols)
        return {
            t["symbol"]: float(t["quoteVolume"])
            for t in tickers
            if t["symbol"] in symbol_set
        }

    def get_funding_info(self, symbol: str) -> dict:
        """Get current funding rate and next funding time for a symbol."""
        from datetime import datetime, timezone, timedelta
        tz_cn = timezone(timedelta(hours=8))
        mark = self._retry(lambda: self.data_client.futures_mark_price(symbol=symbol))
        rate = float(mark.get("lastFundingRate", 0))
        next_ts = int(mark.get("nextFundingTime", 0))
        next_time = datetime.fromtimestamp(
            next_ts / 1000, tz=tz_cn
        ).strftime("%H:%M") if next_ts else "--:--"
        return {
            "rate": rate,
            "rate_pct": rate * 100,
            "next_time": next_time,
        }

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

    def place_order(self, symbol: str, side: str, quantity: float,
                    position_side: str = None) -> dict:
        """Place a market order. Returns order response with commission info.
        position_side: 'LONG'/'SHORT' for hedge mode, None for one-way mode."""
        mode_label = "LIVE" if config.is_live else "PAPER"
        logger.info(f"[{mode_label}] Placing {side} order: {symbol} qty={quantity} positionSide={position_side or 'BOTH'}")
        params = dict(
            symbol=symbol,
            side=side,
            type="MARKET",
            quantity=quantity,
        )
        if self._is_hedge_mode():
            params["positionSide"] = position_side or "BOTH"
        return self._retry(
            lambda: self.trading_client.futures_create_order(**params)
        )

    def _is_hedge_mode(self) -> bool:
        """Check if account is in hedge mode (dual position side)."""
        if not hasattr(self, '_hedge_mode'):
            try:
                resp = self._retry(lambda: self.trading_client.futures_get_position_mode())
                self._hedge_mode = resp.get("dualSidePosition", False)
            except Exception:
                self._hedge_mode = False
        return self._hedge_mode

    def get_order_commission(self, symbol: str, order_id: int) -> float:
        """Get total USDT commission for an order from trade fills."""
        trades = self._retry(
            lambda: self.trading_client.futures_account_trades(
                symbol=symbol, orderId=order_id
            )
        )
        total_commission = 0.0
        for trade in trades:
            commission = float(trade.get("commission", 0))
            asset = trade.get("commissionAsset", "USDT")
            if asset == "USDT":
                total_commission += commission
            elif asset == "BNB":
                # Convert BNB commission to USDT
                try:
                    bnb_price = float(self.data_client.get_symbol_ticker(symbol="BNBUSDT")["price"])
                    total_commission += commission * bnb_price
                except Exception:
                    total_commission += commission  # fallback: use raw value
        return total_commission

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

    def get_account_summary(self) -> dict:
        """Get account summary: total wallet balance, unrealized PnL, total assets."""
        account = self._retry(lambda: self.testnet_client.futures_account())
        total_wallet_balance = float(account.get("totalWalletBalance", 0))
        total_unrealized_pnl = float(account.get("totalUnrealizedProfit", 0))
        total_margin_balance = float(account.get("totalMarginBalance", 0))
        available_balance = 0.0
        for asset in account.get("assets", []):
            if asset["asset"] == "USDT":
                available_balance = float(asset["availableBalance"])
                break
        return {
            "total_wallet_balance": total_wallet_balance,
            "total_unrealized_pnl": total_unrealized_pnl,
            "total_margin_balance": total_margin_balance,
            "available_balance": available_balance,
        }

    def get_open_positions(self) -> list:
        """Get all open positions from testnet account."""
        from datetime import datetime, timezone, timedelta
        tz_cn = timezone(timedelta(hours=8))
        positions = self._retry(
            lambda: self.testnet_client.futures_position_information()
        )
        open_positions = []
        for p in positions:
            qty = float(p["positionAmt"])
            if qty == 0:
                continue
            update_ts = int(p.get("updateTime", 0))
            opened_at = datetime.fromtimestamp(
                update_ts / 1000, tz=tz_cn
            ).strftime("%Y-%m-%d %H:%M:%S") if update_ts else None
            open_positions.append({
                "symbol": p["symbol"],
                "side": "LONG" if qty > 0 else "SHORT",
                "entry_price": float(p["entryPrice"]),
                "quantity": abs(qty),
                "unrealized_pnl": float(p.get("unRealizedProfit", 0)),
                "opened_at": opened_at,
            })
        return open_positions

    def sync_state(self, state_mgr) -> None:
        """Sync local state with actual account (testnet or mainnet)."""
        remote_balance = self.get_account_balance()
        remote_positions = self.get_open_positions()

        added, removed = state_mgr.sync_positions(remote_positions, remote_balance)

        if added:
            logger.info("[同步] 新增本地持仓: %s", ", ".join(added))
        if removed:
            logger.info("[同步] 移除本地持仓: %s", ", ".join(removed))

        mode_label = "实盘" if config.is_live else "Testnet"
        logger.info("[同步] %s 余额: $%.2f | 持仓: %d",
                    mode_label, remote_balance, len(remote_positions))

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

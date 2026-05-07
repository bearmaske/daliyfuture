import math
import time
import concurrent.futures as _cf
from typing import Optional, List
from urllib.parse import urlencode
from binance.client import Client
from notifier import logger
from config import config


# python-binance 1.0.19 signs the raw Python string `a=v1&b=v2`, but `requests`
# sends the body URL-encoded. For ASCII this is identical; for non-ASCII
# symbols (e.g. 币安人生USDT) the server's signature differs → APIError -1022.
# Re-sign using urlencode so the signed string matches the wire body.
def _urlencoded_generate_signature(self, data):
    sig_func = self._hmac_signature
    if getattr(self, "PRIVATE_KEY", None):
        sig_func = self._rsa_signature
    return sig_func(urlencode(self._order_params(data)))


Client._generate_signature = _urlencoded_generate_signature


class Exchange:
    def __init__(self):
        # Mainnet client for market data (always mainnet for kline quality)
        self.data_client = Client()
        # Trading client lazy-initialized (testnet or mainnet depending on mode)
        self._trading_client = None
        self._symbol_filters = {}
        self._tick_sizes = {}
        self._underlying_types = {}
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
        """Return scan pool: top-N by 24h volume UNION coins with a 1H volume spike.

        Pool A (24h): stablecoin/equity/top10 filtered, 24h volume >= MIN_QUOTE_VOLUME_24H,
        ranked desc, take top N.

        Pool B (1H spike, optional): same symbol filters, last-closed 1H quote volume
        >= MIN_1H_QUOTE_VOLUME. Designed to catch explosive moves whose 24h average
        hasn't caught up yet.
        """
        limit = limit or config.TOP_SYMBOLS_COUNT
        self._load_exchange_info()
        tickers = self._retry(lambda: self.data_client.futures_ticker())
        top10_set = set(config.EXCLUDE_TOP10_SYMBOLS or [])
        blacklist_set = set(config.SYMBOL_BLACKLIST or [])
        excluded_equity = config.EXCLUDE_EQUITY_PERPS
        min_vol = config.MIN_QUOTE_VOLUME_24H
        skipped_equity, skipped_top10 = [], []

        # Eligible universe (passes the non-volume filters)
        eligible = []
        for t in tickers:
            sym = t["symbol"]
            if not sym.endswith("USDT") or sym in config.STABLECOIN_FILTER:
                continue
            if sym in blacklist_set:
                continue
            if excluded_equity and self._underlying_types.get(sym, "COIN") in ("EQUITY", "PREMARKET"):
                skipped_equity.append(sym)
                continue
            if sym in top10_set:
                skipped_top10.append(sym)
                continue
            eligible.append(t)

        if skipped_equity:
            logger.info("[扫描] 跳过股票/预上市: %s", ", ".join(skipped_equity))
        if skipped_top10:
            logger.info("[扫描] 跳过市值前10: %s", ", ".join(skipped_top10))

        logger.info("[扫描] 全网 USDT 永续: %d | 去稳定币/股票/Top10 后候选: %d",
                    sum(1 for t in tickers if t["symbol"].endswith("USDT")), len(eligible))

        # Pool A: top N by 24h volume (with 24h floor)
        pool_a = [t for t in eligible if float(t.get("quoteVolume", 0)) >= min_vol]
        pool_a.sort(key=lambda x: float(x["quoteVolume"]), reverse=True)
        pool_a_syms = [t["symbol"] for t in pool_a[:limit]]
        below_floor = len(eligible) - len(pool_a)
        if pool_a_syms:
            top_vol = float(pool_a[0]["quoteVolume"]) / 1e6
            last_vol = float(pool_a[min(limit, len(pool_a)) - 1]["quoteVolume"]) / 1e6
            logger.info("[扫描] 24h 池: %d 个 (≥ $%.0fM, 最高 $%.1fM / 末位 $%.1fM, 另有 %d 个未过线)",
                        len(pool_a_syms), min_vol / 1e6, top_vol, last_vol, below_floor)

        if not config.ENABLE_1H_SPIKE_POOL:
            return pool_a_syms

        # Pool B: 1H spike. Fetch last-closed 1H volume for every eligible symbol.
        t_start = time.time()
        spike_items = self._scan_1h_spikes(
            [t["symbol"] for t in eligible], config.MIN_1H_QUOTE_VOLUME
        )
        elapsed = time.time() - t_start
        logger.info("[扫描] 1H 爆量扫描: 拉取 %d 根 K 线，耗时 %.1fs | 命中 %d 个 (≥ $%.0fM)",
                    len(eligible), elapsed, len(spike_items), config.MIN_1H_QUOTE_VOLUME / 1e6)

        # Sort spike additions by their own 1H volume desc for deterministic order
        extras = [(s, v) for s, v in spike_items if s not in pool_a_syms]
        if extras:
            detail = ", ".join(f"{s}(${v/1e6:.1f}M)" for s, v in extras)
            logger.info("[扫描] 1H 爆量追加(非 24h 池): %d 个 | %s", len(extras), detail)
        return pool_a_syms + [s for s, _ in extras]

    def _scan_1h_spikes(self, symbols: List[str], min_qvol: float) -> List[tuple]:
        """Fetch last-closed 1H quote volume for each symbol in parallel.
        Returns [(symbol, quote_volume), ...] whose 1H quote volume >= min_qvol, sorted desc."""
        def _fetch(sym):
            try:
                kl = self.data_client.futures_klines(symbol=sym, interval="1h", limit=2)
                if len(kl) < 2:
                    return sym, 0.0
                return sym, float(kl[-2][7])  # last CLOSED kline, quote volume at index 7
            except Exception as e:
                logger.debug("[1H扫描] %s 失败: %s", sym, e)
                return sym, 0.0

        results = []
        with _cf.ThreadPoolExecutor(max_workers=20) as ex:
            for sym, qvol in ex.map(_fetch, symbols):
                if qvol >= min_qvol:
                    results.append((sym, qvol))
        results.sort(key=lambda x: x[1], reverse=True)
        return results

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

    def _load_exchange_info(self):
        """Load symbol filters and underlying types (cached)."""
        if self._filters_loaded:
            return
        info = self._retry(lambda: self.data_client.futures_exchange_info())
        for s in info["symbols"]:
            self._underlying_types[s["symbol"]] = s.get("underlyingType", "COIN")
            for f in s["filters"]:
                if f["filterType"] == "LOT_SIZE":
                    self._symbol_filters[s["symbol"]] = float(f["stepSize"])
                elif f["filterType"] == "PRICE_FILTER":
                    self._tick_sizes[s["symbol"]] = float(f["tickSize"])
        self._filters_loaded = True

    def get_step_size(self, symbol: str) -> float:
        """Get lot step size for quantity rounding."""
        self._load_exchange_info()
        return self._symbol_filters.get(symbol, 0.001)

    def get_underlying_type(self, symbol: str) -> str:
        """Get underlyingType (COIN/EQUITY/COMMODITY/INDEX/PREMARKET)."""
        self._load_exchange_info()
        return self._underlying_types.get(symbol, "COIN")

    def round_quantity(self, symbol: str, quantity: float) -> float:
        """Round quantity down to valid step size."""
        step = self.get_step_size(symbol)
        precision = int(round(-math.log10(step)))
        return math.floor(quantity * 10**precision) / 10**precision

    def round_price(self, symbol: str, price: float) -> float:
        """Round price to symbol's tick size."""
        self._load_exchange_info()
        tick = self._tick_sizes.get(symbol, 0.0001)
        precision = int(round(-math.log10(tick))) if tick > 0 else 4
        return round(round(price / tick) * tick, precision)

    def _algo_order(self, params: dict) -> dict:
        """POST /fapi/v1/algoOrder — used for STOP_MARKET and TRAILING_STOP_MARKET.
        Returns the algo order response (contains algoId, algoStatus, etc.)."""
        # Use dict(params) inside the lambda so each retry gets a fresh copy.
        # _request_futures_api mutates its `data` dict in-place (adds timestamp,
        # recvWindow). If we reuse the same dict across retries, the stale
        # timestamp gets included in the HMAC and -1022 fires on attempt 2+.
        return self._retry(
            lambda: self.trading_client._request_futures_api(
                "post", "algoOrder", signed=True, data=dict(params)
            )
        )

    def _get_algo_order(self, algo_id: int) -> dict:
        """GET /fapi/v1/algoOrder — query algo order status."""
        return self._retry(
            lambda: self.trading_client._request_futures_api(
                "get", "algoOrder", signed=True, data={"algoId": algo_id}
            )
        )

    def _cancel_algo_order(self, algo_id: int) -> None:
        """DELETE /fapi/v1/algoOrder — cancel an algo order."""
        self._retry(
            lambda: self.trading_client._request_futures_api(
                "delete", "algoOrder", signed=True, data={"algoId": algo_id}
            )
        )

    def place_trailing_stop_order(self, symbol: str, side: str, quantity: float,
                                  activation_price: float, callback_rate: float,
                                  position_side: str = None) -> dict:
        """Place a TRAILING_STOP_MARKET algo order via /fapi/v1/algoOrder.

        Returns dict with 'orderId' key set to algoId for uniform handling.
        """
        mode_label = "LIVE" if config.is_live else "PAPER"
        logger.info(
            "[%s] Placing TRAILING_STOP_MARKET %s: %s qty=%g activationPrice=%.4f callbackRate=%.1f%%",
            mode_label, side, symbol, quantity, activation_price, callback_rate,
        )
        params = dict(
            symbol=symbol,
            side=side,
            type="TRAILING_STOP_MARKET",
            algoType="CONDITIONAL",
            quantity=quantity,
            triggerPrice=activation_price,
            callbackRate=callback_rate,
            workingType="MARK_PRICE",
        )
        if self._is_hedge_mode():
            params["positionSide"] = position_side or "BOTH"
        resp = self._algo_order(params)
        resp["orderId"] = resp.get("algoId")
        return resp

    def place_stop_order(self, symbol: str, side: str, quantity: float,
                         stop_price: float, position_side: str = None) -> dict:
        """Place a STOP_MARKET algo order via /fapi/v1/algoOrder.

        Returns dict with 'orderId' key set to algoId for uniform handling.
        """
        mode_label = "LIVE" if config.is_live else "PAPER"
        logger.info("[%s] Placing STOP_MARKET %s: %s qty=%g stopPrice=%.4f positionSide=%s",
                    mode_label, side, symbol, quantity, stop_price, position_side or "BOTH")
        params = dict(
            symbol=symbol,
            side=side,
            type="STOP_MARKET",
            algoType="CONDITIONAL",
            quantity=quantity,
            triggerPrice=stop_price,
            workingType="MARK_PRICE",
        )
        if self._is_hedge_mode():
            params["positionSide"] = position_side or "BOTH"
        resp = self._algo_order(params)
        resp["orderId"] = resp.get("algoId")
        return resp

    def get_order_status(self, symbol: str, order_id: int) -> dict:
        """Query algo order status via GET /fapi/v1/algoOrder.

        algoStatus values: NEW, WORKING, CANCELLED, FILLED, EXPIRED, FAILED.
        Mapped to standard status names for uniform handling in risk.py.
        """
        resp = self._get_algo_order(order_id)
        algo_status = resp.get("algoStatus", "")
        # Map algoStatus → status names used in risk.py
        # FINISHED = algo order triggered and the actual market order filled
        status_map = {
            "NEW": "NEW",
            "WORKING": "NEW",
            "FINISHED": "FILLED",   # triggered and executed
            "FILLED": "FILLED",
            "CANCELLED": "CANCELED",
            "CANCELED": "CANCELED",
            "EXPIRED": "EXPIRED",
            "FAILED": "CANCELED",
        }
        status = status_map.get(algo_status, algo_status)
        # algo orders use actualPrice/actualQty for fill info
        avg_price = float(resp.get("actualPrice") or resp.get("avgPrice") or 0)
        exec_qty = float(resp.get("actualQty") or resp.get("executedQty") or 0)
        return {
            "status": status,
            "avgPrice": avg_price,
            "executedQty": exec_qty,
        }

    def cancel_order(self, symbol: str, order_id: int) -> None:
        """Cancel an algo order via DELETE /fapi/v1/algoOrder."""
        try:
            self._cancel_algo_order(order_id)
        except Exception as e:
            logger.debug("[撤单] algoId=%s: %s", order_id, e)

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

    def get_order_fill(self, symbol: str, order_id: int, fallback_price: float) -> tuple:
        """Return (avg_fill_price, executed_qty) for a filled order.

        Reads `avgPrice`/`executedQty` from the order response. For MARKET orders
        Binance fills synchronously, but `avgPrice` occasionally comes back as "0"
        when the response is returned before the fill is fully aggregated — in
        that case we query the order and, failing that, reconstruct from userTrades.
        `fallback_price` is used as last-resort entry price (pre-trade ticker).
        """
        def _parse(resp):
            try:
                ap = float(resp.get("avgPrice") or 0)
                eq = float(resp.get("executedQty") or 0)
            except (TypeError, ValueError):
                ap, eq = 0.0, 0.0
            return ap, eq

        try:
            resp = self._retry(lambda: self.trading_client.futures_get_order(
                symbol=symbol, orderId=order_id))
            ap, eq = _parse(resp)
            if ap > 0 and eq > 0:
                return ap, eq
        except Exception as e:
            logger.warning("[成交价] get_order 失败 %s#%s: %s", symbol, order_id, e)

        try:
            trades = self._retry(lambda: self.trading_client.futures_account_trades(
                symbol=symbol, orderId=order_id))
            if trades:
                total_qty = sum(float(t["qty"]) for t in trades)
                total_quote = sum(float(t["quoteQty"]) for t in trades)
                if total_qty > 0:
                    return total_quote / total_qty, total_qty
        except Exception as e:
            logger.warning("[成交价] userTrades 失败 %s#%s: %s", symbol, order_id, e)

        return fallback_price, 0.0

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
        """Get total USDT commission for an order from trade fills
        (per GET /fapi/v1/userTrades, response field `commission`)."""
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

    def get_total_commission_since(self, start_ms: int) -> float:
        """Sum all platform-reported commission since `start_ms` using
        GET /fapi/v1/income with incomeType=COMMISSION. Returns absolute USDT
        amount paid (positive). Handles pagination (1000 rows per call) and
        converts BNB commissions to USDT at current price."""
        PAGE = 1000
        cursor = int(start_ms)
        now_ms = int(time.time() * 1000)
        total_usdt = 0.0
        total_bnb = 0.0
        while cursor < now_ms:
            rows = self._retry(lambda c=cursor: self.trading_client.futures_income_history(
                incomeType="COMMISSION", startTime=c, limit=PAGE,
            ))
            if not rows:
                break
            for r in rows:
                amt = float(r["income"])  # commission rows are negative (outflow)
                if r.get("asset") == "BNB":
                    total_bnb += amt
                else:
                    total_usdt += amt
            if len(rows) < PAGE:
                break
            # advance cursor past the last row to avoid duplicate ingestion
            cursor = int(rows[-1]["time"]) + 1
        # Convert BNB portion
        if total_bnb != 0:
            try:
                bnb_price = float(self.data_client.get_symbol_ticker(symbol="BNBUSDT")["price"])
                total_usdt += total_bnb * bnb_price
            except Exception:
                total_usdt += total_bnb
        # Income is negative for commissions paid; return positive magnitude
        return abs(total_usdt)

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

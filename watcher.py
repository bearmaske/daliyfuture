"""Mark price watcher: real-time WebSocket for trailing TP activation.

Subscribes to Binance futures mark price streams for each open position that
hasn't activated trailing TP yet. When mark price crosses the activation
threshold (profit >= TRAILING_ACTIVATION_PCT), immediately places a
TRAILING_STOP_MARKET order on the exchange.

Only handles activation — order status polling stays in check_stop_loss().
"""
import threading
from binance import ThreadedWebsocketManager
from config import config
from notifier import logger


class MarkPriceWatcher:
    def __init__(self, exchange, state_mgr):
        self.exchange = exchange
        self.state_mgr = state_mgr
        self._twm: ThreadedWebsocketManager | None = None
        self._streams: dict[str, str] = {}   # symbol → stream key
        self._activating: set[str] = set()   # symbols mid-placement (dedup guard)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        api_key = config.BINANCE_LIVE_API_KEY if config.is_live else config.BINANCE_TESTNET_API_KEY
        api_secret = config.BINANCE_LIVE_API_SECRET if config.is_live else config.BINANCE_TESTNET_API_SECRET
        self._twm = ThreadedWebsocketManager(
            api_key=api_key,
            api_secret=api_secret,
            testnet=not config.is_live,
        )
        self._twm.start()
        logger.info("[WebSocket] 标记价格监控已启动 | 模式: %s",
                    "实盘" if config.is_live else "Testnet")

    def stop(self):
        if self._twm:
            self._twm.stop()
            self._twm.join()
            logger.info("[WebSocket] 已停止")

    # ------------------------------------------------------------------
    # Subscription management — call after each position open/close
    # ------------------------------------------------------------------

    def update_subscriptions(self):
        """Sync WebSocket subscriptions with current open positions.

        Subscribe to positions that haven't placed a trailing order yet.
        Unsubscribe from symbols no longer in that set.
        """
        positions = self.state_mgr.state.get("positions", [])
        need_watch = {
            pos["symbol"] for pos in positions
            if not pos.get("trailing_order_id")
        }
        with self._lock:
            for symbol in need_watch:
                if symbol not in self._streams:
                    self._subscribe(symbol)
            for symbol in list(self._streams):
                if symbol not in need_watch:
                    self._unsubscribe(symbol)

    def _subscribe(self, symbol: str):
        """Must be called with self._lock held."""
        key = self._twm.start_futures_socket(
            callback=self._on_message,
            payload=f"{symbol.lower()}@markPrice",
        )
        self._streams[symbol] = key
        logger.info("[WebSocket] 订阅 %s@markPrice", symbol)

    def _unsubscribe(self, symbol: str):
        """Must be called with self._lock held."""
        key = self._streams.pop(symbol, None)
        if key:
            self._twm.stop_socket(key)
            logger.debug("[WebSocket] 取消订阅 %s", symbol)

    # ------------------------------------------------------------------
    # Message handler (runs in WebSocket thread)
    # ------------------------------------------------------------------

    def _on_message(self, msg: dict):
        if msg.get("e") != "markPriceUpdate":
            return

        symbol = msg.get("s", "")
        try:
            mark_price = float(msg["p"])
        except (KeyError, TypeError, ValueError):
            return
        if not symbol or mark_price <= 0:
            return

        # Dedup guard — prevent concurrent placement for same symbol
        with self._lock:
            if symbol in self._activating:
                return

        pos = self.state_mgr.get_position_by_symbol(symbol)
        if not pos or pos.get("trailing_order_id"):
            # Already activated or position closed — unsubscribe
            with self._lock:
                self._unsubscribe(symbol)
            return

        # Check activation threshold
        entry = pos["entry_price"]
        if pos["side"] == "LONG":
            activated = mark_price >= entry * (1 + config.TRAILING_ACTIVATION_PCT)
            extreme_price = max(pos["highest_price"], mark_price)
        else:
            activated = mark_price <= entry * (1 - config.TRAILING_ACTIVATION_PCT)
            extreme_price = min(pos["lowest_price"], mark_price)

        if not activated:
            return

        with self._lock:
            self._activating.add(symbol)

        try:
            logger.info(
                "[WebSocket] %s %s 浮盈达到 %.0f%% 激活阈值 @ %.4f，立即挂移动止盈单",
                symbol, pos["side"], config.TRAILING_ACTIVATION_PCT * 100, mark_price,
            )
            # Import here to avoid circular import at module load time
            from risk import _place_trailing_order
            _place_trailing_order(self.exchange, self.state_mgr, pos, extreme_price)

            # Unsubscribe — trailing order is now on the exchange
            with self._lock:
                self._unsubscribe(symbol)

        except Exception as e:
            logger.error("[WebSocket] %s 挂移动止盈单异常: %s", symbol, e)
        finally:
            with self._lock:
                self._activating.discard(symbol)

"""
Standalone IBKRConnection for algotrading-monitor.
Stripped of investing_algorithm_framework dependencies.
"""
import logging
import os
import inspect
import time
from datetime import datetime

import asyncio
import threading
import random

from ib_async import IB, Stock, util
from ib_async import MarketOrder, LimitOrder, Trade

logger = logging.getLogger(__name__)

# --- Global Cache for IBKR Data ---
_DEAD_STREAMS = {}  # (symbol, tf_str) -> last_fail_ts
_DEAD_COOLDOWN_MINUTES = 1


class IBKRIgnoreFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        if "Error 162" in msg and "cancelled" in msg:
            return False
        if "Warning 2104" in msg or "Warning 2106" in msg:
            return False
        return True


for _ln in ["ib_async.wrapper", "ib_async.client"]:
    logging.getLogger(_ln).addFilter(IBKRIgnoreFilter())


class IBKRConnection:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(IBKRConnection, cls).__new__(cls)
                cls._instance.ib = IB()
                cls._instance.connected = False
                cls._instance.loop = asyncio.new_event_loop()
                cls._instance.thread = threading.Thread(
                    target=cls._instance._run_loop, daemon=True
                )
                cls._instance.thread.start()

                cls._instance.host = os.getenv("IBKR_HOST", "127.0.0.1")
                cls._instance.port = int(os.getenv("IBKR_PORT", 4002))
                cls._instance.clientId = int(
                    os.getenv("IBKR_CLIENT_ID", random.randint(10000, 99999))
                )

                cls._instance._conn_lock = threading.Lock()
                cls._instance._streaming_bars = {}
                cls._instance._streaming_tickers = {}
                cls._instance._in_flight_sells = {}
                cls._instance._symbol_locks = {}
                cls._instance._recently_cancelled = {}
                cls._instance._short_sell_blocked = {}
                cls._instance.account_id = None

                # Anti-short sentinel background task
                cls._instance.loop.call_soon_threadsafe(
                    lambda: cls._instance.loop.create_task(
                        cls._instance._sentinel_task()
                    )
                )
            return cls._instance

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro_func, *args, **kwargs):
        """Executes a callable (which may return a coroutine) inside the background loop."""
        async def wrap():
            if asyncio.iscoroutine(coro_func):
                return await coro_func
            res = coro_func(*args, **kwargs)
            if asyncio.iscoroutine(res) or inspect.isawaitable(res):
                return await res
            return res

        return asyncio.run_coroutine_threadsafe(wrap(), self.loop).result()

    def connect(self):
        with self._conn_lock:
            if self.connected and self.ib.isConnected():
                return

            max_retries = 5
            for attempt in range(max_retries):
                try:
                    if self.ib.isConnected():
                        self.run(self.ib.disconnect)

                    logger.info(
                        f"🔄 [IBKR] Connecting (attempt {attempt+1}/{max_retries}) "
                        f"to {self.host}:{self.port} (clientId={self.clientId})…"
                    )
                    self.run(
                        asyncio.wait_for,
                        self.ib.connectAsync(self.host, self.port, clientId=self.clientId),
                        timeout=20,
                    )
                    self.connected = True

                    try:
                        self.run(self.ib.reqMarketDataTypeAsync, 3)
                    except Exception:
                        self.ib.client.reqMarketDataType(3)

                    # Auto-detect account
                    self.account_id = os.getenv("IBKR_ACCOUNT")
                    if not self.account_id:
                        try:
                            managed = self.ib.managedAccounts()
                            if managed:
                                self.account_id = managed[0]
                                os.environ["IBKR_ACCOUNT"] = self.account_id
                                logger.info(f"🔐 [IBKR] Auto-selected account: {self.account_id}")
                        except Exception as e:
                            logger.warning(f"⚠️ [IBKR] Could not auto-detect account: {e}")

                    self._register_error_handler()
                    self._activate_firewall()
                    logger.info("✅ [IBKR] Connected successfully.")
                    return

                except (asyncio.TimeoutError, ConnectionRefusedError, Exception) as e:
                    wait = (attempt + 1) * 2
                    logger.warning(
                        f"⚠️ [IBKR] Attempt {attempt+1} failed: {e}. Retrying in {wait}s…"
                    )
                    self.connected = False
                    if attempt < max_retries - 1:
                        time.sleep(wait)
                    else:
                        logger.error("❌ [IBKR] Max retries reached.")
                        raise

    def _register_error_handler(self):
        if getattr(self, "_error_handler_registered", False):
            return

        def _on_error(reqId, errorCode, errorString, contract):
            if errorCode == 10147:
                logger.debug(f"ℹ️ [IBKR] Ignoring stale cancel reqId={reqId}: {errorString}")
                return
            if errorCode == 200 and contract:
                try:
                    from core.risk import mark_symbol_rejected
                    mark_symbol_rejected(contract.symbol)
                except Exception:
                    pass
                logger.warning(f"🚫 [IBKR] {contract.symbol} not tradable ({errorString}).")
                return
            if errorCode == 201 and contract:
                if "short" in str(errorString).lower():
                    self._short_sell_blocked[contract.symbol.upper()] = time.time()
                try:
                    from core.risk import mark_symbol_rejected
                    mark_symbol_rejected(contract.symbol)
                except Exception:
                    pass
            if errorCode in [10197, 366, 162] and contract:
                sym = contract.symbol.upper()
                logger.warning(f"⚠️ [IBKR-DATA] {sym} restricted (Error {errorCode}).")
                for tf in ["1 min", "5 mins", "1 day"]:
                    _DEAD_STREAMS[(sym, tf)] = datetime.now()

        self.ib.errorEvent += _on_error
        self._error_handler_registered = True

    def _activate_firewall(self):
        if getattr(self, "_firewall_active", False):
            return

        original_place = self.ib.placeOrder
        original_cancel = self.ib.cancelOrder

        def sentinel_cancel_order(order):
            if not hasattr(order, "orderId") or order.orderId <= 0:
                return None
            now = time.time()
            if now - self._recently_cancelled.get(order.orderId, 0) < 30:
                return None
            is_open = any(
                t.order.orderId == order.orderId
                and t.orderStatus.status not in ["Cancelled", "Inactive", "Filled"]
                for t in self.ib.openTrades()
            )
            self._recently_cancelled[order.orderId] = now
            return original_cancel(order) if is_open else None

        def sentinel_place_order(contract, order):
            symbol = contract.symbol.upper()
            account_id = os.getenv("IBKR_ACCOUNT") or self.account_id
            if account_id and not getattr(order, "account", None):
                order.account = account_id
            if order.action == "SELL":
                ignoring_id = getattr(order, "orderId", 0)
                with self.get_symbol_lock(symbol):
                    blocked_ts = self._short_sell_blocked.get(symbol, 0)
                    if blocked_ts and (time.time() - blocked_ts) < 3600:
                        try:
                            positions = self.run(self.ib.reqPositionsAsync)
                        except Exception:
                            positions = self.ib.positions()
                        pos = next(
                            (p for p in positions if p.contract.symbol == symbol), None
                        )
                        if not (pos and pos.position > 0):
                            logger.warning(
                                f"🛑 [FIREWALL] BLOCKING SELL for {symbol} (no long pos, broker short-blocked)."
                            )
                            return Trade(contract, order)
                    available = self.calculate_available_to_sell(
                        symbol, ignoring_order_id=ignoring_id
                    )
                    if available <= 0:
                        logger.critical(
                            f"🛑 [FIREWALL] BLOCKING SELL for {symbol}. Zero shares available."
                        )
                        return Trade(contract, order)
                    if order.totalQuantity > available:
                        logger.warning(
                            f"🛡️ [FIREWALL] CAPPING SELL for {symbol}: {order.totalQuantity} → {available}."
                        )
                        order.totalQuantity = available
                    self._in_flight_sells[symbol] = (order.totalQuantity, time.time())
            return original_place(contract, order)

        self.ib.placeOrder = sentinel_place_order
        self.ib.cancelOrder = sentinel_cancel_order
        self._firewall_active = True
        logger.info("🛡️ [IBKR] Anti-Short Firewall ACTIVATED.")

    def get_symbol_lock(self, symbol):
        if symbol not in self._symbol_locks:
            self._symbol_locks[symbol] = threading.Lock()
        return self._symbol_locks[symbol]

    def calculate_available_to_sell(self, symbol: str, ignoring_order_id: int = 0) -> int:
        try:
            self.run(self.ib.reqAllOpenOrdersAsync)
            try:
                pos_list = self.run(self.ib.reqPositionsAsync)
            except Exception:
                pos_list = self.ib.positions()
            pos = next((p for p in pos_list if p.contract.symbol == symbol), None)
            actual_pos = int(pos.position) if (pos and pos.position > 0) else 0

            now = time.time()
            firm_sells = 0
            protection_sells = 0
            for t in self.ib.openTrades():
                if t.contract.symbol == symbol and t.order.action == "SELL":
                    if t.order.orderId == ignoring_order_id:
                        continue
                    if t.order.orderId in [
                        oid for oid, ts in self._recently_cancelled.items() if now - ts < 30
                    ]:
                        continue
                    if t.orderStatus.status in ["Cancelled", "Inactive", "Filled"]:
                        continue
                    qty = t.order.totalQuantity
                    if t.order.orderType in ["STP", "STP LMT", "TRAIL", "TRAIL LIMIT"]:
                        protection_sells += qty
                    else:
                        firm_sells += qty

            in_flight_qty = 0
            if symbol in self._in_flight_sells:
                qty, ts = self._in_flight_sells[symbol]
                if now - ts < 5:
                    in_flight_qty = qty
                else:
                    del self._in_flight_sells[symbol]

            reserved = max(firm_sells, protection_sells) + in_flight_qty
            return max(0, int(actual_pos - reserved))
        except Exception as e:
            logger.error(f"⚠️ [ORACLE] Error calculating available for {symbol}: {e}")
            return 0

    async def _sentinel_task(self):
        """Background task that detects and immediately covers accidental shorts."""
        while True:
            try:
                if self.connected and self.ib.isConnected():
                    positions = await self.ib.reqPositionsAsync()
                    for pos in positions:
                        if pos.position < 0:
                            symbol = pos.contract.symbol
                            qty_to_buy = abs(pos.position)
                            logger.critical(
                                f"👽 [SENTINEL] SHORT detected in {symbol} ({pos.position}). COVERING."
                            )
                            await self.ib.reqAllOpenOrdersAsync()
                            for t in self.ib.openTrades():
                                if t.contract.symbol == symbol:
                                    self.ib.cancelOrder(t.order)
                            contract = Stock(symbol, "SMART", "USD")
                            await self.ib.qualifyContractsAsync(contract)
                            cover = MarketOrder("BUY", qty_to_buy)
                            cover.outsideRth = True
                            self.ib.placeOrder(contract, cover)
                            logger.critical(
                                f"✅ [SENTINEL] Cover placed for {symbol} ({qty_to_buy} shares)."
                            )
                await asyncio.sleep(10)
            except Exception as e:
                logger.debug(f"⚠️ [SENTINEL] Error: {e}")
                await asyncio.sleep(5)

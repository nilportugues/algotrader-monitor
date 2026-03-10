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

from ib_async import IB

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
                cls._instance.account_id = None
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
                logger.warning(f"🚫 [IBKR] {contract.symbol} not tradable ({errorString}).")
                return
            if errorCode in [10197, 366, 162] and contract:
                sym = contract.symbol.upper()
                logger.warning(f"⚠️ [IBKR-DATA] {sym} restricted (Error {errorCode}).")
                for tf in ["1 min", "5 mins", "1 day"]:
                    _DEAD_STREAMS[(sym, tf)] = datetime.now()

        self.ib.errorEvent += _on_error
        self._error_handler_registered = True

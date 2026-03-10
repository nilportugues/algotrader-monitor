"""
Risk management for algotrading-monitor.
Provides trailing stop management for open IBKR positions.
"""
import logging
import os
import json
import time
import threading

import pandas as pd
from ib_async import Stock, StopLimitOrder, MarketOrder, LimitOrder

logger = logging.getLogger(__name__)

# ── Thread Safety ──
_MGMT_LOCK = threading.Lock()
_SL_HIGH_WATERMARK = {}  # symbol -> peak price

# ── Rejected Symbols (PRIIPs/KID blocks) ──
REJECTED_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "rejected_symbols.json")


def _load_rejected_symbols():
    if os.path.exists(REJECTED_PATH):
        try:
            with open(REJECTED_PATH) as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()


def _save_rejected_symbols(rejected_set):
    os.makedirs(os.path.dirname(REJECTED_PATH), exist_ok=True)
    try:
        with open(REJECTED_PATH, "w") as f:
            json.dump(list(rejected_set), f)
    except Exception:
        pass


_REJECTED_SYMBOLS = _load_rejected_symbols()


def is_symbol_rejected(symbol: str) -> bool:
    return symbol.upper() in _REJECTED_SYMBOLS


def mark_symbol_rejected(symbol: str):
    sym = symbol.upper()
    if sym not in _REJECTED_SYMBOLS:
        logger.warning(f"🚫 [GUARD] {sym} marked as REJECTED (regulatory block). Skipping permanently.")
        _REJECTED_SYMBOLS.add(sym)
        _save_rejected_symbols(_REJECTED_SYMBOLS)


def manage_trailing_stop(symbol: str, entry_price: float, current_price: float, atr: float):
    """
    Implements dynamic trailing stops for a live position.

    Logic:
      1. Profit lock: protect (peak_profit_pct - 0.5%) above entry.
         e.g. +0.5% peak → break-even, +1.0% peak → lock +0.5%.
      2. ATR-based trail: peak - (atr × multiplier), session-aware.
      3. Final SL = max(ATR trail, profit floor). Only moves upward.
      4. Ghost-stop bailout: if price falls below existing SL, market-sell.
      5. 10%+ profit: sell half to lock gains.
    """
    def to_scalar(val):
        try:
            if isinstance(val, (pd.Series, pd.DataFrame)):
                return float(val.iloc[-1]) if not val.empty else 0.0
            v = float(val)
            return v if not pd.isna(v) else 0.0
        except Exception:
            return 0.0

    entry_price = to_scalar(entry_price)
    current_price = to_scalar(current_price)
    atr = to_scalar(atr)

    if current_price <= 0 or entry_price <= 0:
        logger.debug(
            f"⚠️ [RISK] Skipping {symbol} – invalid price "
            f"(cur={current_price}, entry={entry_price})"
        )
        return

    with _MGMT_LOCK:
        try:
            from brokers.ibkr import IBKRConnection
            from core.session import is_extended_hours
            from datetime import datetime

            conn = IBKRConnection()
            conn.run(conn.ib.reqAllOpenOrdersAsync)
            ib_trades = conn.ib.openTrades()

            # Bail early if no live long position
            live_positions = conn.ib.positions()
            live_pos = next(
                (p for p in live_positions if p.contract.symbol == symbol), None
            )
            if not live_pos or int(live_pos.position) <= 0:
                _SL_HIGH_WATERMARK.pop(symbol, None)
                return

            qty = int(live_pos.position)

            # ── Current fixed stop price (STP / STP LMT only) ──
            current_sl_val = 0.0
            for t in ib_trades:
                if (
                    t.contract.symbol == symbol
                    and t.order.action == "SELL"
                    and t.orderStatus.status not in ["Cancelled", "Inactive", "Filled"]
                    and t.order.orderType in ["STP", "STP LMT"]
                ):
                    found_sl = (
                        t.order.auxPrice
                        if 0 < t.order.auxPrice < 1_000_000
                        else t.order.lmtPrice
                    )
                    if 1_000_000 > found_sl > current_sl_val:
                        current_sl_val = found_sl

            # ── High-water mark ──
            peak_price = max(_SL_HIGH_WATERMARK.get(symbol, 0.0), current_price)
            _SL_HIGH_WATERMARK[symbol] = peak_price

            # ── Profit lock formula ──
            peak_profit_pct = max(0.0, (peak_price / entry_price - 1) * 100)
            protected_pct = max(0.0, peak_profit_pct - 0.5)
            profit_floor = round(entry_price * (1 + protected_pct / 100.0), 4)

            # ── Session-aware ATR distance ──
            atr_mult = 3.0 if is_extended_hours() else 2.0
            sl_distance_pct = 0.05 if is_extended_hours() else 0.02
            atr_trail = round(
                max(
                    peak_price * (1 - sl_distance_pct),
                    peak_price - (atr * atr_mult),
                ),
                2,
            )

            target_sl = round(max(atr_trail, profit_floor), 2)
            profit_pct = (current_price / entry_price - 1) * 100

            logger.info(
                f"🛡️  [RISK] {symbol} | entry=${entry_price:.2f} cur=${current_price:.2f} "
                f"peak=${peak_price:.2f} peak_profit={peak_profit_pct:.2f}% "
                f"protecting={protected_pct:.2f}% target_sl=${target_sl:.2f} "
                f"current_sl=${current_sl_val:.2f}"
            )

            # ── 10%+ half-sell ──
            if profit_pct >= 10.0:
                try:
                    import pytz

                    conn.run(conn.ib.reqExecutionsAsync)
                    current_fills = conn.ib.fills()
                    sld_qty = sum(
                        int(f.execution.shares)
                        for f in current_fills
                        if f.contract.symbol == symbol
                        and f.execution.side == "SLD"
                        and (
                            datetime.now(pytz.utc) - f.time.astimezone(pytz.utc)
                        ).total_seconds()
                        < 43200
                    )
                    bot_qty = sum(
                        int(f.execution.shares)
                        for f in current_fills
                        if f.contract.symbol == symbol
                        and f.execution.side == "BOT"
                        and (
                            datetime.now(pytz.utc) - f.time.astimezone(pytz.utc)
                        ).total_seconds()
                        < 43200
                    )
                    already_sold_half = sld_qty > 0 and sld_qty < bot_qty
                    if not already_sold_half and qty > 1:
                        sell_qty = qty // 2
                        hs_contract = Stock(symbol, "SMART", "USD")
                        conn.run(conn.ib.qualifyContractsAsync, hs_contract)
                        sell_price = round(current_price * 0.999, 4)
                        hs_order = LimitOrder("SELL", sell_qty, lmtPrice=sell_price)
                        hs_order.outsideRth = True
                        conn.ib.placeOrder(hs_contract, hs_order)
                        logger.warning(
                            f"🎯 [TAKE-PROFIT] {symbol} {profit_pct:.1f}% → selling half "
                            f"({sell_qty} shares) @ ${sell_price:.4f}"
                        )
                except Exception as hs_err:
                    logger.warning(f"⚠️ [RISK] Half-sell check failed for {symbol}: {hs_err}")

            # ── Ghost-stop emergency bailout ──
            if current_sl_val > 0.1 and current_price < (current_sl_val * 0.999):
                logger.error(
                    f"🚨 [RISK-EMERGENCY] {symbol} price=${current_price:.2f} is BELOW "
                    f"stop=${current_sl_val:.2f}. Bailing out."
                )
                for t in ib_trades:
                    if (
                        t.contract.symbol == symbol
                        and t.order.action == "SELL"
                        and t.orderStatus.status not in ["Cancelled", "Inactive", "Filled"]
                    ):
                        try:
                            conn.ib.cancelOrder(t.order)
                        except Exception:
                            pass
                positions_now = conn.ib.positions()
                pos_now = next(
                    (p for p in positions_now if p.contract.symbol == symbol and p.position > 0),
                    None,
                )
                if pos_now:
                    bc = Stock(symbol, "SMART", "USD")
                    conn.run(conn.ib.qualifyContractsAsync, bc)
                    bail_order = MarketOrder("SELL", int(abs(pos_now.position)))
                    bail_order.outsideRth = True
                    conn.ib.placeOrder(bc, bail_order)
                    logger.warning(
                        f"🔥 [RISK-BAILOUT] {symbol} market-sell {int(abs(pos_now.position))} shares."
                    )
                return

            # ── Ratchet gate: only move SL upwards ──
            if target_sl <= current_sl_val:
                logger.debug(
                    f"🛡️  [RISK] {symbol} target=${target_sl:.2f} ≤ current=${current_sl_val:.2f}. No change."
                )
                return

            # ── Anti-instant-trigger cap ──
            safe_sl = round(current_price * 0.995, 2)
            if target_sl >= current_price:
                logger.warning(
                    f"🚨 [GLITCH-GUARD] {symbol} SL ${target_sl:.2f} >= price ${current_price:.2f}. "
                    f"Capping to ${safe_sl:.2f}."
                )
                target_sl = safe_sl
                if target_sl <= current_sl_val:
                    return

            new_sl = target_sl

            # ── Cancel all existing stops → place fresh STP LMT ──
            cancelled_any = False
            for t in conn.ib.openTrades():
                if (
                    t.contract.symbol == symbol
                    and t.order.action == "SELL"
                    and t.orderStatus.status not in ["Cancelled", "Inactive", "Filled"]
                    and t.order.orderType in ["STP", "STP LMT", "TRAIL", "TRAIL LIMIT"]
                ):
                    try:
                        conn.ib.cancelOrder(t.order)
                        cancelled_any = True
                    except Exception as ce:
                        logger.warning(f"⚠️ [RISK] Could not cancel stop #{t.order.orderId}: {ce}")

            if cancelled_any:
                time.sleep(0.4)

            contract = Stock(symbol, "SMART", "USD")
            conn.run(conn.ib.qualifyContractsAsync, contract)

            new_order = StopLimitOrder("SELL", qty, lmtPrice=new_sl, stopPrice=new_sl)
            new_order.outsideRth = True
            new_order.tif = "GTC"
            ibkr_trade = conn.ib.placeOrder(contract, new_order)

            time.sleep(0.5)
            if ibkr_trade.orderStatus.status in ["Cancelled", "Inactive"]:
                logs_lower = [str(l.message).lower() for l in ibkr_trade.log]
                if any("microcap" in m or "short" in m for m in logs_lower):
                    logger.error(f"🚫 [RISK] {symbol} SL rejected (restricted). Blacklisting.")
                    mark_symbol_rejected(symbol)
                else:
                    logger.warning(
                        f"⚠️ [RISK] {symbol} SL rejected ({ibkr_trade.orderStatus.status})."
                    )
                return

            verb = "MOVED" if cancelled_any else "CREATED"
            logger.warning(
                f"✅ [RISK] {symbol} | SL {verb} → ${new_sl:.2f} | "
                f"peak profit: {peak_profit_pct:.2f}% | protecting: {protected_pct:.2f}%"
            )

        except Exception as e:
            import traceback
            logger.error(f"❌ [RISK] manage_trailing_stop failed for {symbol}: {e}")
            logger.error(traceback.format_exc())

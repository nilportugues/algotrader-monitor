"""
algotrading-monitor  —  Standalone IBKR portfolio dashboard.

Displays real-time account summary, open positions with P&L,
active stop-loss levels, and applies dynamic trailing stops.
Sends a Telegram summary every 60 seconds when configured.

Usage:
    python monitor.py

Environment variables (see .env.example):
    MONITOR_CLIENT_ID     IBKR client ID for this process (default: 3)
    IBKR_HOST             TWS / Gateway host (default: 127.0.0.1)
    IBKR_PORT             TWS / Gateway port (default: 4002)
    IBKR_ACCOUNT          IBKR account ID (auto-detected if omitted)
    TELEGRAM_NOTIFICATIONS_ENABLED  true/false
    TELEGRAM_BOT_TOKEN
    TELEGRAM_CHAT_ID
"""

import asyncio
import os
import sys
import time
from datetime import datetime, time as dtime

from dotenv import load_dotenv
import pandas as pd
import pytz

# Load .env and set this service's client ID BEFORE any IBKR code loads.
load_dotenv()
os.environ["IBKR_CLIENT_ID"] = os.getenv("MONITOR_CLIENT_ID", "3")

# Make sure local packages resolve correctly when run from any directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import warnings
warnings.filterwarnings("ignore", message=".*utcnow is deprecated.*")

from brokers.ibkr import IBKRConnection
from helpers.telegram import broadcast_monitor_update

import logging

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("ib_async").setLevel(logging.ERROR)


# ── Formatting helpers ──────────────────────────────────────────────────────

def fmt_currency(val, symbol="$"):
    if val is None or pd.isna(val):
        return f"{symbol}0.00"
    return f"{symbol}{val:,.2f}"


def fmt_pct(val):
    if val is None or pd.isna(val):
        return "0.00%"
    color = "\033[92m" if val >= 0 else "\033[91m"
    return f"{color}{val:+.2f}%\033[0m"


# ── Market status ────────────────────────────────────────────────────────────

_ET = pytz.timezone("America/New_York")

try:
    import exchange_calendars as xcals
    _NYSE_CAL = xcals.get_calendar("XNYS")
    _USE_XCALS = True
except Exception:
    _NYSE_CAL = None
    _USE_XCALS = False


def market_status() -> str:
    """Return a coloured market-status string using the live NYSE calendar.

    Session windows (all times Eastern):
        Pre-market  04:00 – 09:30
        Regular     09:30 – 16:00  (13:00 on early-close days)
        After-hours 16:00 – 20:00  (skipped on early-close days)
        Closed      outside the above, weekends, and market holidays
    """
    now_et = datetime.now(_ET)
    t = now_et.time()

    _PRE_OPEN = dtime(4, 0)
    _AH_CLOSE = dtime(20, 0)
    _MKT_OPEN_DEFAULT  = dtime(9, 30)
    _MKT_CLOSE_DEFAULT = dtime(16, 0)

    if _USE_XCALS:
        try:
            ts_today = pd.Timestamp(now_et.date())
            if not _NYSE_CAL.is_session(ts_today):
                return "\033[90m● CLOSED (weekend/holiday)\033[0m"

            open_ts  = _NYSE_CAL.session_open(ts_today).tz_convert(_ET)
            close_ts = _NYSE_CAL.session_close(ts_today).tz_convert(_ET)
            mkt_open_t  = open_ts.time()
            mkt_close_t = close_ts.time()

            # On early-close days (e.g. day after Thanksgiving) there is no
            # regular after-hours session; cap AH at the early close time.
            ah_close_t = _AH_CLOSE if mkt_close_t >= _MKT_CLOSE_DEFAULT else mkt_close_t
        except Exception:
            mkt_open_t  = _MKT_OPEN_DEFAULT
            mkt_close_t = _MKT_CLOSE_DEFAULT
            ah_close_t  = _AH_CLOSE
    else:
        # Fallback: basic weekday check only (no holiday detection)
        if now_et.weekday() >= 5:
            return "\033[90m● CLOSED (weekend)\033[0m"
        mkt_open_t  = _MKT_OPEN_DEFAULT
        mkt_close_t = _MKT_CLOSE_DEFAULT
        ah_close_t  = _AH_CLOSE

    if t < _PRE_OPEN or t >= ah_close_t:
        return "\033[90m● CLOSED\033[0m"
    if _PRE_OPEN <= t < mkt_open_t:
        return "\033[93m● PRE-MARKET\033[0m"
    if mkt_open_t <= t < mkt_close_t:
        return "\033[92m● MARKET OPEN\033[0m"
    return "\033[93m● AFTER-HOURS\033[0m"


# ── Main loop ───────────────────────────────────────────────────────────────

def monitor():
    conn = IBKRConnection()
    print("🔄 Connecting to IBKR…")
    conn.connect()

    conn.ib.reqMarketDataType(3)          # 3 = Delayed (no live-data subscription required)
    # Subscribe to account value streaming using the real account ID.
    # An empty string causes TWS to fire only an initial snapshot and then go silent.
    _acct = conn.account_id or ""
    conn.ib.client.reqAccountUpdates(True, _acct)
    # Subscribe to real-time position updates so the cache stays current.
    try:
        conn.run(conn.ib.reqPositionsAsync)
    except Exception:
        conn.ib.reqPositions()

    # Pump the event loop so initial account/position data arrives before first render.
    conn.run(asyncio.sleep, 2.0)

    # Tracks the stable row order so tickers don't jump around each cycle.
    symbol_order: dict[str, int] = {}
    # Symbols for which reqMktData has already been called (subscribe exactly once).
    subscribed_symbols: set[str] = set()
    # Refresh open orders every 1 s to keep stop-loss values up to date.
    _last_order_refresh = time.monotonic() - 10.0  # negative offset forces first-cycle refresh
    # Re-send reqAccountUpdates every 30 s to prevent the stream going silent.
    _last_acct_resub = time.monotonic()

    while True:
        try:
            now = time.monotonic()

            # Yield to the ib_async event loop so all queued incoming messages
            # (ticks, account values, position updates) are processed before we read them.
            conn.run(asyncio.sleep, 0.0)

            # ── Re-subscribe account updates (every 30 s) ──────────────────
            if now - _last_acct_resub >= 30.0:
                conn.ib.client.reqAccountUpdates(True, _acct)
                _last_acct_resub = now

            # ── Refresh open orders (every 1 s) ─────────────────────────────
            if now - _last_order_refresh >= 1.0:
                conn.run(conn.ib.reqAllOpenOrdersAsync)
                _last_order_refresh = now

            # ── 1. Account summary ──────────────────────────────────────────
            summary = conn.ib.accountValues()
            if not summary:
                conn.run(asyncio.sleep, 1.0)
                continue

            data: dict = {}
            for item in summary:
                acc, tag, val_str, curr = item.account, item.tag, str(item.value), item.currency
                if not val_str or any(
                    c.isalpha() for c in val_str.replace(".", "", 1).replace("-", "", 1)
                ):
                    continue
                try:
                    val = float(val_str)
                except ValueError:
                    continue
                data.setdefault(acc, {}).setdefault(tag, []).append((val, curr))

            target_acc = next((a for a in data if a != "All"), "All")
            acc_data = data.get(target_acc, {})
            all_acc_data = data.get("All", {})

            def get_val(tag, preferred, fallback):
                for src in [preferred, fallback]:
                    for t in [tag, tag + "ByCurrency"]:
                        matches = src.get(t, [])
                        for p_curr in ["USD", "EUR", "BASE", ""]:
                            for v, c in matches:
                                if c == p_curr:
                                    return v, c
                        if matches:
                            return matches[0]
                return 0.0, "USD"

            net_liq, net_liq_curr = get_val("NetLiquidation", acc_data, all_acc_data)
            unrealized_pnl, pnl_curr = get_val("UnrealizedPnL", acc_data, all_acc_data)
            realized_pnl, _ = get_val("RealizedPnL", acc_data, all_acc_data)
            buying_power, _ = get_val("BuyingPower", acc_data, all_acc_data)
            total_cash, _ = get_val("TotalCashValue", acc_data, all_acc_data)
            available_funds, _ = get_val("AvailableFunds", acc_data, all_acc_data)

            daily_pnl = unrealized_pnl + realized_pnl
            sym = "$" if net_liq_curr == "USD" else ("€" if net_liq_curr == "EUR" else net_liq_curr)

            # ── 2. Positions ────────────────────────────────────────────────
            positions = conn.ib.positions()
            pos_list = []

            if positions:
                # Subscribe market data for any symbol we haven't seen before (once only).
                new_contracts = [
                    p.contract for p in positions
                    if p.contract.symbol not in subscribed_symbols
                    and getattr(p.contract, "conId", 0) != 0
                ]
                for c in new_contracts:
                    try:
                        conn.ib.reqMktData(c, "", False, False)
                        subscribed_symbols.add(c.symbol)
                    except Exception:
                        pass

                ticker_map = {t.contract.symbol: t for t in conn.ib.tickers() if t.contract}

                for pos in positions:
                    symbol = pos.contract.symbol
                    qty = int(pos.position)
                    avg_cost = pos.avgCost
                    ticker = ticker_map.get(symbol)

                    curr_price = 0.0
                    if ticker:
                        curr_price = ticker.marketPrice()
                        if not curr_price or pd.isna(curr_price):
                            curr_price = (
                                ticker.last
                                or ticker.close
                                or (
                                    (ticker.bid + ticker.ask) / 2
                                    if (ticker.bid > 0 and ticker.ask > 0)
                                    else 0.0
                                )
                            )

                    if pd.isna(curr_price) or curr_price == 0.0:
                        curr_price = 0.0

                    if qty == 0:
                        continue

                    if avg_cost > 0 and curr_price > 0:
                        pnl_usd = (curr_price - avg_cost) * qty
                        pnl_pct = ((curr_price / avg_cost) - 1) * 100
                    else:
                        pnl_usd = pnl_pct = 0.0

                    pos_list.append(
                        {
                            "symbol": symbol,
                            "qty": qty,
                            "avg_price": avg_cost,
                            "price": curr_price,
                            "usd_pnl": pnl_usd,
                            "pct_pnl": pnl_pct,
                            "contract": pos.contract,
                        }
                    )

            # Assign a stable slot to every symbol the first time it appears.
            for p in pos_list:
                if p["symbol"] not in symbol_order:
                    symbol_order[p["symbol"]] = len(symbol_order)

            # Keep rows in a fixed position; new symbols are appended at the end.
            pos_list.sort(key=lambda x: symbol_order[x["symbol"]])

            # ── 3. Stop-loss map (always built from live open orders) ────────
            sl_map: dict = {}
            for t in conn.ib.openTrades():
                if t.order.orderType in ["STP", "STP LMT", "TRAIL", "TRAIL LIMIT"]:
                    symbol = t.contract.symbol
                    price = 0.0
                    if t.order.orderType in ["TRAIL", "TRAIL LIMIT"]:
                        ts_price = getattr(t.orderStatus, "trailStopPrice", 0)
                        if 0 < ts_price < 1_000_000:
                            price = ts_price
                    if price <= 0:
                        p = (
                            t.order.auxPrice
                            if 0 < t.order.auxPrice < 1_000_000
                            else t.order.lmtPrice
                        )
                        if 0 < p < 1_000_000:
                            price = p
                    if price > 0:
                        if symbol not in sl_map or price > sl_map[symbol]:
                            sl_map[symbol] = price

            # ── 4. Render dashboard ─────────────────────────────────────────
            os.system("clear" if os.name == "posix" else "cls")

            status_str = market_status()
            print(f"  {status_str}\n")

            color_daily = "\033[92m" if daily_pnl >= 0 else "\033[91m"
            color_unreal = "\033[92m" if unrealized_pnl >= 0 else "\033[91m"
            color_real = "\033[92m" if realized_pnl >= 0 else "\033[91m"
            reset = "\033[0m"

            net_liq_f = f"{net_liq/1000:.1f}K" if net_liq >= 1000 else f"{net_liq:.2f}"

            print(
                f"{'Account':<15} {'Daily P&L':<15} {'Unrealized P&L':<15} "
                f"{'Realized P&L':<15} {'Net Liquidity':<15}"
            )
            print(
                f"\033[1m{target_acc:<15}\033[0m "
                f"{color_daily}{daily_pnl:>10.2f}{reset}      "
                f"{color_unreal}{unrealized_pnl:>12.2f}{reset}      "
                f"{color_real}{realized_pnl:>12.2f}{reset}      "
                f"\033[1m{net_liq_f:>13}\033[0m"
            )
            print("-" * 80)

            print(
                f"{'Ticker':<12} {'Shares':>10} {'Position USD':>15} {'Entry':>10} "
                f"{'Current':>10} {'P&L':>12} {'P&L %':>10} {'Stop':>10} {'SL Dist%':>10} "
                f"{'Locked $':>12} {'Locked %':>10}"
            )
            print("-" * 140)

            if not pos_list:
                print("No open positions.")
            else:
                for p in pos_list:
                    market_val = p["qty"] * p["price"]
                    color = "\033[92m" if p["usd_pnl"] >= 0 else "\033[91m"

                    p_usd_str = (
                        f"{p['usd_pnl']:,.2f}" if abs(p["usd_pnl"]) >= 1 else f"{p['usd_pnl']:.2f}"
                    )
                    p_usd_str = ("+" if p["usd_pnl"] > 0 else "") + p_usd_str
                    market_val_str = (
                        f"{market_val:,.2f}" if market_val >= 1 else f"{market_val:.2f}"
                    )

                    stop_price = sl_map.get(p["symbol"], 0.0)
                    stop_str = f"${stop_price:.2f}" if stop_price > 0 else "NONE"

                    if stop_price > 0 and p["price"] > 0:
                        sl_dist = ((stop_price - p["price"]) / p["price"]) * 100
                        sl_color = "\033[93m" if abs(sl_dist) < 1.5 else reset
                        sl_dist_str = f"{sl_color}{sl_dist:+.2f}%{reset}"
                    else:
                        sl_dist_str = "  ---"

                    if stop_price > 0 and p["avg_price"] > 0:
                        locked_usd = (stop_price - p["avg_price"]) * p["qty"]
                        locked_pct = ((stop_price - p["avg_price"]) / p["avg_price"]) * 100
                        locked_color = "\033[92m" if locked_usd >= 0 else "\033[91m"
                        locked_usd_s = ("+" if locked_usd > 0 else "") + (
                            f"{locked_usd:,.2f}" if abs(locked_usd) >= 1 else f"{locked_usd:.2f}"
                        )
                        locked_str = f"{locked_color}{locked_usd_s}{reset} {locked_color}{locked_pct:>+9.2f}%{reset}"
                    else:
                        locked_str = f"{'---':>6} {'---':>6}"

                    curr_price_str = (
                        f"{p['price']:>10.2f}" if p["price"] > 0 else f"{'ERR':>10}"
                    )

                    print(
                        f"\033[1m{p['symbol']:<12}\033[0m {p['qty']:>10} {market_val_str:>15} "
                        f"{p['avg_price']:>10.2f} {curr_price_str}{reset}"
                        f"{color}{p_usd_str:>12}{reset} {color}{p['pct_pnl']:>12.2f}%{reset} "
                        f"{stop_str:>12}{reset} {sl_dist_str:>12} {locked_str:>12}"
                    )

            # ── Footer ──────────────────────────────────────────────────────
            print("\n" + "=" * 80)
            print(
                f"BP: {fmt_currency(buying_power, sym)} | "
                f"Funds: {fmt_currency(available_funds, sym)} | "
                f"Cash: {fmt_currency(total_cash, sym)}"
            )

            winners = [p for p in pos_list if p["usd_pnl"] > 0]
            losers = [p for p in pos_list if p["usd_pnl"] <= 0]
            print(f"\n\033[92mWinners: {len(winners)}\033[0m | \033[91mLosers: {len(losers)}\033[0m")

            # Realized today from fills (read directly from the in-memory cache).
            fills = conn.ib.fills()
            today_realized_map: dict = {}
            for f in fills:
                if f.time.date() == datetime.now().date():
                    sym_fill = f.contract.symbol
                    pnl = getattr(f.execution, "realizedPnl", 0.0)
                    if pnl != 0:
                        today_realized_map[sym_fill] = (
                            today_realized_map.get(sym_fill, 0.0) + pnl
                        )

            if today_realized_map:
                print(f"\n{'Realized Today':<20} {'P&L':>15}")
                print("-" * 36)
                for s, pnl in sorted(today_realized_map.items(), key=lambda x: x[1], reverse=True):
                    c = "\033[92m" if pnl >= 0 else "\033[91m"
                    print(f"{s:<20} {c}{pnl:>15.2f}{reset}")

            print(f"\nRefreshed: {datetime.now().strftime('%H:%M:%S')} (1 s cycle)")

            # ── Telegram broadcast (every 60 s) ─────────────────────────────
            broadcast_monitor_update(
                target_acc=target_acc,
                net_liq=net_liq,
                daily_pnl=daily_pnl,
                unrealized_pnl=unrealized_pnl,
                realized_pnl=realized_pnl,
                pos_list=pos_list,
                sl_map=sl_map,
            )

            # Sleep via the event loop so incoming IBKR messages keep flowing.
            conn.run(asyncio.sleep, 1.0)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            conn.run(asyncio.sleep, 1.0)


if __name__ == "__main__":
    monitor()

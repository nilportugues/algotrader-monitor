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

import os
import sys
import time
from datetime import datetime

from dotenv import load_dotenv
import pandas as pd

# Load .env and set this service's client ID BEFORE any IBKR code loads.
load_dotenv()
os.environ["IBKR_CLIENT_ID"] = os.getenv("MONITOR_CLIENT_ID", "3")

# Make sure local packages resolve correctly when run from any directory.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import warnings
warnings.filterwarnings("ignore", message=".*utcnow is deprecated.*")
warnings.filterwarnings("ignore", module="yfinance")
warnings.filterwarnings("ignore", message=".*Ticker.info is deprecated.*")

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


# ── Main loop ───────────────────────────────────────────────────────────────

def monitor():
    conn = IBKRConnection()
    print("🔄 Connecting to IBKR…")
    conn.connect()

    conn.ib.reqMarketDataType(3)       # 3 = Delayed (works without live data subscriptions)
    conn.ib.client.reqAccountUpdates(True, "")
    time.sleep(1)

    while True:
        try:
            # Refresh all open orders from all clients every cycle.
            conn.run(conn.ib.reqAllOpenOrdersAsync)

            # ── 1. Account summary ──────────────────────────────────────────
            summary = conn.ib.accountValues()
            if not summary:
                time.sleep(1)
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
                subscribed_conids = {
                    t.contract.conId for t in conn.ib.tickers() if t.contract
                }
                for p in positions:
                    c = p.contract
                    if getattr(c, "conId", 0) not in subscribed_conids and getattr(c, "conId", 0) != 0:
                        try:
                            conn.ib.reqMktData(c, "", False, False)
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
                        try:
                            import yfinance as yf
                            info = getattr(yf.Ticker(symbol), "fast_info", None)
                            if info and getattr(info, "last_price", 0) > 0:
                                curr_price = info.last_price
                        except Exception:
                            pass

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

            # Sort: biggest losers first
            pos_list.sort(key=lambda x: x["usd_pnl"])

            # ── 4. Render dashboard ─────────────────────────────────────────
            os.system("clear" if os.name == "posix" else "cls")

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
                # Build stop-loss map from live open orders
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
                        locked_str = f"{locked_color}{locked_usd_s:>12}{reset} {locked_color}{locked_pct:>+9.2f}%{reset}"
                    else:
                        locked_str = f"{'---':>12} {'---':>10}"

                    curr_price_str = (
                        f"{p['price']:>10.2f}" if p["price"] > 0 else f"{'ERR':>10}"
                    )

                    print(
                        f"\033[1m{p['symbol']:<12}\033[0m {p['qty']:>10} {market_val_str:>15} "
                        f"{p['avg_price']:>10.2f} {curr_price_str} "
                        f"{color}{p_usd_str:>12}{reset} {color}{p['pct_pnl']:>9.1f}%{reset} "
                        f"{stop_str:>10} {sl_dist_str:>10} {locked_str}"
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

            # Realized today from fills
            fills = conn.run(conn.ib.fills)
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
                sl_map=sl_map if "sl_map" in locals() else {},
            )

            time.sleep(1)

        except KeyboardInterrupt:
            break
        except Exception as e:
            print(f"❌ Error: {e}")
            time.sleep(1)


if __name__ == "__main__":
    monitor()

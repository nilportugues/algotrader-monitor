import os
import requests
import time
from datetime import datetime

_last_telegram_time = 0


def broadcast_monitor_update(target_acc, net_liq, daily_pnl, unrealized_pnl, realized_pnl, pos_list, sl_map):
    global _last_telegram_time

    if time.time() - _last_telegram_time < 60:
        return

    msg = f"IBKR Monitor - {datetime.now().strftime('%H:%M:%S')}\n\n"
    msg += f"Account: {target_acc}\n"
    msg += f"Net Liq: ${net_liq:,.2f}\n"
    msg += f"Daily PnL: ${daily_pnl:,.2f}\n"
    msg += f"Unrealized PnL: ${unrealized_pnl:,.2f}\n"
    msg += f"Realized PnL: ${realized_pnl:,.2f}\n\n"
    msg += "Positions:\n"
    if not pos_list:
        msg += "None active"
    else:
        for p in pos_list:
            icon = "🟢" if p["usd_pnl"] >= 0 else "🔴"
            msg += (
                f"{icon} {p['symbol']}: {p['qty']} @ ${p['avg_price']:.2f} "
                f"→ ${p['price']:.2f} ({p['usd_pnl']:+,.2f} / {p['pct_pnl']:+.2f}%)\n"
            )

    if send_telegram(msg) is not False:
        _last_telegram_time = time.time()


def send_telegram(msg: str):
    if os.getenv("TELEGRAM_NOTIFICATIONS_ENABLED", "false").lower() != "true":
        return False

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        for chunk in [msg[i : i + 4000] for i in range(0, len(msg), 4000)]:
            requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk},
                timeout=10,
            )
    except Exception:
        pass

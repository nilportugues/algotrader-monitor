import os
import requests
import time
from datetime import datetime

_last_telegram_time = 0


def broadcast_monitor_update(target_acc, net_liq, daily_pnl, unrealized_pnl, realized_pnl, pos_list, sl_map):
    global _last_telegram_time

    if time.time() - _last_telegram_time < 60:
        return

    msg = f"<b>IBKR Monitor</b> - {datetime.now().strftime('%H:%M:%S')}\n\n"

    if not pos_list:
        msg += "No positions."
    else:
        msg += f"<b>Account:</b> {target_acc}\n"
        msg += f"<b>Net Liq:</b> ${net_liq:,.2f}\n"
        msg += f"<b>Daily PnL:</b> ${daily_pnl:,.2f}\n"
        msg += f"<b>Unrealized PnL:</b> ${unrealized_pnl:,.2f}\n"
        msg += f"<b>Realized PnL:</b> ${realized_pnl:,.2f}\n\n"
        msg += "<b>Positions:</b>\n"
        for p in pos_list:
            icon = "🟢" if p["usd_pnl"] >= 0 else "🔴"
            sl_val = sl_map.get(p["symbol"], 0)
            sl_str = f" | SL: ${sl_val:.2f}" if sl_val > 0 else ""
            msg += (
                f"{icon} <b>{p['symbol']}</b>: {p['qty']} @ ${p['avg_price']:.2f} "
                f"➡️ ${p['price']:.2f} ({p['usd_pnl']:+,.2f} / {p['pct_pnl']:+.2f}%){sl_str}\n"
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
                json={"chat_id": chat_id, "text": chunk, "parse_mode": "HTML"},
                timeout=10,
            )
    except Exception:
        pass

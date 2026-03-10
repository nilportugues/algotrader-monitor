# algotrading-monitor

Standalone real-time IBKR portfolio dashboard with dynamic trailing stops.

## Features

- Live account summary (net liquidation, daily P&L, unrealized/realized P&L)
- Open positions table with current price, cost basis, P&L, and stop-loss distance
- Dynamic trailing-stop management (ratcheting SL, 10%+ half-sell)
- Anti-short firewall + accidental-short sentinel
- Optional Telegram broadcast every 60 seconds
- 1-second refresh cycle via delayed or live market data

## Requirements

- Interactive Brokers TWS or IB Gateway running locally
- Python 3.11+

## Setup

```bash
git clone <repo>
cd algotrading-monitor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in your values
python monitor.py
```

## Configuration

All settings are in `.env` (see `.env.example`):

| Variable | Default | Description |
|---|---|---|
| `IBKR_HOST` | `127.0.0.1` | TWS / Gateway host |
| `IBKR_PORT` | `4002` | TWS / Gateway port |
| `IBKR_ACCOUNT` | *(auto)* | IBKR account ID |
| `MONITOR_CLIENT_ID` | `3` | Client ID for this process (must differ from trading bot) |
| `TELEGRAM_NOTIFICATIONS_ENABLED` | `false` | Enable Telegram alerts |
| `TELEGRAM_BOT_TOKEN` | | Telegram bot token |
| `TELEGRAM_CHAT_ID` | | Telegram chat ID |

## Project layout

```
monitor.py          # Entry point — run this
brokers/ibkr.py     # IBKRConnection singleton (no framework deps)
core/indicators.py  # ATR and other technical indicators
core/risk.py        # Trailing-stop / position protection logic
core/session.py     # Market session helpers (RTH / pre-market detection)
helpers/telegram.py # Telegram notification helper
```

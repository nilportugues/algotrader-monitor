# Telegram Notifications Setup

The `monitor.py` dashboard has a built-in feature to periodically broadcast your account metrics and open positions directly to a Telegram chat. This allows you to monitor your strategy's performance remotely on your phone.

## How to Configure

### 1. Create a Telegram Bot
1. Open the Telegram app and search for **BotFather** (it has a verified checkmark).
2. Start a chat and send the command `/newbot`.
3. Follow the instructions to choose a name and a username for your bot.
4. Once created, BotFather will give you an **HTTP API Token**. 
   *Example:* `123456789:ABCdefGHIjklmNOPQrsTUVwxyZ`
   **Keep this token secure.**

### 2. Get Your Chat ID
You need to tell the bot where to send messages (your personal chat or a specific group).
The `TELEGRAM_CHAT_ID` must be a numerical ID (e.g., `123456789` or `-100123456789`), NOT a username.

**The fastest way to get your chat ID:**
1. Send a quick test message (e.g., "Hello") to your newly created bot or the group where you added it.
2. We've included a helper comment in the `.env` file. Simply paste your bot token into the URL `https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates`, visit it in your browser, and look for `"chat": {"id": <NUMBERS>}`.

### 3. Update Your `.env` File
Now that you have your Bot Token and Chat ID, open the `.env` file at the root of the project and update the following variables:

```env
TELEGRAM_NOTIFICATIONS_ENABLED="true"
TELEGRAM_BOT_TOKEN="your_bot_token_here"
TELEGRAM_CHAT_ID="your_chat_id_here"
```

### 4. Run the Monitor
Once configured, simply run the monitor script:

```bash
python monitor.py
```

If the credentials are valid, the bot will automatically send a broadcast message to your Telegram chat every 60 seconds with your latest portfolio metrics and position updates!

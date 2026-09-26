# Telegram alerts

When price trades into an active order block, Wednesday sends a Telegram
message:

```
XAUUSD entered 1H bullish OB (extreme)
Buy limit 2,471.97 · SL 2,470.16 · risk 1.81
Zone 2,470.16 – 2,471.97 · price 2,471.50
1H structure: bullish CHoCH at 2,452.86
```

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and put the token in
   `.env` as `TELEGRAM_BOT_TOKEN`.
2. Send the bot any message, then run `make telegram-chats`
   (or `uv run wednesday --telegram-chats`) and put the printed id in `.env`
   as `TELEGRAM_CHAT_ID`.
3. Restart, then `make telegram-test` or **Settings > Telegram alerts > Send test message**.

The token and chat id stay in `.env`; they are never written to the database.

## Choosing what alerts

In **Settings** you pick which timeframes and which order blocks (extreme, mid)
alert, pause alerts, and see the recent alert log. By default every timeframe
and both priorities alert; if 5M mid OBs are too chatty, turn them off there.

## Bias labels

Each alert ends with **RISK ON** (with the bias you set on the chart page) or
**RISK OFF** (against it), and extreme order blocks say whether they sit at a
lower high or higher low. With a **neutral** bias you're not trading, so alerts
pause; tick *Alert with a neutral bias too* in Settings to keep them. See
[bias.md](bias.md).

## How entry is detected

- The check uses the **high/low of each new M1 bar**, so a wick into the zone
  counts even if the minute closes outside it.
- **One alert per order block.** The log is kept in the database, so restarts
  don't repeat alerts.
- Bars printed **before the order block was confirmed** (before its break
  candle closed) never trigger it.
- If sending fails (no internet, Telegram down), the attempt is logged as
  failed and retried on the next bar that is still in the zone.
- An order block that price enters and wipes out in the **same minute** is
  already taken by the time the scan runs, so it does not alert.

Alerts run in both dashboard (`--serve`) and console mode.

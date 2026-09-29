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

1. Create a bot with [@BotFather](https://t.me/BotFather) and paste its token in
   **Settings > Telegram alerts > Bot token**. Save.
2. Send the bot any message, press **Find my chat** and pick your chat (or paste a
   chat id). Save.
3. **Send test message**.

The token is kept in the system credential store (Windows Credential Manager),
never in the database; the chat id is saved with the alert settings.
`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env` still work as a fallback,
and `make telegram-chats` / `make telegram-test` do the same from a terminal.

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

## Position size

With **Settings > Risk** on, each alert has a size line, e.g.
`Size: 0.33 lot · risk $99.00 · 2R $198.00`. See [risk.md](risk.md).

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

## Alerts on your drawings

A horizontal line, trendline or rectangle you drew can alert too. Select it and
press the bell in the bar above the chart, or right-click it:

| Drawing | Conditions |
| --- | --- |
| Horizontal line, trendline | Crossing, crossing up, crossing down |
| Rectangle | Entering, leaving |

- **Fires**: *once, then off* (the bell on the chart turns into a struck-through
  outline until you **Re-arm** it), or *every time*, at most once per bar.
- **Name**: shown in the message, e.g. *Asia high*. **Expires**: in 1 hour, 4
  hours, a day or a week, or never.
- **How it's checked**: after every scan, on the closed M1 bars since the last
  check and only bars that closed after you set it. A line is crossed up when a
  bar's high reaches it and the close before was below it (down the other way
  round). A trendline's price is taken at the bar's time, on the line through its
  two points extended to the right. A rectangle is entered when the close before
  was outside its prices and the bar's range reaches into them, and left when the
  close before was inside and the bar closes outside; bars before its left edge
  don't count.
- **Also on the live price (intrabar)**: checks each live tick too (it needs a
  live price interval in Settings), so it can fire before the bar closes. The
  message says so.
- The alert reads the drawing each time, so **moving the drawing moves the
  alert**. Deleting the drawing stops it (Undo brings both back). Alerts live in
  the database (`drawing_alerts`) and survive restarts; every firing is logged
  under its own key first, so a bar never fires twice.

```
XAUUSD crossed above 2,476.30, your line 'Asia high'
Price 2,476.90 · On the closed M1 bar of 2026-09-24 08:32 (chart time)
The alert is now off; re-arm it on the chart.
```

Without Telegram set up (or with alerts paused) a drawing alert still fires on
the chart and is listed under **Recent alerts** as *not sent*. Messages say what
price did; what to do about it is your call.

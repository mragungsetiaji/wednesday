# Dashboard

```bash
make serve          # or: uv run xau-screener --serve
```

Opens on http://127.0.0.1:8000. It refreshes every few seconds; the server
rescans once a minute. API docs live at `/api/docs`.

![Dashboard, dark theme](images/dashboard-dark.png)

## Chart first

The chart shows the selected timeframe's candles with everything the detectors
found on it:

- **Order blocks** as boxes; mid OBs are lighter than extreme OBs.
- **Liquidity** (BSL / SSL, EQH / EQL) and **inducement** as lines.
- **Higher-timeframe levels**, faded, so you see where a 5M setup sits inside the 1H picture.
- The **latest break of structure** (BOS / CHoCH) as a dashed segment.
- **Recent sweeps** as markers on the candle that swept.

The timeframe tabs carry each timeframe's structure direction (↑ / ↓).
**Mid OBs** and **Higher timeframes** can be switched off above the chart, and
each detector can be hidden from the top bar.

## Level ladder

Beside the chart, the **Levels** ladder lists what matters around price in the
same vertical order as the price axis:

- sell limit setups `S1`–`S3` (extreme first, then nearest) and the nearest
  liquidity and IDM above price,
- the live price,
- buy limit setups `B1`–`B3` and the levels below.

Every row is pinned on the chart under the same tag: setups as an entry line
with a dashed stop, levels as lines. Hovering a row highlights it on the chart
and dims the rest. A level outside the visible range docks to the chart edge
with its price. Clicking a row opens its timeframe.

![Hovering S3 highlights it on the chart](images/ladder-highlight.png)

## Below the chart

- **Structure**: direction, last break (BOS / CHoCH, with the streak), its level and age, per timeframe.
- **Events**: levels swept or taken in the last few candles of each timeframe.
- **All timeframes**: the nearest level above and below per detector and timeframe.

## Light theme and mobile

The dashboard follows the system theme and works down to phone width.

<p>
  <img src="images/dashboard-light.png" alt="Dashboard, light theme" width="72%">
  <img src="images/mobile.png" alt="Dashboard on a phone" width="24%">
</p>

## Access

The dashboard has no login, so it binds to `127.0.0.1` by default. To reach it
on a VPS, use an SSH tunnel or RDP rather than opening the port; see
[deploy-windows.md](deploy-windows.md).

## UI development

```bash
make dev    # API on :8000 + Vite with hot reload on http://localhost:5173
```

The UI is React 19 + TypeScript + Vite with
[lightweight-charts](https://github.com/tradingview/lightweight-charts) in `web/`.
`make ui` rebuilds `web/dist`, which the Python server serves.

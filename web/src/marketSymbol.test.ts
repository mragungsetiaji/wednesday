// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { foldTick, paneSymbol, TICKERS, tickerName } from "./marketSymbol.ts";

test("a pane symbol is a TradingView name, or null for the scanned one", () => {
  assert.equal(paneSymbol("fx:xagusd", "FX:XAUUSD"), "FX:XAGUSD");
  assert.equal(paneSymbol("  OANDA:XAUUSD ", "FX:XAUUSD"), "OANDA:XAUUSD");
  assert.equal(paneSymbol("", "FX:XAUUSD"), null);
  assert.equal(paneSymbol("fx:xauusd", "FX:XAUUSD"), null); // the scanned market itself
  assert.equal(paneSymbol("XAGUSD", "XAUUSD"), undefined); // no exchange prefix
  assert.equal(paneSymbol("FX:XAG USD", "XAUUSD"), undefined);
});

test("ticks build the forming M1 candle", () => {
  const t0 = 1_794_606_120; // a minute start
  let bar = foldTick(null, 48.4, t0 + 5);
  assert.deepEqual(bar, { time: t0, open: 48.4, high: 48.4, low: 48.4, close: 48.4 });
  bar = foldTick(bar, 48.5, t0 + 20);
  bar = foldTick(bar, 48.3, t0 + 40);
  assert.deepEqual(bar, { time: t0, open: 48.4, high: 48.5, low: 48.3, close: 48.3 });
  bar = foldTick(bar, 48.35, t0 + 61); // the next minute opens on its first tick
  assert.deepEqual(bar, { time: t0 + 60, open: 48.35, high: 48.35, low: 48.35, close: 48.35 });
});

test("the chart's market menu until the server's list loads: gold from two venues, then the rest", () => {
  assert.deepEqual(TICKERS.map((t) => t.short), ["XAU", "XAU", "XAG", "DXY", "NQ", "S&P", "DJI"]);
  assert.deepEqual(TICKERS.slice(0, 2).map((t) => t.venue), ["FXCM", "OANDA"]);
  assert.equal(new Set(TICKERS.map((t) => t.symbol)).size, TICKERS.length);
  assert.ok(TICKERS.every((t) => paneSymbol(t.symbol, "") === t.symbol), "each is a valid TradingView symbol");
  assert.equal(tickerName("FX:XAUUSD"), "XAUUSD");
  assert.equal(tickerName("OANDA:NAS100USD"), "NAS100USD");
  assert.equal(tickerName("XAUUSD"), "XAUUSD");
});

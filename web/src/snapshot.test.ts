// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { ATTRIBUTION, fileName, footerText, nyStamp } from "./snapshot.ts";

// 13:31 UTC on 24 September 2026: 09:31 in New York (EDT).
const at = Date.UTC(2026, 8, 24, 13, 31);

test("the footer names the chart, both clocks and TradingView", () => {
  const { left, right } = footerText({ symbol: "XAUUSD", timeframes: ["15M"], at, clockOffset: 10 * 3600, clockName: "NY+7" });
  assert.equal(left, "XAUUSD  15M  ·  2026-09-24 09:31 New York  ·  2026-09-24 23:31 NY+7");
  assert.equal(right, ATTRIBUTION);
  assert.match(right, /TradingView/);
  const layout = footerText({ symbol: "XAUUSD", timeframes: ["1H", "4H"], at, clockOffset: 0, clockName: "Europe/London" });
  assert.match(layout.left, /1H · 4H/);
  assert.match(layout.left, /13:31 feed Europe\/London$/);
});

test("New York time follows DST", () => {
  assert.equal(nyStamp(Date.UTC(2026, 0, 15, 14, 30)), "2026-01-15 09:30"); // EST
  assert.equal(nyStamp(at), "2026-09-24 09:31"); // EDT
});

test("file names", () => {
  assert.equal(fileName({ symbol: "XAUUSD", timeframes: ["15M"], at, clockOffset: 0, clockName: "UTC" }), "wednesday-XAUUSD-15M-2026-09-24-0931.png");
  assert.equal(fileName({ symbol: "XAU/USD", timeframes: ["1H", "5M"], at, clockOffset: 0, clockName: "UTC" }), "wednesday-XAUUSD-layout-2026-09-24-0931.png");
});

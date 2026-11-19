// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import type { Candle } from "./api";
import { heikin, heikinBar } from "./heikin.ts";

const c = (time: number, open: number, high: number, low: number, close: number): Candle => ({ time, open, high, low, close });

test("the first Heikin Ashi bar opens halfway through its candle's body", () => {
  const [ha] = heikin([c(0, 10, 14, 8, 12)]);
  assert.equal(ha.open, 11);
  assert.equal(ha.close, 11); // (10 + 14 + 8 + 12) / 4
  assert.equal(ha.high, 14);
  assert.equal(ha.low, 8);
});

test("each later bar opens at the middle of the bar before it, and its range covers its body", () => {
  const bars = heikin([c(0, 10, 14, 8, 12), c(60, 12, 13, 11, 12.5)]);
  assert.equal(bars[1].open, 11); // (11 + 11) / 2
  assert.equal(bars[1].close, 12.125);
  assert.equal(bars[1].low, 11);
  assert.equal(bars[1].high, 13);
  assert.equal(bars[1].time, 60);
});

test("the forming candle builds on the last closed Heikin Ashi bar", () => {
  const bars = heikin([c(0, 10, 14, 8, 12)]);
  const live = heikinBar(c(60, 12, 20, 12, 19), bars[0]);
  assert.equal(live.open, 11);
  assert.equal(live.low, 11); // the open sits below the candle's low
  assert.equal(live.high, 20);
});

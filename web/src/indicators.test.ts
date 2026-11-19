// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { bollinger, cleanLength, ema, rsi, sma } from "./indicators.ts";

test("SMA averages the last n closes and has no value before it has n", () => {
  assert.deepEqual(sma([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
});

test("EMA starts at the SMA of its first n closes, then weights each new close by 2/(n+1)", () => {
  const out = ema([2, 4, 6, 8], 3);
  assert.deepEqual(out.slice(0, 3), [null, null, 4]);
  assert.equal(out[3], 8 * 0.5 + 4 * 0.5);
});

test("Bollinger Bands sit two population standard deviations from the SMA", () => {
  const b = bollinger([2, 4, 4, 4, 5, 5, 7, 9], 8);
  assert.equal(b.mid[7], 5);
  assert.equal(b.upper[7], 9); // sd 2
  assert.equal(b.lower[7], 1);
  assert.equal(b.upper[6], null);
});

test("RSI is 100 on a run of gains, 0 on a run of losses, 50 when nothing moves", () => {
  assert.equal(rsi([1, 2, 3, 4, 5], 3)[4], 100);
  assert.equal(rsi([5, 4, 3, 2, 1], 3)[4], 0);
  assert.equal(rsi([3, 3, 3, 3], 2)[3], 50);
  assert.deepEqual(rsi([1, 2, 3], 3), [null, null, null]);
});

test("RSI smooths the average gain and loss the way Wilder did", () => {
  // Changes +1, -1, +2: first average of 2 changes is gain 0.5, loss 0.5; then gain (0.5*1+2)/2 = 1.25, loss 0.25.
  const out = rsi([10, 11, 10, 12], 2);
  assert.equal(out[2], 50);
  assert.equal(out[3], 100 - 100 / (1 + 1.25 / 0.25));
});

test("a typed length stays between 2 and 500", () => {
  assert.equal(cleanLength(1, 20), 2);
  assert.equal(cleanLength(9999, 20), 500);
  assert.equal(cleanLength(Number.NaN, 20), 20);
  assert.equal(cleanLength(14.4, 20), 14);
});

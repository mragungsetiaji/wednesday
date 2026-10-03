// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import type { Candle } from "./api";
import { planUpdate } from "./perf.ts";

const bar = (time: number, close = 1): Candle => ({ time, open: 1, high: 2, low: 0.5, close });
const base = [bar(60), bar(120), bar(180), bar(240)];

test("identical candles need nothing", () => {
  assert.deepEqual(planUpdate(base, base.map((c) => ({ ...c }))), { kind: "same" });
});

test("a changed forming candle or new ones at the end are updates", () => {
  assert.deepEqual(planUpdate(base, [...base.slice(0, 3), bar(240, 1.5)]), { kind: "update", from: 3 });
  assert.deepEqual(planUpdate(base, [...base, bar(300)]), { kind: "update", from: 4 });
  assert.deepEqual(planUpdate(base, [...base.slice(0, 3), bar(240, 1.2), bar(300)]), { kind: "update", from: 3 });
});

test("anything further back, older pages or a shorter list is a full set", () => {
  assert.deepEqual(planUpdate(base, [bar(60), bar(120, 9), bar(180), bar(240)]), { kind: "set" });
  assert.deepEqual(planUpdate(base, [bar(0), ...base]), { kind: "set" });
  assert.deepEqual(planUpdate(base, base.slice(0, 3)), { kind: "set" });
  assert.deepEqual(planUpdate([], base), { kind: "set" });
});

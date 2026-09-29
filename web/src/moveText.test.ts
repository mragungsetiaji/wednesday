// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import type { Distribution } from "./api";
import { fmtMove, percentileSentence } from "./moveText.ts";

const base: Distribution = {
  period: "day", measure: "change", unit: "%", lookback: "5y", samples: 1254, min_samples: 30,
  current: { day: "2026-09-28", label: "Mon", value: -3.4, forming: true, start_unix: 0 },
  stats: { mean: 0.03, median: 0.04, std: 0.9, min: -5, max: 4 },
  percentile: { below: 0.003, above: 0.996, tail: 0.004, side: "low" }, z: -3.8,
  histogram: { edges: [], counts: [] }, like_this: [], filters: { weekday: null, session: null }, coverage: null,
};

test("the percentile sentence", () => {
  assert.equal(percentileSentence(base), "−3.40% so far: lower than 99.6% of 1,254 days, about 1 in 250.");
  assert.equal(percentileSentence({ ...base, current: { ...base.current!, forming: false } }),
    "−3.40%: lower than 99.6% of 1,254 days, about 1 in 250.");
  assert.equal(percentileSentence({ ...base, samples: 212, filters: { weekday: "Wed", session: null } }),
    "−3.40% so far: lower than 99.6% of 212 Wednesdays, about 1 in 250.");
  assert.equal(percentileSentence({ ...base, period: "session", samples: 88, filters: { weekday: null, session: "NY AM" },
    current: { ...base.current!, value: 1.2 }, percentile: { below: 0.8, above: 0.19, tail: 0.2, side: "high" } }),
    "+1.20% so far: higher than 80.0% of 88 NY AM sessions, about 1 in 5.");
  assert.equal(percentileSentence({ ...base, percentile: { ...base.percentile!, tail: 0, above: 1 } }),
    "−3.40% so far: lower than 100.0% of 1,254 days, beyond every one of them.");
  assert.equal(percentileSentence({ ...base, percentile: { below: 0.4, above: 0.55, tail: 0.45, side: "low" } }),
    "−3.40% so far: lower than 55.0% of 1,254 days.");
  assert.equal(percentileSentence({ ...base, percentile: null }), null);
  assert.equal(fmtMove(1.3, "range_atr"), "1.30 × ATR");
  assert.equal(fmtMove(1.3, "range_pct"), "1.30%");
  assert.equal(fmtMove(-0.001, "change"), "0.00%"); // rounds to zero: no sign
});

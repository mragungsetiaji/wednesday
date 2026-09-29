// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { logicalOf, logicalOfTime, stepOf, timeOfLogical, timeShift } from "./timeMap.ts";

const MIN = 60;
const HOUR = 3600;
const utc = (s: string) => Date.parse(`${s}Z`) / 1000;

/**
 * Gold's M1 opens from Thursday to Tuesday (feed clock = UTC here): the daily break is
 * 21:00-22:00 and the weekend runs from Friday 21:00 to Sunday 22:00.
 */
function m1(from: string, to: string): number[] {
  const out: number[] = [];
  for (let t = utc(from); t < utc(to); t += MIN) {
    const d = new Date(t * 1000);
    const day = d.getUTCDay();
    const h = d.getUTCHours();
    const weekend = (day === 5 && h >= 21) || day === 6 || (day === 0 && h < 22);
    if (h === 21 || weekend) continue;
    out.push(t);
  }
  return out;
}

const resample = (times: number[], seconds: number) => [...new Set(times.map((t) => t - (t % seconds)))];

const M1 = m1("2026-09-24T00:00", "2026-09-29T12:00");
const TF = { "1M": MIN, "5M": 5 * MIN, "15M": 15 * MIN, "1H": HOUR, "4H": 4 * HOUR };
const CHARTS = Object.entries(TF).map(([name, s]) => ({ name, s, times: resample(M1, s) }));

test("the step is the timeframe's length, not the last gap", () => {
  for (const { name, s, times } of CHARTS) assert.equal(stepOf(times), s, name);
  // Closed candles ending right after the weekend: the last gap is two days.
  const h1 = resample(m1("2026-09-25T12:00", "2026-09-27T23:00"), HOUR);
  assert.equal(h1[h1.length - 1] - h1[h1.length - 2], 50 * HOUR);
  assert.equal(stepOf(h1), HOUR);
  const n = h1.length;
  assert.equal(logicalOfTime(h1, h1[n - 1] + 3 * HOUR), n - 1 + 3); // three hours past the last open
});

test("every timeframe puts a time at the same moment", () => {
  // Points on the 1M grid, inside the history and past the last candle, around the break and the weekend.
  const last = M1[M1.length - 1];
  const points = [
    ...M1.filter((t) => t % (7 * MIN) === 0),
    utc("2026-09-25T20:55"), utc("2026-09-27T22:00"), utc("2026-09-28T22:00"),
    last + MIN, last + 45 * MIN, last + 5 * HOUR,
  ];
  for (const { name, s, times } of CHARTS) {
    for (const t of points) {
      const l = logicalOf(times, t);
      const i = Math.min(Math.max(Math.floor(l + 0.5), 0), times.length - 1);
      const open = times[i];
      // Inside the candle that holds it, in proportion to the timeframe's length.
      assert.ok(Math.abs(l - (i - 0.5) - (t - open) / s) < 1e-9, `${name} ${new Date(t * 1000).toISOString()}`);
      assert.equal(timeOfLogical(times, logicalOfTime(times, t)), t, `${name} round trip`);
    }
  }
  // A 5M box from 10:00 to 11:00 is 12 candles on 5M, 4 on 15M, 1 on 1H, 60 on 1M.
  const [a, b] = [utc("2026-09-28T10:00"), utc("2026-09-28T11:00")];
  const width = (name: string) => {
    const { times } = CHARTS.find((c) => c.name === name)!;
    return logicalOfTime(times, b) - logicalOfTime(times, a);
  };
  assert.deepEqual(["1M", "5M", "15M", "1H", "4H"].map(width), [60, 12, 4, 1, 0.25]);
});

test("a time in a gap sits on the boundary, the same on every timeframe", () => {
  const breakAt = utc("2026-09-28T21:30"); // inside the daily break
  for (const { name, times } of CHARTS) {
    if (name === "4H") continue; // 20:00-24:00 holds the break: no boundary there
    const before = times.filter((t) => t < breakAt).length - 1;
    assert.equal(logicalOf(times, breakAt), before + 0.5, name);
  }
});

test("a box past the live candle keeps its duration after a candle closes", () => {
  const m5 = CHARTS.find((c) => c.name === "5M")!.times;
  const start = m5[m5.length - 3];
  const end = start + 20 * 5 * MIN;
  const before = logicalOfTime(m5, end) - logicalOfTime(m5, start);
  const later = [...m5, m5[m5.length - 1] + 5 * MIN];
  assert.equal(logicalOfTime(later, end) - logicalOfTime(later, start), before);
  assert.equal(before, 20);
});

test("dragging moves every point by the same time", () => {
  const h1 = CHARTS.find((c) => c.name === "1H")!.times;
  const fri = logicalOfTime(h1, utc("2026-09-25T19:00"));
  const sun = logicalOfTime(h1, utc("2026-09-27T22:00"));
  const dt = timeShift(h1, fri + 0.2, sun - 0.1); // the pointer is never exactly on a candle
  assert.equal(dt, utc("2026-09-27T22:00") - utc("2026-09-25T19:00"));
  // A 5M-exact box keeps its length and its 5M alignment after the drag on 1H.
  const box = [utc("2026-09-25T19:05"), utc("2026-09-25T20:35")].map((t) => t + dt);
  assert.deepEqual(box, [utc("2026-09-27T22:05"), utc("2026-09-27T23:35")]);
  assert.equal(timeShift(h1, sun, sun), 0);
});

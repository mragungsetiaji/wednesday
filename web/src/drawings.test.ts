// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import type { Candle, Drawing } from "./api";
import { trackPosition } from "./drawings.ts";

const utc = (s: string) => Date.parse(`${s}Z`) / 1000;

/** Flat candles at 100 from `from`, one every `step` seconds, with one spike to `low` at `at`. */
function candles(from: string, n: number, step: number, at: number, low: number): Candle[] {
  return Array.from({ length: n }, (_, i) => {
    const time = utc(from) + i * step;
    const hit = at >= time && at < time + step;
    return { time, open: 100, high: 100.5, low: hit ? low : 99.5, close: 100, volume: 1 } as Candle;
  });
}

const long = (t0: string, t1: string): Drawing =>
  ({ id: "p", kind: "long", points: [{ t: utc(t0), p: 99 }, { t: utc(t1), p: 99 }], style: {}, props: { stop: 98, target: 102 } }) as Drawing;

test("a position tracks the same start and end times on every timeframe", () => {
  const spike = utc("2026-09-28T10:40"); // the only candle to reach the entry
  const box = long("2026-09-28T10:00", "2026-09-28T11:00");
  for (const step of [60, 300, 900, 3600]) {
    const track = trackPosition(box, candles("2026-09-28T08:00", 8 * 3600 / step, step, spike, 98.9), null);
    assert.equal(track?.state, "ended", `${step}`);
    assert.equal(track?.fillT, spike - (spike % step), `${step}: fills on the candle holding 10:40`);
  }
  // A box that ended before the spike missed it on 1M and 5M; a 1H candle holds both.
  const early = long("2026-09-28T10:00", "2026-09-28T10:30");
  assert.equal(trackPosition(early, candles("2026-09-28T08:00", 96, 300, spike, 98.9), null)?.state, "missed");
  assert.equal(trackPosition(early, candles("2026-09-28T08:00", 8, 3600, spike, 98.9), null)?.state, "ended");
});

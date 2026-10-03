/**
 * Chart performance helpers (#34): how to apply new candles with the least work, and counters
 * for the debug overlay (Alt+Shift+P, or ?perf in the address).
 */
import type { Candle } from "./api";

export type CandlePlan = { kind: "same" } | { kind: "update"; from: number } | { kind: "set" };

const sameBar = (a: Candle, b: Candle) =>
  a.time === b.time && a.open === b.open && a.high === b.high && a.low === b.low && a.close === b.close;

/**
 * How to go from the candles on the chart to `next`: nothing, `update` the bars from index `from` on
 * (the forming candle changed, or new ones were added at the end), or a full `set` (older candles
 * paged in, a different timeframe, or history that changed further back).
 */
export function planUpdate(prev: Candle[], next: Candle[]): CandlePlan {
  if (!prev.length || !next.length || next.length < prev.length || prev[0].time !== next[0].time) return { kind: "set" };
  let i = 0;
  while (i < prev.length && sameBar(prev[i], next[i])) i++;
  if (i === prev.length && next.length === prev.length) return { kind: "same" };
  // Only the last two bars may differ (the one that just closed and the one forming); older change = full set.
  if (i < prev.length - 2) return { kind: "set" };
  return { kind: "update", from: i };
}

/** Counters the overlay reads; cheap enough to keep on all the time. */
export const counters = { setData: 0, update: 0, ticks: 0, skipped: 0 };

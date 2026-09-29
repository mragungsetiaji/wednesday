/**
 * Times to chart positions and back, the same way on every timeframe.
 *
 * Drawings, quarter blocks and news lines are anchored to times (unix seconds, feed clock),
 * so each chart places them on its own candles: a 5M point at 10:05 sits a twelfth of the
 * way into the 1H candle that opened at 10:00. Candle i spans logical i - 0.5 to i + 0.5.
 *
 * A candle covers its timeframe's length from its open, never more: a time in a gap (gold's
 * daily break, a weekend, a missing bar) sits on the boundary between the candles around it.
 * Before the first and after the last candle, time runs at the timeframe's length per candle
 * (not the last gap, which may be a weekend).
 */

/** Seconds per candle: the smallest gap among the last few (a break or weekend is never the smallest). */
export function stepOf(times: number[]): number {
  let p = Infinity;
  for (let i = Math.max(1, times.length - 20); i < times.length; i++) p = Math.min(p, times[i] - times[i - 1]);
  return Number.isFinite(p) && p > 0 ? p : 60;
}

/** Index of the candle holding t: the last one opening at or before it (0 before the first). */
function candleIndex(times: number[], t: number): number {
  let lo = 0;
  let hi = times.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (times[mid] <= t) lo = mid;
    else hi = mid - 1;
  }
  return lo;
}

/** Where time t sits on this chart, in (fractional) candles: candle i opens at i - 0.5. */
export function logicalOf(times: number[], t: number, step = stepOf(times)): number {
  const n = times.length;
  if (n === 0) return 0;
  if (t <= times[0]) return -0.5 - (times[0] - t) / step;
  const i = candleIndex(times, t);
  const into = (t - times[i]) / step;
  return i - 0.5 + (i === n - 1 ? into : Math.min(1, into)); // past the last candle: extend at the step
}

/** logicalOf moved half a candle, so a candle's open lands on its centre (where the chart draws it). */
export const logicalOfTime = (times: number[], t: number, step = stepOf(times)): number => logicalOf(times, t, step) + 0.5;

/** The time at a logical position: the inverse of logicalOfTime. */
export function timeOfLogical(times: number[], l: number, step = stepOf(times)): number {
  const n = times.length;
  if (n === 0) return 0;
  if (l < 0) return Math.round(times[0] + l * step);
  const i = Math.min(Math.floor(l), n - 1);
  const into = i === n - 1 ? l - i : Math.min(1, l - i);
  return Math.round(times[i] + into * step);
}

/**
 * The time shift of a drag from logical `from` to `to`, in whole candles of this chart:
 * the time of the candle under the pointer now minus the one it started on. Moving every
 * point by it keeps a drawing's duration, even when the drag crosses a gap.
 */
export function timeShift(times: number[], from: number, to: number, step = stepOf(times)): number {
  return timeOfLogical(times, Math.round(to), step) - timeOfLogical(times, Math.round(from), step);
}

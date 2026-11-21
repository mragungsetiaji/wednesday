/**
 * Chart indicators: moving averages, Bollinger Bands and RSI on the closes of closed candles.
 * Each value at a bar uses only that bar and the ones before it, so history shows what the
 * indicator said at the time. A bar without enough history before it has no value (null).
 */

export type IndicatorKind = "ema" | "sma" | "bb" | "rsi";

export interface Indicator {
  id: string;
  kind: IndicatorKind;
  length: number;
  color: string;
  hidden?: boolean; // on the chart's list but not drawn
  width?: number; // line width in px, 1 to 4 (default: 2, RSI 1)
  mult?: number; // Bollinger Bands: standard deviations either side (default 2)
}

export const LINE_WIDTHS = [1, 2, 3, 4];
export const lineWidth = (i: Indicator) => i.width ?? (i.kind === "rsi" ? 1 : 2);

/** A multiplier a person typed, kept within 0.5 and 5. */
export function cleanMult(v: number, fallback: number): number {
  if (!Number.isFinite(v)) return fallback;
  return Math.min(5, Math.max(0.5, Math.round(v * 10) / 10));
}

export const INDICATOR_KINDS: { kind: IndicatorKind; name: string; short: string; length: number; hint: string }[] = [
  { kind: "ema", name: "Exponential moving average", short: "EMA", length: 20, hint: "On the candles, weighting recent closes more" },
  { kind: "sma", name: "Simple moving average", short: "SMA", length: 50, hint: "On the candles, the plain average of the closes" },
  { kind: "bb", name: "Bollinger Bands", short: "BB", length: 20, hint: "SMA with bands two standard deviations away" },
  { kind: "rsi", name: "Relative strength index", short: "RSI", length: 14, hint: "In a pane under the chart, with 70 and 30 lines" },
];

/** TradeCanvas's series colours, one after another for each indicator added. */
export const INDICATOR_COLORS = ["#4cbedf", "#e8a144", "#ca8cff", "#f387c1", "#a5c45d"];

export const MAX_LENGTH = 500;

export const indicatorLabel = (i: Indicator) => `${INDICATOR_KINDS.find((k) => k.kind === i.kind)?.short ?? i.kind} ${i.length}`;

export type Series = (number | null)[];

export function sma(values: number[], n: number): Series {
  const out: Series = new Array(values.length).fill(null);
  let sum = 0;
  for (let i = 0; i < values.length; i++) {
    sum += values[i];
    if (i >= n) sum -= values[i - n];
    if (i >= n - 1) out[i] = sum / n;
  }
  return out;
}

/** EMA seeded with the SMA of its first n values. */
export function ema(values: number[], n: number): Series {
  const out: Series = new Array(values.length).fill(null);
  if (values.length < n) return out;
  const k = 2 / (n + 1);
  let prev = values.slice(0, n).reduce((a, b) => a + b, 0) / n;
  out[n - 1] = prev;
  for (let i = n; i < values.length; i++) {
    prev = values[i] * k + prev * (1 - k);
    out[i] = prev;
  }
  return out;
}

/** Bollinger Bands: the SMA and the bands `mult` population standard deviations either side. */
export function bollinger(values: number[], n: number, mult = 2): { mid: Series; upper: Series; lower: Series } {
  const mid = sma(values, n);
  const upper: Series = new Array(values.length).fill(null);
  const lower: Series = new Array(values.length).fill(null);
  for (let i = n - 1; i < values.length; i++) {
    const m = mid[i]!;
    let v = 0;
    for (let j = i - n + 1; j <= i; j++) v += (values[j] - m) ** 2;
    const sd = Math.sqrt(v / n);
    upper[i] = m + mult * sd;
    lower[i] = m - mult * sd;
  }
  return { mid, upper, lower };
}

/** Wilder's RSI: its first value at bar n, from the average gain and loss of the first n changes. */
export function rsi(values: number[], n: number): Series {
  const out: Series = new Array(values.length).fill(null);
  if (values.length <= n) return out;
  let gain = 0;
  let loss = 0;
  for (let i = 1; i <= n; i++) {
    const d = values[i] - values[i - 1];
    if (d > 0) gain += d;
    else loss -= d;
  }
  gain /= n;
  loss /= n;
  const value = () => (loss === 0 ? (gain === 0 ? 50 : 100) : 100 - 100 / (1 + gain / loss));
  out[n] = value();
  for (let i = n + 1; i < values.length; i++) {
    const d = values[i] - values[i - 1];
    gain = (gain * (n - 1) + Math.max(d, 0)) / n;
    loss = (loss * (n - 1) + Math.max(-d, 0)) / n;
    out[i] = value();
  }
  return out;
}

/** The lines an indicator draws, each with its own name for the legend. */
export function linesOf(ind: Indicator, closes: number[]): { name: string; values: Series }[] {
  switch (ind.kind) {
    case "ema":
      return [{ name: "", values: ema(closes, ind.length) }];
    case "sma":
      return [{ name: "", values: sma(closes, ind.length) }];
    case "rsi":
      return [{ name: "", values: rsi(closes, ind.length) }];
    case "bb": {
      const b = bollinger(closes, ind.length, ind.mult ?? 2);
      return [{ name: "upper", values: b.upper }, { name: "basis", values: b.mid }, { name: "lower", values: b.lower }];
    }
  }
}

/** A length a person typed, kept within 2 and MAX_LENGTH. */
export function cleanLength(v: number, fallback: number): number {
  if (!Number.isFinite(v)) return fallback;
  return Math.min(MAX_LENGTH, Math.max(2, Math.round(v)));
}

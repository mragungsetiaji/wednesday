import type { Candle } from "./api";

/** One Heikin Ashi bar from a candle and the Heikin Ashi bar before it (none for the first). */
export function heikinBar(c: Candle, prev: Candle | null): Candle {
  const close = (c.open + c.high + c.low + c.close) / 4;
  const open = prev ? (prev.open + prev.close) / 2 : (c.open + c.close) / 2;
  return { ...c, open, close, high: Math.max(c.high, open, close), low: Math.min(c.low, open, close) };
}

/** The Heikin Ashi bars of a run of candles. */
export function heikin(candles: Candle[]): Candle[] {
  const out: Candle[] = [];
  for (const c of candles) out.push(heikinBar(c, out.length ? out[out.length - 1] : null));
  return out;
}

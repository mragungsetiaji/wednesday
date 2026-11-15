/**
 * A full-screen pane's own symbol: TradingView names with an exchange prefix (FX:XAGUSD).
 * Empty, or the source's own symbol, means the pane follows the scanned market.
 */

/** TradingView symbols the symbol box suggests. `FX:` is FXCM. */
export const SUGGESTED_SYMBOLS = ["FX:XAUUSD", "FX:XAGUSD", "OANDA:XAUUSD", "OANDA:XAGUSD", "TVC:DXY"];

const SYMBOL = /^[A-Z0-9_.]{1,32}:[A-Z0-9_.!&-]{1,32}$/;

/**
 * What the trader typed, as a pane symbol: null to follow the scanned `main` symbol, the
 * upper-cased TradingView symbol, or undefined when it isn't one (the box says so).
 */
export function paneSymbol(typed: string, main: string): string | null | undefined {
  const s = typed.trim().toUpperCase();
  if (!s || s === main.trim().toUpperCase()) return null;
  return SYMBOL.test(s) ? s : undefined;
}

export interface Bar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

/** The forming M1 candle after a tick at `time` (unix seconds): the same minute grows, a new one starts. */
export function foldTick(bar: Bar | null, price: number, time: number): Bar {
  const minute = Math.floor(time / 60) * 60;
  if (bar && bar.time === minute) {
    return { ...bar, high: Math.max(bar.high, price), low: Math.min(bar.low, price), close: price };
  }
  return { time: minute, open: price, high: price, low: price, close: price };
}

/**
 * A full-screen pane's own symbol: TradingView names with an exchange prefix (FX:XAGUSD).
 * Empty, or the source's own symbol, means the pane follows the scanned market.
 */

export interface Ticker {
  symbol: string; // TradingView's name, with its exchange prefix
  short: string; // what the menu shows
  name: string;
  venue?: string | null; // who quotes it (FXCM, OANDA): the same short can be listed twice
}

/**
 * The markets the chart's symbol menu offers until the server's list (`/api/markets`, Settings >
 * Markets) has loaded, and when it can't be had; picking one moves the whole engine (scan,
 * levels, alerts) to it. `FX:` is FXCM spot; the indices are OANDA's CFDs, which trade nearly
 * round the clock like gold. The same as the server's first list (`DEFAULT_MARKETS`).
 */
export const TICKERS: Ticker[] = [
  { symbol: "FX:XAUUSD", short: "XAU", name: "Gold spot", venue: "FXCM" },
  { symbol: "OANDA:XAUUSD", short: "XAU", name: "Gold spot", venue: "OANDA" },
  { symbol: "FX:XAGUSD", short: "XAG", name: "Silver spot", venue: "FXCM" },
  { symbol: "TVC:DXY", short: "DXY", name: "US Dollar Index", venue: "TVC" },
  { symbol: "OANDA:NAS100USD", short: "NQ", name: "Nasdaq 100", venue: "OANDA" },
  { symbol: "OANDA:SPX500USD", short: "S&P", name: "S&P 500", venue: "OANDA" },
  { symbol: "OANDA:US30USD", short: "DJI", name: "Dow Jones 30", venue: "OANDA" },
];

/** A market of the server's list as the menus show it. */
export function tickerOf(m: { symbol: string; label: string; name: string | null; venue: string | null }): Ticker {
  return { symbol: m.symbol, short: m.label, name: m.name ?? tickerName(m.symbol), venue: m.venue };
}

/** TradingView symbols the full-screen pane's symbol box suggests. */
export const SUGGESTED_SYMBOLS = TICKERS.map((t) => t.symbol);

/** A symbol without its exchange prefix, as the chart's top bar shows it: FX:XAUUSD -> XAUUSD. */
export function tickerName(symbol: string): string {
  const s = symbol.trim();
  return s.slice(s.indexOf(":") + 1) || s;
}

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

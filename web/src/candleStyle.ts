/**
 * How candles are painted from the chart settings: body, borders and wick, each with an up and
 * a down colour (opacity included), and optionally coloured by the previous close.
 */
import type { ChartSettings } from "./chartSettings.ts";

export const CLEAR = "rgba(0, 0, 0, 0)";

/** A colour as "#rrggbb" plus its opacity (0 to 1), from hex (3, 6 or 8 digits) or rgb()/rgba(). */
export function parseColor(c: string): { hex: string; alpha: number } {
  const s = c.trim().toLowerCase();
  const h = s.match(/^#([0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/);
  if (h) {
    const d = h[1].length === 3 ? [...h[1]].map((x) => x + x).join("") : h[1];
    return { hex: `#${d.slice(0, 6)}`, alpha: d.length === 8 ? Math.round((parseInt(d.slice(6), 16) / 255) * 100) / 100 : 1 };
  }
  const r = s.match(/^rgba?\(\s*([\d.]+)[\s,]+([\d.]+)[\s,]+([\d.]+)(?:[\s,/]+([\d.]+%?))?\s*\)$/);
  if (r) {
    const hex = "#" + [r[1], r[2], r[3]].map((v) => Math.min(255, Math.round(Number(v))).toString(16).padStart(2, "0")).join("");
    const a = r[4] === undefined ? 1 : r[4].endsWith("%") ? Number(r[4].slice(0, -1)) / 100 : Number(r[4]);
    return { hex, alpha: Math.min(1, Math.max(0, a)) };
  }
  return { hex: "#000000", alpha: 1 };
}

/** The colour with this opacity: hex when opaque, else rgba(). */
export function withAlpha(color: string, alpha: number): string {
  const { hex } = parseColor(color);
  const a = Math.min(1, Math.max(0, Math.round(alpha * 100) / 100));
  if (a >= 1) return hex;
  const [r, g, b] = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
  return `rgba(${r}, ${g}, ${b}, ${a})`;
}

/** The six colours and three switches, with the theme filling what the settings leave null. */
export interface CandleLook {
  body: boolean;
  borders: boolean;
  wick: boolean;
  hollow: boolean; // the Hollow chart type: rising candles drawn by their border only
  prevClose: boolean;
  bodyUp: string;
  bodyDown: string;
  borderUp: string;
  borderDown: string;
  wickUp: string;
  wickDown: string;
}

export function candleLook(s: ChartSettings, theme: { bull: string; bear: string }, hollow: boolean): CandleLook {
  const bodyUp = s.bull ?? theme.bull;
  const bodyDown = s.bear ?? theme.bear;
  return {
    body: s.body, borders: s.borders || hollow, wick: s.wick, hollow, prevClose: s.prevClose,
    bodyUp, bodyDown,
    borderUp: s.borderUp ?? bodyUp, borderDown: s.borderDown ?? bodyDown,
    wickUp: s.wickUp ?? bodyUp, wickDown: s.wickDown ?? bodyDown,
  };
}

/** The candlestick series' options for the look. */
export function seriesOptions(l: CandleLook) {
  return {
    upColor: l.body && !l.hollow ? l.bodyUp : CLEAR,
    downColor: l.body ? l.bodyDown : CLEAR,
    borderVisible: l.borders,
    borderUpColor: l.borderUp,
    borderDownColor: l.borderDown,
    wickVisible: l.wick,
    wickUpColor: l.wickUp,
    wickDownColor: l.wickDown,
  };
}

/**
 * One candle's own colours when they follow the previous close (null: the series' up and down
 * by its open, as usual). A hollow candle stays hollow when it rises from its open.
 */
export function barColors(l: CandleLook, bar: { open: number; close: number }, prevClose: number | null):
  { color: string; borderColor: string; wickColor: string } | null {
  if (!l.prevClose || prevClose === null) return null;
  const up = bar.close >= prevClose;
  const clear = !l.body || (l.hollow && bar.close > bar.open);
  return {
    color: clear ? CLEAR : up ? l.bodyUp : l.bodyDown,
    borderColor: up ? l.borderUp : l.borderDown,
    wickColor: up ? l.wickUp : l.wickDown,
  };
}

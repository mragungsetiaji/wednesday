import type { IChartApi, ISeriesApi, Logical, SeriesType } from "lightweight-charts";

import type { Candle, Drawing, DrawingKind, DrawingPoint, Sizing } from "./api";
import { logicalOf } from "./quartersPrimitive";

export type DrawTool = "cursor" | DrawingKind;

/** What a chart needs to show and edit the drawings; one shared by every chart on screen. */
export interface DrawingCtl {
  items: Drawing[];
  hidden: boolean; // all drawings hidden
  tool: DrawTool;
  setTool: (t: DrawTool) => void;
  selected: string | null;
  select: (id: string | null) => void;
  create: (d: Drawing) => void;
  change: (d: Drawing, commit: boolean) => void; // commit: save it (a drag ended)
  remove: (id: string) => void;
  sizing: Sizing | null; // lot size and money at risk on positions; null without Settings > Risk
  editText: (id: string) => void; // select a text drawing and put the cursor in its text field
}

export const newId = (): string =>
  typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : // Plain http on a LAN has no randomUUID (it needs a secure context).
      "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
        const r = (Math.random() * 16) | 0;
        return (c === "x" ? r : (r & 0x3) | 0x8).toString(16);
      });

const stepOf = (times: number[]) => (times.length > 1 ? times[times.length - 1] - times[times.length - 2] : 60);

/**
 * Logical position of a time on this chart: candle i opens at i (drawn at its centre) and a
 * time inside it lands proportionally further on, so a 15M point sits inside its 1H candle.
 * Before the first and after the last candle it extends at the candle step.
 */
export const logicalOfTime = (times: number[], t: number): number => logicalOf(times, t) + 0.5;

/** The time at a logical position: the inverse of logicalOfTime. */
export function timeOfLogical(times: number[], l: number): number {
  const n = times.length;
  if (n === 0) return 0;
  const step = stepOf(times);
  const i = Math.floor(l);
  if (i >= n - 1) return Math.round(times[n - 1] + (l - (n - 1)) * step);
  if (i < 0) return Math.round(times[0] + l * step);
  const span = Math.min(times[i + 1] - times[i], step * 1.5);
  return Math.round(times[i] + (l - i) * span);
}

/** Chart pixels (CSS px, relative to the price pane) of a point; null when the price is off scale. */
export function toXY(chart: IChartApi, series: ISeriesApi<SeriesType>, times: number[], pt: DrawingPoint) {
  const x = chart.timeScale().logicalToCoordinate(logicalOfTime(times, pt.t) as Logical);
  const y = series.priceToCoordinate(pt.p);
  return x === null || y === null ? null : { x, y };
}

/** The point under the pointer, its time snapped to the candle there. */
export function toPoint(chart: IChartApi, series: ISeriesApi<SeriesType>, times: number[], x: number, y: number): DrawingPoint | null {
  const l = chart.timeScale().coordinateToLogical(x);
  const p = series.coordinateToPrice(y);
  if (l === null || p === null) return null;
  return { t: timeOfLogical(times, Math.round(l)), p };
}

type XY = { x: number; y: number };
type YOf = (price: number) => number | null;

export const isPosition = (d: Drawing) => d.kind === "long" || d.kind === "short";

/** A position's prices: entry (its points' price), stop and target. */
export function positionOf(d: Drawing) {
  const entry = d.points[0].p;
  return { entry, stop: d.props?.stop ?? entry, target: d.props?.target ?? entry };
}

/** A position's box in pixels: x from start to end, y at entry, stop and target. */
export function positionBox(d: Drawing, xy: XY[], yOf: YOf) {
  const { stop, target } = positionOf(d);
  const ys = yOf(stop);
  const yt = yOf(target);
  if (ys === null || yt === null) return null;
  return { x0: Math.min(xy[0].x, xy[1].x), x1: Math.max(xy[0].x, xy[1].x), xStart: xy[0].x, xEnd: xy[1].x, ye: xy[0].y, ys, yt };
}

/**
 * Where a position stands on this chart's candles:
 * - waiting: no candle has reached the entry yet;
 * - missed: the box ended before one did;
 * - open: filled, neither stop nor target hit yet (price: the latest close);
 * - target / stop: the first one hit after the fill (price: that level);
 * - ended: filled, still open when the box ended (price: the close there).
 */
export type PositionState = "waiting" | "missed" | "open" | "target" | "stop" | "ended";
export interface PositionTrack {
  state: PositionState;
  price: number;
  fillT: number | null; // candle time the entry filled on
  t: number; // candle time the position stands on (the exit, or the latest in the box)
}

/**
 * Track a position through `candles` (with `live`, the forming candle, replacing or following
 * the last): it fills on the first candle from its start whose range reaches the entry, as a
 * limit order does, then closes on the first later candle that reaches the stop or the target
 * (on the fill candle itself, only by closing past one). A candle reaching both counts as the
 * stop, since its range can't tell which came first.
 * Candles are this chart's, so a higher timeframe sees fewer, coarser candles.
 */
export function trackPosition(d: Drawing, candles: Candle[], live: Candle | null): PositionTrack | null {
  const n = candles.length;
  const liveAt = live && n && live.time >= candles[n - 1].time ? (live.time === candles[n - 1].time ? n - 1 : n) : -1;
  const count = liveAt === n ? n + 1 : n;
  if (!count) return null;
  const bar = (i: number) => (i === liveAt ? live! : candles[i]);
  const t0 = Math.min(d.points[0].t, d.points[1]?.t ?? d.points[0].t);
  const t1 = Math.max(d.points[0].t, d.points[1]?.t ?? d.points[0].t);
  // The candle holding the start: the last one opening at or before it.
  let lo = 0;
  let hi = count - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (bar(mid).time <= t0) lo = mid;
    else hi = mid - 1;
  }
  const { entry, stop, target } = positionOf(d);
  const long = d.kind === "long";
  let fillT: number | null = null;
  let last: Candle | null = null;
  for (let i = lo; i < count; i++) {
    const b = bar(i);
    if (b.time > t1) {
      if (!last) break;
      return fillT === null
        ? { state: "missed", price: last.close, fillT, t: last.time }
        : { state: "ended", price: last.close, fillT, t: last.time };
    }
    last = b;
    if (fillT === null) {
      if (b.low <= entry && entry <= b.high) {
        fillT = b.time;
        // Of the fill candle only the close surely comes after the fill: past the stop or the
        // target, price crossed it after filling. Its high and low count from the next candle.
        if (long ? b.close <= stop : b.close >= stop) return { state: "stop", price: stop, fillT, t: b.time };
        if (long ? b.close >= target : b.close <= target) return { state: "target", price: target, fillT, t: b.time };
      }
      continue;
    }
    if (long ? b.low <= stop : b.high >= stop) return { state: "stop", price: stop, fillT, t: b.time };
    if (long ? b.high >= target : b.low <= target) return { state: "target", price: target, fillT, t: b.time };
  }
  if (!last) return null;
  return { state: fillT === null ? "waiting" : "open", price: last.close, fillT, t: last.time };
}

/** The rectangle's corners, clockwise from its first point: which point gives each corner's t and p. */
export const RECT_CORNERS: [number, number][] = [[0, 0], [1, 0], [1, 1], [0, 1]];

export const TEXT_SIZE = 14; // a text drawing's default font size

let measureCtx: CanvasRenderingContext2D | null = null;
let textFont = "system-ui, sans-serif";
export const setTextFont = (font: string) => {
  textFont = font;
};
export const textFontOf = (size: number) => `600 ${size}px ${textFont}`;

/** A text drawing's box in CSS px: it starts at its point and is centred on it vertically. */
export function textBox(d: Drawing, at: XY) {
  const size = d.style.size ?? TEXT_SIZE;
  measureCtx ??= document.createElement("canvas").getContext("2d");
  let w = size * 4;
  if (measureCtx) {
    measureCtx.font = textFontOf(size);
    w = measureCtx.measureText(d.props?.text ?? "").width;
  }
  return { x: at.x, y: at.y - size * 0.7, w, h: size * 1.4 };
}

/**
 * Where the handles sit, in pixels. A horizontal line's one handle stays in view. A position
 * has four: entry, stop and target on its start edge, and its end on the entry line.
 */
export function handlesOf(d: Drawing, xy: (XY | null)[], width: number, yOf: YOf): XY[] {
  if (xy.some((p) => p === null)) return [];
  const pts = xy as XY[];
  if (isPosition(d)) {
    const b = positionBox(d, pts, yOf);
    return b ? [{ x: b.xStart, y: b.ye }, { x: b.xStart, y: b.ys }, { x: b.xStart, y: b.yt }, { x: b.xEnd, y: b.ye }] : [];
  }
  if (d.kind === "hline") return [{ x: Math.min(Math.max(pts[0].x, 24), width - 24), y: pts[0].y }];
  if (d.kind === "rect") return RECT_CORNERS.map(([ti, pi]) => ({ x: pts[ti].x, y: pts[pi].y }));
  if (d.kind === "text") return [{ x: pts[0].x - 10, y: pts[0].y }]; // just left of the text, not over its first letter
  return pts; // trendline and path: every point
}

function distToSegment(p: XY, a: XY, b: XY): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const len = dx * dx + dy * dy;
  const k = len ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / len)) : 0;
  return Math.hypot(p.x - (a.x + k * dx), p.y - (a.y + k * dy));
}

const HIT = 6; // px of slack around a line
export const HANDLE_HIT = 8;

/** Whether the pointer is on the drawing (its line, or inside a rectangle or position). */
export function hits(d: Drawing, xy: (XY | null)[], p: XY, yOf: YOf): boolean {
  if (xy.some((q) => q === null)) return false;
  const [a, b] = xy as XY[];
  if (isPosition(d)) {
    const box = positionBox(d, [a, b], yOf);
    if (!box) return false;
    const [y0, y1] = [Math.min(box.ys, box.yt), Math.max(box.ys, box.yt)];
    return p.x >= box.x0 - HIT && p.x <= box.x1 + HIT && p.y >= y0 - HIT && p.y <= y1 + HIT;
  }
  if (d.kind === "hline") return Math.abs(p.y - a.y) <= HIT;
  if (d.kind === "trendline") return distToSegment(p, a, b) <= HIT;
  if (d.kind === "path") {
    const pts = xy as XY[];
    return pts.slice(1).some((q, i) => distToSegment(p, pts[i], q) <= HIT);
  }
  if (d.kind === "text") {
    const box = textBox(d, a);
    return p.x >= box.x - HIT && p.x <= box.x + box.w + HIT && p.y >= box.y - HIT && p.y <= box.y + box.h + HIT;
  }
  const [x0, x1] = [Math.min(a.x, b.x), Math.max(a.x, b.x)];
  const [y0, y1] = [Math.min(a.y, b.y), Math.max(a.y, b.y)];
  return p.x >= x0 - HIT && p.x <= x1 + HIT && p.y >= y0 - HIT && p.y <= y1 + HIT;
}

/**
 * The drawing tools beyond the first few (trendline, horizontal line, rectangle, path, text and
 * positions, which drawingEditor.ts draws itself), after TradeCanvas's tool menus. Each one is its
 * anchor points and a geometry: the lines, filled areas and labels it draws, in chart pixels. One
 * renderer, hit test and set of handles serves them all, so a tool is only its geometry here.
 *
 * Anchors are (time, price) like every drawing, so a tool shows in the same place on every
 * timeframe. Tools that read candles (regression channel, bars pattern) use the closed candles of
 * the chart they are drawn on.
 */
import type { Candle, Drawing } from "./api";
import { fmtPrice } from "./format.ts";
import type { TcIconName } from "./tcIcons";

export interface XY {
  x: number;
  y: number;
}

export interface GeomCtx {
  w: number; // the pane, in CSS px
  h: number;
  yOf: (price: number) => number | null;
  xOfT: (t: number) => number | null;
  candles: Candle[]; // the chart's closed candles
  step: number; // seconds per candle
  bull: string;
  bear: string;
}

export interface GeomLine {
  pts: XY[];
  dash?: boolean;
  closed?: boolean;
  width?: number; // CSS px, over the drawing's own
  alpha?: number;
  color?: string;
}
export interface GeomFill {
  pts: XY[];
  alpha: number;
  color?: string;
}
export interface GeomLabel {
  x: number;
  y: number;
  text: string;
  align?: "left" | "center" | "right";
  base?: "top" | "middle" | "bottom";
  pill?: boolean; // on a filled box in the drawing's colour
  box?: boolean; // in an outlined box (notes)
  color?: string;
  size?: number;
}
export interface Geom {
  lines: GeomLine[];
  fills: GeomFill[];
  labels: GeomLabel[];
}

/** How a tool is placed: a fixed number of clicks, clicks until Enter or a click on the last one, or a free stroke. */
export type Placement = number | "multi" | "free";

export interface ShapeDef {
  label: string;
  icon: TcIconName;
  points: Placement;
  text?: string; // the words it starts with (notes, callouts, ...): its label, editable with T
  width?: number; // its starting line width
  anchored?: boolean; // stays where it is on the screen (props.ax, props.ay), not on a time and price
  geom: (p: XY[], d: Drawing, g: GeomCtx) => Geom;
}

// ---- Geometry helpers ------------------------------------------------------------------------

const FAR = 20000; // px: "to the edge of the chart" for rays and extended lines
const sub = (a: XY, b: XY): XY => ({ x: a.x - b.x, y: a.y - b.y });
const add = (a: XY, b: XY): XY => ({ x: a.x + b.x, y: a.y + b.y });
const mul = (a: XY, k: number): XY => ({ x: a.x * k, y: a.y * k });
const mid = (a: XY, b: XY): XY => ({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
const len = (a: XY) => Math.hypot(a.x, a.y);
/** From a through b, on to the edge of the chart. */
const far = (a: XY, b: XY): XY => {
  const v = sub(b, a);
  const l = len(v) || 1;
  return add(a, mul(v, FAR / l));
};
const hLine = (y: number, x0 = -FAR, x1 = FAR): XY[] => [{ x: x0, y }, { x: x1, y }];
const vLine = (x: number): XY[] => [{ x, y: -FAR }, { x, y: FAR }];

function arc(c: XY, rx: number, ry: number, a0 = 0, a1 = Math.PI * 2, n = 64): XY[] {
  const out: XY[] = [];
  for (let i = 0; i <= n; i++) {
    const a = a0 + ((a1 - a0) * i) / n;
    out.push({ x: c.x + Math.cos(a) * rx, y: c.y + Math.sin(a) * ry });
  }
  return out;
}

function quad(a: XY, c: XY, b: XY, n = 40): XY[] {
  const out: XY[] = [];
  for (let i = 0; i <= n; i++) {
    const t = i / n;
    out.push({ x: (1 - t) ** 2 * a.x + 2 * (1 - t) * t * c.x + t * t * b.x, y: (1 - t) ** 2 * a.y + 2 * (1 - t) * t * c.y + t * t * b.y });
  }
  return out;
}

/** An arrowhead at b, pointing from a. */
function head(a: XY, b: XY, size = 10): XY[] {
  const v = sub(b, a);
  const l = len(v) || 1;
  const u = mul(v, 1 / l);
  const n = { x: -u.y, y: u.x };
  const base = sub(b, mul(u, size));
  return [add(base, mul(n, size * 0.5)), b, sub(base, mul(n, size * 0.5))];
}

const pct = (from: number, to: number) => (from ? ((to - from) / from) * 100 : 0);
const signed = (v: number) => `${v >= 0 ? "+" : "−"}${fmtPrice(Math.abs(v))}`;
const signedPct = (v: number) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}%`;

function duration(seconds: number): string {
  const s = Math.abs(seconds);
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.round((s % 3600) / 60);
  return [d ? `${d}d` : "", h ? `${h}h` : "", m || (!d && !h) ? `${m}m` : ""].filter(Boolean).join(" ");
}
const bars = (d: Drawing, g: GeomCtx, i = 0, j = 1) => Math.round((d.points[j].t - d.points[i].t) / g.step);

const geom = (lines: GeomLine[] = [], fills: GeomFill[] = [], labels: GeomLabel[] = []): Geom => ({ lines, fills, labels });

/** Lines at price levels between two prices: fib retracement style, labelled with level and price. */
function levels(g: GeomCtx, from: number, to: number, ks: number[], x0: number, x1: number): Geom {
  const out = geom();
  let prev: number | null = null;
  for (const k of ks) {
    const price = from + (to - from) * k;
    const y = g.yOf(price);
    if (y === null) continue;
    out.lines.push({ pts: hLine(y, x0, x1) });
    if (prev !== null) out.fills.push({ pts: [{ x: x0, y: prev }, { x: x1, y: prev }, { x: x1, y }, { x: x0, y }], alpha: 0.05 });
    out.labels.push({ x: x0 - 4, y, text: `${k} (${fmtPrice(price)})`, align: "right", base: "middle", size: 10 });
    prev = y;
  }
  return out;
}

const FIB = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];
const FIB_EXT = [0, 0.382, 0.618, 1, 1.272, 1.618, 2.618];

/** A labelled polyline through the clicked points: harmonic patterns and Elliott waves. */
function pattern(p: XY[], names: string[], opts: { fills?: number[][]; dashed?: number[][] } = {}): Geom {
  const out = geom([{ pts: p }]);
  for (const tri of opts.fills ?? []) if (tri.every((i) => p[i])) out.fills.push({ pts: tri.map((i) => p[i]), alpha: 0.1 });
  for (const [i, j] of opts.dashed ?? []) if (p[i] && p[j]) out.lines.push({ pts: [p[i], p[j]], dash: true });
  p.forEach((q, i) => {
    if (!names[i]) return;
    const up = i > 0 ? q.y <= p[i - 1].y : p[1] ? q.y <= p[1].y : true; // a high labelled above, a low below
    out.labels.push({ x: q.x, y: q.y + (up ? -8 : 8), text: names[i], align: "center", base: up ? "bottom" : "top", size: 11 });
  });
  return out;
}

/** The ratio of two legs' price moves, as a pattern's label between them. */
function ratio(d: Drawing, i: number, j: number, k: number): string {
  const a = Math.abs(d.points[j].p - d.points[i].p);
  const b = Math.abs(d.points[k].p - d.points[j].p);
  return a ? (b / a).toFixed(3) : "";
}

/** Least-squares line through the closes between two times, with the widest close's distance. */
function regression(d: Drawing, g: GeomCtx, p: XY[]): Geom {
  const [t0, t1] = [Math.min(d.points[0].t, d.points[1].t), Math.max(d.points[0].t, d.points[1].t)];
  const pts: XY[] = [];
  for (const c of g.candles) {
    if (c.time < t0 || c.time > t1) continue;
    const x = g.xOfT(c.time);
    const y = g.yOf(c.close);
    if (x !== null && y !== null) pts.push({ x, y });
  }
  if (pts.length < 2) return geom([{ pts: p, dash: true }]);
  const n = pts.length;
  const mx = pts.reduce((s, q) => s + q.x, 0) / n;
  const my = pts.reduce((s, q) => s + q.y, 0) / n;
  let sxy = 0;
  let sxx = 0;
  for (const q of pts) {
    sxy += (q.x - mx) * (q.y - my);
    sxx += (q.x - mx) ** 2;
  }
  const m = sxx ? sxy / sxx : 0;
  const at = (x: number) => my + m * (x - mx);
  const dev = Math.max(...pts.map((q) => Math.abs(q.y - at(q.x))));
  const [x0, x1] = [pts[0].x, pts[n - 1].x];
  const line = (off: number): XY[] => [{ x: x0, y: at(x0) + off }, { x: x1, y: at(x1) + off }];
  const up = line(-dev);
  const down = line(dev);
  return geom([{ pts: line(0), dash: true }, { pts: up }, { pts: down }], [{ pts: [up[0], up[1], down[1], down[0]], alpha: 0.08 }]);
}

/** The candles between two times, copied to start at the second point: the bars pattern. */
function barsCopy(d: Drawing, g: GeomCtx, p: XY[]): Geom {
  const [t0, t1] = [Math.min(d.points[0].t, d.points[1].t), Math.max(d.points[0].t, d.points[1].t)];
  const src = g.candles.filter((c) => c.time >= t0 && c.time <= t1);
  const out = geom([{ pts: vLine(p[0].x), dash: true, alpha: 0.4 }]);
  if (!src.length) return out;
  const x0 = g.xOfT(src[0].time);
  const y0 = g.yOf(src[0].open);
  if (x0 === null || y0 === null) return out;
  const dx = p[1].x - x0;
  const dy = p[1].y - y0;
  const bw = src.length > 1 ? Math.max(2, ((g.xOfT(src[1].time) ?? x0 + 6) - x0) * 0.6) : 6;
  for (const c of src) {
    const x = g.xOfT(c.time);
    const [o, h, l, cl] = [g.yOf(c.open), g.yOf(c.high), g.yOf(c.low), g.yOf(c.close)];
    if (x === null || o === null || h === null || l === null || cl === null) continue;
    const X = x + dx;
    const color = c.close >= c.open ? g.bull : g.bear;
    out.lines.push({ pts: [{ x: X, y: h + dy }, { x: X, y: l + dy }], color, alpha: 0.6, width: 1 });
    const [top, bot] = [Math.min(o, cl) + dy, Math.max(o, cl) + dy];
    out.fills.push({ pts: [{ x: X - bw / 2, y: top }, { x: X + bw / 2, y: top }, { x: X + bw / 2, y: bot + 1 }, { x: X - bw / 2, y: bot + 1 }], alpha: 0.5, color });
  }
  return out;
}

/** Price and time between two points, for the range tools and measure. */
function range(d: Drawing, g: GeomCtx, p: XY[], show: { price: boolean; time: boolean }, colored = false): Geom {
  const [a, b] = p;
  const dp = d.points[1].p - d.points[0].p;
  const color = colored ? (dp >= 0 ? g.bull : g.bear) : undefined;
  const box = [{ x: a.x, y: a.y }, { x: b.x, y: a.y }, { x: b.x, y: b.y }, { x: a.x, y: b.y }];
  const out = geom([], [{ pts: box, alpha: 0.12, color }]);
  const mx = (a.x + b.x) / 2;
  const my = (a.y + b.y) / 2;
  if (show.price) {
    out.lines.push({ pts: [{ x: mx, y: a.y }, { x: mx, y: b.y }], color }, { pts: head({ x: mx, y: a.y }, { x: mx, y: b.y }, 8), color });
    out.lines.push({ pts: [{ x: a.x, y: a.y }, { x: b.x, y: a.y }], color, alpha: 0.6 }, { pts: [{ x: a.x, y: b.y }, { x: b.x, y: b.y }], color, alpha: 0.6 });
  }
  if (show.time) {
    out.lines.push({ pts: [{ x: a.x, y: my }, { x: b.x, y: my }], color }, { pts: head({ x: a.x, y: my }, { x: b.x, y: my }, 8), color });
    out.lines.push({ pts: [{ x: a.x, y: a.y }, { x: a.x, y: b.y }], color, alpha: 0.6 }, { pts: [{ x: b.x, y: a.y }, { x: b.x, y: b.y }], color, alpha: 0.6 });
  }
  const parts: string[] = [];
  if (show.price) parts.push(`${signed(dp)} (${signedPct(pct(d.points[0].p, d.points[1].p))})`);
  if (show.time) parts.push(`${bars(d, g)} bars, ${duration(d.points[1].t - d.points[0].t)}`);
  const below = b.y >= a.y;
  out.labels.push({ x: mx, y: (below ? Math.max(a.y, b.y) : Math.min(a.y, b.y)) + (below ? 8 : -8), text: parts.join("  ·  "),
    align: "center", base: below ? "top" : "bottom", pill: true, color });
  return out;
}

/** A rounded-ish box of text at a point: notes, signposts, balloons. */
function textBox(at: XY, text: string, opts: { align?: "left" | "center"; above?: number } = {}): GeomLabel {
  return { x: at.x, y: at.y - (opts.above ?? 0), text, align: opts.align ?? "left", base: "bottom", box: true };
}

/** A circle through three points (its centre and radius), or null when they are on a line. */
function circleThrough(a: XY, b: XY, c: XY): { c: XY; r: number } | null {
  const d = 2 * (a.x * (b.y - c.y) + b.x * (c.y - a.y) + c.x * (a.y - b.y));
  if (Math.abs(d) < 1e-6) return null;
  const sq = (q: XY) => q.x * q.x + q.y * q.y;
  const x = (sq(a) * (b.y - c.y) + sq(b) * (c.y - a.y) + sq(c) * (a.y - b.y)) / d;
  const y = (sq(a) * (c.x - b.x) + sq(b) * (a.x - c.x) + sq(c) * (b.x - a.x)) / d;
  return { c: { x, y }, r: Math.hypot(a.x - x, a.y - y) };
}

function star(c: XY, r: number): XY[] {
  const out: XY[] = [];
  for (let i = 0; i < 10; i++) {
    const a = -Math.PI / 2 + (i * Math.PI) / 5;
    const rr = i % 2 ? r * 0.45 : r;
    out.push({ x: c.x + Math.cos(a) * rr, y: c.y + Math.sin(a) * rr });
  }
  return out;
}

/** A pitchfork from its handle h through the middle of b and c, with tines through b and c. */
function fork(h: XY, b: XY, c: XY): Geom {
  const m = mid(b, c);
  const dir = sub(m, h);
  return geom([
    { pts: [h, far(h, m)] },
    { pts: [b, far(b, add(b, dir))] },
    { pts: [c, far(c, add(c, dir))] },
    { pts: [b, c] },
  ]);
}

const label = (d: Drawing, fallback = "") => d.props?.text ?? fallback;

// ---- The tools ---------------------------------------------------------------------------------

export const SHAPES = {
  // Lines
  ray: { label: "Ray", icon: "ray", points: 2, width: 2, geom: ([a, b]) => geom([{ pts: [a, far(a, b)] }]) },
  extendedLine: { label: "Extended line", icon: "extendedLine", points: 2, width: 2, geom: ([a, b]) => geom([{ pts: [far(b, a), far(a, b)] }]) },
  infoLine: {
    label: "Info line", icon: "infoLine", points: 2, width: 2,
    geom: ([a, b], d, g) => {
      const dp = d.points[1].p - d.points[0].p;
      const deg = (Math.atan2(a.y - b.y, b.x - a.x) * 180) / Math.PI;
      return geom([{ pts: [a, b] }], [], [{ x: b.x + 8, y: b.y, base: "middle", pill: true,
        text: `${signed(dp)} (${signedPct(pct(d.points[0].p, d.points[1].p))}) · ${bars(d, g)} bars · ${deg.toFixed(1)}°` }]);
    },
  },
  trendAngle: {
    label: "Trend angle", icon: "trendAngle", points: 2, width: 2,
    geom: ([a, b]) => {
      const r = Math.min(40, len(sub(b, a)) * 0.5);
      const ang = Math.atan2(b.y - a.y, b.x - a.x);
      const deg = (-ang * 180) / Math.PI;
      return geom([{ pts: [a, b] }, { pts: [a, { x: a.x + Math.max(r * 1.6, Math.abs(b.x - a.x)), y: a.y }], dash: true },
        { pts: arc(a, r, r, Math.min(0, ang), Math.max(0, ang), 24) }], [],
      [{ x: a.x + r + 6, y: a.y + (ang < 0 ? -6 : 6), text: `${deg.toFixed(1)}°`, base: ang < 0 ? "bottom" : "top", size: 11 }]);
    },
  },
  horizontalRay: {
    label: "Horizontal ray", icon: "horizontalRay", points: 1, width: 2,
    geom: ([a], d) => geom([{ pts: [a, { x: FAR, y: a.y }] }], [], [{ x: a.x + 4, y: a.y - 4, text: fmtPrice(d.points[0].p), base: "bottom", size: 10 }]),
  },
  verticalLine: { label: "Vertical line", icon: "verticalLine", points: 1, width: 1, geom: ([a]) => geom([{ pts: vLine(a.x) }]) },
  crossLine: { label: "Cross line", icon: "crossLine", points: 1, width: 1, geom: ([a]) => geom([{ pts: vLine(a.x) }, { pts: hLine(a.y) }]) },
  parallelChannel: {
    label: "Parallel channel", icon: "parallelChannel", points: 3, width: 1,
    geom: ([a, b, c]) => {
      if (!c) return geom([{ pts: [a, b] }]);
      const k = b.x !== a.x ? (b.y - a.y) / (b.x - a.x) : 0;
      const off = { x: 0, y: c.y - (a.y + k * (c.x - a.x)) };
      const [a2, b2] = [add(a, off), add(b, off)];
      return geom([{ pts: [a, b] }, { pts: [a2, b2] }, { pts: [mid(a, a2), mid(b, b2)], dash: true, alpha: 0.6 }],
        [{ pts: [a, b, b2, a2], alpha: 0.08 }]);
    },
  },
  regression: { label: "Regression channel", icon: "regressionChannel", points: 2, width: 1, geom: (p, d, g) => regression(d, g, p) },
  disjointChannel: {
    label: "Disjoint channel", icon: "disjointChannel", points: 3, width: 1,
    geom: ([a, b, c]) => {
      if (!c) return geom([{ pts: [a, b] }]);
      const c2 = { x: c.x + (b.x - a.x), y: c.y - (b.y - a.y) };
      return geom([{ pts: [a, b] }, { pts: [c, c2] }], [{ pts: [a, b, c2, c], alpha: 0.08 }]);
    },
  },
  flatTopBottom: {
    label: "Flat top/bottom", icon: "flatTopBottom", points: 3, width: 1,
    geom: ([a, b, c]) => {
      if (!c) return geom([{ pts: [a, b] }]);
      const [f0, f1] = [{ x: a.x, y: c.y }, { x: b.x, y: c.y }];
      return geom([{ pts: [a, b] }, { pts: [f0, f1] }], [{ pts: [a, b, f1, f0], alpha: 0.08 }]);
    },
  },

  // Fibonacci
  fibRetracement: {
    label: "Fib retracement", icon: "fibRetracement", points: 2, width: 1,
    geom: ([a, b], d, g) => {
      const out = levels(g, d.points[1].p, d.points[0].p, FIB, Math.min(a.x, b.x), Math.max(a.x, b.x));
      out.lines.push({ pts: [a, b], dash: true, alpha: 0.6 });
      return out;
    },
  },
  fibExtension: {
    label: "Fib extension", icon: "fibExtension", points: 3, width: 1,
    geom: ([a, b, c], d, g) => {
      if (!c) return geom([{ pts: [a, b], dash: true }]);
      const move = d.points[1].p - d.points[0].p;
      const out = levels(g, d.points[2].p, d.points[2].p + move, FIB_EXT, c.x, Math.max(b.x, c.x) + Math.abs(b.x - a.x));
      out.lines.push({ pts: [a, b, c], dash: true, alpha: 0.6 });
      return out;
    },
  },
  fibChannel: {
    label: "Fib channel", icon: "fibChannel", points: 3, width: 1,
    geom: ([a, b, c]) => {
      if (!c) return geom([{ pts: [a, b] }]);
      const k = b.x !== a.x ? (b.y - a.y) / (b.x - a.x) : 0;
      const off = c.y - (a.y + k * (c.x - a.x));
      const out = geom();
      for (const f of [0, 0.382, 0.618, 1, 1.618]) {
        const o = { x: 0, y: off * f };
        out.lines.push({ pts: [far(add(b, o), add(a, o)), far(add(a, o), add(b, o))], alpha: f === 0 || f === 1 ? 1 : 0.6 });
        out.labels.push({ x: a.x - 4, y: a.y + off * f, text: String(f), align: "right", base: "middle", size: 10 });
      }
      return out;
    },
  },
  fibTimeZones: {
    label: "Fib time zones", icon: "fibTimeZones", points: 2, width: 1,
    geom: ([a, b], _d, g) => {
      const unit = b.x - a.x;
      const out = geom();
      for (const n of [0, 1, 2, 3, 5, 8, 13, 21, 34, 55, 89]) {
        const x = a.x + unit * n;
        if (Math.abs(x) > g.w * 4) break;
        out.lines.push({ pts: vLine(x), alpha: n < 2 ? 1 : 0.7 });
        out.labels.push({ x: x + 3, y: 12, text: String(n), base: "top", size: 10 });
      }
      return out;
    },
  },
  fibFan: {
    label: "Fib speed resistance fan", icon: "fibSpeedResistanceFan", points: 2, width: 1,
    geom: ([a, b]) => {
      const out = geom([{ pts: [a, { x: b.x, y: a.y }, b, { x: a.x, y: b.y }, a], dash: true, alpha: 0.4 }]);
      for (const k of [0.25, 0.382, 0.5, 0.618, 0.75, 1]) {
        out.lines.push({ pts: [a, far(a, { x: b.x, y: a.y + (b.y - a.y) * k })], alpha: 0.8 });
        if (k < 1) out.lines.push({ pts: [a, far(a, { x: a.x + (b.x - a.x) * k, y: b.y })], alpha: 0.5 });
      }
      return out;
    },
  },
  fibArcs: {
    label: "Fib speed resistance arcs", icon: "fibArcs", points: 2, width: 1,
    geom: ([a, b]) => {
      const r = len(sub(b, a));
      const up = a.y < b.y; // the arcs open toward the first point
      const out = geom([{ pts: [a, b], dash: true, alpha: 0.6 }]);
      for (const k of [0.236, 0.382, 0.5, 0.618, 0.786, 1]) out.lines.push({ pts: arc(b, r * k, r * k, up ? Math.PI : 0, up ? Math.PI * 2 : Math.PI, 48) });
      return out;
    },
  },
  fibCircles: {
    label: "Fib circles", icon: "fibCircles", points: 2, width: 1,
    geom: ([a, b]) => {
      const r = len(sub(b, a));
      const out = geom([{ pts: [a, b], dash: true, alpha: 0.6 }]);
      for (const k of [0.382, 0.618, 1, 1.618, 2.618]) out.lines.push({ pts: arc(a, r * k, r * k), alpha: k === 1 ? 1 : 0.7 });
      return out;
    },
  },
  fibSpiral: {
    label: "Fib spiral", icon: "fibSpiral", points: 2, width: 1,
    geom: ([a, b]) => {
      const r0 = len(sub(b, a));
      const a0 = Math.atan2(b.y - a.y, b.x - a.x);
      const phi = (1 + Math.sqrt(5)) / 2;
      const pts: XY[] = [];
      for (let i = 0; i <= 240; i++) {
        const th = (i / 240) * Math.PI * 6 - Math.PI * 4; // from inside out to two turns past b
        const r = r0 * phi ** (th / (Math.PI / 2));
        pts.push({ x: a.x + Math.cos(a0 + th) * r, y: a.y + Math.sin(a0 + th) * r });
      }
      return geom([{ pts }, { pts: [a, b], dash: true, alpha: 0.6 }]);
    },
  },
  fibWedge: {
    label: "Fib wedge", icon: "fibWedge", points: 3, width: 1,
    geom: ([a, b, c]) => {
      if (!c) return geom([{ pts: [a, b] }]);
      const r = len(sub(b, a));
      const [t0, t1] = [Math.atan2(b.y - a.y, b.x - a.x), Math.atan2(c.y - a.y, c.x - a.x)];
      const out = geom([{ pts: [a, b] }, { pts: [a, c] }]);
      for (const k of [0.236, 0.382, 0.5, 0.618, 0.786, 1]) out.lines.push({ pts: arc(a, r * k, r * k, Math.min(t0, t1), Math.max(t0, t1), 24), alpha: 0.8 });
      return out;
    },
  },
  pitchfork: { label: "Pitchfork", icon: "pitchfork", points: 3, width: 1, geom: ([a, b, c]) => (c ? fork(a, b, c) : geom([{ pts: [a, b] }])) },
  schiffPitchfork: {
    label: "Schiff pitchfork", icon: "schiffPitchfork", points: 3, width: 1,
    geom: ([a, b, c]) => (c ? fork({ x: a.x, y: (a.y + b.y) / 2 }, b, c) : geom([{ pts: [a, b] }])),
  },
  modSchiffFork: {
    label: "Modified Schiff pitchfork", icon: "modifiedSchiffPitchfork", points: 3, width: 1,
    geom: ([a, b, c]) => (c ? fork(mid(a, b), b, c) : geom([{ pts: [a, b] }])),
  },
  gannFan: {
    label: "Gann fan", icon: "gannFan", points: 2, width: 1,
    geom: ([a, b]) => {
      const out = geom();
      const v = sub(b, a);
      for (const [r, name] of [[8, "1x8"], [4, "1x4"], [3, "1x3"], [2, "1x2"], [1, "1x1"], [1 / 2, "2x1"], [1 / 3, "3x1"], [1 / 4, "4x1"], [1 / 8, "8x1"]] as const) {
        const end = r >= 1 ? { x: a.x + v.x / r, y: a.y + v.y } : { x: a.x + v.x, y: a.y + v.y * r };
        out.lines.push({ pts: [a, far(a, end)], alpha: r === 1 ? 1 : 0.6 });
        out.labels.push({ x: end.x + 4, y: end.y, text: name, base: "middle", size: 10 });
      }
      return out;
    },
  },
  gannBox: {
    label: "Gann box", icon: "gannBox", points: 2, width: 1,
    geom: ([a, b]) => {
      const out = geom([{ pts: [a, { x: b.x, y: a.y }, b, { x: a.x, y: b.y }], closed: true }, { pts: [a, b], alpha: 0.6 },
        { pts: [{ x: a.x, y: b.y }, { x: b.x, y: a.y }], alpha: 0.6 }], [{ pts: [a, { x: b.x, y: a.y }, b, { x: a.x, y: b.y }], alpha: 0.06 }]);
      for (const k of [0.25, 0.382, 0.5, 0.618, 0.75]) {
        out.lines.push({ pts: [{ x: a.x + (b.x - a.x) * k, y: a.y }, { x: a.x + (b.x - a.x) * k, y: b.y }], dash: true, alpha: 0.5 });
        out.lines.push({ pts: [{ x: a.x, y: a.y + (b.y - a.y) * k }, { x: b.x, y: a.y + (b.y - a.y) * k }], dash: true, alpha: 0.5 });
      }
      return out;
    },
  },
  gannSquare: {
    label: "Gann square", icon: "gannSquare", points: 2, width: 1,
    geom: ([a, b]) => {
      const s = Math.max(Math.abs(b.x - a.x), Math.abs(b.y - a.y));
      const sx = Math.sign(b.x - a.x) || 1;
      const sy = Math.sign(b.y - a.y) || 1;
      const c = { x: a.x + s * sx, y: a.y + s * sy };
      const box = [a, { x: c.x, y: a.y }, c, { x: a.x, y: c.y }];
      const m = mid(a, c);
      return geom([{ pts: box, closed: true }, { pts: [a, c], alpha: 0.7 }, { pts: [box[1], box[3]], alpha: 0.7 },
        { pts: [{ x: m.x, y: a.y }, { x: m.x, y: c.y }], dash: true, alpha: 0.5 }, { pts: [{ x: a.x, y: m.y }, { x: c.x, y: m.y }], dash: true, alpha: 0.5 },
        { pts: arc({ x: a.x, y: c.y }, s, s, sy > 0 ? -Math.PI / 2 : 0, sy > 0 ? 0 : Math.PI / 2, 32), alpha: 0.7 }],
      [{ pts: box, alpha: 0.05 }]);
    },
  },
  pitchfan: {
    label: "Pitchfan", icon: "pitchfan", points: 3, width: 1,
    geom: ([a, b, c]) => (c ? geom([{ pts: [a, far(a, b)] }, { pts: [a, far(a, mid(b, c))] }, { pts: [a, far(a, c)] }, { pts: [b, c], dash: true }]) : geom([{ pts: [a, b] }])),
  },
  cyclicLines: {
    label: "Cyclic lines", icon: "cyclicLines", points: 2, width: 1,
    geom: ([a, b], _d, g) => {
      const unit = b.x - a.x;
      const out = geom();
      if (Math.abs(unit) < 2) return geom([{ pts: vLine(a.x) }]);
      for (let x = a.x, i = 0; Math.abs(x - a.x) <= g.w * 3 && i < 300; x += unit, i++) out.lines.push({ pts: vLine(x), alpha: 0.7 });
      return out;
    },
  },
  timeCycles: {
    label: "Time cycles", icon: "timeCycles", points: 2, width: 1,
    geom: ([a, b], _d, g) => {
      const unit = b.x - a.x;
      const r = Math.abs(unit) / 2;
      const out = geom([{ pts: hLine(a.y), dash: true, alpha: 0.4 }]);
      if (r < 1) return out;
      for (let x = a.x, i = 0; Math.abs(x - a.x) <= g.w * 3 && i < 200; x += unit, i++) {
        out.lines.push({ pts: arc({ x: x + unit / 2, y: a.y }, r, r, Math.PI, Math.PI * 2, 32) });
      }
      return out;
    },
  },
  sineLine: {
    label: "Sine line", icon: "sineLine", points: 2, width: 2,
    geom: ([a, b], _d, g) => {
      const half = b.x - a.x; // a peak to the next trough
      if (Math.abs(half) < 2) return geom([{ pts: [a, b] }]);
      const amp = (b.y - a.y) / 2;
      const cy = (a.y + b.y) / 2;
      const pts: XY[] = [];
      const lo = Math.min(-g.w, a.x - g.w);
      const hi = Math.max(g.w * 2, a.x + g.w * 2);
      for (let x = lo; x <= hi; x += 3) pts.push({ x, y: cy - amp * Math.cos((Math.PI * (x - a.x)) / half) });
      return geom([{ pts }]);
    },
  },

  // Patterns
  xabcd: {
    label: "XABCD pattern", icon: "xabcdPattern", points: 5, width: 2,
    geom: (p, d) => {
      const out = pattern(p, ["X", "A", "B", "C", "D"], { fills: [[0, 1, 2], [2, 3, 4]], dashed: [[0, 2], [2, 4], [0, 4]] });
      if (p[2]) out.labels.push({ ...mid(p[0], p[2]), text: ratio(d, 0, 1, 2), align: "center", base: "middle", size: 10 });
      if (p[4]) out.labels.push({ ...mid(p[2], p[4]), text: ratio(d, 2, 3, 4), align: "center", base: "middle", size: 10 });
      return out;
    },
  },
  abcd: {
    label: "ABCD pattern", icon: "abcdPattern", points: 4, width: 2,
    geom: (p, d) => {
      const out = pattern(p, ["A", "B", "C", "D"], { dashed: [[0, 2], [1, 3]] });
      if (p[2]) out.labels.push({ ...mid(p[0], p[2]), text: ratio(d, 0, 1, 2), align: "center", base: "middle", size: 10 });
      if (p[3]) out.labels.push({ ...mid(p[1], p[3]), text: ratio(d, 1, 2, 3), align: "center", base: "middle", size: 10 });
      return out;
    },
  },
  headShoulders: {
    label: "Head and shoulders", icon: "headAndShoulders", points: 7, width: 2,
    geom: (p) => {
      const out = pattern(p, ["", "Left shoulder", "", "Head", "", "Right shoulder", ""], { fills: [[0, 1, 2], [2, 3, 4], [4, 5, 6]] });
      if (p[4]) out.lines.push({ pts: [far(p[4], p[2]), far(p[2], p[4])], dash: true });
      return out;
    },
  },
  cypher: {
    label: "Cypher pattern", icon: "cypherPattern", points: 5, width: 2,
    geom: (p, d) => {
      const out = pattern(p, ["X", "A", "B", "C", "D"], { fills: [[0, 1, 2], [2, 3, 4]], dashed: [[0, 2], [2, 4]] });
      if (p[3]) out.labels.push({ ...mid(p[1], p[3]), text: ratio(d, 1, 2, 3), align: "center", base: "middle", size: 10 });
      return out;
    },
  },
  threeDrives: {
    label: "Three drives pattern", icon: "threeDrives", points: 7, width: 2,
    geom: (p) => pattern(p, ["", "Drive 1", "", "Drive 2", "", "Drive 3", ""], { dashed: [[1, 3], [3, 5]] }),
  },
  elliottWave: { label: "Elliott wave", icon: "elliottWave", points: 6, width: 2, geom: (p) => pattern(p, ["", "i", "ii", "iii", "iv", "v"]) },
  elliottImpulse: { label: "Elliott impulse wave", icon: "elliottImpulse", points: 6, width: 2, geom: (p) => pattern(p, ["(0)", "(1)", "(2)", "(3)", "(4)", "(5)"]) },
  elliottCorrect: { label: "Elliott correction wave", icon: "elliottCorrection", points: 4, width: 2, geom: (p) => pattern(p, ["(0)", "(A)", "(B)", "(C)"]) },
  elliottTriangle: { label: "Elliott triangle wave", icon: "elliottTriangle", points: 6, width: 2, geom: (p) => pattern(p, ["(0)", "(A)", "(B)", "(C)", "(D)", "(E)"]) },
  elliottDouble: { label: "Elliott double combo wave", icon: "elliottDoubleCombo", points: 4, width: 2, geom: (p) => pattern(p, ["(0)", "(W)", "(X)", "(Y)"]) },
  elliottTriple: { label: "Elliott triple combo wave", icon: "elliottTripleCombo", points: 6, width: 2, geom: (p) => pattern(p, ["(0)", "(W)", "(X)", "(Y)", "(X)", "(Z)"]) },

  // Forecasting and measuring
  forecast: {
    label: "Forecast", icon: "forecast", points: 2, width: 2,
    geom: ([a, b], d, g) => {
      const dp = d.points[1].p - d.points[0].p;
      return geom([{ pts: [a, b] }, { pts: arc(b, 6, 6) }], [], [{ x: b.x + 10, y: b.y, base: "middle", pill: true,
        text: `${signed(dp)} (${signedPct(pct(d.points[0].p, d.points[1].p))}) in ${bars(d, g)} bars`, color: dp >= 0 ? g.bull : g.bear }]);
    },
  },
  projection: {
    label: "Projection", icon: "projection", points: 3, width: 2,
    geom: ([a, b, c], d) => {
      if (!c) return geom([{ pts: [a, b], dash: true }]);
      return geom([{ pts: [a, b], dash: true }, { pts: [b, c] }, { pts: head(b, c) }], [],
        [{ x: c.x + 8, y: c.y, base: "middle", size: 11, text: `${ratio(d, 0, 1, 2)} of AB` }]);
    },
  },
  barsPattern: { label: "Bars pattern", icon: "barsPattern", points: 2, width: 1, geom: (p, d, g) => barsCopy(d, g, p) },
  priceRange: { label: "Price range", icon: "priceRange", points: 2, width: 1, geom: (p, d, g) => range(d, g, p, { price: true, time: false }) },
  dateRange: { label: "Date range", icon: "dateRange", points: 2, width: 1, geom: (p, d, g) => range(d, g, p, { price: false, time: true }) },
  datePriceRange: { label: "Date and price range", icon: "dateAndPriceRange", points: 2, width: 1, geom: (p, d, g) => range(d, g, p, { price: true, time: true }) },
  measure: { label: "Measure", icon: "measure", points: 2, width: 1, geom: (p, d, g) => range(d, g, p, { price: true, time: true }, true) },

  // Shapes
  rotatedRect: {
    label: "Rotated rectangle", icon: "rotatedRectangle", points: 3, width: 1,
    geom: ([a, b, c]) => {
      if (!c) return geom([{ pts: [a, b] }]);
      const v = sub(b, a);
      const l = len(v) || 1;
      const n = { x: -v.y / l, y: v.x / l };
      const k = (c.x - b.x) * n.x + (c.y - b.y) * n.y;
      const o = mul(n, k);
      const box = [a, b, add(b, o), add(a, o)];
      return geom([{ pts: box, closed: true }], [{ pts: box, alpha: 0.14 }]);
    },
  },
  circle: {
    label: "Circle", icon: "circle", points: 2, width: 1,
    geom: ([a, b]) => {
      const r = len(sub(b, a));
      const pts = arc(a, r, r);
      return geom([{ pts }], [{ pts, alpha: 0.14 }]);
    },
  },
  ellipse: {
    label: "Ellipse", icon: "ellipse", points: 2, width: 1,
    geom: ([a, b]) => {
      const pts = arc(mid(a, b), Math.abs(b.x - a.x) / 2, Math.abs(b.y - a.y) / 2);
      return geom([{ pts }], [{ pts, alpha: 0.14 }]);
    },
  },
  triangle: {
    label: "Triangle", icon: "triangle", points: 3, width: 1,
    geom: (p) => (p[2] ? geom([{ pts: p, closed: true }], [{ pts: p, alpha: 0.14 }]) : geom([{ pts: p }])),
  },
  polyline: {
    label: "Polyline", icon: "polyline", points: "multi", width: 1,
    geom: (p) => geom([{ pts: p, closed: p.length > 2 }], p.length > 2 ? [{ pts: p, alpha: 0.14 }] : []),
  },
  curve: {
    label: "Curve", icon: "curve", points: 3, width: 2,
    geom: ([a, b, c]) => geom([{ pts: c ? quad(a, c, b) : [a, b] }]),
  },
  arc: {
    label: "Arc", icon: "arc", points: 3, width: 2,
    geom: ([a, b, c]) => {
      if (!c) return geom([{ pts: [a, b] }]);
      const circ = circleThrough(a, c, b);
      if (!circ) return geom([{ pts: [a, b] }]);
      const ang = (q: XY) => Math.atan2(q.y - circ.c.y, q.x - circ.c.x);
      const t0 = ang(a);
      const turn = (t: number) => (((t - t0) % (Math.PI * 2)) + Math.PI * 2) % (Math.PI * 2); // from a, going one way
      // From a to b, the way that passes c.
      const t1 = turn(ang(c)) <= turn(ang(b)) ? t0 + turn(ang(b)) : t0 - (Math.PI * 2 - turn(ang(b)));
      return geom([{ pts: arc(circ.c, circ.r, circ.r, t0, t1, 64) }]);
    },
  },
  brush: { label: "Brush", icon: "brush", points: "free", width: 2, geom: (p) => geom([{ pts: p }]) },
  highlighter: { label: "Highlighter", icon: "highlighter", points: "free", width: 1, geom: (p) => geom([{ pts: p, width: 12, alpha: 0.3 }]) },

  // Annotation
  priceLabel: {
    label: "Price label", icon: "priceLabel", points: 1, width: 1,
    geom: ([a], d) => geom([{ pts: [a, { x: a.x + 10, y: a.y - 10 }] }], [], [{ x: a.x + 10, y: a.y - 10, text: label(d, fmtPrice(d.points[0].p)), base: "middle", pill: true }]),
  },
  arrow: { label: "Arrow", icon: "arrow", points: 2, width: 2, geom: ([a, b]) => geom([{ pts: [a, b] }, { pts: head(a, b, 12) }], [{ pts: head(a, b, 12), alpha: 1 }]) },
  note: { label: "Note", icon: "note", points: 1, text: "Note", width: 1, geom: ([a], d) => geom([], [], [textBox(a, label(d, "Note"))]) },
  callout: {
    label: "Callout", icon: "callout", points: 2, text: "Callout", width: 1,
    geom: ([a, b], d) => geom([{ pts: [a, b] }], [], [textBox(a, label(d, "Callout"))]),
  },
  flag: {
    label: "Flag mark", icon: "flag", points: 1, width: 1,
    geom: ([a]) => {
      const flag = [{ x: a.x, y: a.y - 30 }, { x: a.x + 18, y: a.y - 30 }, { x: a.x + 13, y: a.y - 24 }, { x: a.x + 18, y: a.y - 18 }, { x: a.x, y: a.y - 18 }];
      return geom([{ pts: [a, { x: a.x, y: a.y - 30 }], width: 2 }], [{ pts: flag, alpha: 0.9 }]);
    },
  },
  arrowMark: {
    label: "Arrow mark", icon: "arrowMark", points: 1, width: 1,
    geom: ([a]) => {
      const s = 7;
      const pts = [{ x: a.x, y: a.y }, { x: a.x + s, y: a.y + s }, { x: a.x + s / 2.5, y: a.y + s }, { x: a.x + s / 2.5, y: a.y + s * 2.6 },
        { x: a.x - s / 2.5, y: a.y + s * 2.6 }, { x: a.x - s / 2.5, y: a.y + s }, { x: a.x - s, y: a.y + s }];
      return geom([], [{ pts, alpha: 1 }]);
    },
  },
  icon: { label: "Icon", icon: "icon", points: 1, width: 1, geom: ([a]) => geom([], [{ pts: star(a, 10), alpha: 1 }]) },
  signpost: {
    label: "Signpost", icon: "signpost", points: 1, text: "Signpost", width: 1,
    geom: ([a], d) => geom([{ pts: [a, { x: a.x, y: a.y - 28 }] }], [], [textBox(a, label(d, "Signpost"), { align: "center", above: 28 })]),
  },
  balloon: {
    label: "Balloon", icon: "balloon", points: 1, text: "Comment", width: 1,
    geom: ([a], d) => geom([{ pts: [a, { x: a.x - 6, y: a.y - 12 }, { x: a.x + 6, y: a.y - 12 }], closed: true }], [], [textBox(a, label(d, "Comment"), { align: "center", above: 12 })]),
  },
  anchoredText: {
    label: "Anchored text", icon: "anchoredText", points: 1, text: "Text", width: 1, anchored: true,
    geom: ([a], d) => geom([], [], [{ x: a.x, y: a.y, text: label(d, "Text"), base: "top", size: d.style.size ?? 14 }]),
  },
  anchoredNote: {
    label: "Anchored note", icon: "anchoredNote", points: 1, text: "Note", width: 1, anchored: true,
    geom: ([a], d) => geom([], [], [{ ...textBox(a, label(d, "Note")), base: "top" }]),
  },
} satisfies Record<string, ShapeDef>;

export type ShapeKind = keyof typeof SHAPES;

export const shapeOf = (kind: string): ShapeDef | null => (kind in SHAPES ? (SHAPES as Record<string, ShapeDef>)[kind] : null);

/** Anchored tools keep a place on the pane (fractions of its width and height), not a time and price. */
export function anchorXY(d: Drawing, g: { w: number; h: number }): XY {
  const props = d.props as { ax?: number; ay?: number } | null;
  return { x: (props?.ax ?? 0.1) * g.w, y: (props?.ay ?? 0.1) * g.h };
}

// ---- Hit testing ----------------------------------------------------------------------------

function distToSegment(p: XY, a: XY, b: XY): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  const l2 = dx * dx + dy * dy;
  const k = l2 ? Math.max(0, Math.min(1, ((p.x - a.x) * dx + (p.y - a.y) * dy) / l2)) : 0;
  return Math.hypot(p.x - (a.x + k * dx), p.y - (a.y + k * dy));
}

function inPolygon(p: XY, poly: XY[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i++) {
    const [a, b] = [poly[i], poly[j]];
    if (a.y > p.y !== b.y > p.y && p.x < ((b.x - a.x) * (p.y - a.y)) / (b.y - a.y) + a.x) inside = !inside;
  }
  return inside;
}

/** A label's box, roughly: enough to click on it. */
export function labelBox(l: GeomLabel): { x: number; y: number; w: number; h: number } {
  const size = l.size ?? 12;
  const w = l.text.length * size * 0.58 + (l.pill || l.box ? 12 : 0);
  const h = size * 1.4 + (l.pill || l.box ? 6 : 0);
  const x = l.align === "center" ? l.x - w / 2 : l.align === "right" ? l.x - w : l.x;
  const y = l.base === "top" ? l.y : l.base === "middle" ? l.y - h / 2 : l.y - h;
  return { x, y, w, h };
}

export function hitsGeom(g: Geom, p: XY, slack = 6): boolean {
  for (const l of g.lines) {
    const pts = l.closed ? [...l.pts, l.pts[0]] : l.pts;
    const s = slack + (l.width ?? 0) / 2;
    for (let i = 1; i < pts.length; i++) if (distToSegment(p, pts[i - 1], pts[i]) <= s) return true;
  }
  for (const f of g.fills) if (f.pts.length > 2 && inPolygon(p, f.pts)) return true;
  for (const l of g.labels) {
    const b = labelBox(l);
    if (p.x >= b.x - 2 && p.x <= b.x + b.w + 2 && p.y >= b.y - 2 && p.y <= b.y + b.h + 2) return true;
  }
  return false;
}

// ---- The tool menus, after TradeCanvas's sidebar ----------------------------------------------

export interface MenuTool {
  tool: string; // a drawing kind
  label: string;
  icon: TcIconName;
  keys?: string;
  heading?: string; // starts a section of the menu
  disabled?: string; // why it can't be used here
}

const S = (kind: ShapeKind, heading?: string): MenuTool => ({ tool: kind, label: SHAPES[kind].label, icon: SHAPES[kind].icon, heading });

export const TOOL_GROUPS: { id: string; name: string; tools: MenuTool[] }[] = [
  {
    id: "lines",
    name: "Lines",
    tools: [
      { tool: "trendline", label: "Trend line", icon: "trendLine", keys: "Alt+T", heading: "Lines" },
      S("ray"), S("extendedLine"), S("infoLine"), S("trendAngle"),
      { tool: "hline", label: "Horizontal line", icon: "horizontalLine", keys: "Alt+H", heading: "Horizontal / vertical" },
      S("horizontalRay"), S("verticalLine"), S("crossLine"),
      S("parallelChannel", "Channels"), S("regression"), S("disjointChannel"), S("flatTopBottom"),
    ],
  },
  {
    id: "fibonacci",
    name: "Fibonacci",
    tools: [
      S("fibRetracement", "Fibonacci"), S("fibExtension"), S("fibChannel"), S("fibTimeZones"), S("fibFan"), S("fibArcs"),
      S("fibCircles"), S("fibSpiral"), S("fibWedge"),
      S("pitchfork", "Gann and pitchforks"), S("schiffPitchfork"), S("modSchiffFork"), S("gannFan"), S("gannBox"), S("gannSquare"), S("pitchfan"),
      S("cyclicLines", "Cycles"), S("timeCycles"), S("sineLine"),
    ],
  },
  {
    id: "patterns",
    name: "Patterns",
    tools: [
      S("xabcd", "Patterns"), S("abcd"), S("headShoulders"), S("cypher"), S("threeDrives"),
      S("elliottWave", "Elliott waves"), S("elliottImpulse"), S("elliottCorrect"), S("elliottTriangle"), S("elliottDouble"), S("elliottTriple"),
    ],
  },
  {
    id: "forecasting",
    name: "Forecasting",
    tools: [
      { tool: "long", label: "Long position", icon: "riskReward", heading: "Forecasting" },
      { tool: "short", label: "Short position", icon: "trendingDown" },
      { tool: "anchoredVWAP", label: "Anchored VWAP", icon: "anchoredVWAP", disabled: "Needs volume, which this feed doesn't send" },
      { tool: "volumeProfile", label: "Fixed range volume profile", icon: "volumeProfileRange", disabled: "Needs volume, which this feed doesn't send" },
      S("forecast"), S("projection"), S("barsPattern"),
      S("priceRange", "Measure"), S("dateRange"), S("datePriceRange"), S("measure"),
    ],
  },
  {
    id: "shapes",
    name: "Shapes",
    tools: [
      { tool: "rect", label: "Rectangle", icon: "rectangle", keys: "Alt+B", heading: "Shapes" },
      S("rotatedRect"), S("circle"), S("ellipse"), S("triangle"), S("polyline"), S("curve"), S("arc"),
      S("brush", "Brushes"), S("highlighter"),
      { tool: "path", label: "Path", icon: "path" },
    ],
  },
  {
    id: "annotation",
    name: "Annotation",
    tools: [
      { tool: "text", label: "Text", icon: "text", heading: "Annotation" },
      S("priceLabel"), S("arrow"), S("note"), S("callout"), S("flag"), S("arrowMark"), S("icon"), S("signpost"), S("balloon"),
      S("anchoredText"), S("anchoredNote"),
    ],
  },
];

/**
 * Full screen layouts: a grid of columns and rows, each track sized by a share of the whole.
 * A "big" layout gives the first pane the whole first column and stacks the rest beside it.
 */

export type LayoutId = "1" | "2" | "3c" | "3r" | "1+2" | "1+3" | "4" | "6";

export interface LayoutSpec {
  id: LayoutId;
  title: string;
  cols: number;
  rows: number;
  big?: boolean;
}

export const LAYOUTS: LayoutSpec[] = [
  { id: "1", title: "One chart", cols: 1, rows: 1 },
  { id: "2", title: "Two charts, side by side", cols: 2, rows: 1 },
  { id: "3c", title: "Three columns", cols: 3, rows: 1 },
  { id: "3r", title: "Three rows", cols: 1, rows: 3 },
  { id: "1+2", title: "One big chart, two stacked beside it", cols: 2, rows: 2, big: true },
  { id: "1+3", title: "One big chart, three stacked beside it", cols: 2, rows: 3, big: true },
  { id: "4", title: "Four charts, 2 × 2", cols: 2, rows: 2 },
  { id: "6", title: "Six charts, 3 columns × 2 rows", cols: 3, rows: 2 },
];

export const MAX_PANES = 6;
export const MIN_SHARE = 0.1; // no track narrower than this

/** A layout from a saved value: the ids, or the old 1 / 2 / 4 pane counts. */
export const layoutOf = (saved: unknown): LayoutSpec =>
  LAYOUTS.find((l) => l.id === String(saved)) ?? LAYOUTS[1];

export const paneCount = (l: LayoutSpec) => (l.big ? 1 + l.rows * (l.cols - 1) : l.cols * l.rows);

/** Each pane's grid cell (1-based), in reading order. */
export function paneCells(l: LayoutSpec): { col: number; row: number; rowSpan: number }[] {
  const cells = [];
  if (l.big) {
    cells.push({ col: 1, row: 1, rowSpan: l.rows });
    for (let r = 1; r <= l.rows; r++) for (let c = 2; c <= l.cols; c++) cells.push({ col: c, row: r, rowSpan: 1 });
    return cells;
  }
  for (let r = 1; r <= l.rows; r++) for (let c = 1; c <= l.cols; c++) cells.push({ col: c, row: r, rowSpan: 1 });
  return cells;
}

/** Track sizes as shares of the width (cols) and height (rows); each list sums to 1. */
export interface Sizes {
  cols: number[];
  rows: number[];
}

const even = (n: number) => Array.from({ length: n }, () => 1 / n);

/** The default sizes: even, except the big pane, which takes 62% of the width. */
export const defaultSizes = (l: LayoutSpec): Sizes => ({ cols: l.big ? [0.62, 0.38] : even(l.cols), rows: even(l.rows) });

const valid = (v: unknown, n: number): v is number[] =>
  Array.isArray(v) && v.length === n && v.every((x) => typeof x === "number" && x >= MIN_SHARE - 1e-9) &&
  Math.abs(v.reduce((a, b) => a + b, 0) - 1) < 1e-6;

/** The saved sizes for a layout when they fit it, else the defaults. */
export function sizesFor(l: LayoutSpec, saved: Partial<Record<LayoutId, Sizes>>): Sizes {
  const s = saved[l.id];
  const d = defaultSizes(l);
  return { cols: valid(s?.cols, l.cols) ? s!.cols : d.cols, rows: valid(s?.rows, l.rows) ? s!.rows : d.rows };
}

/** Where the lines between tracks are, as shares from the start. */
export function boundaries(shares: number[]): number[] {
  const out: number[] = [];
  let at = 0;
  for (const s of shares.slice(0, -1)) out.push((at += s));
  return out;
}

/** Move the line after track i to `at` (a share from the start), resizing only its two neighbours. */
export function moveBoundary(shares: number[], i: number, at: number): number[] {
  const before = shares.slice(0, i).reduce((a, b) => a + b, 0);
  const after = before + shares[i] + shares[i + 1];
  const pos = Math.min(after - MIN_SHARE, Math.max(before + MIN_SHARE, at));
  const out = [...shares];
  out[i] = pos - before;
  out[i + 1] = after - pos;
  return out;
}

/** A grid template for the shares. */
export const template = (shares: number[]) => shares.map((s) => `minmax(0, ${s.toFixed(4)}fr)`).join(" ");

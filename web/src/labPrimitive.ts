import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesApi,
  ISeriesPrimitive,
  Logical,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";

import type { LabLabel, LabShape, LabSuggestion, MlBlock } from "./api";
import type { Role } from "./format";
import { withAlpha, type ChartPalette } from "./theme";

/**
 * Lab marks on the price pane, drawn from the candles they cover:
 * - label:      your tag, filled in the tag's color
 * - not:        marked "not this tag", a muted outline with a cross
 * - suggestion: what the detector found, dotted muted outline
 * - prediction: what the model found, dashed outline in the tag's color and its probability;
 *               once reviewed, solid (valid) or muted (invalid)
 * Lines (liquidity, IDM) run a few candles to the right so they read as levels.
 * Reviewed ranges are a strip along the bottom; the selection is an accent band.
 */
export interface LabMark {
  id: string;
  kind: "label" | "not" | "suggestion" | "prediction";
  role: Role;
  shape: LabShape;
  start: number; // open time of the first candle
  end: number; // open time of the last candle
  top: number;
  bottom: number;
  text: string;
  verdict?: "valid" | "invalid" | null;
}

export const TAG_ROLE: Record<string, Role> = {
  ob_bull: "bull", ob_bear: "bear", bsl: "liquidity", ssl: "liquidity", idm_bull: "idm", idm_bear: "idm",
};
export const TAG_SHAPE: Record<string, LabShape> = {
  ob_bull: "body", ob_bear: "body", bsl: "high", ssl: "low", idm_bull: "low", idm_bear: "high",
};
export const TAG_TITLE: Record<string, string> = {
  ob_bull: "OB bull", ob_bear: "OB bear", bsl: "BSL", ssl: "SSL", idm_bull: "IDM bull", idm_bear: "IDM bear",
};

export const pct = (p: number) => `${Math.round(p * 100)}%`;

export function labelMark(l: LabLabel, candles: Map<number, { open: number; high: number; low: number; close: number }>): LabMark | null {
  const shape = TAG_SHAPE[l.tag];
  let top = l.top;
  let bottom = l.bottom;
  if (top === null || bottom === null) {
    const inRange = [...candles.entries()].filter(([t]) => t >= l.start && t <= l.end).map(([, c]) => c);
    if (!inRange.length) return null;
    if (shape === "high") top = bottom = Math.max(...inRange.map((c) => c.high));
    else if (shape === "low") top = bottom = Math.min(...inRange.map((c) => c.low));
    else {
      top = Math.max(...inRange.map((c) => Math.max(c.open, c.close)));
      bottom = Math.min(...inRange.map((c) => Math.min(c.open, c.close)));
    }
  }
  return {
    id: l.id, kind: l.value ? "label" : "not", role: TAG_ROLE[l.tag], shape, start: l.start, end: l.end, top, bottom,
    text: `${l.value ? "" : "not "}${TAG_TITLE[l.tag]}`,
  };
}

export const suggestionMark = (s: LabSuggestion): LabMark => ({
  id: s.id, kind: "suggestion", role: TAG_ROLE[s.tag], shape: TAG_SHAPE[s.tag], start: s.time_unix, end: s.time_unix,
  top: s.top, bottom: s.bottom, text: s.label,
});

export const predictionMark = (b: MlBlock): LabMark => ({
  id: b.id, kind: "prediction", role: TAG_ROLE[b.tag], shape: TAG_SHAPE[b.tag], start: b.time_unix, end: b.time_unix,
  top: b.top, bottom: b.bottom, verdict: b.verdict ?? null,
  text: `ML ${b.title} ${pct(b.prob)}${b.outcome_prob !== null ? ` · win ${pct(b.outcome_prob)}` : ""}`,
});

function indexOf(times: number[], t: number): number {
  let lo = 0;
  let hi = times.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo < times.length && times[lo] === t ? lo : Math.max(0, lo - 1);
}

const LINE_BARS = 6; // how far a level line runs past its candle

class LabRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly source: LabPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, series, marks, reviewed, selection, times, palette, font, highlight } = this.source;
    if (!chart || !series || times.length < 2) return;
    const ts = chart.timeScale();
    const x0 = ts.logicalToCoordinate(0 as Logical);
    const x1 = ts.logicalToCoordinate(1 as Logical);
    if (x0 === null || x1 === null) return;
    const step = x1 - x0;
    const xAt = (i: number) => x0 + i * step;
    const colorOf = (r: Role) => (r === "bull" ? palette.bull : r === "bear" ? palette.bear : r === "liquidity" ? palette.liquidity : palette.idm);

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const H = bitmapSize.height;
      const span = (start: number, end: number, extend = 0) => {
        const a = indexOf(times, start);
        const b = indexOf(times, end) + extend;
        return [Math.round((xAt(a) - step / 2) * hr), Math.round((xAt(b) + step / 2) * hr)] as const;
      };
      const yOf = (p: number) => {
        const y = series.priceToCoordinate(p);
        return y === null ? null : Math.round(y * vr);
      };
      const lw = Math.max(1, Math.round(hr));
      ctx.font = `600 ${Math.round(10 * vr)}px ${font}`;
      ctx.textBaseline = "bottom";

      // Reviewed ranges: a strip along the bottom edge.
      for (const r of reviewed) {
        const [l, rt] = span(r.start, r.end);
        ctx.fillStyle = withAlpha(palette.muted, 0.28);
        ctx.fillRect(l, H - 5 * vr, rt - l, 5 * vr);
      }

      if (selection) {
        const [l, rt] = span(selection.start, selection.end);
        ctx.fillStyle = withAlpha(palette.accent, 0.13);
        ctx.fillRect(l, 0, rt - l, H);
        ctx.strokeStyle = withAlpha(palette.accent, 0.8);
        ctx.lineWidth = lw;
        ctx.strokeRect(l + 0.5, 0.5, rt - l - 1, H - 1);
      }

      const order = { suggestion: 0, not: 1, label: 2, prediction: 3 } as const;
      const texts: { m: LabMark; x: number; y: number; color: string }[] = [];
      for (const m of [...marks].sort((a, b) => order[a.kind] - order[b.kind])) {
        const line = m.shape !== "body";
        const [l, rt] = span(m.start, m.end, line ? LINE_BARS : 0);
        const yt = yOf(m.top);
        const yb = yOf(m.bottom);
        if (yt === null || yb === null) continue;
        const dim = highlight && highlight !== m.id ? 0.35 : 1;
        const color = m.kind === "not" || m.kind === "suggestion" || m.verdict === "invalid" ? palette.muted : colorOf(m.role);
        const top = Math.min(yt, yb);
        const h = Math.max(line ? 0 : 2 * vr, Math.abs(yb - yt));
        ctx.globalAlpha = dim;
        ctx.lineWidth = (m.kind === "prediction" && m.verdict !== "invalid") || highlight === m.id ? 2 * lw : lw;
        ctx.setLineDash(m.kind === "suggestion" ? [2 * hr, 3 * hr] : m.kind === "prediction" && !m.verdict ? [5 * hr, 3 * hr] : []);
        ctx.strokeStyle = withAlpha(color, m.kind === "suggestion" ? 0.8 : 1);
        if (line) {
          ctx.beginPath();
          ctx.moveTo(l, top + 0.5);
          ctx.lineTo(rt, top + 0.5);
          ctx.stroke();
        } else {
          if (m.kind === "label") {
            ctx.fillStyle = withAlpha(color, 0.3);
            ctx.fillRect(l, top, rt - l, h);
          }
          ctx.strokeRect(l + 0.5, top + 0.5, rt - l - 1, h - 1);
          if (m.kind === "not") {
            ctx.beginPath();
            ctx.moveTo(l, top);
            ctx.lineTo(rt, top + h);
            ctx.moveTo(rt, top);
            ctx.lineTo(l, top + h);
            ctx.stroke();
          }
        }
        ctx.setLineDash([]);
        ctx.globalAlpha = 1;
        texts.push({ m, x: l, y: top - 3 * vr, color: m.kind === "prediction" && m.verdict !== "invalid" ? palette.textStrong : palette.text });
      }

      // Names go on only where they fit. The highlighted mark and the ones on the selection first,
      // then model output, then labels; suggestions only when highlighted. With the candles
      // packed tight, only the highlighted and selected marks get a name.
      const inSel = (m: LabMark) => !!selection && m.start <= selection.end && selection.start <= m.end;
      const rank = (m: LabMark) => (m.id === highlight ? 0 : inSel(m) ? 1 : m.kind === "prediction" ? 2 : 3);
      const roomy = step >= 6; // css px per candle
      const taken: [number, number, number, number][] = [];
      const fontPx = Math.round(10 * vr);
      for (const t of texts.sort((a, b) => rank(a.m) - rank(b.m))) {
        const r = rank(t.m);
        if (r > 1 && (!roomy || t.m.kind === "suggestion")) continue;
        const text = t.m.verdict ? `${t.m.verdict === "valid" ? "✓" : "✕"} ${t.m.text}` : t.m.text;
        const w = ctx.measureText(text).width;
        const box: [number, number, number, number] = [t.x, t.y - fontPx, t.x + w, t.y];
        if (taken.some((b) => box[0] < b[2] && b[0] < box[2] && box[1] < b[3] && b[1] < box[3])) continue;
        taken.push(box);
        ctx.fillStyle = withAlpha(palette.surface, 0.85);
        ctx.fillRect(box[0] - 2 * hr, box[1] - 1 * vr, w + 4 * hr, fontPx + 2 * vr);
        ctx.fillStyle = t.color;
        ctx.fillText(text, t.x, t.y);
      }
    });
  }
}

class LabView implements IPrimitivePaneView {
  constructor(private readonly source: LabPrimitive) {}
  zOrder(): "top" {
    return "top";
  }
  renderer(): IPrimitivePaneRenderer {
    return new LabRenderer(this.source);
  }
}

export class LabPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  marks: LabMark[] = [];
  reviewed: { start: number; end: number }[] = [];
  selection: { start: number; end: number } | null = null;
  highlight: string | null = null;
  times: number[] = [];
  font = "system-ui, sans-serif";
  private requestUpdate?: () => void;
  private readonly views = [new LabView(this)];

  constructor(public palette: ChartPalette) {}

  attached(param: SeriesAttachedParameter<Time, SeriesType>): void {
    this.chart = param.chart as IChartApi;
    this.series = param.series;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  update(patch: Partial<Pick<LabPrimitive, "marks" | "reviewed" | "selection" | "highlight" | "times" | "palette" | "font">>): void {
    Object.assign(this, patch);
    this.requestUpdate?.();
  }
}

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

import type { Role } from "./format";
import { withAlpha, type ChartPalette } from "./theme";

/**
 * Something drawn on the chart. Marks:
 * - box:     order block body (bull/bear), optional dashed capped stop
 * - line:    liquidity (solid; 2px for equal pools) or IDM (dotted), to the right edge
 * - setup:   a limit setup from its order block candle: solid entry line, dashed stop, tinted risk band, pill with its id
 * - segment: a break of structure, from the broken swing to the break candle
 *
 * Every mark starts at the bar holding the level's candle; a level from a lower
 * timeframe starts at the bar that contains its candle.
 *
 * Pinned zones (the ones listed next to the chart) always carry a pill; when
 * they sit outside the visible price range the pill docks to the top/bottom edge.
 */
export interface Zone {
  id: string;
  role: Role | "structure";
  mark: "box" | "line" | "setup" | "segment";
  top: number;
  bottom: number;
  startTime: number; // unix seconds (feed clock)
  endTime?: number; // segment only
  label: string; // canvas label; context zones only show it when there is room
  tag?: string; // pill text for pinned zones, e.g. "S1"
  stop?: number; // setup stop, or a capped stop inside an OB box
  entry?: number; // setup entry
  faded?: boolean; // higher timeframe context
  weak?: boolean; // mid (low priority) order block
  strong?: boolean; // equal highs/lows pool
  pinned?: boolean;
}

function indexAtOrAfter(times: number[], t: number): number {
  let lo = 0;
  let hi = times.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (times[mid] < t) lo = mid + 1;
    else hi = mid;
  }
  return lo;
}

/** Index of the bar that contains time t (a lower-timeframe level falls inside a bigger bar). */
function barIndexOf(times: number[], t: number): number {
  const i = indexAtOrAfter(times, t);
  return i < times.length && times[i] === t ? i : Math.max(0, i - 1);
}

function colorOf(role: Zone["role"], p: ChartPalette): string {
  switch (role) {
    case "bull":
      return p.bull;
    case "bear":
      return p.bear;
    case "liquidity":
      return p.liquidity;
    case "idm":
      return p.idm;
    default:
      return p.muted;
  }
}

type Rect = { x: number; y: number; w: number; h: number };
const overlaps = (a: Rect, b: Rect) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/** Two passes over the same geometry: shapes under the candles, pills and labels above them. */
class ZonesRenderer implements IPrimitivePaneRenderer {
  constructor(
    private readonly source: ZonesPrimitive,
    private readonly layer: "shapes" | "text",
  ) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, series, zones, times, palette, highlight, formatPrice } = this.source;
    if (!chart || !series || times.length === 0) return;
    const timeScale = chart.timeScale();

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const W = bitmapSize.width;
      const H = bitmapSize.height;
      const px = (v: number) => Math.round(v * hr);
      const py = (v: number) => Math.round(v * vr);
      const xOf = (t: number) => Math.max(0, px(timeScale.logicalToCoordinate(barIndexOf(times, t) as Logical) ?? 0));
      const yOf = (price: number) => {
        const y = series.priceToCoordinate(price);
        return y === null ? null : py(y);
      };
      // With something highlighted, everything else steps back.
      const dim = (z: Zone) => (highlight && z.id !== highlight ? 0.28 : 1);
      const lw = Math.max(1, Math.round(hr));
      const shapes = this.layer === "shapes";

      const hline = (x0: number, x1: number, y: number, width: number, dash: number[]) => {
        if (!shapes) return;
        ctx.lineWidth = width;
        ctx.setLineDash(dash);
        ctx.beginPath();
        ctx.moveTo(x0, y + 0.5);
        ctx.lineTo(x1, y + 0.5);
        ctx.stroke();
        ctx.setLineDash([]);
      };

      const order = [...zones].sort(
        (a, b) => Number(!!b.faded) - Number(!!a.faded) || Number(!!a.pinned) - Number(!!b.pinned) ||
          Number(a.id === highlight) - Number(b.id === highlight),
      );

      const labels: { z: Zone; x: number; y: number; h: number }[] = [];
      const pills: { z: Zone; y: number; off: -1 | 0 | 1 }[] = [];

      for (const z of order) {
        const color = colorOf(z.role, palette);
        const k = dim(z);
        // A setup keeps its pill even when its order block candle is scrolled off to the right.
        const x = Math.min(xOf(z.startTime), W);
        if (x >= W && z.mark !== "setup") continue;

        if (z.mark === "segment") {
          const y = yOf(z.top);
          if (y === null || y < 0 || y > H) continue;
          const x1 = z.endTime !== undefined ? xOf(z.endTime) : W;
          ctx.strokeStyle = withAlpha(palette.text, 0.7 * k);
          hline(x, x1, y, lw, [4 * hr, 3 * hr]);
          labels.push({ z, x: (x + x1) / 2 - 12 * hr, y, h: 0 });
          continue;
        }

        if (z.mark === "setup") {
          const ye = yOf(z.entry ?? z.top);
          const ys = z.stop !== undefined ? yOf(z.stop) : null;
          if (ye === null) continue;
          const onScreen = ye >= 0 && ye <= H;
          if (onScreen) {
            if (ys !== null) {
              ctx.fillStyle = withAlpha(color, (z.id === highlight ? 0.22 : 0.1) * k);
              if (shapes) ctx.fillRect(x, Math.min(ye, ys), W - x, Math.max(1, Math.abs(ys - ye)));
              ctx.strokeStyle = withAlpha(color, 0.8 * k);
              hline(x, W, ys, lw, [3 * hr, 3 * hr]);
            }
            ctx.strokeStyle = withAlpha(color, k);
            hline(x, W, ye, (z.id === highlight ? 2 : 1.5) * lw, []);
          }
          pills.push({ z, y: ye, off: ye < 0 ? -1 : ye > H ? 1 : 0 });
          continue;
        }

        const yTop = yOf(z.top);
        const yBottom = yOf(z.bottom);
        if (yTop === null || yBottom === null) continue;
        const y = Math.min(yTop, yBottom);
        const h = Math.abs(yBottom - yTop);

        if (z.mark === "box" && shapes) {
          const fill = z.faded ? 0.07 : z.weak ? 0.09 : 0.2;
          ctx.fillStyle = withAlpha(color, (z.id === highlight ? 0.34 : fill) * k);
          ctx.fillRect(x, y, W - x, Math.max(1, h));
          ctx.strokeStyle = withAlpha(color, (z.faded ? 0.4 : z.weak ? 0.5 : 0.9) * k);
          ctx.lineWidth = lw;
          ctx.setLineDash(z.faded ? [4 * hr, 3 * hr] : []);
          ctx.strokeRect(x + 0.5, y + 0.5, W - x - 1, Math.max(1, h) - 1);
          ctx.setLineDash([]);
          if (z.stop !== undefined) {
            const ys = yOf(z.stop);
            if (ys !== null) hline(x, W, ys, lw, [3 * hr, 3 * hr]);
          }
        } else if (z.mark === "line") {
          if (h > 1 && shapes) {
            ctx.fillStyle = withAlpha(color, (z.faded ? 0.04 : 0.1) * k);
            ctx.fillRect(x, y, W - x, h);
          }
          ctx.strokeStyle = withAlpha(color, (z.faded ? 0.45 : 0.95) * k);
          const dash = z.role === "idm" ? [1.5 * hr, 3 * hr] : z.faded ? [6 * hr, 4 * hr] : [];
          const width = (z.strong || z.id === highlight ? 2 : 1) * lw;
          for (const ly of h > 1 ? [y, y + h] : [y]) hline(x, W, ly, width, dash);
        }

        if (z.pinned) pills.push({ z, y: yTop, off: y + h < 0 ? -1 : y > H ? 1 : 0 });
        else if (y + h >= 0 && y <= H) labels.push({ z, x, y, h });
      }

      if (shapes) return;

      const fontPx = Math.round(11 * vr);
      const lh = fontPx + 6 * vr;
      const placed: Rect[] = [];

      // Pills first: they carry the ids that tie the chart to the level list.
      ctx.font = `600 ${fontPx}px ${this.source.font}`;
      ctx.textBaseline = "middle";
      const edgeStack = { top: 4 * vr, bottom: H - 4 * vr };
      // Lay pills out in price order (top to bottom) so each stays next to its own line;
      // an overlapping pill is pushed down just enough. The highlighted one is drawn last.
      const laid = [...pills]
        .sort((a, b) => a.y - b.y)
        .map(({ z, y, off }) => {
          const text = off === 0 ? z.tag ?? z.label : `${z.tag ?? z.label} ${formatPrice(z.entry ?? z.top)}`;
          const arrow = off === 0 ? 0 : 10 * hr;
          const w = ctx.measureText(text).width + 12 * hr + arrow;
          const x = W - w - 6 * hr;
          let top: number;
          if (off === -1) {
            top = edgeStack.top;
            edgeStack.top += lh + 3 * vr;
          } else if (off === 1) {
            top = 0; // placed below, after the on-screen pills
          } else {
            top = y - lh / 2;
            while (placed.some((p) => overlaps(p, { x, y: top, w, h: lh }))) top += 2 * vr;
          }
          const rect = { x, y: top, w, h: lh };
          if (off !== 1) placed.push(rect);
          return { z, off, text, arrow, rect };
        });
      for (const item of [...laid].reverse().filter((i) => i.off === 1)) {
        edgeStack.bottom -= lh;
        item.rect.y = edgeStack.bottom;
        edgeStack.bottom -= 3 * vr;
        placed.push(item.rect);
      }
      laid.sort((a, b) => Number(a.z.id === highlight) - Number(b.z.id === highlight));

      for (const { z, off, text, arrow, rect } of laid) {
        const { x, y: py0 } = rect;
        const color = colorOf(z.role, palette);
        const hot = z.id === highlight;
        ctx.fillStyle = hot ? color : palette.surface;
        ctx.strokeStyle = withAlpha(color, dim(z));
        ctx.lineWidth = lw;
        ctx.beginPath();
        ctx.roundRect(rect.x + 0.5, rect.y + 0.5, rect.w - 1, rect.h - 1, 4 * hr);
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = hot ? palette.surface : withAlpha(palette.textStrong, dim(z));
        ctx.fillText(text, x + 6 * hr + arrow, py0 + lh / 2 + 0.5 * vr);
        if (arrow) {
          // Small triangle pointing to where the level is.
          const cx = x + 6 * hr + 3 * hr;
          const cy = py0 + lh / 2;
          ctx.beginPath();
          if (off === -1) {
            ctx.moveTo(cx - 3 * hr, cy + 2 * vr);
            ctx.lineTo(cx + 3 * hr, cy + 2 * vr);
            ctx.lineTo(cx, cy - 3 * vr);
          } else {
            ctx.moveTo(cx - 3 * hr, cy - 2 * vr);
            ctx.lineTo(cx + 3 * hr, cy - 2 * vr);
            ctx.lineTo(cx, cy + 3 * vr);
          }
          ctx.closePath();
          ctx.fill();
        }
      }

      // Context labels: only where they fit.
      ctx.font = `${fontPx}px ${this.source.font}`;
      ctx.textBaseline = "top";
      const labelH = fontPx + 2 * vr;
      for (const { z, x, y, h } of labels) {
        if (highlight && z.id !== highlight && z.mark !== "segment") continue;
        const tw = ctx.measureText(z.label).width;
        const lx = Math.max(4 * hr, Math.min(x + 6 * hr, W - tw - 4 * hr));
        for (const ly of [y - labelH - 1 * vr, y + Math.max(h, 1) + 2 * vr]) {
          const rect = { x: lx - 2 * hr, y: ly, w: tw + 4 * hr, h: labelH };
          if (ly < 0 || ly + labelH > H || placed.some((p) => overlaps(p, rect))) continue;
          placed.push(rect);
          ctx.fillStyle = withAlpha(palette.surface, 0.85);
          ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
          ctx.fillStyle = z.faded ? palette.muted : palette.text;
          ctx.fillText(z.label, lx, ly + 1 * vr);
          break;
        }
      }
    });
  }
}

class ZonesPaneView implements IPrimitivePaneView {
  private readonly rendererInstance: ZonesRenderer;

  constructor(
    source: ZonesPrimitive,
    private readonly layer: "shapes" | "text",
  ) {
    this.rendererInstance = new ZonesRenderer(source, layer);
  }

  zOrder() {
    return this.layer === "shapes" ? ("bottom" as const) : ("top" as const);
  }

  renderer() {
    return this.rendererInstance;
  }
}

export class ZonesPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  zones: Zone[] = [];
  times: number[] = [];
  highlight: string | null = null;
  palette: ChartPalette;
  font = "system-ui, sans-serif";
  formatPrice: (v: number) => string = (v) => v.toFixed(2);
  private requestUpdate: (() => void) | null = null;
  private readonly views = [new ZonesPaneView(this, "shapes"), new ZonesPaneView(this, "text")];

  constructor(palette: ChartPalette) {
    this.palette = palette;
  }

  attached({ chart, series, requestUpdate }: SeriesAttachedParameter<Time>): void {
    this.chart = chart;
    this.series = series;
    this.requestUpdate = requestUpdate;
  }

  detached(): void {
    this.chart = null;
    this.series = null;
    this.requestUpdate = null;
  }

  paneViews() {
    return this.views;
  }

  update(patch: Partial<Pick<ZonesPrimitive, "zones" | "times" | "palette" | "highlight" | "font" | "formatPrice">>): void {
    Object.assign(this, patch);
    this.requestUpdate?.();
  }
}

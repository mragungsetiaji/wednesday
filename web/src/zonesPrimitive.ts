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
 * One level to draw. Mark by role:
 * - bull / bear (order blocks): filled box
 * - liquidity: solid line (2px for equal-high/low pools, with a light band if it spans a range)
 * - idm: dotted neutral line
 * Every mark carries a text label, so identity never depends on color alone.
 */
export interface Zone {
  role: Role;
  top: number;
  bottom: number;
  startTime: number; // unix seconds of the origin candle
  label: string;
  faded: boolean; // levels from higher timeframes are drawn lighter/dashed
  strong?: boolean; // equal-high/low pools
  weak?: boolean; // low-priority (mid) order blocks: lighter fill
  stop?: number; // capped stop inside an order block box, drawn as a dashed line
}

/** First candle index whose time is >= t (candle times are ascending). */
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

const colorOf = (role: Role, p: ChartPalette) =>
  role === "bull" ? p.bull : role === "bear" ? p.bear : role === "liquidity" ? p.liquidity : p.idm;

// Priority for labels when they collide: order blocks, then liquidity, then IDM.
const RANK: Record<Role, number> = { bull: 0, bear: 0, liquidity: 1, idm: 2 };

class ZonesRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly source: ZonesPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, series, zones, times, palette } = this.source;
    if (!chart || !series || times.length === 0) return;
    const timeScale = chart.timeScale();

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const drawn: { z: Zone; x: number; y: number; h: number }[] = [];
      // Faded (higher timeframe) levels first so the selected timeframe sits on top.
      for (const z of [...zones].sort((a, b) => Number(b.faded) - Number(a.faded))) {
        const yTop = series.priceToCoordinate(z.top);
        const yBottom = series.priceToCoordinate(z.bottom);
        if (yTop === null || yBottom === null) continue;
        // Map the origin time to a bar slot so levels from other timeframes line up too.
        const xStart = timeScale.logicalToCoordinate(indexAtOrAfter(times, z.startTime) as Logical) ?? 0;
        const x = Math.max(0, Math.round(xStart * hr));
        const y = Math.round(Math.min(yTop, yBottom) * vr);
        const h = Math.round(Math.abs(yBottom - yTop) * vr);
        const w = bitmapSize.width - x;
        if (w <= 0) continue;
        const color = colorOf(z.role, palette);
        const lw = Math.max(1, Math.round(hr));

        if (z.role === "bull" || z.role === "bear") {
          ctx.fillStyle = withAlpha(color, z.faded ? 0.08 : z.weak ? 0.1 : 0.22);
          ctx.fillRect(x, y, w, Math.max(1, h));
          ctx.strokeStyle = withAlpha(color, z.faded ? 0.45 : z.weak ? 0.55 : 0.95);
          ctx.lineWidth = lw;
          ctx.setLineDash(z.faded ? [4 * hr, 3 * hr] : []);
          ctx.strokeRect(x + 0.5, y + 0.5, w - 1, Math.max(1, h) - 1);
          const ys = z.stop !== undefined ? series.priceToCoordinate(z.stop) : null;
          if (ys !== null) {
            ctx.setLineDash([3 * hr, 3 * hr]);
            ctx.beginPath();
            ctx.moveTo(x, Math.round(ys * vr) + 0.5);
            ctx.lineTo(bitmapSize.width, Math.round(ys * vr) + 0.5);
            ctx.stroke();
          }
        } else {
          if (h > 1) {
            ctx.fillStyle = withAlpha(color, z.faded ? 0.05 : 0.12);
            ctx.fillRect(x, y, w, h);
          }
          ctx.strokeStyle = withAlpha(color, z.faded ? 0.5 : 0.95);
          ctx.lineWidth = (z.strong ? 2 : 1) * lw;
          ctx.setLineDash(z.role === "idm" ? [1.5 * hr, 3 * hr] : z.faded ? [6 * hr, 4 * hr] : []);
          for (const ly of h > 1 ? [y, y + h] : [y]) {
            ctx.beginPath();
            ctx.moveTo(x, ly + 0.5);
            ctx.lineTo(bitmapSize.width, ly + 0.5);
            ctx.stroke();
          }
        }
        ctx.setLineDash([]);
        drawn.push({ z, x, y, h });
      }

      // Labels: selected timeframe first, then by detector rank; skip any that would overlap.
      const fontPx = Math.round(11 * vr);
      ctx.font = `${fontPx}px ui-sans-serif, system-ui, sans-serif`;
      ctx.textBaseline = "top";
      const placed: { x: number; y: number; w: number; h: number }[] = [];
      const pad = 4 * hr;
      const lh = fontPx + 2 * vr;
      const order = [...drawn].sort((a, b) => Number(a.z.faded) - Number(b.z.faded) || RANK[a.z.role] - RANK[b.z.role]);
      for (const { z, x, y, h } of order) {
        const tw = ctx.measureText(z.label).width;
        const lx = Math.max(pad, Math.min(x + 6 * hr, bitmapSize.width - tw - pad));
        // Try above the mark, then below it.
        for (const ly of [y - lh - 1 * vr, y + Math.max(h, 1) + 2 * vr]) {
          const rect = { x: lx - 2 * hr, y: ly, w: tw + 4 * hr, h: lh };
          if (ly < 0 || ly + lh > bitmapSize.height) continue;
          if (placed.some((p) => rect.x < p.x + p.w && p.x < rect.x + rect.w && rect.y < p.y + p.h && p.y < rect.y + rect.h)) continue;
          placed.push(rect);
          ctx.fillStyle = withAlpha(palette.surface, 0.8);
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

  constructor(source: ZonesPrimitive) {
    this.rendererInstance = new ZonesRenderer(source);
  }

  zOrder() {
    return "bottom" as const;
  }

  renderer() {
    return this.rendererInstance;
  }
}

/** Series primitive that draws levels (boxes and lines) extending to the right edge. */
export class ZonesPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  zones: Zone[] = [];
  times: number[] = [];
  palette: ChartPalette;
  private requestUpdate: (() => void) | null = null;
  private readonly views = [new ZonesPaneView(this)];

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

  setData(zones: Zone[], times: number[], palette: ChartPalette): void {
    this.zones = zones;
    this.times = times;
    this.palette = palette;
    this.requestUpdate?.();
  }
}

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

import { withAlpha, type ChartPalette } from "./theme";

export interface Zone {
  kind: "bullish" | "bearish";
  top: number;
  bottom: number;
  startTime: number; // unix seconds of the order block candle
  label: string;
  faded: boolean; // zones from other timeframes are drawn lighter
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

class ZonesRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly source: ZonesPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, series, zones, times, palette } = this.source;
    if (!chart || !series || times.length === 0) return;
    const timeScale = chart.timeScale();

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const boxes: { z: Zone; x: number; y: number; w: number; h: number }[] = [];
      // Faded (other timeframe) zones first so the selected timeframe's boxes sit on top.
      for (const z of [...zones].sort((a, b) => Number(b.faded) - Number(a.faded))) {
        const yTop = series.priceToCoordinate(z.top);
        const yBottom = series.priceToCoordinate(z.bottom);
        if (yTop === null || yBottom === null) continue;
        // Map the OB time to a bar slot so zones from other timeframes line up too.
        const idx = indexAtOrAfter(times, z.startTime);
        const xStart = timeScale.logicalToCoordinate(idx as Logical) ?? 0;
        const x = Math.max(0, Math.round(xStart * hr));
        const y = Math.round(Math.min(yTop, yBottom) * vr);
        const h = Math.max(1, Math.round(Math.abs(yBottom - yTop) * vr));
        const w = bitmapSize.width - x;
        if (w <= 0) continue;

        const color = z.kind === "bullish" ? palette.bull : palette.bear;
        ctx.fillStyle = withAlpha(color, z.faded ? 0.08 : 0.2);
        ctx.fillRect(x, y, w, h);
        ctx.strokeStyle = withAlpha(color, z.faded ? 0.45 : 0.9);
        ctx.lineWidth = Math.max(1, Math.round(hr));
        if (z.faded) ctx.setLineDash([4 * hr, 3 * hr]);
        ctx.strokeRect(x + 0.5, y + 0.5, w - 1, h - 1);
        ctx.setLineDash([]);
        boxes.push({ z, x, y, w, h });
      }

      // Text labels (identity is never carried by color alone). Selected timeframe
      // labels win; any label that would overlap one already drawn is skipped.
      const fontPx = Math.round(11 * vr);
      ctx.font = `${fontPx}px ui-sans-serif, system-ui, sans-serif`;
      ctx.textBaseline = "top";
      const placed: { x: number; y: number; w: number; h: number }[] = [];
      const pad = 4 * hr;
      for (const { z, x, y, h } of [...boxes].reverse()) {
        const tw = ctx.measureText(z.label).width;
        const lh = fontPx + 2 * vr;
        const lx = Math.max(pad, Math.min(x + 6 * hr, bitmapSize.width - tw - pad));
        // Supply labels above the box, demand labels below it: away from price action.
        const ly = z.kind === "bullish" ? y + h + 3 * vr : y - lh - 1 * vr;
        const rect = { x: lx - 2 * hr, y: ly, w: tw + 4 * hr, h: lh };
        if (ly < 0 || ly + lh > bitmapSize.height) continue;
        if (placed.some((p) => rect.x < p.x + p.w && p.x < rect.x + rect.w && rect.y < p.y + p.h && p.y < rect.y + rect.h)) continue;
        placed.push(rect);
        ctx.fillStyle = withAlpha(palette.surface, 0.75);
        ctx.fillRect(rect.x, rect.y, rect.w, rect.h);
        ctx.fillStyle = z.faded ? palette.muted : palette.text;
        ctx.fillText(z.label, lx, ly + 1 * vr);
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

/** Series primitive that draws order block zones as boxes extending to the right edge. */
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

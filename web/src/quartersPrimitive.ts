import type { CanvasRenderingTarget2D } from "fancy-canvas";
import type {
  IChartApi,
  IPrimitivePaneRenderer,
  IPrimitivePaneView,
  ISeriesPrimitive,
  Logical,
  SeriesAttachedParameter,
  SeriesType,
  Time,
} from "lightweight-charts";

import type { QuarterBlock, QuarterRow } from "./api";
import { withAlpha, type ChartPalette } from "./theme";

export const ROW_TITLES: Record<QuarterRow, string> = { week: "Week", session: "Session", q90: "90m" };
const SHORT: Record<string, string> = { Tokyo: "TKY", London: "LDN", "NY AM": "NY", "NY PM": "PM" };

/**
 * Where time t sits on the charted timeframe, in (fractional) bars: bar i spans
 * logical i - 0.5 to i + 0.5, so a 90-minute block inside a 4H bar gets its
 * share of that bar's width. Gaps (weekends) collapse to the bar boundary.
 */
export function logicalOf(times: number[], t: number): number {
  const n = times.length;
  if (n === 0) return 0;
  const step = n > 1 ? times[n - 1] - times[n - 2] : 60;
  if (t <= times[0]) return -0.5 - (times[0] - t) / step;
  let lo = 0;
  let hi = n - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (times[mid] <= t) lo = mid;
    else hi = mid - 1;
  }
  const span = lo + 1 < n ? Math.min(times[lo + 1] - times[lo], step * 1.5) : step;
  return lo - 0.5 + Math.min(1, (t - times[lo]) / span);
}

/**
 * The quarterly theory pane under the price chart: one row per cycle (week,
 * session, 90 minutes), each block tinted by direction (close above open is
 * green) with a solid edge, the running block outlined only.
 */
class QuartersRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly source: QuartersPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, blocks, rows, times, palette, hoverTime, font } = this.source;
    if (!chart || times.length === 0 || rows.length === 0) return;
    const timeScale = chart.timeScale();

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const W = bitmapSize.width;
      const H = bitmapSize.height;
      const pad = Math.round(4 * vr);
      const gap = Math.round(3 * vr);
      const rowH = Math.floor((H - 2 * pad - gap * (rows.length - 1)) / rows.length);
      // logicalToCoordinate only takes whole bars; interpolate between bar 0 and bar 1 instead.
      const x0Bar = timeScale.logicalToCoordinate(0 as Logical);
      const x1Bar = timeScale.logicalToCoordinate(1 as Logical);
      if (x0Bar === null || x1Bar === null) return;
      const xOf = (t: number) => Math.round((x0Bar + logicalOf(times, t) * (x1Bar - x0Bar)) * hr);
      const fontPx = Math.round(10.5 * vr);
      ctx.font = `600 ${fontPx}px ${font}`;
      ctx.textBaseline = "middle";
      ctx.textAlign = "center";

      rows.forEach((row, r) => {
        const y = pad + r * (rowH + gap);
        for (const b of blocks[row] ?? []) {
          let x0 = xOf(b.start_unix);
          let x1 = xOf(b.end_unix);
          if (x1 <= 0 || x0 >= W) continue;
          x0 = Math.max(0, x0) + Math.round(hr); // 1px surface gap on each side
          x1 = Math.min(W, x1) - Math.round(hr);
          const w = x1 - x0;
          if (w < 1) continue;
          const color = b.change > 0 ? palette.bull : b.change < 0 ? palette.bear : palette.muted;
          const hovered = hoverTime !== null && hoverTime >= b.start_unix && hoverTime < b.end_unix;
          const edge = Math.max(1, Math.round(2 * vr));
          if (b.live) {
            ctx.fillStyle = withAlpha(color, hovered ? 0.2 : 0.1);
            ctx.fillRect(x0, y, w, rowH);
            ctx.strokeStyle = withAlpha(color, 0.9);
            ctx.lineWidth = Math.max(1, Math.round(hr));
            ctx.setLineDash([3 * hr, 2 * hr]);
            ctx.strokeRect(x0 + 0.5, y + 0.5, w - 1, rowH - 1);
            ctx.setLineDash([]);
          } else {
            ctx.fillStyle = withAlpha(color, hovered ? 0.5 : 0.3);
            ctx.fillRect(x0, y, w, rowH);
            ctx.fillStyle = color;
            ctx.fillRect(x0, y + rowH - edge, w, edge);
          }
          // Label when it fits: full, then short, then nothing.
          const full = row === "week" ? `${b.label} ${Number(b.day.slice(8))}` : b.label;
          const short = row === "week" ? b.label : SHORT[b.label] ?? b.label;
          const text = [full, short].find((s) => ctx.measureText(s).width + 8 * hr <= w);
          if (text) {
            ctx.fillStyle = palette.textStrong;
            ctx.fillText(text, x0 + w / 2, y + (rowH - edge) / 2);
          }
        }
      });

      // Row titles on a surface chip at the left edge.
      ctx.textAlign = "left";
      ctx.font = `500 ${Math.round(10 * vr)}px ${font}`;
      rows.forEach((row, r) => {
        const y = pad + r * (rowH + gap);
        const title = ROW_TITLES[row];
        const tw = ctx.measureText(title).width + 10 * hr;
        ctx.fillStyle = withAlpha(palette.surface, 0.9);
        ctx.fillRect(0, y, tw, rowH);
        ctx.fillStyle = palette.muted;
        ctx.fillText(title, 5 * hr, y + rowH / 2);
      });
    });
  }
}

class QuartersView implements IPrimitivePaneView {
  constructor(private readonly source: QuartersPrimitive) {}
  zOrder(): "bottom" {
    return "bottom";
  }
  renderer(): IPrimitivePaneRenderer {
    return new QuartersRenderer(this.source);
  }
}

export class QuartersPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  blocks: Partial<Record<QuarterRow, QuarterBlock[]>> = {};
  rows: QuarterRow[] = [];
  times: number[] = [];
  hoverTime: number | null = null;
  font = "system-ui, sans-serif";
  private requestUpdate?: () => void;
  private readonly views = [new QuartersView(this)];

  constructor(public palette: ChartPalette) {}

  attached(param: SeriesAttachedParameter<Time, SeriesType>): void {
    this.chart = param.chart as IChartApi;
    this.requestUpdate = param.requestUpdate;
  }

  detached(): void {
    this.chart = null;
  }

  paneViews(): readonly IPrimitivePaneView[] {
    return this.views;
  }

  update(patch: Partial<Pick<QuartersPrimitive, "blocks" | "rows" | "times" | "palette" | "hoverTime" | "font">>): void {
    Object.assign(this, patch);
    this.requestUpdate?.();
  }
}

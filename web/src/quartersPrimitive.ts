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
import { logicalOf } from "./timeMap";

export const ROW_TITLES: Record<QuarterRow, string> = { week: "Week", session: "Session", q90: "90m" };
const SHORT: Record<string, string> = { Tokyo: "TKY", London: "LDN", "NY AM": "NY", "NY PM": "PM" };

/** Which quarter a block is (0 to 3): Mon to Thu, Tokyo to NY PM, Q1 to Q4. Friday has none. */
const QUARTER: Record<string, number> = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Tokyo: 0, London: 1, "NY AM": 2, "NY PM": 3, Q1: 0, Q2: 1, Q3: 2, Q4: 3 };
export const quarterOf = (label: string): number | null => QUARTER[label] ?? null;

/** How the blocks are coloured: by direction (null), or one colour per quarter, Q1 to Q4. */
export type QuarterColors = [string, string, string, string] | null;

/**
 * The quarterly theory pane under the price chart: one row per cycle (week,
 * session, 90 minutes), each block tinted by direction (close above open is
 * green) with a solid edge, the running block outlined only.
 */
class QuartersRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly source: QuartersPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, blocks, rows, times, palette, hoverTime, font, colors } = this.source;
    if (!chart || times.length === 0 || rows.length === 0) return;
    const timeScale = chart.timeScale();

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const W = bitmapSize.width;
      const H = bitmapSize.height;
      const pad = Math.round(4 * vr);
      const gap = Math.round(3 * vr);
      const rowH = Math.floor((H - 2 * pad - gap * (rows.length - 1)) / rows.length);
      // The grid is chart-wide; cover it in this pane so it doesn't show through the translucent blocks.
      ctx.fillStyle = palette.surface;
      ctx.fillRect(0, 0, W, H);
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
          const direction = b.change > 0 ? palette.bull : b.change < 0 ? palette.bear : palette.muted;
          // Coloured by quarter, the block takes its Q's colour and its edge keeps the direction.
          const q = colors ? quarterOf(b.label) : null;
          const color = colors && q !== null ? colors[q] : direction;
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
            ctx.fillStyle = direction;
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
        ctx.fillStyle = palette.surface;
        ctx.fillRect(0, y, tw, rowH);
        ctx.fillStyle = palette.muted;
        ctx.fillText(title, 5 * hr, y + rowH / 2);
      });
    });
  }
}

class QuartersView implements IPrimitivePaneView {
  constructor(private readonly source: QuartersPrimitive) {}
  // Above the grid ("bottom" draws under it); the pane's only series is an invisible line.
  zOrder(): "normal" {
    return "normal";
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
  colors: QuarterColors = null;
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

  update(patch: Partial<Pick<QuartersPrimitive, "blocks" | "rows" | "times" | "palette" | "hoverTime" | "font" | "colors">>): void {
    Object.assign(this, patch);
    this.requestUpdate?.();
  }
}

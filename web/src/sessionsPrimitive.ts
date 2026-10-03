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

import type { RefLevel } from "./api";
import { groupOf, NO_LEVELS, spreadLabels, type LevelView } from "./refLevels";
import { withAlpha, type ChartPalette } from "./theme";
import { logicalOf } from "./timeMap";

/** Dash per group: day levels solid, week dashed, month dash-dot, opens dotted. */
const DASH: Record<string, number[]> = { asia: [], day: [], week: [6, 3], month: [8, 3, 2, 3], opens: [2, 3], quarter: [2, 3] };

/**
 * Killzones as faint bands behind the candles, and reference levels as thin lines from
 * their start to their end. A level fades from the bar that swept it. The running
 * period's levels are named above their start.
 */
class SessionsRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly source: SessionsPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, series, view, times, palette, font, formatPrice, zoneNames } = this.source;
    if (!chart || !series || times.length < 2 || (!view.killzones.length && !view.lines.length)) return;
    const ts = chart.timeScale();
    const a = ts.logicalToCoordinate(0 as Logical);
    const b = ts.logicalToCoordinate(1 as Logical);
    if (a === null || b === null) return;

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const W = bitmapSize.width;
      const H = bitmapSize.height;
      const xOf = (t: number) => Math.round((a + logicalOf(times, t) * (b - a)) * hr);
      const fontPx = Math.round(10 * vr);
      ctx.font = `600 ${fontPx}px ${font}`;
      ctx.textBaseline = "middle";

      // Killzones: a band the height of the pane, named at the bottom when it fits (unless
      // the quarter pane's session row, right below, names the sessions already).
      for (const k of view.killzones) {
        const x0 = Math.max(0, xOf(k.start_unix));
        const x1 = Math.min(W, xOf(k.end_unix));
        if (x1 - x0 < 1) continue;
        ctx.fillStyle = withAlpha(palette.muted, palette.mode === "dark" ? 0.09 : 0.08);
        ctx.fillRect(x0, 0, x1 - x0, H);
        const w = ctx.measureText(k.name).width;
        if (zoneNames && w + 8 * hr <= x1 - x0) {
          ctx.fillStyle = withAlpha(palette.muted, 0.8);
          ctx.textAlign = "left";
          ctx.fillText(k.name, x0 + 4 * hr, H - 9 * vr);
        }
      }

      // Levels: solid until swept, faint after.
      const labels: { y: number; line: RefLevel; color: string; swept: boolean; x: number }[] = [];
      for (const l of view.lines) {
        const y = series.priceToCoordinate(l.price);
        if (y === null) continue;
        const py = Math.round(y * vr) + 0.5;
        if (py < 0 || py > H) continue;
        const x0 = Math.max(0, xOf(l.start_unix));
        const x1 = Math.min(W, xOf(l.end_unix));
        if (x1 <= x0) continue;
        const group = groupOf(l.kind);
        const color = group === "asia" ? palette.liquidity : palette.text;
        const xs = l.swept_unix === null ? x1 : Math.min(x1, Math.max(x0, xOf(l.swept_unix)));
        ctx.lineWidth = Math.max(1, Math.round(hr));
        ctx.setLineDash(DASH[group].map((d) => d * hr));
        ctx.strokeStyle = withAlpha(color, 0.8);
        ctx.beginPath();
        ctx.moveTo(x0, py);
        ctx.lineTo(xs, py);
        ctx.stroke();
        if (xs < x1) {
          ctx.strokeStyle = withAlpha(color, 0.25);
          ctx.beginPath();
          ctx.moveTo(xs, py);
          ctx.lineTo(x1, py);
          ctx.stroke();
        }
        ctx.setLineDash([]);
        if (l.current) labels.push({ y: py, line: l, color, swept: l.swept_unix !== null, x: x0 });
      }

      // Labels above the start of the running period's lines (the right edge belongs to the
      // zones' labels), spread so none overlap.
      ctx.textAlign = "left";
      const h = fontPx + 3 * vr;
      const ys = spreadLabels(labels.map((l) => l.y - h / 2 - 1 * vr), h, H);
      labels.forEach(({ line, color, swept, x }, i) => {
        ctx.fillStyle = withAlpha(color === palette.text ? palette.textStrong : color, swept ? 0.45 : 0.9);
        ctx.fillText(`${line.label} ${formatPrice(line.price)}`, x + 4 * hr, ys[i]);
      });
    });
  }
}

class SessionsView implements IPrimitivePaneView {
  constructor(private readonly source: SessionsPrimitive) {}
  zOrder(): "bottom" {
    return "bottom";
  }
  renderer(): IPrimitivePaneRenderer {
    return new SessionsRenderer(this.source);
  }
}

export class SessionsPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  series: ISeriesApi<SeriesType> | null = null;
  view: LevelView = NO_LEVELS;
  times: number[] = [];
  zoneNames = true;
  font = "system-ui, sans-serif";
  formatPrice: (p: number) => string = (p) => p.toFixed(2);
  private requestUpdate?: () => void;
  private readonly views = [new SessionsView(this)];

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

  update(patch: Partial<Pick<SessionsPrimitive, "view" | "times" | "zoneNames" | "palette" | "font" | "formatPrice">>): void {
    Object.assign(this, patch);
    this.requestUpdate?.();
  }
}

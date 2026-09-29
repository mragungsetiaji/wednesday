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

import type { CalendarEvent } from "./api";
import { logicalOf } from "./timeMap";
import { withAlpha, type ChartPalette } from "./theme";

/** Releases at the same minute drawn as one line. */
export interface NewsMark {
  time: number; // chart axis (feed clock)
  label: string; // "USD CPI m/m +2"
  at: number; // real time of the release, ms
}

export function newsMarks(events: CalendarEvent[]): NewsMark[] {
  const byTime = new Map<number, CalendarEvent[]>();
  for (const e of events) byTime.set(e.chart_time_unix, [...(byTime.get(e.chart_time_unix) ?? []), e]);
  return [...byTime.entries()]
    .sort(([a], [b]) => a - b)
    .map(([time, group]) => ({
      time,
      label: `${group[0].currency} ${group[0].title}${group.length > 1 ? ` +${group.length - 1}` : ""}`,
      at: Date.parse(group[0].time),
    }));
}

/**
 * High-impact news on the price pane: a dashed vertical line at each release
 * with its name at the top. Upcoming releases are amber, past ones muted.
 */
class NewsRenderer implements IPrimitivePaneRenderer {
  constructor(private readonly source: NewsPrimitive) {}

  draw(target: CanvasRenderingTarget2D): void {
    const { chart, marks, times, palette, font } = this.source;
    if (!chart || !marks.length || times.length < 2) return;
    const ts = chart.timeScale();
    const a = ts.logicalToCoordinate(0 as Logical);
    const b = ts.logicalToCoordinate(1 as Logical);
    if (a === null || b === null) return;
    const now = Date.now();

    target.useBitmapCoordinateSpace(({ context: ctx, bitmapSize, horizontalPixelRatio: hr, verticalPixelRatio: vr }) => {
      const W = bitmapSize.width;
      const H = bitmapSize.height;
      const fontPx = Math.round(10 * vr);
      ctx.font = `600 ${fontPx}px ${font}`;
      ctx.textBaseline = "middle";
      const h = fontPx + 6 * vr;
      const rows: [number, number][][] = [[], []]; // label spans placed in each of two rows
      // Fractional position: an 08:30 release sits halfway through the 08:00 1H candle.
      const placed = marks
        .map((m) => ({ m, x: Math.round((a + logicalOf(times, m.time) * (b - a)) * hr), upcoming: m.at > now - 10 * 60_000 }))
        .filter((p) => p.x >= 0 && p.x <= W);

      for (const { x, upcoming } of placed) {
        const color = upcoming ? palette.accent : palette.muted;
        ctx.strokeStyle = withAlpha(color, upcoming ? 0.85 : 0.55);
        ctx.lineWidth = Math.max(1, Math.round(hr));
        ctx.setLineDash([4 * hr, 4 * hr]);
        ctx.beginPath();
        ctx.moveTo(x + 0.5, 0);
        ctx.lineTo(x + 0.5, H);
        ctx.stroke();
        ctx.setLineDash([]);
      }

      // Labels at the top: upcoming releases claim space first, then the most recent past ones;
      // a label takes the first of two rows where it fits, else it's left off (the line stays).
      const order = [...placed].sort((p, q) => Number(q.upcoming) - Number(p.upcoming) || q.m.time - p.m.time);
      for (const { m, x, upcoming } of order) {
        const w = ctx.measureText(m.label).width + 10 * hr;
        const left = Math.min(Math.max(0, x - w / 2), W - w);
        const gap = 4 * hr;
        const row = rows.findIndex((spans) => spans.every(([l, r]) => left + w + gap <= l || left >= r + gap));
        if (row < 0) continue;
        rows[row].push([left, left + w]);
        const top = 22 * vr + row * (h + 3 * vr); // below the OHLC readout
        const color = upcoming ? palette.accent : palette.muted;
        ctx.fillStyle = withAlpha(palette.surface, 0.92);
        ctx.fillRect(left, top, w, h);
        ctx.strokeStyle = withAlpha(color, upcoming ? 0.9 : 0.5);
        ctx.strokeRect(left + 0.5, top + 0.5, w - 1, h - 1);
        ctx.fillStyle = upcoming ? palette.textStrong : palette.muted;
        ctx.fillText(m.label, left + 5 * hr, top + h / 2);
      }
    });
  }
}

class NewsView implements IPrimitivePaneView {
  constructor(private readonly source: NewsPrimitive) {}
  zOrder(): "bottom" {
    return "bottom";
  }
  renderer(): IPrimitivePaneRenderer {
    return new NewsRenderer(this.source);
  }
}

export class NewsPrimitive implements ISeriesPrimitive<Time> {
  chart: IChartApi | null = null;
  marks: NewsMark[] = [];
  times: number[] = [];
  font = "system-ui, sans-serif";
  private requestUpdate?: () => void;
  private readonly views = [new NewsView(this)];

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

  update(patch: Partial<Pick<NewsPrimitive, "marks" | "times" | "palette" | "font">>): void {
    Object.assign(this, patch);
    this.requestUpdate?.();
  }
}

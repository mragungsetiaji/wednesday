import {
  AreaSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  LineType,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import type { JournalStats, Point } from "../api";
import { fmtPrice, fmtUnix } from "../format";
import { withAlpha, type ChartPalette } from "../theme";

const FONT = getComputedStyle(document.documentElement).getPropertyValue("--font-ui").trim() || "system-ui, sans-serif";

export type JournalView = "growth" | "balance" | "drawdown";

const pct = (v: number) => `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(2)}%`;

/** The value in force at ``time``: the last point at or before it. */
function valueAt(points: Point[], time: number): number | null {
  let lo = 0;
  let hi = points.length - 1;
  let found: number | null = null;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (points[mid][0] <= time) {
      found = points[mid][1];
      lo = mid + 1;
    } else hi = mid - 1;
  }
  return found;
}

/** Strictly increasing times (the chart needs them): the last value at a repeated time wins. */
function line(points: Point[]) {
  const out: { time: UTCTimestamp; value: number }[] = [];
  for (const [t, v] of points) {
    if (out.length && out[out.length - 1].time >= t) out[out.length - 1] = { time: out[out.length - 1].time, value: v };
    else out.push({ time: t as UTCTimestamp, value: v });
  }
  return out;
}

interface Series {
  key: string;
  title: string;
  color: string;
  format: (v: number) => string;
}

/** The growth, balance/equity or drawdown curve of a journal. */
export function JournalChart({ stats, view, palette, currency }: { stats: JournalStats; view: JournalView; palette: ChartPalette; currency: string }) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line" | "Area">[]>([]);
  const [hover, setHover] = useState<number | null>(null);

  const money = (v: number) => `${v < 0 ? "−" : ""}${fmtPrice(Math.abs(v))} ${currency}`.trim();
  const defs: Series[] =
    view === "growth"
      ? [{ key: "growth", title: "Growth", color: palette.liquidity, format: pct }]
      : view === "balance"
        ? [
            { key: "balance", title: "Balance", color: palette.textStrong, format: money },
            { key: "equity", title: "Equity (lowest each hour)", color: palette.liquidity, format: money },
          ]
        : [{ key: "drawdown", title: "Drawdown", color: palette.bear, format: pct }];

  useEffect(() => {
    const chart = createChart(containerRef.current!, {
      autoSize: true,
      crosshair: { mode: CrosshairMode.Magnet },
      timeScale: { timeVisible: true, secondsVisible: false },
      rightPriceScale: { scaleMargins: { top: 0.12, bottom: 0.08 } },
      handleScroll: { vertTouchDrag: false },
    });
    chartRef.current = chart;
    return () => {
      chart.remove();
      chartRef.current = null;
    };
  }, []);

  useEffect(() => {
    chartRef.current?.applyOptions({
      layout: { background: { type: ColorType.Solid, color: palette.surface }, textColor: palette.text, fontFamily: FONT, fontSize: 11, attributionLogo: false },
      grid: { vertLines: { visible: false }, horzLines: { color: palette.grid } },
      rightPriceScale: { borderVisible: false },
      timeScale: { borderColor: palette.grid },
      crosshair: {
        vertLine: { color: palette.muted, labelBackgroundColor: palette.grid },
        horzLine: { color: palette.muted, labelBackgroundColor: palette.grid },
      },
    });
  }, [palette]);

  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    for (const s of seriesRef.current) chart.removeSeries(s);
    const format = { type: "custom" as const, formatter: defs[0].format, minMove: 0.01 };
    seriesRef.current = defs.map((d, i) => {
      const data = line(stats.series[d.key as keyof JournalStats["series"]]);
      const common = { color: d.color, lineWidth: 2 as const, priceFormat: format, lastValueVisible: false, priceLineVisible: false };
      const s =
        view === "drawdown"
          ? chart.addSeries(AreaSeries, { ...common, lineColor: d.color, topColor: withAlpha(d.color, 0.04), bottomColor: withAlpha(d.color, 0.28), invertFilledArea: true })
          : view === "growth"
            ? chart.addSeries(AreaSeries, { ...common, lineColor: d.color, topColor: withAlpha(d.color, 0.22), bottomColor: withAlpha(d.color, 0.02) })
            : chart.addSeries(LineSeries, { ...common, lineWidth: i === 0 ? 2 : 1, lineType: i === 0 ? LineType.WithSteps : LineType.Simple });
      s.setData(data);
      return s;
    });
    chart.timeScale().fitContent();
    const onMove = (param: Parameters<Parameters<IChartApi["subscribeCrosshairMove"]>[0]>[0]) =>
      setHover(param.time === undefined ? null : (param.time as number));
    chart.subscribeCrosshairMove(onMove);
    return () => chart.unsubscribeCrosshairMove(onMove);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stats, view, palette, currency]);

  const pointsOf = (key: string) => stats.series[key as keyof JournalStats["series"]];
  const empty = defs.every((d) => pointsOf(d.key).length === 0);
  return (
    <div className="journal-chart">
      <div className="chart-legend num journal-legend" aria-hidden="true">
        {hover !== null && <span className="muted">{fmtUnix(hover)}</span>}
        {defs.map((d) => {
          const pts = pointsOf(d.key);
          const v = hover !== null ? valueAt(pts, hover) : pts.length ? pts[pts.length - 1][1] : null;
          return (
            <span key={d.key}>
              <i className="swatch" style={{ background: d.color }} />
              {d.title} <b>{v === null ? "—" : d.format(v)}</b>
            </span>
          );
        })}
      </div>
      {empty && <p className="empty journal-chart-empty">Nothing to draw yet.</p>}
      <div ref={containerRef} className="chart" role="img"
        aria-label={`${defs.map((d) => d.title).join(" and ")} over time`} />
    </div>
  );
}

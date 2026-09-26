import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import type { Candle, QuarterBlock, QuarterRow, QuartersResponse } from "../api";
import type { CrosshairBus } from "../crosshairSync";
import { fmtPrice } from "../format";
import { QuartersPrimitive } from "../quartersPrimitive";
import type { ChartPalette } from "../theme";
import { ZonesPrimitive, type Zone } from "../zonesPrimitive";

export interface ChartEvent {
  time: number; // unix seconds of the candle it happened on
  above: boolean; // sweep of a level above price (marker above the bar)
  text: string;
}

interface Props {
  candles: Candle[];
  zones: Zone[];
  events: ChartEvent[];
  highlight: string | null;
  palette: ChartPalette;
  resetKey: string; // refit the view when this changes (e.g. timeframe switch)
  loading: boolean;
  quarters: QuartersResponse | null;
  quarterRows: QuarterRow[]; // rows of the quarterly pane, [] hides it
  sync?: { bus: CrosshairBus; id: number }; // crosshair linked with other charts
}

const ROW_PX = 22;
const QUARTER_PANE = 1;

const fmtChange = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtPrice(Math.abs(v))}`;

/** The block of each row that contains t. */
function blocksAt(quarters: QuartersResponse | null, rows: QuarterRow[], t: number): QuarterBlock[] {
  if (!quarters) return [];
  return rows.flatMap((row) => {
    const b = quarters.rows[row].find((q) => t >= q.start_unix && t < q.end_unix);
    return b ? [b] : [];
  });
}

/** Open time of the candle containing t (last candle time <= t). */
function candleAt(times: number[], t: number): number {
  let lo = 0;
  let hi = times.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (times[mid] <= t) lo = mid;
    else hi = mid - 1;
  }
  return times[lo];
}

const FONT = getComputedStyle(document.documentElement).getPropertyValue("--font-ui").trim() || "system-ui, sans-serif";

export function PriceChart({ candles, zones, events, highlight, palette, resetKey, loading, quarters, quarterRows, sync }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const zonesRef = useRef<ZonesPrimitive | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const quarterSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const quartersRef = useRef<QuartersPrimitive | null>(null);
  const quarterPaneHeight = useRef(0);
  const candlesRef = useRef<Candle[]>([]);
  const syncRef = useRef(sync);
  syncRef.current = sync;
  const fittedKey = useRef<string | null>(null);
  const [hover, setHover] = useState<Candle | null>(null);

  useEffect(() => {
    const chart = createChart(containerRef.current!, {
      autoSize: true,
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 12 },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.08 } },
    });
    const series = chart.addSeries(CandlestickSeries, { borderVisible: false, priceLineStyle: 2 });
    const primitive = new ZonesPrimitive(palette);
    primitive.update({ font: FONT, formatPrice: fmtPrice });
    series.attachPrimitive(primitive);
    markersRef.current = createSeriesMarkers(series, []);
    const quartersPrimitive = new QuartersPrimitive(palette);
    quartersPrimitive.update({ font: FONT });
    chart.subscribeCrosshairMove((param) => {
      const bar = param.seriesData.get(series) as Candle | undefined;
      setHover(bar && "open" in bar ? { ...bar, time: param.time as number } : null);
      quartersPrimitive.update({ hoverTime: param.time === undefined ? null : (param.time as number) });
      // Only moves made by the pointer on this chart are passed on (not ones set by a linked chart).
      const link = syncRef.current;
      if (link && param.sourceEvent && param.time !== undefined && param.point) {
        const price = param.paneIndex === 0 ? series.coordinateToPrice(param.point.y) : null;
        link.bus.publish(link.id, { time: param.time as number, price });
      }
    });
    quartersRef.current = quartersPrimitive;
    // Resizing the chart rescales every pane; keep the quarterly pane at its fixed height.
    const resize = new ResizeObserver(() => {
      const h = quarterPaneHeight.current;
      if (h) requestAnimationFrame(() => chartRef.current?.panes()[QUARTER_PANE]?.setHeight(h));
    });
    resize.observe(containerRef.current!);
    chartRef.current = chart;
    seriesRef.current = series;
    zonesRef.current = primitive;
    return () => {
      resize.disconnect();
      chart.remove();
      chartRef.current = null;
      quarterSeriesRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    chartRef.current?.applyOptions({
      layout: {
        background: { type: ColorType.Solid, color: palette.surface },
        textColor: palette.text,
        fontFamily: FONT,
        fontSize: 11,
        attributionLogo: false,
        panes: { separatorColor: palette.grid, separatorHoverColor: palette.grid, enableResize: false },
      },
      grid: { vertLines: { color: palette.grid }, horzLines: { color: palette.grid } },
      rightPriceScale: { borderColor: palette.grid },
      timeScale: { borderColor: palette.grid },
      crosshair: {
        vertLine: { color: palette.muted, labelBackgroundColor: palette.grid },
        horzLine: { color: palette.muted, labelBackgroundColor: palette.grid },
      },
    });
    quartersRef.current?.update({ palette });
    seriesRef.current?.applyOptions({
      upColor: palette.bull,
      downColor: palette.bear,
      wickUpColor: palette.bull,
      wickDownColor: palette.bear,
    });
  }, [palette]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    series.setData(candles.map((c) => ({ ...c, time: c.time as UTCTimestamp })));
    candlesRef.current = candles;
    zonesRef.current?.update({ zones, times: candles.map((c) => c.time), palette });
    const times = candles.map((c) => c.time);
    const markers: SeriesMarker<Time>[] = events
      .filter((e) => times.length && e.time >= times[0])
      .map((e) => ({
        time: candleAt(times, e.time) as UTCTimestamp,
        position: e.above ? ("aboveBar" as const) : ("belowBar" as const),
        shape: e.above ? ("arrowDown" as const) : ("arrowUp" as const),
        color: palette.accent,
        text: e.text,
        size: 1,
      }))
      .sort((a, b) => (a.time as number) - (b.time as number));
    markersRef.current?.setMarkers(markers);
    if (candles.length && fittedKey.current !== resetKey) {
      chartRef.current?.timeScale().fitContent();
      fittedKey.current = resetKey;
    }
  }, [candles, zones, events, palette, resetKey]);

  useEffect(() => {
    zonesRef.current?.update({ highlight });
  }, [highlight]);

  // Follow the linked charts: same time (the candle containing it on this timeframe), same price.
  useEffect(() => {
    if (!sync) return;
    return sync.bus.subscribe(sync.id, (p) => {
      const chart = chartRef.current;
      const series = seriesRef.current;
      const bars = candlesRef.current;
      if (!chart || !series) return;
      if (!p || !bars.length || p.time < bars[0].time) {
        chart.clearCrosshairPosition();
        setHover(null);
        quartersRef.current?.update({ hoverTime: null });
        return;
      }
      const t = candleAt(bars.map((c) => c.time), p.time);
      const bar = bars.find((c) => c.time === t) ?? bars[bars.length - 1];
      chart.setCrosshairPosition(p.price ?? bar.close, t as UTCTimestamp, series);
      // The readout and the quarterly pane follow too.
      setHover(bar);
      quartersRef.current?.update({ hoverTime: p.time });
    });
  }, [sync]);

  const onLeave = () => sync?.bus.publish(sync.id, null);

  // Quarterly pane: an empty series whose only job is to host the blocks primitive; its
  // invisible points on the candle times keep it on the shared time scale. Removing the
  // series removes the pane.
  useEffect(() => {
    const chart = chartRef.current;
    const primitive = quartersRef.current;
    if (!chart || !primitive) return;
    let qs = quarterSeriesRef.current;
    if (!quarterRows.length) {
      if (qs) chart.removeSeries(qs);
      quarterSeriesRef.current = null;
      quarterPaneHeight.current = 0;
      return;
    }
    if (!qs) {
      // Its price scale stays visible (hiding one pane's scale trips the library); blank labels instead.
      qs = chart.addSeries(LineSeries, {
        lineVisible: false, lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false,
        priceFormat: { type: "custom", formatter: () => "" },
      }, QUARTER_PANE);
      qs.attachPrimitive(primitive);
      quarterSeriesRef.current = qs;
    }
    qs.setData(candles.map((c) => ({ time: c.time as UTCTimestamp, value: 0 })));
    primitive.update({ blocks: quarters?.rows ?? {}, rows: quarterRows, times: candles.map((c) => c.time) });
    quarterPaneHeight.current = quarterRows.length * ROW_PX + 10;
    chart.panes()[QUARTER_PANE]?.setHeight(quarterPaneHeight.current);
  }, [candles, quarters, quarterRows]);

  const shown = hover ?? candles[candles.length - 1];
  const quarterNow = shown ? blocksAt(quarters, quarterRows, shown.time) : [];
  return (
    <div className="chart-wrap" aria-busy={loading} onMouseLeave={onLeave}>
      {shown && (
        <div className="chart-legend num" aria-hidden="true">
          <span><i>O</i>{fmtPrice(shown.open)}</span>
          <span><i>H</i>{fmtPrice(shown.high)}</span>
          <span><i>L</i>{fmtPrice(shown.low)}</span>
          <span><i>C</i>{fmtPrice(shown.close)}</span>
          {quarterNow.map((b) => (
            <span key={b.row} className={`q ${b.change > 0 ? "up" : b.change < 0 ? "down" : ""}`}>
              <i>{b.label}</i>{fmtChange(b.change)}
            </span>
          ))}
        </div>
      )}
      {loading && <div className="chart-skeleton" aria-hidden="true" />}
      <div ref={containerRef} className="chart" role="img" aria-label="Price chart with order blocks, liquidity and inducement levels" />
    </div>
  );
}

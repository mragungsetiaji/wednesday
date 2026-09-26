import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
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

import type { Candle } from "../api";
import { fmtPrice } from "../format";
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

export function PriceChart({ candles, zones, events, highlight, palette, resetKey, loading }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const zonesRef = useRef<ZonesPrimitive | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
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
    chart.subscribeCrosshairMove((param) => {
      const bar = param.seriesData.get(series) as Candle | undefined;
      setHover(bar && "open" in bar ? { ...bar, time: param.time as number } : null);
    });
    chartRef.current = chart;
    seriesRef.current = series;
    zonesRef.current = primitive;
    return () => {
      chart.remove();
      chartRef.current = null;
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
      },
      grid: { vertLines: { color: palette.grid }, horzLines: { color: palette.grid } },
      rightPriceScale: { borderColor: palette.grid },
      timeScale: { borderColor: palette.grid },
      crosshair: {
        vertLine: { color: palette.muted, labelBackgroundColor: palette.grid },
        horzLine: { color: palette.muted, labelBackgroundColor: palette.grid },
      },
    });
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

  const shown = hover ?? candles[candles.length - 1];
  return (
    <div className="chart-wrap" aria-busy={loading}>
      {shown && (
        <div className="chart-legend num" aria-hidden="true">
          <span><i>O</i>{fmtPrice(shown.open)}</span>
          <span><i>H</i>{fmtPrice(shown.high)}</span>
          <span><i>L</i>{fmtPrice(shown.low)}</span>
          <span><i>C</i>{fmtPrice(shown.close)}</span>
        </div>
      )}
      {loading && <div className="chart-skeleton" aria-hidden="true" />}
      <div ref={containerRef} className="chart" role="img" aria-label="Price chart with order blocks, liquidity and inducement levels" />
    </div>
  );
}

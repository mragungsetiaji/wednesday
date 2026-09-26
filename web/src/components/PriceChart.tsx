import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import type { Candle } from "../api";
import { fmtPrice } from "../format";
import type { ChartPalette } from "../theme";
import { ZonesPrimitive, type Zone } from "../zonesPrimitive";

interface Props {
  candles: Candle[];
  zones: Zone[];
  palette: ChartPalette;
  resetKey: string; // refit the view when this changes (e.g. timeframe switch)
}

export function PriceChart({ candles, zones, palette, resetKey }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const zonesRef = useRef<ZonesPrimitive | null>(null);
  const fittedKey = useRef<string | null>(null);
  const [hover, setHover] = useState<Candle | null>(null);

  // Create the chart once.
  useEffect(() => {
    const el = containerRef.current!;
    const chart = createChart(el, {
      autoSize: true,
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 8 },
      rightPriceScale: { scaleMargins: { top: 0.1, bottom: 0.1 } },
    });
    const series = chart.addSeries(CandlestickSeries, { borderVisible: false, priceLineStyle: 2 });
    const primitive = new ZonesPrimitive(palette);
    series.attachPrimitive(primitive);
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

  // Theme.
  useEffect(() => {
    chartRef.current?.applyOptions({
      layout: {
        background: { type: ColorType.Solid, color: palette.surface },
        textColor: palette.text,
        fontFamily: "ui-sans-serif, system-ui, sans-serif",
        attributionLogo: false,
      },
      grid: { vertLines: { color: palette.grid }, horzLines: { color: palette.grid } },
      rightPriceScale: { borderColor: palette.grid },
      timeScale: { borderColor: palette.grid },
    });
    seriesRef.current?.applyOptions({
      upColor: palette.bull,
      downColor: palette.bear,
      wickUpColor: palette.bull,
      wickDownColor: palette.bear,
    });
  }, [palette]);

  // Data + zones.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    series.setData(candles.map((c) => ({ ...c, time: c.time as UTCTimestamp })));
    zonesRef.current?.setData(zones, candles.map((c) => c.time), palette);
    if (candles.length && fittedKey.current !== resetKey) {
      chartRef.current?.timeScale().fitContent();
      fittedKey.current = resetKey;
    }
  }, [candles, zones, palette, resetKey]);

  const shown = hover ?? candles[candles.length - 1];
  return (
    <div className="chart-wrap">
      {shown && (
        <div className="chart-legend" aria-live="off">
          <span>O {fmtPrice(shown.open)}</span>
          <span>H {fmtPrice(shown.high)}</span>
          <span>L {fmtPrice(shown.low)}</span>
          <span>C {fmtPrice(shown.close)}</span>
        </div>
      )}
      <div ref={containerRef} className="chart" />
    </div>
  );
}

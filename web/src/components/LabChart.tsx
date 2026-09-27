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
import { LabPrimitive, type LabMark } from "../labPrimitive";
import type { ChartPalette } from "../theme";

const FONT = getComputedStyle(document.documentElement).getPropertyValue("--font-ui").trim() || "system-ui, sans-serif";

interface Props {
  candles: Candle[];
  marks: LabMark[];
  reviewed: { start: number; end: number }[];
  selection: { start: number; end: number } | null;
  highlight: string | null;
  palette: ChartPalette;
  resetKey: string; // refit when this changes (timeframe or page of history)
  loading: boolean;
  onPick: (time: number, extend: boolean) => void; // click a candle; shift-click extends the selection
}

/** The Lab's chart: candles you click to select, with labels, suggestions and model output drawn on them. */
export function LabChart({ candles, marks, reviewed, selection, highlight, palette, resetKey, loading, onPick }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const labRef = useRef<LabPrimitive | null>(null);
  const fittedKey = useRef<string | null>(null);
  const pickRef = useRef(onPick);
  pickRef.current = onPick;
  const [hover, setHover] = useState<Candle | null>(null);

  useEffect(() => {
    const chart = createChart(containerRef.current!, {
      autoSize: true,
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 6 },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.08 } },
    });
    const series = chart.addSeries(CandlestickSeries, { borderVisible: false, priceLineVisible: false, lastValueVisible: false });
    const lab = new LabPrimitive(palette);
    lab.update({ font: FONT });
    series.attachPrimitive(lab);
    chart.subscribeCrosshairMove((param) => {
      const bar = param.seriesData.get(series) as Candle | undefined;
      setHover(bar && "open" in bar ? { ...bar, time: param.time as number } : null);
    });
    chart.subscribeClick((param) => {
      if (param.time === undefined) return;
      pickRef.current(param.time as number, !!param.sourceEvent?.shiftKey);
    });
    chartRef.current = chart;
    seriesRef.current = series;
    labRef.current = lab;
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
        attributionLogo: false, // the TradingView notice and link are in the status bar instead
      },
      grid: { vertLines: { color: palette.grid }, horzLines: { color: palette.grid } },
      rightPriceScale: { borderColor: palette.grid },
      timeScale: { borderColor: palette.grid },
      crosshair: {
        vertLine: { color: palette.muted, labelBackgroundColor: palette.grid },
        horzLine: { color: palette.muted, labelBackgroundColor: palette.grid },
      },
    });
    seriesRef.current?.applyOptions({ upColor: palette.bull, downColor: palette.bear, wickUpColor: palette.bull, wickDownColor: palette.bear });
    labRef.current?.update({ palette });
  }, [palette]);

  useEffect(() => {
    seriesRef.current?.setData(candles.map((c) => ({ ...c, time: c.time as UTCTimestamp })));
    labRef.current?.update({ times: candles.map((c) => c.time) });
    if (candles.length && fittedKey.current !== resetKey) {
      chartRef.current?.timeScale().fitContent();
      fittedKey.current = resetKey;
    }
  }, [candles, resetKey]);

  useEffect(() => {
    labRef.current?.update({ marks, reviewed, selection, highlight });
  }, [marks, reviewed, selection, highlight]);

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
      <div ref={containerRef} className="chart lab-chart" role="img"
        aria-label="Candles to label. Click a candle to select it, shift-click to select a range." />
    </div>
  );
}

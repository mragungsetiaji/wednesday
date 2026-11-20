import {
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  LineSeries,
  PriceScaleMode,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from "lightweight-charts";
import { createContext, useContext, useEffect, useImperativeHandle, useMemo, useRef, useState, useSyncExternalStore, type Ref } from "react";

import type { Candle, QuarterBlock, QuarterRow, QuartersResponse, SwingPoint } from "../api";
import { countdown, measure, PRESETS, presetStart, type Preset } from "../chartNav";
import type { CrosshairLink } from "../crosshairSync";
import { linkBus, type LinkGroup } from "../windowLink";
import { counters, planUpdate } from "../perf";
import { DrawingEditor } from "../drawingEditor";
import { heikin, heikinBar, useChartType } from "../chartType";
import { useChartSettings } from "../chartSettings";
import { indicatorLabel, linesOf } from "../indicators";
import { useIndicators } from "../indicatorPrefs";
import type { DrawingCtl } from "../drawings";
import { useLiveTick, type LiveFeed } from "../liveData";
import { fmtPrice } from "../format";
import { NewsPrimitive, type NewsMark } from "../newsPrimitive";
import { QuartersPrimitive } from "../quartersPrimitive";
import { NO_LEVELS, type LevelView } from "../refLevels";
import { SessionsPrimitive } from "../sessionsPrimitive";
import type { ChartPalette } from "../theme";
import { logicalOfTime, stepOf, timeOfLogical } from "../timeMap";
import { ZonesPrimitive, type Zone } from "../zonesPrimitive";
import { compose, fileName } from "../snapshot";
import { AUTO_SCALE, ScaleMenu, type ScaleState } from "./ScaleMenu";
import { copySnapshot, SnapshotMenu, type SnapshotSource } from "./SnapshotMenu";

/** The symbol, its pip and the feed clock (name, and offset from UTC in seconds), from /api/scan. */
export interface Market {
  pip: number;
  clockOffset: number;
  symbol: string;
  clockName: string;
}
export const ChartMarket = createContext<Market>({ pip: 0.1, clockOffset: 0, symbol: "", clockName: "UTC" });

/** What the keyboard shortcuts do to a chart. */
export interface ChartNav {
  reset: () => void; // default zoom, scrolled to the live candle
  goTo: (t: number) => void; // centre a feed-clock time, loading older candles as needed
  snapshot: () => void; // copy a picture of the chart (saved where the clipboard is refused)
  capture: () => HTMLCanvasElement | null; // the chart's own screenshot, for a layout snapshot
  snapshotSource: () => SnapshotSource; // for a camera button outside the chart (the top bar's)
}

/** Opens the keyboard shortcut list (the chart's "?" button). */
export const SHOW_SHORTCUTS = "wed:shortcuts";

/** A shift-drag measure, in chart pixels (x, y) and logical bars and price (l, p). */
interface Ruler {
  x0: number;
  y0: number;
  l0: number;
  p0: number;
  x1: number;
  y1: number;
  l1: number;
  p1: number;
}

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
  sync?: { bus: CrosshairLink; id: number }; // crosshair linked with other charts
  swings?: SwingPoint[]; // HH / LH / HL / LL labels at swing points, [] hides them
  news?: NewsMark[]; // high-impact releases as vertical lines, [] hides them
  levels?: LevelView; // killzones and reference levels (sessions.py)
  onNeedOlder?: () => void; // the view reached the first candle: load older ones
  live?: LiveFeed | null; // live ticks: their forming M1 candle is folded into the last candle
  drawings?: DrawingCtl | null; // the trader's drawings, shown and edited here
  nav?: Ref<ChartNav | null>; // for the keyboard shortcuts
  timeframe?: string; // named in a snapshot's footer; resetKey when not given
  snapshotButton?: boolean; // the camera under the chart; false where a toolbar holds one
  viewKey?: string; // keep the zoom and scroll under this name for the session: leaving the page and coming back keeps them
  rangeLink?: { group: LinkGroup; id: string } | null; // scroll and zoom shared with a link group (windowLink.ts)
  precision?: number; // price decimals: 2 for gold, more for a pane's own symbol (e.g. 3 for silver)
}

const ROW_PX = 22;
const NO_SWINGS: SwingPoint[] = []; // stable defaults, so the effects don't rerun every render
const NO_NEWS: NewsMark[] = [];
/**
 * A chart's zoom and scroll, kept for the session (sessionStorage) so a chart built again (back from
 * Settings, out of full screen) opens where it was. Counted in bars back from the last closed candle,
 * so newer candles or older pages loaded since don't move it. Only for the same reset key (timeframe):
 * another timeframe still opens fitted.
 */
interface SavedView {
  reset: string;
  fromEnd: number;
  toEnd: number;
}
const VIEW_PREFIX = "wed.view:";
function loadView(key: string): SavedView | null {
  try {
    const v = sessionStorage.getItem(VIEW_PREFIX + key);
    return v ? (JSON.parse(v) as SavedView) : null;
  } catch {
    return null;
  }
}
function saveView(key: string, v: SavedView): void {
  try {
    sessionStorage.setItem(VIEW_PREFIX + key, JSON.stringify(v));
  } catch {
    /* storage unavailable: the view just isn't kept */
  }
}

const QUARTER_PANE = 1;
const RSI_PANE_PX = 110;

/**
 * Give the quarterly pane (quarterPx, 0 without it) and the RSI pane (at rsiPane, 0 without it) their
 * fixed heights and the candles the rest. Stretch factors, not setHeight: setting one pane's height
 * squeezes the others.
 */
function fitPanes(chart: IChartApi, quarterPx: number, rsiPane: number): void {
  const panes = chart.panes();
  const total = panes.reduce((h, p) => h + p.getHeight(), 0);
  if (!total) return;
  const fixed = new Map<number, number>();
  if (quarterPx && panes[QUARTER_PANE]) fixed.set(QUARTER_PANE, quarterPx);
  if (rsiPane && panes[rsiPane]) fixed.set(rsiPane, RSI_PANE_PX);
  const rest = Math.max(80, total - [...fixed.values()].reduce((a, b) => a + b, 0));
  panes.forEach((p, i) => p.setStretchFactor(fixed.get(i) ?? (i === 0 ? rest : 1)));
}

/** The chart's candle opens with the forming one, as the chart shows them. */
const timesOf = (candles: Candle[], live: Candle | null) => {
  const times = candles.map((c) => c.time);
  if (live && (!times.length || live.time > times[times.length - 1])) times.push(live.time);
  return times;
};

const fmtSigned = (v: number, digits: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(digits)}`;

/** The candle holding time t: the last one opening at or before it (the first one before them all). */
function barAt(bars: Candle[], t: number): Candle {
  let lo = 0;
  let hi = bars.length - 1;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (bars[mid].time <= t) lo = mid;
    else hi = mid - 1;
  }
  return bars[lo];
}

/**
 * The candle under the crosshair, outside React state: the crosshair moves on every mouse move
 * (and on every linked chart), and re-rendering the whole chart for it cost hundreds of renders
 * a second. Only the legend reads it.
 */
function hoverStore() {
  let value: Candle | null = null;
  const subs = new Set<() => void>();
  return {
    get: () => value,
    set(next: Candle | null) {
      if (next === value || (next && value && next.time === value.time && next.close === value.close && next.high === value.high
        && next.low === value.low)) return;
      value = next;
      for (const fn of subs) fn();
    },
    subscribe(fn: () => void) {
      subs.add(fn);
      return () => {
        subs.delete(fn);
      };
    },
  };
}
type HoverStore = ReturnType<typeof hoverStore>;

/** O H L C of the hovered candle (else the live or last one), and the quarter blocks it sits in. */
/** An indicator in the legend: its name, colour and its lines' values by candle time. */
interface LegendRow {
  label: string;
  color: string;
  values: Map<number, number>[];
}

/** A line's value at t, or its last one when t has none (the forming candle). */
function valueAt(m: Map<number, number>, t: number): number | null {
  const v = m.get(t);
  if (v !== undefined) return v;
  let last: number | null = null;
  for (const x of m.values()) last = x;
  return last;
}

function ChartLegend({ hover, fallback, quarters, quarterRows, indicators }: {
  hover: HoverStore; fallback: Candle | undefined; quarters: QuartersResponse | null; quarterRows: QuarterRow[]; indicators: LegendRow[];
}) {
  const shown = useSyncExternalStore(hover.subscribe, hover.get) ?? fallback;
  if (!shown) return null;
  const quarterNow = blocksAt(quarters, quarterRows, shown.time);
  return (
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
      {indicators.map((r) => (
        <span key={r.label} className="ind" style={{ color: r.color }}>
          <i>{r.label}</i>{r.values.map((m) => valueAt(m, shown.time)).map((v) => (v === null ? "—" : fmtPrice(v))).join(" ")}
        </span>
      ))}
    </div>
  );
}

const fmtChange = (v: number) => `${v > 0 ? "+" : v < 0 ? "−" : ""}${fmtPrice(Math.abs(v))}`;

/** The block of each row that contains t. */
function blocksAt(quarters: QuartersResponse | null, rows: QuarterRow[], t: number): QuarterBlock[] {
  if (!quarters) return [];
  return rows.flatMap((row) => {
    const b = quarters.rows[row].find((q) => t >= q.start_unix && t < q.end_unix);
    return b ? [b] : [];
  });
}

/** Seconds per candle: the smallest gap among the last few (a weekend gap is never the smallest). */
function candlePeriod(candles: Candle[]): number {
  let p = Infinity;
  for (let i = Math.max(1, candles.length - 10); i < candles.length; i++) p = Math.min(p, candles[i].time - candles[i - 1].time);
  return Number.isFinite(p) && p > 0 ? p : 60;
}

/**
 * The chart's last candle with the live M1 candle folded in: the same candle while the minute
 * falls inside it, or a new one once the minute starts the next period.
 */
function withLive(candles: Candle[], live: Candle): Candle | null {
  const last = candles[candles.length - 1];
  if (!last || live.time < last.time) return null;
  const period = candlePeriod(candles);
  const k = Math.floor((live.time - last.time) / period);
  if (k === 0) {
    return { ...last, high: Math.max(last.high, live.high), low: Math.min(last.low, live.low), close: live.close };
  }
  return { ...live, time: last.time + k * period };
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

export function PriceChart({ candles, zones, events, highlight, palette, resetKey, loading, quarters, quarterRows, sync, swings = NO_SWINGS, news = NO_NEWS,
  onNeedOlder, live = null, drawings = null, nav, levels = NO_LEVELS, timeframe, rangeLink = null, precision = 2, snapshotButton = true, viewKey }: Props) {
  counters.renders++; // the perf overlay's renders/s: panning and dragging a drawing should add none
  const liveBar = useLiveTick(live)?.bar ?? null; // only the chart re-renders on a tick, not its parent
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const zonesRef = useRef<ZonesPrimitive | null>(null);
  const markersRef = useRef<ISeriesMarkersPluginApi<Time> | null>(null);
  const quarterSeriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const quartersRef = useRef<QuartersPrimitive | null>(null);
  const newsRef = useRef<NewsPrimitive | null>(null);
  const sessionsRef = useRef<SessionsPrimitive | null>(null);
  const editorRef = useRef<DrawingEditor | null>(null);
  const quarterPaneHeight = useRef(0);
  const candlesRef = useRef<Candle[]>([]);
  const syncRef = useRef(sync);
  syncRef.current = sync;
  const rangeLinkRef = useRef(rangeLink);
  rangeLinkRef.current = rangeLink;
  const userAt = useRef(0); // when the trader last scrolled or zoomed this chart (performance.now)
  const applying = useRef(false); // setting a range that came from the link group
  const needOlderRef = useRef(onNeedOlder);
  needOlderRef.current = onNeedOlder;
  const fittedKey = useRef<string | null>(null);
  const resetKeyRef = useRef(resetKey);
  resetKeyRef.current = resetKey;
  const viewKeyRef = useRef(viewKey);
  viewKeyRef.current = viewKey;
  const hover = useMemo(hoverStore, []);
  const liveCandle = useMemo(() => (liveBar ? withLive(candles, liveBar) : null), [liveBar, candles]);
  const liveRef = useRef(liveCandle);
  liveRef.current = liveCandle;
  const market = useContext(ChartMarket);
  const marketRef = useRef(market);
  marketRef.current = market;
  const [scale, setScale] = useState<ScaleState>(AUTO_SCALE);
  const [chartType] = useChartType();
  const [settings] = useChartSettings();
  const settingsKey = JSON.stringify(settings);
  const [indicators] = useIndicators();
  const indicatorSeries = useRef<ISeriesApi<"Line">[]>([]);
  const rsiPaneRef = useRef(0); // the RSI pane's index, 0 when there is none
  const [indicatorRows, setIndicatorRows] = useState<LegendRow[]>([]);
  const heikinRef = useRef<Candle[] | null>(null); // the Heikin Ashi bars shown, when that's the chart type
  const [offLive, setOffLive] = useState(false); // the live candle is scrolled out of view
  const [ruler, setRuler] = useState<Ruler | null>(null);
  const [newsTip, setNewsTip] = useState<{ mark: NewsMark; x: number } | null>(null); // the news line under the pointer

  useEffect(() => {
    fittedKey.current = null; // a new chart (StrictMode makes two) sets or restores its view again
    const chart = createChart(containerRef.current!, {
      autoSize: true,
      crosshair: { mode: CrosshairMode.Normal },
      timeScale: { timeVisible: true, secondsVisible: false, rightOffset: 12 },
      rightPriceScale: { scaleMargins: { top: 0.08, bottom: 0.08 } },
    });
    const series = chart.addSeries(CandlestickSeries, { borderVisible: false, priceLineStyle: 2 });
    // Killzones and reference levels first: they sit under the zones and the news lines.
    const sessionsPrimitive = new SessionsPrimitive(palette);
    sessionsPrimitive.update({ font: FONT, formatPrice: fmtPrice });
    series.attachPrimitive(sessionsPrimitive);
    sessionsRef.current = sessionsPrimitive;
    const primitive = new ZonesPrimitive(palette);
    primitive.update({ font: FONT, formatPrice: fmtPrice });
    series.attachPrimitive(primitive);
    const newsPrimitive = new NewsPrimitive(palette);
    newsPrimitive.update({ font: FONT });
    series.attachPrimitive(newsPrimitive);
    newsRef.current = newsPrimitive;
    markersRef.current = createSeriesMarkers(series, []);
    const quartersPrimitive = new QuartersPrimitive(palette);
    quartersPrimitive.update({ font: FONT });
    chart.subscribeCrosshairMove((param) => {
      const bar = param.seriesData.get(series) as Candle | undefined;
      hover.set(bar && "open" in bar ? { ...bar, time: param.time as number } : null);
      quartersPrimitive.update({ hoverTime: param.time === undefined ? null : (param.time as number) });
      const tip = param.point && param.paneIndex === 0 ? newsPrimitive.markAt(param.point.x) : null;
      setNewsTip((cur) => (cur?.mark === tip?.mark ? cur : tip));
      // Only moves made by the pointer on this chart are passed on (not ones set by a linked chart).
      const link = syncRef.current;
      if (link && param.sourceEvent && param.time !== undefined && param.point) {
        const price = param.paneIndex === 0 ? series.coordinateToPrice(param.point.y) : null;
        link.bus.publish(link.id, { time: param.time as number, price });
      }
    });
    quartersRef.current = quartersPrimitive;
    // Near the left edge: page in older candles. New data keeps the view anchored on the right.
    chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      // Keep the view for the session, once this timeframe's view is set (not the default before it).
      const vk = viewKeyRef.current;
      const last = candlesRef.current.length - 1;
      if (vk && range && last >= 0 && fittedKey.current === resetKeyRef.current) {
        saveView(vk, { reset: resetKeyRef.current, fromEnd: last - range.from, toEnd: last - range.to });
      }
      if (range && range.from < 10) needOlderRef.current?.();
      const n = timesOf(candlesRef.current, liveRef.current).length;
      setOffLive(!!range && n > 0 && range.to < n - 1);
      // Pass on a scroll or zoom the trader made here, not new data or a linked chart's range.
      const link = rangeLinkRef.current;
      if (link && !applying.current && performance.now() - userAt.current < 400) {
        const vis = chart.timeScale().getVisibleRange();
        if (vis) linkBus().publish(link.group, `${link.id}:r`, { kind: "range", from: vis.from as number, to: vis.to as number });
      }
    });
    const touched = () => {
      userAt.current = performance.now();
    };
    for (const type of ["wheel", "pointerdown", "pointermove", "keydown", "touchmove"] as const) {
      containerRef.current!.addEventListener(type, (e) => {
        if (type !== "pointermove" || (e as PointerEvent).buttons) touched();
      }, { passive: true });
    }
    // Shift-drag measures: price change, pips, %, bars and time. It goes away on release.
    const el = containerRef.current!;
    const measureDown = (e: PointerEvent) => {
      if (!e.shiftKey || e.button !== 0) return;
      const r = el.getBoundingClientRect();
      const at = (ev: PointerEvent) => {
        const x = Math.min(Math.max(ev.clientX - r.left, 0), chart.timeScale().width());
        const y = Math.min(Math.max(ev.clientY - r.top, 0), chart.panes()[0]?.getHeight() ?? 0);
        const l = chart.timeScale().coordinateToLogical(x);
        const p = series.coordinateToPrice(y);
        return l === null || p === null ? null : { x, y, l, p };
      };
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      if (x > chart.timeScale().width() || y > (chart.panes()[0]?.getHeight() ?? 0)) return;
      const a = at(e);
      if (!a) return;
      // Take the press from the chart and the drawing editor: no pan, no drawing.
      e.preventDefault();
      e.stopImmediatePropagation();
      const show = (b: NonNullable<ReturnType<typeof at>>) =>
        setRuler({ x0: a.x, y0: a.y, l0: a.l, p0: a.p, x1: b.x, y1: b.y, l1: b.l, p1: b.p });
      const move = (ev: PointerEvent) => {
        const b = at(ev);
        if (b) show(b);
      };
      const end = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", end);
        window.removeEventListener("pointercancel", end);
        setRuler(null);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", end);
      window.addEventListener("pointercancel", end);
      show(a);
    };
    el.addEventListener("pointerdown", measureDown, true); // before the drawing editor's listener
    // Resizing the chart rescales every pane; keep the quarterly pane at its fixed height.
    const resize = new ResizeObserver(() => {
      const h = quarterPaneHeight.current;
      const rsi = rsiPaneRef.current;
      if (h || rsi) requestAnimationFrame(() => {
        if (chartRef.current) fitPanes(chartRef.current, h, rsi);
      });
    });
    resize.observe(containerRef.current!);
    // Drawings last: they sit above everything else on the price pane.
    const editor = new DrawingEditor(chart, series, containerRef.current!, palette);
    editor.update({ font: FONT });
    editorRef.current = editor;
    chartRef.current = chart;
    seriesRef.current = series;
    zonesRef.current = primitive;
    return () => {
      el.removeEventListener("pointerdown", measureDown, true);
      editor.destroy();
      editorRef.current = null;
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
        attributionLogo: false, // the TradingView notice and link are in the status bar instead
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
    newsRef.current?.update({ palette });
    sessionsRef.current?.update({ palette });
    editorRef.current?.update({ palette });
  }, [palette]);

  // The candles' look: filled, or hollow up candles (their border drawn instead).
  useEffect(() => {
    const hollow = chartType === "hollow";
    const bull = settings.bull ?? palette.bull;
    const bear = settings.bear ?? palette.bear;
    seriesRef.current?.applyOptions({
      upColor: hollow ? "rgba(0, 0, 0, 0)" : bull,
      downColor: bear,
      wickUpColor: bull,
      wickDownColor: bear,
      borderVisible: hollow,
      borderUpColor: bull,
      borderDownColor: bear,
      priceLineVisible: settings.priceLine,
    });
    chartRef.current?.applyOptions({
      grid: {
        vertLines: { visible: settings.grid === "both" || settings.grid === "vert" },
        horzLines: { visible: settings.grid === "both" || settings.grid === "horz" },
      },
      crosshair: { mode: settings.crosshair === "magnet" ? CrosshairMode.Magnet : CrosshairMode.Normal },
      timeScale: { rightOffset: settings.rightOffset },
    });
    quartersRef.current?.update({ colors: settings.quarterColors === "quarter" ? settings.quarters : null });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [palette, chartType, settingsKey]);


  // New candles: update only the bars that changed when that's all it is (a scan adds one or two at
  // the end); a full setData only when history changed (#34).
  const shownCandles = useRef<Candle[]>([]);
  const shownType = useRef(chartType);
  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
    if (shownType.current !== chartType) {
      shownType.current = chartType;
      shownCandles.current = []; // another chart type: draw them all again
    }
    if (chartType === "heikin") {
      // Each Heikin Ashi bar depends on the one before it: the whole run again (a scan, not a tick).
      const bars = heikin(candles);
      heikinRef.current = bars;
      series.setData(bars.map((c) => ({ ...c, time: c.time as UTCTimestamp })));
      counters.setData++;
      shownCandles.current = candles;
      candlesRef.current = candles;
      return;
    }
    heikinRef.current = null;
    const plan = planUpdate(shownCandles.current, candles);
    if (plan.kind === "set") {
      series.setData(candles.map((c) => ({ ...c, time: c.time as UTCTimestamp })));
      counters.setData++;
    } else if (plan.kind === "update") {
      try {
        for (const c of candles.slice(plan.from)) series.update({ ...c, time: c.time as UTCTimestamp });
        counters.update++;
      } catch {
        // A tick already opened a later candle than these: replace everything instead.
        series.setData(candles.map((c) => ({ ...c, time: c.time as UTCTimestamp })));
        counters.setData++;
      }
    }
    shownCandles.current = candles;
    candlesRef.current = candles;
  }, [candles, chartType]);

  useEffect(() => {
    const series = seriesRef.current;
    if (!series) return;
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
      }));
    // Swing labels: small dots with HH/LH above swing highs, HL/LL below swing lows, in muted ink.
    const known = new Set(times);
    const swingMarkers: SeriesMarker<Time>[] = swings
      .filter((sw) => sw.label && known.has(sw.time_unix))
      .map((sw) => ({
        time: sw.time_unix as UTCTimestamp,
        position: sw.kind === "high" ? ("aboveBar" as const) : ("belowBar" as const),
        shape: "circle" as const,
        color: palette.muted,
        text: sw.label!,
        size: 0.4,
      }));
    markersRef.current?.setMarkers([...markers, ...swingMarkers].sort((a, b) => (a.time as number) - (b.time as number)));
    if (candles.length && fittedKey.current !== resetKey) {
      const saved = viewKey ? loadView(viewKey) : null;
      const last = candles.length - 1;
      const chart = chartRef.current;
      const ts = chart?.timeScale();
      if (chart && ts && saved && saved.reset === resetKey) {
        // Just mounted, the chart has no width until autoSize measures it, and a range set before
        // that is lost: wait for the width, and save nothing until the view is back.
        const range = { from: last - saved.fromEnd, to: last - saved.toEnd };
        let tries = 0;
        const restore = () => {
          if (chartRef.current !== chart) return; // the chart was replaced
          if (ts.width() > 0 || tries++ > 60) {
            ts.setVisibleLogicalRange(range);
            fittedKey.current = resetKey;
          } else requestAnimationFrame(restore);
        };
        fittedKey.current = `restoring:${resetKey}`;
        restore();
      } else {
        ts?.fitContent();
        fittedKey.current = resetKey;
      }
    }
  }, [candles, zones, events, swings, palette, resetKey]);

  useEffect(() => {
    editorRef.current?.update({ ctl: drawings });
  }, [drawings]);

  useEffect(() => {
    editorRef.current?.update({ candles });
  }, [candles]);

  // Live ticks move the last candle between scans; the next scan's candles replace it. A chart out of
  // view (scrolled away, a collapsed pane) skips them and catches up when it shows again.
  const [onScreen, setOnScreen] = useState(true);
  useEffect(() => {
    const el = containerRef.current;
    if (!el || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(([e]) => setOnScreen(e.isIntersecting && e.boundingClientRect.width > 0));
    io.observe(el);
    return () => io.disconnect();
  }, []);
  useEffect(() => {
    if (!onScreen) {
      if (liveCandle) counters.skipped++;
      return;
    }
    if (liveCandle) {
      const ha = heikinRef.current;
      let prev: Candle | null = null;
      for (let i = (ha?.length ?? 0) - 1; ha && i >= 0 && !prev; i--) if (ha[i].time < liveCandle.time) prev = ha[i];
      const bar = ha ? heikinBar(liveCandle, prev) : liveCandle;
      seriesRef.current?.update({ ...bar, time: bar.time as UTCTimestamp });
      counters.ticks++;
    }
    editorRef.current?.update({ live: liveCandle }); // positions track the forming candle too
  }, [liveCandle, onScreen]);

  useEffect(() => {
    zonesRef.current?.update({ highlight });
  }, [highlight]);

  // The price scale: log or % from the session open, a locked range (autoScale off keeps the
  // range the chart has now, through new data), inverted.
  const sessionOpen = useMemo(() => {
    const s = quarters?.rows.session;
    return s?.length ? s[s.length - 1].open : null;
  }, [quarters]);
  const percent = scale.mode === "percent" && sessionOpen !== null;
  useEffect(() => {
    chartRef.current?.priceScale("right").applyOptions({
      mode: scale.mode === "log" ? PriceScaleMode.Logarithmic : PriceScaleMode.Normal,
      autoScale: !scale.locked,
      invertScale: scale.inverted,
    });
  }, [scale]);
  useEffect(() => {
    seriesRef.current?.applyOptions({
      priceFormat: percent
        ? { type: "custom", minMove: 0.01, formatter: (p: number) => `${fmtSigned((p / sessionOpen! - 1) * 100, 2)}%` }
        : { type: "price", precision, minMove: 10 ** -precision },
    });
  }, [percent, sessionOpen, precision]);

  // Time left on the forming candle, beside the price label. Without live ticks the forming
  // candle is the one after the last close.
  useEffect(() => {
    const series = seriesRef.current;
    if (!series || !candles.length) return;
    const step = stepOf(candles.map((c) => c.time));
    let shown = "";
    const tick = () => {
      const open = liveRef.current?.time ?? candles[candles.length - 1].time + step;
      const text = countdown(open, step, Date.now() / 1000 + marketRef.current.clockOffset) ?? "";
      if (text !== shown) series.applyOptions({ title: text });
      shown = text;
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => {
      clearInterval(id);
      series.applyOptions({ title: "" });
    };
  }, [candles]);

  const goPreset = (preset: Preset) => {
    const chart = chartRef.current;
    const start = quarters ? presetStart(quarters.rows, preset) : null;
    if (!chart || start === null) return;
    const times = timesOf(candlesRef.current, liveRef.current);
    // From the left edge of the candle holding the start to a little past the live one; older
    // candles page in when the start is before the first loaded one.
    chart.timeScale().setVisibleLogicalRange({ from: logicalOfTime(times, start) - 0.5, to: times.length + 2 });
  };
  const toLive = () => chartRef.current?.timeScale().scrollToRealTime();

  // Go to a time: centre it at the current zoom and put the crosshair on its candle. Before the
  // first loaded candle, scroll to the start so older candles load, then try again.
  const goToRef = useRef<{ t: number; first: number; tries: number } | null>(null);
  const applyGoTo = () => {
    const chart = chartRef.current;
    const series = seriesRef.current;
    const want = goToRef.current;
    if (!chart || !series || !want) return;
    const times = timesOf(candlesRef.current, liveRef.current);
    if (!times.length) return;
    const ts = chart.timeScale();
    const range = ts.getVisibleLogicalRange();
    const width = range ? range.to - range.from : 120;
    if (want.t < times[0] && want.tries < 30) {
      goToRef.current = { ...want, first: times[0], tries: want.tries + 1 };
      ts.setVisibleLogicalRange({ from: -2, to: width - 2 }); // near the start: older candles page in
      return;
    }
    goToRef.current = null;
    const l = logicalOfTime(times, want.t);
    ts.setVisibleLogicalRange({ from: l - width / 2, to: l + width / 2 });
    const bar = candlesRef.current.find((c) => c.time === candleAt(times, want.t));
    if (bar) chart.setCrosshairPosition(bar.close, bar.time as UTCTimestamp, series);
  };
  useEffect(() => {
    const want = goToRef.current;
    if (!want) return;
    if (candles.length && candles[0].time < want.first) applyGoTo(); // older candles came in
    else goToRef.current = null; // no older history: stay at the start
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [candles]);
  // Snapshot: the chart's own screenshot (every primitive and drawing is painted into it), with
  // the footer. The crosshair stays out.
  const capture = () => chartRef.current?.takeScreenshot(true, false) ?? null;
  const tfName = timeframe ?? resetKey;
  const snapshotInfo = () => ({ symbol: marketRef.current.symbol || "XAUUSD", timeframes: [tfName], at: Date.now(),
    clockOffset: marketRef.current.clockOffset, clockName: marketRef.current.clockName });
  const snapshot: SnapshotSource = {
    make: () => {
      const canvas = capture();
      const el = containerRef.current;
      if (!canvas || !el) return null;
      const w = el.clientWidth;
      const h = el.clientHeight;
      return compose([{ canvas, x: 0, y: 0, w, h }], w, h, snapshotInfo(),
        { surface: palette.surface, text: palette.textStrong, muted: palette.muted, grid: palette.grid, font: FONT });
    },
    name: () => fileName(snapshotInfo()),
  };
  const snapshotRef = useRef(snapshot);
  snapshotRef.current = snapshot;

  useImperativeHandle(nav, () => ({
    snapshot: () => {
      copySnapshot(snapshotRef.current).catch(() => {});
    },
    capture,
    snapshotSource: () => snapshotRef.current,
    reset: () => {
      const ts = chartRef.current?.timeScale();
      ts?.resetTimeScale();
      ts?.scrollToRealTime();
    },
    goTo: (t: number) => {
      goToRef.current = { t, first: Infinity, tries: 0 };
      applyGoTo();
    },
  }));

  useEffect(() => {
    newsRef.current?.update({ marks: news, times: candles.map((c) => c.time) });
  }, [news, candles]);

  useEffect(() => {
    sessionsRef.current?.update({ view: levels, times: candles.map((c) => c.time), zoneNames: !quarterRows.includes("session") });
  }, [levels, candles, quarterRows]);

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
        hover.set(null);
        quartersRef.current?.update({ hoverTime: null });
        return;
      }
      const bar = barAt(bars, p.time); // runs on every mouse move of every linked chart: no copies, no scans
      chart.setCrosshairPosition(p.price ?? bar.close, bar.time as UTCTimestamp, series);
      // The readout and the quarterly pane follow too.
      hover.set(bar);
      quartersRef.current?.update({ hoverTime: p.time });
    });
  }, [sync]);

  const onLeave = () => sync?.bus.publish(sync.id, null);

  // Scroll and zoom with the link group: the same stretch of time, whatever the timeframe.
  useEffect(() => {
    if (!rangeLink) return;
    return linkBus().subscribe(`${rangeLink.id}:r`, rangeLink.group, (m) => {
      const chart = chartRef.current;
      if (m.kind !== "range" || !chart || !candlesRef.current.length) return;
      applying.current = true;
      try {
        chart.timeScale().setVisibleRange({ from: m.from as UTCTimestamp, to: m.to as UTCTimestamp });
      } catch {
        /* outside this chart's candles: stay put */
      }
      requestAnimationFrame(() => requestAnimationFrame(() => {
        applying.current = false;
      }));
    });
  }, [rangeLink?.group, rangeLink?.id]); // eslint-disable-line react-hooks/exhaustive-deps

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
    fitPanes(chart, quarterPaneHeight.current, rsiPaneRef.current);
  }, [candles, quarters, quarterRows]);

  // Indicators: a line series each (three for Bollinger Bands) on the closed candles; RSI in a pane of
  // its own under the chart, after the quarterly pane when that shows.
  const hasQuarters = quarterRows.length > 0;
  const indicatorKey = JSON.stringify(indicators);
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    indicatorSeries.current = [];
    const closes = candles.map((c) => c.close);
    const rows: LegendRow[] = [];
    let rsiPane = 0;
    for (const ind of indicators) {
      const oscillator = ind.kind === "rsi";
      if (oscillator && !rsiPane) rsiPane = hasQuarters ? QUARTER_PANE + 1 : 1;
      const lines = linesOf(ind, closes);
      const values: Map<number, number>[] = [];
      for (const line of lines) {
        const sr = chart.addSeries(LineSeries, {
          color: ind.color,
          lineWidth: line.name === "basis" ? 1 : oscillator ? 1 : 2,
          lineStyle: line.name === "basis" ? 2 : 0,
          priceLineVisible: false,
          lastValueVisible: line.name !== "basis",
          crosshairMarkerVisible: false,
          title: line.name && line.name !== "basis" ? "" : indicatorLabel(ind),
          autoscaleInfoProvider: oscillator ? () => ({ priceRange: { minValue: 0, maxValue: 100 } }) : undefined,
        }, oscillator ? rsiPane : 0);
        const data: { time: UTCTimestamp; value: number }[] = [];
        const at = new Map<number, number>();
        line.values.forEach((v, i) => {
          if (v === null) return;
          data.push({ time: candles[i].time as UTCTimestamp, value: v });
          at.set(candles[i].time, v);
        });
        sr.setData(data);
        if (oscillator) {
          sr.createPriceLine({ price: 70, color: palette.muted, lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
          sr.createPriceLine({ price: 30, color: palette.muted, lineWidth: 1, lineStyle: 2, axisLabelVisible: false, title: "" });
        }
        indicatorSeries.current.push(sr);
        values.push(at);
      }
      rows.push({ label: indicatorLabel(ind), color: ind.color, values });
    }
    // A new pane takes its room from the others: give each fixed pane its height, again once laid out.
    const sizePanes = () => fitPanes(chart, hasQuarters ? quarterPaneHeight.current : 0, rsiPane);
    sizePanes();
    requestAnimationFrame(sizePanes);
    rsiPaneRef.current = rsiPane;
    setIndicatorRows(rows);
    const mine = indicatorSeries.current;
    return () => {
      for (const sr of mine) {
        try {
          chart.removeSeries(sr);
        } catch {
          // the chart itself is gone (unmounted, or rebuilt): its series went with it
        }
      }
      rsiPaneRef.current = 0;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [indicatorKey, candles, hasQuarters, palette]);

  let rulerView = null;
  if (ruler && containerRef.current) {
    const times = timesOf(candles, liveCandle);
    const [b0, b1] = [Math.round(ruler.l0), Math.round(ruler.l1)];
    const m = measure(ruler.p0, ruler.p1, b1 - b0, timeOfLogical(times, b1) - timeOfLogical(times, b0), market.pip);
    const { offsetLeft: ox, offsetTop: oy } = containerRef.current;
    const up = m.change >= 0;
    rulerView = (
      <div className="chart-ruler" aria-hidden="true">
        <div className={`chart-ruler-box ${up ? "up" : "down"}`} style={{
          left: ox + Math.min(ruler.x0, ruler.x1), top: oy + Math.min(ruler.y0, ruler.y1),
          width: Math.abs(ruler.x1 - ruler.x0), height: Math.abs(ruler.y1 - ruler.y0),
        }} />
        <div className={`chart-ruler-label num ${up ? "up" : "down"}`} style={{
          left: ox + ruler.x1, top: oy + ruler.y1 + (ruler.y1 >= ruler.y0 ? 10 : -10),
          transform: `translate(-50%, ${ruler.y1 >= ruler.y0 ? "0" : "-100%"})`,
        }}>
          <span>{fmtSigned(m.change, 2)}{m.pct !== null && ` (${fmtSigned(m.pct, 2)}%)`}</span>
          {m.pips !== null && <span>{fmtSigned(m.pips, 1)} pips</span>}
          <span>{m.bars} bars · {m.duration}</span>
        </div>
      </div>
    );
  }
  return (
    <div className="chart-wrap has-nav" aria-busy={loading} onMouseLeave={onLeave}>
      <ChartLegend hover={hover} fallback={liveCandle ?? candles[candles.length - 1]} quarters={quarters} quarterRows={quarterRows} indicators={indicatorRows} />
      {loading && <div className="chart-skeleton" aria-hidden="true" />}
      <div ref={containerRef} className="chart" role="img" aria-label="Price chart with order blocks, liquidity and inducement levels" />
      {rulerView}
      {newsTip && containerRef.current && (
        <div className="news-tip" role="tooltip" style={{
          left: containerRef.current.offsetLeft + Math.min(Math.max(newsTip.x, 140), containerRef.current.clientWidth - 140),
          top: containerRef.current.offsetTop + 64,
        }}>
          {newsTip.mark.events.map((e) => <b key={e}>{e}</b>)}
          {newsTip.mark.reactions.map((r) => <span key={r} className="num">{r}</span>)}
          {newsTip.mark.reactions.length === 0 && <span className="muted">No past releases of it stored yet</span>}
        </div>
      )}
      {offLive && (
        <button type="button" className="chart-live-btn" title="Scroll to the live candle (Alt+R also resets the zoom)" onClick={toLive}>
          Live <span aria-hidden="true">→</span>
        </button>
      )}
      <div className="chart-nav">
        <div className="chart-presets" role="group" aria-label="Time range">
          {PRESETS.map((p) => (
            <button key={p.id} type="button" className="chart-nav-btn" title={p.title} disabled={!quarters} onClick={() => goPreset(p.id)}>
              {p.label}
            </button>
          ))}
        </div>
        <span className="chart-nav-hint muted">Shift-drag to measure</span>
        <button type="button" className="chart-nav-btn" title="Keyboard shortcuts (?)" aria-label="Keyboard shortcuts"
          onClick={() => window.dispatchEvent(new Event(SHOW_SHORTCUTS))}>?</button>
        {snapshotButton && <SnapshotMenu source={snapshot} />}
        <ScaleMenu value={scale} onChange={setScale} percentOk={sessionOpen !== null} />
      </div>
    </div>
  );
}

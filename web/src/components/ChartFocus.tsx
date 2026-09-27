import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";

import type { CalendarEvent, QuartersResponse, Scan, TradeBias } from "../api";
import type { NewsMark } from "../newsPrimitive";
import type { LayerOptions } from "../chartData";
import { CrosshairBus } from "../crosshairSync";
import { CollapseIcon, LayoutIcon } from "../icons";
import type { LiveFeed } from "../liveData";
import { usePref } from "../prefs";
import type { RailItem } from "../rail";
import type { ChartPalette } from "../theme";
import { BiasPill } from "./BiasPill";
import { ChartPane } from "./ChartPane";
import { DrawingStyleBar } from "./DrawingStyleBar";
import { DrawingToolbar } from "./DrawingToolbar";
import type { Drawings } from "../drawingsData";
import { NewsAlert } from "./NewsAlert";
import { LiveTickerPrice } from "./TickerPrice";

type Layout = 1 | 2 | 4;
const LAYOUTS: { panes: Layout; title: string }[] = [
  { panes: 1, title: "One chart" },
  { panes: 2, title: "Two charts, side by side" },
  { panes: 4, title: "Four charts, 2 × 2" },
];
const DEFAULT_TFS = ["1H", "15M", "4H", "5M"];
const MIN_SPLIT = 0.15;
const clampSplit = (v: number) => Math.min(1 - MIN_SPLIT, Math.max(MIN_SPLIT, v));

interface Split {
  x: number; // share of the width taken by the left column
  y: number; // share of the height taken by the top row (2 × 2 only)
}

/**
 * A draggable line between panes. Arrow keys move it too; double-click
 * puts it back in the middle.
 */
function Splitter({ axis, value, onChange, onDone }: {
  axis: "x" | "y";
  value: number;
  onChange: (v: number) => void;
  onDone: (v: number) => void;
}) {
  const [dragging, setDragging] = useState(false);
  const last = useRef(value);
  last.current = value;
  const at = (e: React.PointerEvent) => {
    const grid = (e.currentTarget as HTMLElement).parentElement!.getBoundingClientRect();
    return clampSplit(axis === "x" ? (e.clientX - grid.left) / grid.width : (e.clientY - grid.top) / grid.height);
  };
  const step = (d: number) => {
    const v = clampSplit(value + d);
    onChange(v);
    onDone(v);
  };
  return (
    <div
      className={`splitter splitter-${axis}${dragging ? " is-dragging" : ""}`}
      role="separator"
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      aria-label={axis === "x" ? "Resize columns" : "Resize rows"}
      aria-valuemin={MIN_SPLIT * 100}
      aria-valuemax={(1 - MIN_SPLIT) * 100}
      aria-valuenow={Math.round(value * 100)}
      tabIndex={0}
      title="Drag to resize, double-click to reset"
      onPointerDown={(e) => {
        e.preventDefault();
        e.currentTarget.setPointerCapture(e.pointerId);
        setDragging(true);
      }}
      onPointerMove={(e) => dragging && onChange(at(e))}
      onPointerUp={(e) => {
        if (!dragging) return;
        e.currentTarget.releasePointerCapture(e.pointerId);
        setDragging(false);
        onDone(last.current);
      }}
      onDoubleClick={() => step(0.5 - value)}
      onKeyDown={(e) => {
        const back = axis === "x" ? "ArrowLeft" : "ArrowUp";
        const fwd = axis === "x" ? "ArrowRight" : "ArrowDown";
        if (e.key === back || e.key === fwd) {
          e.preventDefault();
          step((e.key === fwd ? 1 : -1) * (e.shiftKey ? 0.1 : 0.02));
        }
      }}
    />
  );
}

interface Toggle {
  label: string;
  title?: string; // full name when the label is short
  checked: boolean;
  onChange: (v: boolean) => void;
}

interface Props {
  symbol: string;
  scan: Scan;
  timeframes: string[];
  version: number;
  lookback: number;
  rail: RailItem[];
  layers: LayerOptions;
  palette: ChartPalette;
  quarters: QuartersResponse | null;
  showQuarters: boolean;
  toggles: Toggle[];
  tf: string; // the first chart follows the dashboard's timeframe
  onTf: (tf: string) => void;
  status: { cls: string; text: string };
  bias: TradeBias | null;
  news: NewsMark[];
  live: LiveFeed | null; // live ticks
  drawings: Drawings;
  upcomingNews: CalendarEvent[];
  onClose: () => void;
}

/**
 * Full-screen charts, TradingView style: one, two side by side or four in a
 * grid, each with its own timeframe. Uses the browser's full screen when it is
 * allowed; leaving it (Esc) closes the view.
 */
export function ChartFocus({ symbol, scan, timeframes, version, lookback, rail, layers, palette, quarters, showQuarters, toggles, tf, onTf, status, bias, news, live, drawings, upcomingNews, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [layout, setLayout] = usePref<Layout>("wed.focusLayout", 2);
  const [tfs, setTfs] = usePref<string[]>("wed.focusTfs", DEFAULT_TFS);
  const [savedSplit, saveSplit] = usePref<Split>("wed.focusSplit", { x: 0.5, y: 0.5 });
  const [split, setSplit] = useState<Split>(savedSplit); // follows the drag; saved when it ends
  const bus = useMemo(() => new CrosshairBus(), []);

  useEffect(() => {
    const el = ref.current;
    let entered = false;
    const onChange = () => {
      if (document.fullscreenElement) entered = true;
      else if (entered) onClose(); // Esc (or the browser's own exit) leaves the view too
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose(); // browsers usually take Esc to leave full screen first; this covers the rest
    };
    document.addEventListener("fullscreenchange", onChange);
    window.addEventListener("keydown", onKey);
    el?.requestFullscreen?.().catch(() => {
      /* not allowed (e.g. in a frame): the view still fills the window */
    });
    el?.focus();
    return () => {
      document.removeEventListener("fullscreenchange", onChange);
      window.removeEventListener("keydown", onKey);
      if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    };
  }, [onClose]);

  const paneTf = (i: number) => (i === 0 ? tf : tfs[i] ?? DEFAULT_TFS[i]);
  const setPaneTf = (i: number, value: string) => {
    if (i === 0) onTf(value);
    else setTfs(DEFAULT_TFS.map((d, j) => (j === i ? value : tfs[j] ?? d)));
  };
  const known = timeframes.length ? timeframes : ["4H", "1H", "30M", "15M", "5M"];

  return (
    <div ref={ref} className="focus" role="dialog" aria-modal="true" aria-label="Full screen charts" tabIndex={-1}>
      <header className="focus-bar">
        <div className="ticker">
          <span className="brand">Wednesday</span>
          <span className="focus-symbol">{symbol}</span>
          <LiveTickerPrice live={live} fallback={scan.price} />
          {bias && <BiasPill bias={bias} />}
          <span className={`live ${status.cls}`}>
            <span className="dot" aria-hidden="true" />
            {status.text}
          </span>
        </div>
        <div className="focus-tools">
          <DrawingToolbar ctl={drawings} row />
          <div className="segmented" role="group" aria-label="Layout">
            {LAYOUTS.map((l) => (
              <button key={l.panes} type="button" className="seg" aria-pressed={layout === l.panes} title={l.title} aria-label={l.title}
                onClick={() => setLayout(l.panes)}>
                <LayoutIcon panes={l.panes} size={15} />
              </button>
            ))}
          </div>
          <div className="toggles">
            {toggles.map((t) => (
              <label key={t.label} className="toggle" title={t.title}>
                <input type="checkbox" checked={t.checked} onChange={(e) => t.onChange(e.target.checked)} />
                {t.label}
              </label>
            ))}
          </div>
          <button type="button" className="button quiet focus-exit" onClick={onClose} title="Exit full screen (Esc)">
            <CollapseIcon /> Exit
          </button>
        </div>
      </header>
      <div className={`focus-grid layout-${layout}`}
        style={{ "--split-x": split.x, "--split-y": split.y } as CSSProperties}>
        {Array.from({ length: layout }, (_, i) => (
          <ChartPane key={i} label={`Chart ${i + 1}`} tf={paneTf(i)} onTf={(v) => setPaneTf(i, v)} timeframes={known}
            scan={scan} version={version} lookback={lookback} rail={rail} layers={layers} palette={palette}
            quarters={quarters} showQuarters={showQuarters} sync={{ bus, id: i }} news={news} live={live} drawings={drawings} />
        ))}
        {layout > 1 && (
          <Splitter axis="x" value={split.x} onChange={(x) => setSplit((s) => ({ ...s, x }))}
            onDone={(x) => saveSplit({ ...split, x })} />
        )}
        {layout === 4 && (
          <Splitter axis="y" value={split.y} onChange={(y) => setSplit((s) => ({ ...s, y }))}
            onDone={(y) => saveSplit({ ...split, y })} />
        )}
      </div>
      <DrawingStyleBar ctl={drawings} />
      <NewsAlert events={upcomingNews} />
    </div>
  );
}

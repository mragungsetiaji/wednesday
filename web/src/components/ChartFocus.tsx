import { useCallback, useContext, useEffect, useId, useMemo, useRef, useState, type CSSProperties } from "react";

import type { CalendarEvent, DetectorInfo, QuartersResponse, Scan, SessionsResponse } from "../api";
import type { NewsMark } from "../newsPrimitive";
import type { LayerOptions } from "../chartData";
import { CrosshairBus } from "../crosshairSync";
import { openChartWindow, useDesktopApi } from "../desktop";
import { CollapseIcon, LayoutIcon } from "../icons";
import type { LiveFeed } from "../liveData";
import { boundaries, LAYOUTS, layoutOf, moveBoundary, paneCells, paneCount, sizesFor, template, type LayoutId, type Sizes } from "../focusLayouts";
import { usePref } from "../prefs";
import type { RailItem } from "../rail";
import type { LevelGroup } from "../refLevels";
import type { ChartPalette } from "../theme";
import { ChartKeys } from "./ChartKeys";
import { ChartPane, type PaneLayers } from "./ChartPane";
import { ChartMarket, type ChartNav } from "./PriceChart";
import { DrawingStyleBar } from "./DrawingStyleBar";
import { DrawingToolbar } from "./DrawingToolbar";
import { LevelChecks } from "./LevelsMenu";
import { IndicatorsMenu, type QuartersIndicator } from "./ChartTopMenus";
import { usePopover } from "./usePopover";
import { TcIcon } from "../tcIcons";
import { SnapshotMenu, type SnapshotSource } from "./SnapshotMenu";
import { Toasts } from "./Toasts";
import { compose, fileName } from "../snapshot";
import type { Drawings } from "../drawingsData";
import { NewsAlert } from "./NewsAlert";

export const DEFAULT_TFS = ["1H", "15M", "4H", "5M", "30M", "4H"];

/**
 * A draggable line between two tracks, at `value` (a share of the grid from its start). Arrow
 * keys move it too; double-click shares the two tracks evenly. `from` insets it along its
 * length (a row line beside the big pane starts after that pane).
 */
export function Splitter({ axis, value, from = 0, label, onChange, onDone, onReset }: {
  axis: "x" | "y";
  value: number;
  from?: number;
  label: string;
  onChange: (v: number) => void;
  onDone: () => void;
  onReset: () => void;
}) {
  const [dragging, setDragging] = useState(false);
  const at = (e: React.PointerEvent) => {
    const grid = (e.currentTarget as HTMLElement).parentElement!.getBoundingClientRect();
    return axis === "x" ? (e.clientX - grid.left) / grid.width : (e.clientY - grid.top) / grid.height;
  };
  const step = (d: number) => {
    onChange(value + d);
    onDone();
  };
  const style = { "--at": value, "--from": from } as CSSProperties;
  return (
    <div
      className={`splitter splitter-${axis}${dragging ? " is-dragging" : ""}`}
      style={style}
      role="separator"
      aria-orientation={axis === "x" ? "vertical" : "horizontal"}
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(value * 100)}
      tabIndex={0}
      title="Drag to resize, double-click to share evenly"
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
        onDone();
      }}
      onDoubleClick={onReset}
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
  showNews: boolean;
  allDetectors: DetectorInfo[];
  sessions: SessionsResponse | null;
  levelGroups: LevelGroup[];
  onLevelGroups: (v: LevelGroup[]) => void;
  toggles: Toggle[];
  tf: string; // the first chart follows the dashboard's timeframe
  onTf: (tf: string) => void;
  news: NewsMark[]; // every high-impact release, whether the News toggle is on or not
  live: LiveFeed | null; // live ticks
  drawings: Drawings;
  source: string; // the data source, which a pane's own drawings are kept under
  onMainKeys?: (on: boolean) => void; // false while a pane on another market has the keyboard
  quartersCtl: QuartersIndicator;
  upcomingNews: CalendarEvent[];
  onClose: () => void;
}

/** The layout picker: the current layout's icon, opening every layout. */
export function LayoutMenu({ value, onChange, icon = false }: { value: string; onChange: (id: LayoutId) => void; icon?: boolean }) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  const current = LAYOUTS.find((l) => l.id === value) ?? LAYOUTS[0];
  return (
    <div className="tb-menu" ref={ref}>
      <button type="button" className={`tb-btn${icon ? " tb-icon" : ""}`} aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title={`Layout: ${current.title}`} aria-label={`Layout: ${current.title}`}>
        <LayoutIcon cols={current.cols} rows={current.rows} big={current.big} size={icon ? 16 : 15} />
        {!icon && <TcIcon name="chevronDown" size={12} />}
      </button>
      {open && (
        <div className={`chart-pop menu-pop layout-pop${icon ? " is-end" : ""}`} id={id} role="menu" aria-label="Layout">
          {LAYOUTS.map((l) => (
            <button key={l.id} type="button" role="menuitemradio" aria-checked={l.id === value}
              className={`draw-flyout-item${l.id === value ? " is-active" : ""}`}
              onClick={() => { onChange(l.id); setOpen(false); }}>
              <LayoutIcon cols={l.cols} rows={l.rows} big={l.big} size={15} />
              {l.title}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/** The full screen layers in one menu: the toggles and the reference levels. */
function FocusLayersMenu({ toggles, levelGroups, onLevelGroups }: {
  toggles: Toggle[];
  levelGroups: LevelGroup[];
  onLevelGroups: (v: LevelGroup[]) => void;
}) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  return (
    <div className="tb-menu" ref={ref}>
      <button type="button" className="tb-btn" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}
        title="Layers and levels on every chart (a chart's own layers button overrides them)">
        <TcIcon name="layers" />
        <span>Layers</span>
        <TcIcon name="chevronDown" size={12} />
      </button>
      {open && (
        <div className="chart-pop menu-pop" id={id} role="group" aria-label="Layers">
          {toggles.map((t) => (
            <label key={t.label} className="scale-opt" title={t.title}>
              <input type="checkbox" checked={t.checked} onChange={(e) => t.onChange(e.target.checked)} />
              {t.title ?? t.label}
            </label>
          ))}
          <hr />
          <LevelChecks value={levelGroups} onChange={onLevelGroups} />
        </div>
      )}
    </div>
  );
}

/**
 * Full-screen charts, TradingView style: one to six charts in a grid (columns, rows, 2 × 2,
 * 3 × 2, or a big chart with two or three stacked beside it), each with its own timeframe and,
 * if you like, its own layers. Uses the browser's full screen when it is allowed; leaving it
 * (Esc) closes the view.
 */
export function ChartFocus({ symbol, scan, timeframes, version, lookback, rail, layers, palette, quarters, showQuarters, showNews, allDetectors, sessions, levelGroups, onLevelGroups, toggles, tf, onTf, news, live, drawings, source, onMainKeys, quartersCtl, upcomingNews, onClose }: Props) {
  const ref = useRef<HTMLDivElement>(null);
  const [savedLayout, setLayoutId] = usePref<LayoutId | number>("wed.focusLayout", "2"); // 1 / 2 / 4 before more layouts
  const spec = layoutOf(savedLayout);
  const layout = paneCount(spec);
  const [tfs, setTfs] = usePref<string[]>("wed.focusTfs", DEFAULT_TFS);
  // Each pane's own symbol (null = the scanned one), e.g. FX:XAGUSD beside gold. Price only.
  const [paneSymbols, setPaneSymbols] = usePref<(string | null)[]>("wed.focusSymbols", []);
  // Track sizes per layout. The old one split (2 and 2 × 2) seeds those two.
  const [oldSplit] = usePref<{ x: number; y: number } | null>("wed.focusSplit", null);
  const [savedSizes, saveSizes] = usePref<Partial<Record<LayoutId, Sizes>>>("wed.focusSizes",
    oldSplit ? { "2": { cols: [oldSplit.x, 1 - oldSplit.x], rows: [1] }, "4": { cols: [oldSplit.x, 1 - oldSplit.x], rows: [oldSplit.y, 1 - oldSplit.y] } } : {});
  const [dragSizes, setDragSizes] = useState<Sizes | null>(null); // follows a drag; saved when it ends
  const sizes = dragSizes ?? sizesFor(spec, savedSizes);
  const [paneLayers, setPaneLayers] = usePref<Record<number, PaneLayers>>("wed.focusPaneLayers", {});
  const bus = useMemo(() => new CrosshairBus(), []);
  const [focused, setFocused] = useState(0); // the pane the keyboard shortcuts act on
  const navs = useRef<(ChartNav | null)[]>([]);
  const market = useContext(ChartMarket);
  const desktop = useDesktopApi();
  const pane = Math.min(focused, layout - 1);
  // Panes on another market keep their own drawings; the style bar and the keys follow the focused pane.
  const [paneCtls, setPaneCtls] = useState<Record<number, Drawings | null>>({});
  const reporters = useMemo(() => Array.from({ length: 6 }, (_, i) => (ctl: Drawings | null) =>
    setPaneCtls((m) => (m[i] === ctl ? m : { ...m, [i]: ctl }))), []);

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
  const mainSymbol = market.symbol || symbol;
  // In replay (no live feed) every pane shows the scanned market: another symbol would show its future.
  const paneSymbol = (i: number) => (live ? paneSymbols[i] ?? null : null);
  const setPaneSymbol = (i: number, value: string | null) =>
    setPaneSymbols(Array.from({ length: Math.max(paneSymbols.length, i + 1) }, (_, j) => (j === i ? value : paneSymbols[j] ?? null)));
  const paneLabel = (i: number) => { const s = paneSymbol(i); return s ? `${s} ${paneTf(i)}` : paneTf(i); };
  const focusedOther = !!paneSymbol(pane);
  const activeDrawings = (focusedOther && paneCtls[pane]) || drawings;
  useEffect(() => {
    onMainKeys?.(!focusedOther);
  }, [focusedOther, onMainKeys]);
  useEffect(() => () => onMainKeys?.(true), [onMainKeys]);
  const close = useCallback(() => onClose(), [onClose]);

  // Snapshot of the whole layout: each chart's screenshot where it sits in the grid, its
  // timeframe on it, one footer under all of them.
  const gridRef = useRef<HTMLDivElement>(null);
  const layoutInfo = () => ({ symbol: mainSymbol, timeframes: cells.map((_, i) => paneTf(i)), at: Date.now(),
    clockOffset: market.clockOffset, clockName: market.clockName });
  const layoutShot: SnapshotSource = {
    make: () => {
      const grid = gridRef.current;
      if (!grid) return null;
      const g = grid.getBoundingClientRect();
      const charts = grid.querySelectorAll<HTMLElement>(".pane .chart");
      const parts = cells.flatMap((_, i) => {
        const canvas = navs.current[i]?.capture();
        const el = charts[i];
        if (!canvas || !el) return [];
        const r = el.getBoundingClientRect();
        return [{ canvas, x: r.left - g.left, y: r.top - g.top, w: r.width, h: r.height, label: cells.length > 1 || paneSymbol(i) ? paneLabel(i) : undefined }];
      });
      if (!parts.length) return null;
      return compose(parts, g.width, g.height, layoutInfo(),
        { surface: palette.surface, text: palette.textStrong, muted: palette.muted, grid: palette.grid,
          font: getComputedStyle(document.documentElement).getPropertyValue("--font-ui").trim() || "system-ui, sans-serif" });
    },
    name: () => fileName(layoutInfo()),
  };
  const setPaneTf = (i: number, value: string) => {
    if (i === 0) onTf(value);
    else setTfs(DEFAULT_TFS.map((d, j) => (j === i ? value : tfs[j] ?? d)));
  };
  const cells = paneCells(spec);
  const resize = (axis: "cols" | "rows", i: number, at: number) =>
    setDragSizes({ ...sizes, [axis]: moveBoundary(sizes[axis], i, at) });
  const saveDrag = () => {
    if (dragSizes) saveSizes({ ...savedSizes, [spec.id]: dragSizes });
    setDragSizes(null);
  };
  const even = (axis: "cols" | "rows", i: number) => {
    const shares = sizes[axis];
    const half = (shares[i] + shares[i + 1]) / 2;
    const next = { ...sizes, [axis]: shares.map((v, j) => (j === i || j === i + 1 ? half : v)) };
    saveSizes({ ...savedSizes, [spec.id]: next });
  };
  // A row line beside the big pane starts after it, at the first column line.
  const rowFrom = spec.big ? boundaries(sizes.cols)[0] : 0;
  const known = timeframes.length ? timeframes : ["4H", "1H", "30M", "15M", "5M"];

  return (
    <div ref={ref} className="focus" role="dialog" aria-modal="true" aria-label="Full screen charts" tabIndex={-1}>
      {/* No page header: the tools, then Exit, which always shows (the tools scroll on a narrow screen). */}
      <header className="focus-bar">
        <div className="focus-tools" role="toolbar" aria-label="Full screen tools">
          <DrawingToolbar ctl={drawings} row />
          <span className="tb-sep" aria-hidden="true" />
          <LayoutMenu value={spec.id} onChange={setLayoutId} />
          <FocusLayersMenu toggles={toggles} levelGroups={levelGroups} onLevelGroups={onLevelGroups} />
          <IndicatorsMenu quarters={quartersCtl} />
          <SnapshotMenu source={layoutShot} label="Snapshot layout" className="tb-btn tb-icon" iconSize={16} />
        </div>
        <button type="button" className="button quiet focus-exit" onClick={close} title="Exit full screen (Esc or F)">
          <CollapseIcon /> Exit
        </button>
      </header>
      <div ref={gridRef} className={`focus-grid${layout > 1 ? " is-split has-focus" : ""}`}
        style={{ "--cols": template(sizes.cols), "--rows": template(sizes.rows) } as CSSProperties}>
        {cells.map((c, i) => (
          <ChartPane key={i} label={`Chart ${i + 1}`} tf={paneTf(i)} onTf={(v) => setPaneTf(i, v)} timeframes={known}
            scan={scan} version={version} lookback={lookback} rail={rail} layers={layers} palette={palette}
            quarters={quarters} showQuarters={showQuarters} showNews={showNews} allDetectors={allDetectors}
            sessions={sessions} levelGroups={levelGroups}
            own={paneLayers[i]} onOwn={(o) => {
              const { [i]: _old, ...rest } = paneLayers;
              setPaneLayers(Object.keys(o).length ? { ...rest, [i]: o } : rest);
            }}
            cell={c} sync={{ bus, id: i }} onPopOut={() => openChartWindow(desktop, paneTf(i))} news={news} live={live} drawings={drawings}
            nav={(h) => { navs.current[i] = h; }} focused={i === pane} onFocus={() => setFocused(i)}
            symbol={paneSymbol(i)} mainSymbol={mainSymbol} onSymbol={live ? (v) => setPaneSymbol(i, v) : undefined}
            source={source} onDrawings={reporters[i]} />
        ))}
        {boundaries(sizes.cols).map((at, i) => (
          <Splitter key={`x${i}`} axis="x" value={at} label="Resize columns" onChange={(v) => resize("cols", i, v)}
            onDone={saveDrag} onReset={() => even("cols", i)} />
        ))}
        {boundaries(sizes.rows).map((at, i) => (
          <Splitter key={`y${i}`} axis="y" value={at} from={rowFrom} label="Resize rows" onChange={(v) => resize("rows", i, v)}
            onDone={saveDrag} onReset={() => even("rows", i)} />
        ))}
      </div>
      <DrawingStyleBar ctl={activeDrawings} />
      <NewsAlert events={upcomingNews} />
      <Toasts />
      <ChartKeys active timeframes={known} setTf={(v) => setPaneTf(pane, v)} nav={() => navs.current[pane] ?? null}
        panes={layout} focusPane={setFocused} setTool={drawings.available ? drawings.setTool : null} toggleFullScreen={onClose}
        clockOffset={market.clockOffset} />
    </div>
  );
}

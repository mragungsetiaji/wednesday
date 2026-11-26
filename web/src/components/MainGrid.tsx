import { useMemo, useState, type CSSProperties, type ReactNode } from "react";

import type { DetectorInfo, QuartersResponse, Scan, SessionsResponse } from "../api";
import type { LayerOptions } from "../chartData";
import { CrosshairBus } from "../crosshairSync";
import { openChartWindow, useDesktopApi } from "../desktop";
import type { Drawings } from "../drawingsData";
import { boundaries, layoutOf, moveBoundary, paneCells, sizesFor, template, type LayoutId, type Sizes } from "../focusLayouts";
import type { LiveFeed } from "../liveData";
import type { NewsMark } from "../newsPrimitive";
import { usePref } from "../prefs";
import type { RailItem } from "../rail";
import type { LevelGroup } from "../refLevels";
import type { ChartPalette } from "../theme";
import { DEFAULT_TFS, Splitter } from "./ChartFocus";
import { ChartPane, type PaneLayers } from "./ChartPane";

interface Props {
  layout: LayoutId;
  main: ReactNode; // the main chart: the scanned market, on the toolbar's timeframe
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
  news: NewsMark[];
  live: LiveFeed | null;
  drawings: Drawings;
  source: string;
}

/**
 * The chart page's layouts (the layout button at the top right): the main chart always takes
 * the first place, left or top, and stays the scanned market and the toolbar's timeframe; the
 * other charts have their own timeframe, market and layers, kept with full screen's (chart 2 is
 * chart 2 in both).
 */
export function MainGrid({ layout, main, symbol, scan, timeframes, version, lookback, rail, layers, palette, quarters, showQuarters, showNews, allDetectors, sessions, levelGroups, news, live, drawings, source }: Props) {
  const spec = layoutOf(layout);
  const cells = paneCells(spec);
  const [tfs, setTfs] = usePref<string[]>("wed.focusTfs", DEFAULT_TFS);
  const [paneSymbols, setPaneSymbols] = usePref<(string | null)[]>("wed.focusSymbols", []);
  const [paneLayers, setPaneLayers] = usePref<Record<number, PaneLayers>>("wed.focusPaneLayers", {});
  const [savedSizes, saveSizes] = usePref<Partial<Record<LayoutId, Sizes>>>("wed.mainSizes", {});
  const [dragSizes, setDragSizes] = useState<Sizes | null>(null); // follows a drag; saved when it ends
  const sizes = dragSizes ?? sizesFor(spec, savedSizes);
  const bus = useMemo(() => new CrosshairBus(), []);
  const desktop = useDesktopApi();
  const known = timeframes.length ? timeframes : ["4H", "1H", "30M", "15M", "5M"];

  const paneTf = (i: number) => tfs[i] ?? DEFAULT_TFS[i];
  const setPaneTf = (i: number, value: string) => setTfs(DEFAULT_TFS.map((d, j) => (j === i ? value : tfs[j] ?? d)));
  const paneSymbol = (i: number) => (live ? paneSymbols[i] ?? null : null);
  const setPaneSymbol = (i: number, value: string | null) =>
    setPaneSymbols(Array.from({ length: Math.max(paneSymbols.length, i + 1) }, (_, j) => (j === i ? value : paneSymbols[j] ?? null)));

  const resize = (axis: "cols" | "rows", i: number, at: number) =>
    setDragSizes({ ...sizes, [axis]: moveBoundary(sizes[axis], i, at) });
  const saveDrag = () => {
    if (dragSizes) saveSizes({ ...savedSizes, [spec.id]: dragSizes });
    setDragSizes(null);
  };
  const even = (axis: "cols" | "rows", i: number) => {
    const shares = sizes[axis];
    const half = (shares[i] + shares[i + 1]) / 2;
    saveSizes({ ...savedSizes, [spec.id]: { ...sizes, [axis]: shares.map((v, j) => (j === i || j === i + 1 ? half : v)) } });
  };
  const rowFrom = spec.big ? boundaries(sizes.cols)[0] : 0;
  const first = cells[0];

  return (
    <div className="focus-grid main-grid is-split" style={{ "--cols": template(sizes.cols), "--rows": template(sizes.rows) } as CSSProperties}>
      <div className="pane is-main" aria-label="Chart 1, the main chart"
        style={{ "--col": first.col, "--row": first.row, "--row-span": first.rowSpan } as CSSProperties}>
        {main}
      </div>
      {cells.slice(1).map((c, j) => {
        const i = j + 1;
        return (
          <ChartPane key={i} label={`Chart ${i + 1}`} tf={paneTf(i)} onTf={(v) => setPaneTf(i, v)} timeframes={known}
            scan={scan} version={version} lookback={lookback} rail={rail} layers={layers} palette={palette}
            quarters={quarters} showQuarters={showQuarters} showNews={showNews} allDetectors={allDetectors}
            sessions={sessions} levelGroups={levelGroups}
            own={paneLayers[i]} onOwn={(o) => {
              const { [i]: _old, ...rest } = paneLayers;
              setPaneLayers(Object.keys(o).length ? { ...rest, [i]: o } : rest);
            }}
            cell={c} sync={{ bus, id: i }} onPopOut={() => openChartWindow(desktop, paneTf(i))} news={news} live={live} drawings={drawings}
            symbol={paneSymbol(i)} mainSymbol={symbol} onSymbol={live ? (v) => setPaneSymbol(i, v) : undefined} source={source} />
        );
      })}
      {boundaries(sizes.cols).map((at, i) => (
        <Splitter key={`x${i}`} axis="x" value={at} label="Resize columns" onChange={(v) => resize("cols", i, v)}
          onDone={saveDrag} onReset={() => even("cols", i)} />
      ))}
      {boundaries(sizes.rows).map((at, i) => (
        <Splitter key={`y${i}`} axis="y" value={at} from={rowFrom} label="Resize rows" onChange={(v) => resize("rows", i, v)}
          onDone={saveDrag} onReset={() => even("rows", i)} />
      ))}
    </div>
  );
}

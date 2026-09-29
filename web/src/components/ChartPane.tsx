import { useId, useMemo, type CSSProperties, type Ref } from "react";

import type { DetectorInfo, QuartersResponse, Scan } from "../api";
import type { CrosshairBus } from "../crosshairSync";
import type { DrawingCtl } from "../drawings";
import type { LiveFeed } from "../liveData";
import { buildEvents, buildZones, quarterRowsFor, useCandles, type LayerOptions } from "../chartData";
import { fmtPrice } from "../format";
import { Direction, LayersIcon } from "../icons";
import type { NewsMark } from "../newsPrimitive";
import type { RailItem } from "../rail";
import type { ChartPalette } from "../theme";
import { PriceChart, type ChartNav } from "./PriceChart";
import { usePopover } from "./usePopover";

/**
 * One pane's own layer choices over the full screen toggles; a missing key follows the
 * toggle. `lookback` is how many candles it loads.
 */
export interface PaneLayers {
  detectors?: string[];
  midOb?: boolean;
  htf?: boolean;
  swings?: boolean;
  quarters?: boolean;
  news?: boolean;
  lookback?: number;
}

const LOOKBACKS = [100, 200, 300, 500, 1000, 2000];

interface Props {
  tf: string;
  onTf: (tf: string) => void;
  timeframes: string[];
  scan: Scan;
  version: number;
  lookback: number;
  rail: RailItem[];
  layers: LayerOptions;
  palette: ChartPalette;
  quarters: QuartersResponse | null;
  showQuarters: boolean;
  showNews: boolean;
  allDetectors: DetectorInfo[];
  own?: PaneLayers; // this pane's overrides
  onOwn?: (o: PaneLayers) => void;
  label: string; // accessible name, e.g. "Chart 2"
  sync?: { bus: CrosshairBus; id: number };
  news?: NewsMark[];
  live?: LiveFeed | null;
  drawings?: DrawingCtl | null;
  nav?: Ref<ChartNav | null>;
  cell?: { col: number; row: number; rowSpan: number }; // its place in the full screen grid
  focused?: boolean; // receives the keyboard shortcuts (outlined when there are several panes)
  onFocus?: () => void;
}

/** One chart of the full-screen layout, with its own timeframe. */
export function ChartPane({ tf, onTf, timeframes, scan, version, lookback, rail, layers: global, palette, quarters, showQuarters, showNews,
  allDetectors, own = NO_OWN, onOwn, label, sync, news, live, drawings, nav, cell, focused, onFocus }: Props) {
  const layers = useMemo<LayerOptions>(() => ({
    detectors: own.detectors ? allDetectors.filter((d) => own.detectors!.includes(d.name)) : global.detectors,
    showHigherTf: own.htf ?? global.showHigherTf,
    showMidOb: own.midOb ?? global.showMidOb,
    showSwings: own.swings ?? global.showSwings,
  }), [own, allDetectors, global]);
  const quartersOn = own.quarters ?? showQuarters;
  const newsOn = own.news ?? showNews;
  const [chart, loadOlder] = useCandles(tf, version, own.lookback ?? lookback);
  const zones = useMemo(() => (chart ? buildZones(scan, chart, tf, rail, layers) : []), [scan, chart, tf, rail, layers]);
  const events = useMemo(() => buildEvents(scan, tf, layers.detectors), [scan, tf, layers.detectors]);
  const rows = useMemo(() => (quartersOn ? quarterRowsFor(tf) : []), [quartersOn, tf]);

  return (
    <section className={`pane${focused ? " is-focused" : ""}`} aria-label={`${label}: ${tf}`} onPointerDownCapture={onFocus}
      style={cell && ({ "--col": cell.col, "--row": cell.row, "--row-span": cell.rowSpan } as CSSProperties)}>
      <div className="pane-head">
        <div className="tabs tabs-compact" role="tablist" aria-label={`${label} timeframe`}>
          {timeframes.map((name) => {
            const b = scan.timeframes.find((t) => t.timeframe === name)?.bias;
            return (
              <button key={name} type="button" role="tab" aria-selected={name === tf} className="tab" onClick={() => onTf(name)}
                title={b ? `${name}: ${b.direction} ${b.event} at ${fmtPrice(b.level)}` : name}>
                {name}
                {b && name === tf && (
                  <span className={`dir ${b.direction}`}>
                    <Direction dir={b.direction} size={11} title={`${b.direction} ${b.event}`} />
                  </span>
                )}
              </button>
            );
          })}
        </div>
        {onOwn && (
          <PaneLayersMenu label={label} own={own} onOwn={onOwn} allDetectors={allDetectors} lookback={lookback} effective={{
            detectors: layers.detectors.map((d) => d.name), midOb: layers.showMidOb, htf: layers.showHigherTf,
            swings: layers.showSwings, quarters: quartersOn, news: newsOn, lookback: own.lookback ?? lookback,
          }} />
        )}
      </div>
      <PriceChart
        candles={chart?.candles ?? []}
        zones={zones}
        events={events}
        highlight={null}
        palette={palette}
        resetKey={tf}
        loading={!chart}
        onNeedOlder={loadOlder}
        quarters={quarters}
        quarterRows={rows}
        sync={sync}
        swings={layers.showSwings ? chart?.swings : undefined}
        news={newsOn ? news : undefined}
        live={live}
        drawings={drawings}
        nav={nav}
      />
    </section>
  );
}

const NO_OWN: PaneLayers = {};

const TOGGLES: { key: "midOb" | "htf" | "swings" | "quarters" | "news"; label: string }[] = [
  { key: "midOb", label: "Mid OBs" },
  { key: "htf", label: "Higher timeframes" },
  { key: "swings", label: "Swings" },
  { key: "quarters", label: "Quarters" },
  { key: "news", label: "News" },
];

/** A pane's layer menu: its own layers and candle count, or back to the full screen toggles. */
function PaneLayersMenu({ label, own, onOwn, allDetectors, lookback, effective }: {
  label: string;
  own: PaneLayers;
  onOwn: (o: PaneLayers) => void;
  allDetectors: DetectorInfo[];
  lookback: number;
  effective: Required<PaneLayers>;
}) {
  const { open, setOpen, ref } = usePopover<HTMLDivElement>();
  const id = useId();
  const custom = Object.keys(own).length > 0;
  const toggleDetector = (name: string, on: boolean) =>
    onOwn({ ...own, detectors: on ? [...effective.detectors, name] : effective.detectors.filter((n) => n !== name) });
  return (
    <div className="pane-layers" ref={ref}>
      <button type="button" className={`pane-layers-btn${custom ? " is-on" : ""}`} aria-expanded={open} aria-controls={id}
        title={custom ? `${label}: its own layers` : `${label}: layers (following the toggles above)`} aria-label={`${label} layers`}
        onClick={() => setOpen(!open)}>
        <LayersIcon size={14} />
        {custom && <span className="pane-layers-dot" aria-hidden="true" />}
      </button>
      {open && (
        <div className="chart-pop pane-layers-pop" id={id} role="group" aria-label={`${label} layers`}>
          {allDetectors.map((d) => (
            <label key={d.name} className="scale-opt">
              <input type="checkbox" checked={effective.detectors.includes(d.name)} onChange={(e) => toggleDetector(d.name, e.target.checked)} />
              {d.title}
            </label>
          ))}
          <hr />
          {TOGGLES.map((t) => (
            <label key={t.key} className="scale-opt">
              <input type="checkbox" checked={effective[t.key]} onChange={(e) => onOwn({ ...own, [t.key]: e.target.checked })} />
              {t.label}
            </label>
          ))}
          <hr />
          <label className="scale-opt pane-lookback">
            Candles
            <select value={own.lookback ?? ""} onChange={(e) => {
              const { lookback: _drop, ...rest } = own;
              onOwn(e.target.value ? { ...rest, lookback: Number(e.target.value) } : rest);
            }}>
              <option value="">Default ({lookback})</option>
              {LOOKBACKS.map((n) => <option key={n} value={n}>{n}</option>)}
            </select>
          </label>
          <p className="field-hint">{custom ? "This chart has its own layers." : "Follows the toggles above until you change one here."}</p>
          {custom && (
            <button type="button" className="button quiet scale-reset" onClick={() => onOwn({})}>Reset to the toggles above</button>
          )}
        </div>
      )}
    </div>
  );
}

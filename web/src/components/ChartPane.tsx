import { useEffect, useId, useMemo, type CSSProperties, type Ref } from "react";

import type { DetectorInfo, QuartersResponse, Scan, SessionsResponse } from "../api";
import type { CrosshairLink } from "../crosshairSync";
import type { LinkGroup } from "../windowLink";
import { useDrawings, type Drawings } from "../drawingsData";
import type { LiveFeed } from "../liveData";
import { useMarketCandles, useMarketLive } from "../marketData";
import { TICKERS } from "../marketSymbol";
import { buildEvents, buildZones, quarterRowsFor, useCandles, type LayerOptions } from "../chartData";
import { fmtPrice } from "../format";
import { Direction, LayersIcon, PopOutIcon } from "../icons";
import type { NewsMark } from "../newsPrimitive";
import type { RailItem } from "../rail";
import { levelGroups as knownGroups, levelView, type LevelGroup } from "../refLevels";
import type { ChartPalette } from "../theme";
import { LevelChecks } from "./LevelsMenu";
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
  levels?: LevelGroup[];
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
  sessions: SessionsResponse | null;
  levelGroups: LevelGroup[];
  own?: PaneLayers; // this pane's overrides
  onOwn?: (o: PaneLayers) => void;
  label: string; // accessible name, e.g. "Chart 2"
  sync?: { bus: CrosshairLink; id: number };
  rangeLink?: { group: LinkGroup; id: string } | null; // scroll and zoom with a link group
  onPopOut?: () => void; // open this chart in its own window
  news?: NewsMark[];
  live?: LiveFeed | null;
  drawings?: Drawings | null; // the scanned market's; a pane on another market keeps its own, with the same tool
  source?: string; // the data source, which drawings are kept under
  onDrawings?: (ctl: Drawings | null) => void; // a pane on another market hands up its drawings (the style bar edits them)
  nav?: Ref<ChartNav | null>;
  cell?: { col: number; row: number; rowSpan: number }; // its place in the full screen grid
  focused?: boolean; // receives the keyboard shortcuts (outlined when there are several panes)
  onFocus?: () => void;
  // Full screen: the pane's own symbol (null = the scanned one), shown from TradingView without levels.
  // Picked from the same markets as the main chart's menu.
  symbol?: string | null;
  mainSymbol?: string;
  onSymbol?: (symbol: string | null) => void;
  tickSeconds?: number; // live price interval for the pane's own symbol
}

/** One chart of the full-screen layout, with its own timeframe and, optionally, its own symbol. */
export function ChartPane(props: Props) {
  return props.symbol ? <MarketPane {...props} symbol={props.symbol} /> : <ScanPane {...props} />;
}

/** A pane on the scanned symbol: its candles with the scan's levels. */
function ScanPane({ tf, onTf, timeframes, scan, version, lookback, rail, layers: global, palette, quarters, showQuarters, showNews,
  allDetectors, sessions, levelGroups, own = NO_OWN, onOwn, label, sync, rangeLink, onPopOut, news, live, drawings, nav, cell, focused, onFocus,
  mainSymbol = "", onSymbol }: Props) {
  const layers = useMemo<LayerOptions>(() => ({
    detectors: own.detectors ? allDetectors.filter((d) => own.detectors!.includes(d.name)) : global.detectors,
    showHigherTf: own.htf ?? global.showHigherTf,
    showMidOb: own.midOb ?? global.showMidOb,
    showSwings: own.swings ?? global.showSwings,
  }), [own, allDetectors, global]);
  const quartersOn = own.quarters ?? showQuarters;
  const newsOn = own.news ?? showNews;
  const groupsOn = useMemo(() => (own.levels ? knownGroups(own.levels) : levelGroups), [own.levels, levelGroups]);
  const levels = useMemo(() => levelView(sessions, groupsOn), [sessions, groupsOn]);
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
        {onSymbol && <SymbolBox label={label} value={null} main={mainSymbol} onChange={onSymbol} />}
        {onPopOut && (
          <button type="button" className="pane-layers-btn" onClick={onPopOut} title={`Open ${tf} in its own window`}
            aria-label={`${label}: open in its own window`}>
            <PopOutIcon size={14} />
          </button>
        )}
        {onOwn && (
          <PaneLayersMenu label={label} own={own} onOwn={onOwn} allDetectors={allDetectors} lookback={lookback} effective={{
            detectors: layers.detectors.map((d) => d.name), midOb: layers.showMidOb, htf: layers.showHigherTf,
            swings: layers.showSwings, quarters: quartersOn, news: newsOn, levels: groupsOn, lookback: own.lookback ?? lookback,
          }} />
        )}
      </div>
      <PriceChart
        candles={chart?.candles ?? []}
        zones={zones}
        events={events}
        highlight={null}
        palette={palette}
        resetKey={`${mainSymbol}|${tf}`}
        timeframe={tf}
        viewKey={`pane:${label}:${mainSymbol}`}
        loading={!chart}
        onNeedOlder={loadOlder}
        quarters={quarters}
        quarterRows={rows}
        sync={sync}
        rangeLink={rangeLink}
        swings={layers.showSwings ? chart?.swings : undefined}
        news={newsOn ? news : undefined}
        levels={levels}
        live={live}
        drawings={drawings}
        nav={nav}
      />
    </section>
  );
}

const NO_OWN: PaneLayers = {};

/**
 * A pane on another symbol (e.g. FX:XAGUSD beside gold): TradingView candles and live price, and
 * drawings of its own (kept per symbol, drawn with the main chart's tool, without alerts). Levels,
 * setups and alerts belong to the scanned symbol, so none are drawn here.
 */
function MarketPane({ symbol, tf, onTf, timeframes, version, lookback, palette, showNews, own = NO_OWN, label, sync, rangeLink,
  news, nav, cell, focused, onFocus, mainSymbol = "", onSymbol, tickSeconds = 1, drawings: main, source, onDrawings }: Props & { symbol: string }) {
  const { chart, loadOlder, error } = useMarketCandles(symbol, tf, version, own.lookback ?? lookback);
  const live = useMarketLive(symbol, tickSeconds);
  const drawings = useDrawings(main ? source : undefined, symbol, 0, { pane: true, shared: main ?? undefined, keys: !!focused });
  useEffect(() => {
    onDrawings?.(main ? drawings : null);
  }, [drawings, main, onDrawings]);
  useEffect(() => () => onDrawings?.(null), [onDrawings]);
  const newsOn = own.news ?? showNews;
  return (
    <section className={`pane${focused ? " is-focused" : ""}`} aria-label={`${label}: ${symbol} ${tf}`} onPointerDownCapture={onFocus}
      style={cell && ({ "--col": cell.col, "--row": cell.row, "--row-span": cell.rowSpan } as CSSProperties)}>
      <div className="pane-head">
        <div className="tabs tabs-compact" role="tablist" aria-label={`${label} timeframe`}>
          {timeframes.map((name) => (
            <button key={name} type="button" role="tab" aria-selected={name === tf} className="tab" onClick={() => onTf(name)}>
              {name}
            </button>
          ))}
        </div>
        {onSymbol && <SymbolBox label={label} value={symbol} main={mainSymbol} onChange={onSymbol} />}
        <span className="pane-note" title="This symbol isn't scanned: no levels, setups or alerts. Candles and price from TradingView; drawings are kept for it.">
          Not scanned
        </span>
      </div>
      {error && !chart && <p className="pane-error" role="alert">{error}</p>}
      <PriceChart
        candles={chart?.candles ?? []}
        zones={NO_ZONES}
        events={NO_EVENTS}
        highlight={null}
        palette={palette}
        resetKey={`${symbol}|${tf}`}
        viewKey={`pane:${label}:${symbol}`}
        timeframe={tf}
        loading={!chart && !error}
        onNeedOlder={loadOlder}
        quarters={null}
        quarterRows={NO_ROWS}
        sync={sync}
        rangeLink={rangeLink}
        news={newsOn ? news : undefined}
        live={live}
        drawings={main ? drawings : null}
        nav={nav}
        precision={chart?.precision ?? 2}
      />
    </section>
  );
}

const NO_ZONES: never[] = [];
const NO_EVENTS: never[] = [];
const NO_ROWS: never[] = [];

/**
 * The pane's market, from the same list as the main chart's menu: the scanned one follows the
 * scan with its levels; another shows that market's candles beside it.
 */
function SymbolBox({ label, value, main, onChange }: {
  label: string;
  value: string | null;
  main: string;
  onChange: (symbol: string | null) => void;
}) {
  const id = useId();
  const shown = (value ?? main).toUpperCase();
  const listed = TICKERS.some((t) => t.symbol === shown);
  return (
    <span className="pane-symbol">
      <select id={id} value={shown} aria-label={`${label} market`}
        title={`Market of this chart. ${main} is scanned (levels, setups); another shows its price only.`}
        onKeyDown={(e) => e.stopPropagation() /* arrow keys pick a market, not the chart's shortcuts */}
        onChange={(e) => onChange(e.target.value === main.toUpperCase() ? null : e.target.value)}>
        {!listed && <option value={shown}>{shown}</option>}
        {TICKERS.map((t) => (
          <option key={t.symbol} value={t.symbol}>{t.short} · {t.name}{t.symbol === main.toUpperCase() ? " (scanned)" : ""}</option>
        ))}
      </select>
    </span>
  );
}

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
          <LevelChecks className="pane-levels" value={effective.levels} onChange={(v) => onOwn({ ...own, levels: v })} />
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

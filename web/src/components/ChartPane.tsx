import { useMemo } from "react";

import type { QuartersResponse, Scan } from "../api";
import type { CrosshairBus } from "../crosshairSync";
import type { DrawingCtl } from "../drawings";
import type { LiveFeed } from "../liveData";
import { buildEvents, buildZones, quarterRowsFor, useCandles, type LayerOptions } from "../chartData";
import { fmtPrice } from "../format";
import { Direction } from "../icons";
import type { NewsMark } from "../newsPrimitive";
import type { RailItem } from "../rail";
import type { ChartPalette } from "../theme";
import { PriceChart } from "./PriceChart";

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
  label: string; // accessible name, e.g. "Chart 2"
  sync?: { bus: CrosshairBus; id: number };
  news?: NewsMark[];
  live?: LiveFeed | null;
  drawings?: DrawingCtl | null;
}

/** One chart of the full-screen layout, with its own timeframe. */
export function ChartPane({ tf, onTf, timeframes, scan, version, lookback, rail, layers, palette, quarters, showQuarters, label, sync, news, live, drawings }: Props) {
  const [chart, loadOlder] = useCandles(tf, version, lookback);
  const zones = useMemo(() => (chart ? buildZones(scan, chart, tf, rail, layers) : []), [scan, chart, tf, rail, layers]);
  const events = useMemo(() => buildEvents(scan, tf, layers.detectors), [scan, tf, layers.detectors]);
  const rows = useMemo(() => (showQuarters ? quarterRowsFor(tf) : []), [showQuarters, tf]);

  return (
    <section className="pane" aria-label={`${label}: ${tf}`}>
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
        news={news}
        live={live}
        drawings={drawings}
      />
    </section>
  );
}

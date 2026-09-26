import { useEffect, useState } from "react";

import { fetchCandles, type CandlesResponse, type DetectorInfo, type Level, type QuarterRow, type Scan } from "./api";
import type { ChartEvent } from "./components/PriceChart";
import { contextZone, levelKey, railZone, type RailItem } from "./rail";
import type { Zone } from "./zonesPrimitive";

export interface LayerOptions {
  detectors: DetectorInfo[]; // the ones shown
  showHigherTf: boolean;
  showMidOb: boolean;
  showSwings: boolean; // HH / HL / LH / LL labels at swing points
}

/** Candles of one timeframe, refetched after every scan. */
export function useCandles(tf: string, version: number, lookback: number, onError?: (msg: string) => void) {
  const [chart, setChart] = useState<CandlesResponse | null>(null);
  useEffect(() => {
    if (!version) {
      setChart(null); // feed (re)starting: don't keep showing the previous source's candles
      return;
    }
    let alive = true;
    fetchCandles(tf, lookback)
      .then((res) => alive && setChart(res))
      .catch((e) => alive && onError?.(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tf, version, lookback]);
  return chart && chart.timeframe === tf ? chart : null;
}

/** Everything drawn on a chart of `tf`: its own levels, higher-timeframe context, the last break and the ladder. */
export function buildZones(scan: Scan, chart: CandlesResponse, tf: string, rail: RailItem[], opts: LayerOptions): Zone[] {
  const shown = new Set(opts.detectors.map((d) => d.name));
  const pinned = new Set(rail.map((i) => levelKey(i.tf, i.level)));
  const keep = (t: string, lv: Level) =>
    shown.has(lv.detector) && (opts.showMidOb || lv.meta.priority !== "middle") && !pinned.has(levelKey(t, lv));

  const own = chart.levels.filter((lv) => keep(tf, lv)).map((lv) => contextZone(tf, lv, false));
  const idx = scan.timeframes.findIndex((t) => t.timeframe === tf);
  const higher = opts.showHigherTf
    ? scan.timeframes.slice(0, Math.max(0, idx)).flatMap((t) =>
        opts.detectors.flatMap((d) =>
          (t.detectors[d.name]?.active ?? []).filter((lv) => keep(t.timeframe, lv)).map((lv) => contextZone(t.timeframe, lv, true)),
        ),
      )
    : [];
  const bias = scan.timeframes[idx]?.bias;
  const structure: Zone[] = bias
    ? [{
        id: `bos-${tf}`, role: "structure", mark: "segment", top: bias.level, bottom: bias.level,
        startTime: bias.swing_time_unix, endTime: bias.break_time_unix, label: bias.event,
      }]
    : [];
  return [...higher, ...own, ...structure, ...rail.map(railZone)];
}

/** Which side of the bar an event marker goes: sweeps of highs above, of lows below. */
function eventAbove(lv: Level): boolean {
  if (lv.detector === "liquidity") return lv.kind === "bsl";
  return lv.kind === "bearish";
}

function eventText(lv: Level): string {
  if (lv.detector === "ob") return `${lv.kind === "bullish" ? "Bull" : "Bear"} OB taken`;
  const what = lv.detector === "liquidity" ? lv.kind.toUpperCase() : "IDM";
  return `${what} ${lv.meta.grab ? "grab" : "break"}`;
}

/** Recent sweeps on `tf`, as chart markers. */
export function buildEvents(scan: Scan, tf: string, detectors: DetectorInfo[]): ChartEvent[] {
  const tfScan = scan.timeframes.find((t) => t.timeframe === tf);
  if (!tfScan) return [];
  return detectors.flatMap((d) =>
    (tfScan.detectors[d.name]?.recent ?? [])
      .filter((lv) => lv.ended_time_unix !== null)
      .map((lv) => ({ time: lv.ended_time_unix!, above: eventAbove(lv), text: eventText(lv) })),
  );
}

/** Quarterly rows worth drawing on a timeframe: 90-minute blocks get too thin on 4H. */
export const quarterRowsFor = (tf: string): QuarterRow[] => (tf === "4H" ? ["week", "session"] : ["week", "session", "q90"]);

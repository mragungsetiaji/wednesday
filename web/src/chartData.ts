import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchCandles, fetchOlderCandles, type Candle, type CandlesResponse, type DetectorInfo, type Level, type QuarterRow, type Scan } from "./api";
import type { ChartEvent } from "./components/PriceChart";
import { contextZone, levelKey, railZone, type RailItem } from "./rail";
import type { Zone } from "./zonesPrimitive";

export interface LayerOptions {
  detectors: DetectorInfo[]; // the ones shown
  showHigherTf: boolean;
  showMidOb: boolean;
  showSwings: boolean; // HH / HL / LH / LL labels at swing points
}

const OLDER_PAGE = 300; // candles per request when the chart scrolls back

/** Candles by time, sorted; `b` wins where both have a candle. */
function mergeCandles(a: Candle[], b: Candle[]): Candle[] {
  const byTime = new Map(a.map((c) => [c.time, c]));
  for (const c of b) byTime.set(c.time, c);
  return [...byTime.values()].sort((x, y) => x.time - y.time);
}

interface Loaded {
  tf: string;
  candles: Candle[]; // older pages, plus candles that moved out of the latest window
  more: boolean;
}

/**
 * Candles of one timeframe, refetched after every scan, and `loadOlder` to page back
 * in time when the chart reaches its left edge. The levels and swings are the latest scan's.
 */
export function useCandles(tf: string, version: number, lookback: number,
  onError?: (msg: string) => void): [CandlesResponse | null, () => void] {
  const [chart, setChart] = useState<CandlesResponse | null>(null);
  const [loaded, setLoaded] = useState<Loaded>({ tf, candles: [], more: true });
  const busy = useRef(false);

  useEffect(() => {
    if (!version) {
      setChart(null); // feed (re)starting: don't keep showing the previous source's candles
      setLoaded({ tf, candles: [], more: true });
      return;
    }
    let alive = true;
    fetchCandles(tf, lookback)
      .then((res) => {
        if (!alive) return;
        // Not urgent: the chart keeps answering the pointer while the new candles render.
        startTransition(() => {
          setChart(res);
          setLoaded((l) => (l.tf === tf ? { ...l, candles: mergeCandles(l.candles, res.candles) } : { tf, candles: res.candles, more: true }));
        });
      })
      .catch((e) => alive && onError?.(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tf, version, lookback]);

  const loadOlder = useCallback(() => {
    if (busy.current || loaded.tf !== tf || !loaded.more || !loaded.candles.length) return;
    busy.current = true;
    fetchOlderCandles(tf, loaded.candles[0].time, OLDER_PAGE)
      .then((res) => setLoaded((l) => (l.tf === tf
        ? { tf, candles: mergeCandles(res.candles, l.candles), more: res.has_more && res.candles.length > 0 }
        : l)))
      // MT5 closed or not connected: stop asking until the timeframe or feed changes.
      .catch(() => setLoaded((l) => (l.tf === tf ? { ...l, more: false } : l)))
      .finally(() => {
        busy.current = false;
      });
  }, [tf, loaded]);

  const merged = useMemo(
    () => (chart && chart.timeframe === tf ? { ...chart, candles: loaded.tf === tf ? loaded.candles : chart.candles } : null),
    [chart, loaded, tf]);
  return [merged, loadOlder];
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

import { startTransition, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { fetchMarketCandles, fetchMarketTick, type Candle, type MarketCandles, type TickResponse } from "./api";
import type { LiveFeed } from "./liveData";
import { foldTick, type Bar } from "./marketSymbol";

const OLDER_PAGE = 300;

function merge(a: Candle[], b: Candle[]): Candle[] {
  const byTime = new Map(a.map((c) => [c.time, c]));
  for (const c of b) byTime.set(c.time, c);
  return [...byTime.values()].sort((x, y) => x.time - y.time);
}

export interface MarketChart {
  chart: MarketCandles | null;
  loadOlder: () => void;
  error: string | null;
}

/**
 * Candles of a pane's own symbol (TradingView, /api/markets/candles), refetched after every scan of
 * the main symbol, and `loadOlder` to page back. No levels: only the source's symbol is scanned.
 */
export function useMarketCandles(symbol: string, tf: string, version: number, lookback: number): MarketChart {
  const [chart, setChart] = useState<MarketCandles | null>(null);
  const [older, setOlder] = useState<{ key: string; candles: Candle[]; more: boolean }>({ key: "", candles: [], more: true });
  const [error, setError] = useState<string | null>(null);
  const busy = useRef(false);
  const key = `${symbol}|${tf}`;

  useEffect(() => {
    let alive = true;
    fetchMarketCandles(symbol, tf, lookback)
      .then((res) => {
        if (!alive) return;
        setError(null);
        startTransition(() => {
          setChart(res);
          setOlder((o) => (o.key === key ? o : { key, candles: [], more: true }));
        });
      })
      .catch((e) => {
        if (!alive) return;
        setError(e instanceof Error ? e.message : String(e));
        setChart((c) => (c && c.symbol === symbol && c.timeframe === tf ? c : null));
      });
    return () => {
      alive = false;
    };
  }, [symbol, tf, lookback, version, key]);

  const loadOlder = useCallback(() => {
    const first = (older.key === key && older.candles.length ? older.candles : chart?.candles ?? [])[0];
    if (busy.current || !first || (older.key === key && !older.more)) return;
    busy.current = true;
    fetchMarketCandles(symbol, tf, OLDER_PAGE, first.time)
      .then((res) => setOlder((o) => {
        const base = o.key === key ? o.candles : [];
        return { key, candles: merge(res.candles, base), more: res.has_more };
      }))
      .catch(() => setOlder((o) => ({ key, candles: o.key === key ? o.candles : [], more: false })))
      .finally(() => {
        busy.current = false;
      });
  }, [symbol, tf, key, chart, older]);

  const merged = useMemo(() => {
    if (!chart || chart.symbol !== symbol || chart.timeframe !== tf) return null;
    return older.key === key && older.candles.length ? { ...chart, candles: merge(older.candles, chart.candles) } : chart;
  }, [chart, older, symbol, tf, key]);
  return { chart: merged, loadOlder, error };
}

/**
 * The live price of a pane's own symbol, polled every `seconds` (0 = off) while the tab is shown,
 * with the forming M1 candle built from those prices for the chart to fold in.
 */
export function useMarketLive(symbol: string, seconds: number): LiveFeed {
  const feed = useMemo(() => {
    let value: TickResponse | null = null;
    const listeners = new Set<() => void>();
    return {
      get: () => value,
      set: (t: TickResponse | null) => {
        value = t;
        listeners.forEach((l) => l());
      },
      subscribe: (l: () => void) => {
        listeners.add(l);
        return () => listeners.delete(l);
      },
    };
  }, []);

  useEffect(() => {
    feed.set(null);
    if (!seconds) return;
    let alive = true;
    let bar: Bar | null = null;
    let n = 0;
    const poll = async () => {
      if (document.hidden) return;
      try {
        const q = await fetchMarketTick(symbol);
        if (!alive || q.price === null || q.time === null) return;
        const prev = feed.get();
        if (prev && prev.price === q.price && prev.time === q.time) return;
        bar = foldTick(bar, q.price, q.time);
        feed.set({ version: 0, tick: ++n, price: q.price, time: q.time, bar });
      } catch {
        /* the candles request shows the error */
      }
    };
    poll();
    const id = setInterval(poll, Math.max(1000, seconds * 1000));
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [symbol, seconds, feed]);

  return feed;
}

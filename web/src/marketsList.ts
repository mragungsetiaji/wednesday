import { useEffect, useState } from "react";

import { fetchMarkets, type Market } from "./api";
import { tickerOf, TICKERS, type Ticker } from "./marketSymbol";

/**
 * The symbol menus' markets: the server's list (`/api/markets`, edited in Settings > Markets),
 * fetched once and shared by every menu; the built-in `TICKERS` until it loads or without a
 * database.
 */
let tickers: Ticker[] = TICKERS;
let loading: Promise<void> | null = null;
const listeners = new Set<(t: Ticker[]) => void>();

/** Settings > Markets saved a new list: every menu shows it at once. */
export function marketsChanged(markets: Market[]): void {
  tickers = markets.length ? markets.map(tickerOf) : TICKERS;
  listeners.forEach((l) => l(tickers));
}

function load(): void {
  loading ??= fetchMarkets()
    .then((res) => marketsChanged(res.markets))
    .catch(() => {
      loading = null; // try again when the next menu mounts
    });
}

export function useMarkets(): Ticker[] {
  const [list, setList] = useState(tickers);
  useEffect(() => {
    listeners.add(setList);
    load();
    setList(tickers);
    return () => {
      listeners.delete(setList);
    };
  }, []);
  return list;
}

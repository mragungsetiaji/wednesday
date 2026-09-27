import { useEffect, useMemo, useRef, useSyncExternalStore } from "react";

import { fetchTick, type TickResponse } from "./api";

/**
 * The latest live tick, outside React state: the component that polls never re-renders on a
 * tick, only the ones that read it with useLiveTick (the price, the ladder's price, the charts).
 */
export interface LiveFeed {
  get: () => TickResponse | null;
  subscribe: (listener: () => void) => () => void;
}

function createFeed(): LiveFeed & { set: (t: TickResponse | null) => void } {
  let value: TickResponse | null = null;
  const listeners = new Set<() => void>();
  return {
    get: () => value,
    set: (t) => {
      if (t === value) return;
      value = t;
      listeners.forEach((l) => l());
    },
    subscribe: (l) => {
      listeners.add(l);
      return () => listeners.delete(l);
    },
  };
}

/**
 * Polls the live price every `seconds` (0: off) while the tab is visible. Calls `onScan` when
 * the server has a newer scan than `version`, so the dashboard doesn't wait for its slow poll.
 * The returned feed never changes.
 */
export function useLiveFeed(seconds: number, version: number, onScan: () => void): LiveFeed {
  const feed = useMemo(createFeed, []);
  const versionRef = useRef(version);
  versionRef.current = version;
  const onScanRef = useRef(onScan);
  onScanRef.current = onScan;

  useEffect(() => {
    feed.set(null);
    if (!seconds) return;
    let alive = true;
    const poll = async () => {
      if (document.hidden) return;
      try {
        const t = await fetchTick();
        if (!alive) return;
        const prev = feed.get();
        if (!prev || prev.tick !== t.tick || prev.version !== t.version) feed.set(t);
        if (t.version > versionRef.current) onScanRef.current();
      } catch {
        /* the scan poll reports connection errors */
      }
    };
    poll();
    const id = setInterval(poll, Math.max(500, seconds * 1000));
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [seconds, feed]);

  return feed;
}

const none = () => null;
const noSubscribe = () => () => {};

/** The latest tick of `feed` (null before the first, or without a feed); re-renders on each tick. */
export const useLiveTick = (feed: LiveFeed | null | undefined): TickResponse | null =>
  useSyncExternalStore(feed?.subscribe ?? noSubscribe, feed?.get ?? none);

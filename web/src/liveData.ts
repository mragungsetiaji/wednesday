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
 * The live price from the server's push stream (/api/stream), with polling every `seconds` as the
 * fallback while the stream (re)connects; 0 turns both off. Ticks that arrive within one frame are
 * applied once. A hidden tab closes the stream and stops polling; it catches up when shown. Calls
 * `onScan` when the server has a newer scan than `version`. The returned feed never changes.
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
    let frame = 0;
    let pending: TickResponse | null = null;
    let stream: EventSource | null = null;

    const apply = (t: TickResponse) => {
      const prev = feed.get();
      if (!prev || prev.tick !== t.tick || prev.version !== t.version) feed.set(t);
      if (t.version > versionRef.current) onScanRef.current();
    };
    // One update per frame, however many ticks came in it.
    const push = (t: TickResponse) => {
      pending = t;
      if (frame) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        if (alive && pending) apply(pending);
        pending = null;
      });
    };
    const open = () => {
      if (stream || typeof EventSource === "undefined" || document.hidden) return;
      stream = new EventSource("/api/stream");
      stream.addEventListener("tick", (e) => {
        try {
          push(JSON.parse((e as MessageEvent<string>).data) as TickResponse);
        } catch {
          /* a broken message: the next one replaces it */
        }
      });
    };
    const close = () => {
      stream?.close();
      stream = null;
    };
    const poll = async () => {
      if (document.hidden || stream?.readyState === EventSource.OPEN) return;
      try {
        const t = await fetchTick();
        if (alive) push(t);
      } catch {
        /* the scan poll reports connection errors */
      }
    };
    const onVisibility = () => {
      if (document.hidden) close();
      else {
        open();
        poll();
      }
    };

    open();
    poll();
    const id = setInterval(poll, Math.max(500, seconds * 1000));
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      alive = false;
      close();
      clearInterval(id);
      cancelAnimationFrame(frame);
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [seconds, feed]);

  return feed;
}

const none = () => null;
const noSubscribe = () => () => {};

/** The latest tick of `feed` (null before the first, or without a feed); re-renders on each tick. */
export const useLiveTick = (feed: LiveFeed | null | undefined): TickResponse | null =>
  useSyncExternalStore(feed?.subscribe ?? noSubscribe, feed?.get ?? none);

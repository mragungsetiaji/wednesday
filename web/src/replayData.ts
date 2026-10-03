import { useCallback, useEffect, useRef, useState } from "react";

import { fetchReplay, fetchReplayRange, type ReplayResponse } from "./api";

/** Replay speeds, in M1 bars per second. */
export const SPEEDS = [1, 10, 60] as const;

export interface Replay {
  on: boolean;
  at: number | null; // the replay clock (unix, feed clock)
  data: ReplayResponse | null; // the market at the clock, once loaded
  range: { first: number; last: number } | null;
  playing: boolean;
  speed: number;
  error: string | null;
  start: (at?: number) => void;
  stop: () => void;
  seek: (at: number) => void;
  step: (minutes: number) => void;
  setPlaying: (p: boolean) => void;
  setSpeed: (s: number) => void;
}

/**
 * Bar replay: a clock that moves through the stored history, and the candles and scan at it from the
 * server (nothing after the clock). Playing advances `speed` minutes a second; when the server is
 * slower than that, the clock still moves and the newest view replaces the ones in between.
 */
export function useReplay(tf: string, lookback: number): Replay {
  const [on, setOn] = useState(false);
  const [at, setAt] = useState<number | null>(null);
  const [data, setData] = useState<ReplayResponse | null>(null);
  const [range, setRange] = useState<{ first: number; last: number } | null>(null);
  const [playing, setPlaying] = useState(false);
  const [speed, setSpeed] = useState<number>(10);
  const [error, setError] = useState<string | null>(null);
  const inFlight = useRef(false);
  const wanted = useRef<{ at: number; tf: string } | null>(null);

  const load = useCallback(async () => {
    if (inFlight.current || !wanted.current) return;
    inFlight.current = true;
    const want = wanted.current;
    try {
      const res = await fetchReplay(want.at, want.tf, lookback);
      setData(res);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPlaying(false);
    } finally {
      inFlight.current = false;
      if (wanted.current && (wanted.current.at !== want.at || wanted.current.tf !== want.tf)) load();
    }
  }, [lookback]);

  useEffect(() => {
    if (!on || at === null) return;
    wanted.current = { at, tf };
    load();
  }, [on, at, tf, load]);

  // Playing: the clock moves `speed` minutes per second, in 4 steps a second, and stops at the end.
  useEffect(() => {
    if (!on || !playing) return;
    const id = setInterval(() => {
      setAt((cur) => {
        if (cur === null) return cur;
        const next = cur + Math.max(1, Math.round(speed / 4)) * 60;
        if (range && next >= range.last) {
          setPlaying(false);
          return range.last;
        }
        return next;
      });
    }, 250);
    return () => clearInterval(id);
  }, [on, playing, speed, range]);

  const clamp = useCallback((t: number) => {
    if (!range) return t;
    return Math.min(Math.max(t, range.first), range.last);
  }, [range]);

  const start = useCallback((from?: number) => {
    setOn(true);
    setPlaying(false);
    setData(null);
    fetchReplayRange()
      .then((r) => {
        setRange(r);
        // Default: a day before the latest bar, on a whole minute.
        const t = from ?? r.last - 86400;
        setAt(Math.min(Math.max(t - (t % 60), r.first), r.last));
        setError(null);
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  const stop = useCallback(() => {
    setOn(false);
    setPlaying(false);
    setAt(null);
    setData(null);
    wanted.current = null;
  }, []);

  return {
    on, at, data, range, playing, speed, error, start, stop,
    seek: (t) => setAt(clamp(t - (t % 60))),
    step: (minutes) => setAt((cur) => (cur === null ? cur : clamp(cur + minutes * 60))),
    setPlaying, setSpeed,
  };
}

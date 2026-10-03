import { useEffect, useState } from "react";

import { SPEEDS, type Replay } from "../replayData";

/** Minutes in a timeframe name: "5M" → 5, "4H" → 240. */
export const tfMinutes = (tf: string) => {
  const n = parseInt(tf, 10) || 1;
  return tf.toUpperCase().endsWith("H") ? n * 60 : n;
};

/** Unix seconds on the feed clock ↔ the value of a datetime-local input showing that clock. */
const toInput = (t: number) => new Date(t * 1000).toISOString().slice(0, 16);
const fromInput = (v: string) => Math.floor(Date.parse(`${v}:00Z`) / 1000);

/**
 * The replay controls over the chart: the clock, a time to jump to, step back or forward a candle (or a
 * minute), play at 1, 10 or 60 minutes a second, and exit. Keys: Space plays or pauses, the arrows step
 * a candle, Shift+arrow a minute.
 */
export function ReplayBar({ replay, tf, clockName }: { replay: Replay; tf: string; clockName: string }) {
  const { at, range, playing, speed, error, data } = replay;
  const [draft, setDraft] = useState("");
  useEffect(() => {
    if (at !== null && !playing) setDraft(toInput(at));
  }, [at, playing]);
  const candle = tfMinutes(tf);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === "INPUT" || el.tagName === "SELECT" || el.tagName === "TEXTAREA" || el.isContentEditable)) return;
      if (e.key === " ") {
        e.preventDefault();
        replay.setPlaying(!playing);
      } else if (e.key === "ArrowRight" || e.key === "ArrowLeft") {
        e.preventDefault();
        const sign = e.key === "ArrowRight" ? 1 : -1;
        replay.step(sign * (e.shiftKey ? 1 : candle));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [replay, playing, candle]);

  return (
    <div className="replay-bar" role="group" aria-label="Bar replay">
      <span className="badge accent">Replay</span>
      <span className="replay-clock num" aria-live="off" title={`The replay clock, on the ${clockName} clock. Nothing after it is shown.`}>
        {at === null ? "Loading…" : toInput(at).replace("T", " ")}
      </span>
      <form className="replay-jump" onSubmit={(e) => {
        e.preventDefault();
        if (draft) replay.seek(fromInput(draft));
      }}>
        <label className="sr-only" htmlFor="replay-at">Jump to</label>
        <input id="replay-at" type="datetime-local" value={draft} disabled={playing}
          min={range ? toInput(range.first) : undefined} max={range ? toInput(range.last) : undefined}
          onChange={(e) => setDraft(e.target.value)} />
        <button type="submit" className="button quiet" disabled={playing || !draft}>Go</button>
      </form>
      <div className="replay-controls">
        <button type="button" className="icon-button" onClick={() => replay.step(-candle)} disabled={playing}
          title={`Back one ${tf} candle (←)`} aria-label={`Back one ${tf} candle`}>‹</button>
        <button type="button" className="button secondary replay-play" onClick={() => replay.setPlaying(!playing)}
          aria-pressed={playing} title="Play or pause (Space)">
          {playing ? "Pause" : "Play"}
        </button>
        <button type="button" className="icon-button" onClick={() => replay.step(1)} disabled={playing}
          title="Forward one minute (Shift+→)" aria-label="Forward one minute">+1m</button>
        <button type="button" className="icon-button" onClick={() => replay.step(candle)} disabled={playing}
          title={`Forward one ${tf} candle (→)`} aria-label={`Forward one ${tf} candle`}>›</button>
        <label className="replay-speed">
          <span className="sr-only">Speed</span>
          <select value={speed} onChange={(e) => replay.setSpeed(Number(e.target.value))}>
            {SPEEDS.map((s) => <option key={s} value={s}>{s} min/s</option>)}
          </select>
        </label>
      </div>
      {error ? <span className="text-error replay-note">{error}</span>
        : data && <span className="muted replay-note">Quarters, sessions and news lines are off in replay.</span>}
      <button type="button" className="button quiet replay-exit" onClick={replay.stop}>Exit replay</button>
    </div>
  );
}

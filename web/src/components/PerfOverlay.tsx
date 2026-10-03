import { useEffect, useState } from "react";

import { counters } from "../perf";

/**
 * A small debug readout (off by default): frames per second and, per second, full candle loads
 * (setData), incremental updates, live ticks applied and ticks skipped by charts out of view.
 * Alt+Shift+P toggles it; `?perf` in the address starts with it on.
 */
export function PerfOverlay() {
  const [on, setOn] = useState(() => new URLSearchParams(window.location.search).has("perf"));
  const [stats, setStats] = useState({ fps: 0, setData: 0, update: 0, ticks: 0, skipped: 0, charts: 0 });

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.altKey && e.shiftKey && e.code === "KeyP") {
        e.preventDefault();
        setOn((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    if (!on) return;
    let frames = 0;
    let raf = 0;
    const loop = () => {
      frames++;
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);
    let last = { ...counters };
    const id = setInterval(() => {
      const now = { ...counters };
      setStats({
        fps: frames, setData: now.setData - last.setData, update: now.update - last.update,
        ticks: now.ticks - last.ticks, skipped: now.skipped - last.skipped,
        charts: document.querySelectorAll(".tv-lightweight-charts").length,
      });
      last = now;
      frames = 0;
    }, 1000);
    return () => {
      cancelAnimationFrame(raf);
      clearInterval(id);
    };
  }, [on]);

  if (!on) return null;
  return (
    <div className="perf-overlay num" role="status" aria-label="Chart performance">
      <span>{stats.fps} fps</span>
      <span>{stats.charts} charts</span>
      <span>{stats.setData} loads/s</span>
      <span>{stats.update} updates/s</span>
      <span>{stats.ticks} ticks/s</span>
      <span>{stats.skipped} skipped/s</span>
    </div>
  );
}

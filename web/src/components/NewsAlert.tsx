import { useEffect, useMemo, useState } from "react";

import type { CalendarEvent } from "../api";
import { reactionLines } from "../newsReaction";

const SOON_MS = 60 * 60_000; // the card appears an hour before
const IMMINENT_MS = 30 * 60_000; // and starts to glow in the last half hour
const AFTER_MS = 10 * 60_000; // it stays for ten minutes after the release
const STORE_KEY = "wed.newsDismissed";

type Stage = "soon" | "imminent" | "released";

function loadDismissed(): string[] {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || "[]") as string[];
  } catch {
    return [];
  }
}

function saveDismissed(keys: string[]) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify(keys.slice(-50)));
  } catch {
    /* not remembered across reloads */
  }
}

const pad = (n: number) => String(n).padStart(2, "0");
function countdown(ms: number): string {
  const s = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(s / 3600);
  return h ? `${h}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}` : `${pad(Math.floor(s / 60))}:${pad(s % 60)}`;
}

const localTime = (d: Date) => d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const nyTime = (d: Date) => d.toLocaleTimeString("en-US", { hour: "2-digit", minute: "2-digit", timeZone: "America/New_York" });

/**
 * Risk-time warning, bottom right: the next high-impact release within the
 * hour, with a countdown. In the last 30 minutes a light runs around the card.
 * Closing it during the first half hour brings it back once at 30 minutes;
 * closing it after that hides it for this release.
 */
export function NewsAlert({ events }: { events: CalendarEvent[] }) {
  const [now, setNow] = useState(Date.now());
  const [dismissed, setDismissed] = useState<string[]>(loadDismissed);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  // Releases at the same minute (CPI m/m, y/y, core...) are one card.
  const next = useMemo(() => {
    const byTime = new Map<string, CalendarEvent[]>();
    for (const e of events) byTime.set(e.time, [...(byTime.get(e.time) ?? []), e]);
    for (const [time, group] of [...byTime.entries()].sort(([a], [b]) => a.localeCompare(b))) {
      const left = Date.parse(time) - now;
      if (left > SOON_MS) break;
      if (left < -AFTER_MS) continue;
      return { time, group, left };
    }
    return null;
  }, [events, now]);

  if (!next) return null;
  const stage: Stage = next.left <= 0 ? "released" : next.left <= IMMINENT_MS ? "imminent" : "soon";
  // Dismissing in the first half hour only snoozes it until the 30-minute mark.
  const key = `${next.time}|${stage === "soon" ? "soon" : "late"}`;
  if (dismissed.includes(key)) return null;

  const when = new Date(next.time);
  const first = next.group[0];
  const close = () => {
    const keys = [...dismissed, key];
    setDismissed(keys);
    saveDismissed(keys);
  };

  return (
    <aside className={`news-card is-${stage}`} role="status" aria-live="polite" aria-label="Upcoming high-impact news">
      <div className="news-inner">
        <div className="news-head">
          <span className="news-kicker">
            <span className="news-dot" aria-hidden="true" />
            {stage === "released" ? "Just released" : "Risk time"} · {first.impact} impact {first.currency}
          </span>
          <button type="button" className="news-close" onClick={close}
            aria-label={stage === "soon" ? "Hide until 30 minutes before" : "Hide for this release"}
            title={stage === "soon" ? "Hide until 30 minutes before" : "Hide for this release"}>
            <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
              <path d="M2.5 2.5l7 7M9.5 2.5l-7 7" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
            </svg>
          </button>
        </div>
        <p className="news-title">
          {next.group.slice(0, 3).map((e) => e.title).join(", ")}
          {next.group.length > 3 && <span className="muted"> +{next.group.length - 3} more</span>}
        </p>
        <div className="news-row">
          <span className="news-count num">
            {stage === "released" ? `${Math.max(1, Math.round(-next.left / 60_000))} min ago` : `in ${countdown(next.left)}`}
          </span>
          <span className="news-time num">{localTime(when)} · {nyTime(when)} NY</span>
        </div>
        {(first.forecast || first.previous) && (
          <p className="news-figures num">
            {first.title}: forecast {first.forecast ?? "–"} · previous {first.previous ?? "–"}
          </p>
        )}
        {reactionLines(next.group, 2).map((line) => (
          <p key={line} className="news-figures news-reaction num" title="Gold's move after past releases of it, from stored M1 bars">
            {line}
          </p>
        ))}
      </div>
    </aside>
  );
}

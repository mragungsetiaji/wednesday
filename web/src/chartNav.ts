/**
 * Chart navigation helpers: where the time range presets start, the measure readout and the
 * bar close countdown. Pure functions, so they can be tested without a chart.
 */
import type { QuarterBlock, QuarterRow } from "./api";

export type Preset = "day" | "session" | "week" | "5d" | "1m";
export const PRESETS: { id: Preset; label: string; title: string }[] = [
  { id: "day", label: "Today", title: "Today: the trading day since 18:00 New York" },
  { id: "session", label: "Session", title: "This session: Tokyo, London, NY AM or NY PM, in New York time" },
  { id: "week", label: "Week", title: "This week, from Sunday 18:00 New York" },
  { id: "5d", label: "5D", title: "The last five trading days" },
  { id: "1m", label: "1M", title: "The last month" },
];

const dayNum = (day: string) => Date.parse(`${day}T00:00Z`) / 86_400_000;
const dayStr = (n: number) => new Date(n * 86_400_000).toISOString().slice(0, 10);

/**
 * The feed-clock time a preset starts at, from the quarter blocks (the server cuts them in
 * New York time, so the presets follow New York whatever the feed clock). Null without blocks.
 * Trading days start 18:00 New York the evening before; `day` is the day they end on.
 */
export function presetStart(rows: Partial<Record<QuarterRow, QuarterBlock[]>>, preset: Preset): number | null {
  const days = rows.week ?? [];
  const today = days[days.length - 1];
  if (preset === "session") {
    const sessions = rows.session ?? [];
    return sessions.length ? sessions[sessions.length - 1].start_unix : null;
  }
  if (!today) return null;
  if (preset === "day") return today.start_unix;
  if (preset === "5d") return days[Math.max(0, days.length - 5)].start_unix;
  const n = dayNum(today.day);
  let from: string;
  if (preset === "week") {
    const weekday = (new Date(n * 86_400_000).getUTCDay() + 6) % 7; // Monday 0
    from = dayStr(n - weekday);
  } else {
    const d = new Date(n * 86_400_000);
    d.setUTCMonth(d.getUTCMonth() - 1);
    from = dayStr(d.getTime() / 86_400_000 + 1); // a month back, not counting that day itself
  }
  return (days.find((b) => b.day >= from) ?? today).start_unix;
}

/** "45m", "2h 35m", "3d 4h": a duration in seconds, to the minute. */
export function fmtDuration(seconds: number): string {
  const m = Math.round(Math.abs(seconds) / 60);
  const [d, h, min] = [Math.floor(m / 1440), Math.floor((m % 1440) / 60), m % 60];
  if (d) return h ? `${d}d ${h}h` : `${d}d`;
  if (h) return min ? `${h}h ${min}m` : `${h}h`;
  return `${min}m`;
}

/** "04:59" or "1:04:59": time left on a candle; null when it isn't running (closed market, stale bar). */
export function countdown(open: number, step: number, now: number): string | null {
  const left = Math.ceil(open + step - now);
  if (left <= 0 || left > step) return null;
  const [h, m, s] = [Math.floor(left / 3600), Math.floor((left % 3600) / 60), left % 60];
  const two = (v: number) => String(v).padStart(2, "0");
  return h ? `${h}:${two(m)}:${two(s)}` : `${two(m)}:${two(s)}`;
}

/** What a measure from (t0, p0) to (t1, p1) spans. `pip` is the symbol's pip in price. */
export function measure(p0: number, p1: number, bars: number, seconds: number, pip: number) {
  const change = p1 - p0;
  return { change, pips: pip > 0 ? change / pip : null, pct: p0 ? (change / p0) * 100 : null, bars, duration: fmtDuration(seconds) };
}

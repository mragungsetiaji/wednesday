/**
 * Chart keyboard shortcuts: the table (the cheat sheet and the docs list the same), the
 * typed timeframe and the go-to-date text. Keys never fire while typing in a field.
 */

export interface Shortcut {
  keys: string;
  action: string;
  where?: string; // only there; otherwise on the chart and in full screen
}

export const SHORTCUTS: Shortcut[] = [
  { keys: "5, 15, 1h, 4h … then Enter", action: "Switch the focused chart's timeframe" },
  { keys: "Alt+1 … Alt+4", action: "Focus chart 1 to 4", where: "Full screen" },
  { keys: "Alt+R", action: "Reset the zoom and scroll to the live candle" },
  { keys: "Alt+G", action: "Go to a date and time (New York or feed clock)" },
  { keys: "Alt+H", action: "Horizontal line tool" },
  { keys: "Alt+T", action: "Trendline tool" },
  { keys: "Alt+B", action: "Rectangle (box) tool" },
  { keys: "F", action: "Full screen on / off" },
  { keys: "?", action: "This list" },
  { keys: "Shift+drag", action: "Measure a move" },
  { keys: "Enter", action: "Finish a path" },
  { keys: "Esc", action: "Cancel the tool or the selection; leave full screen" },
  { keys: "Delete", action: "Delete the selected drawing" },
  { keys: "Ctrl+Z / ⌘Z", action: "Undo a drawing change" },
  { keys: "Ctrl+Shift+Z, Ctrl+Y / ⌘⇧Z", action: "Redo" },
];

export const typing = (el: EventTarget | null) =>
  el instanceof HTMLElement && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));

/**
 * The timeframe a typed text means: "5" and "15" are minutes, "1h" and "4h" hours, "d" or "1d"
 * a day. Null unless it's one of `known` (the chart's timeframe names, e.g. "15M", "4H").
 */
export function parseTimeframe(text: string, known: string[]): string | null {
  const m = /^(\d*)([mhd]?)$/i.exec(text.trim());
  if (!m || (!m[1] && !m[2])) return null;
  const n = m[1] || "1";
  const unit = (m[2] || "m").toUpperCase();
  const wanted = unit === "D" ? [`${n}D`, `D${n}`] : [`${n}${unit}`, `${unit}${n}`];
  return known.find((k) => wanted.includes(k.toUpperCase())) ?? null;
}

export type Clock = "ny" | "feed";

/** Wall-clock date and time parts (no zone): months are 1-12. */
export interface Wall {
  y: number;
  mo: number;
  d: number;
  h: number;
  mi: number;
}

const DAYS = ["sunday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday"];
const MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];

const dayIndex = (w: string) => DAYS.findIndex((d) => w.length >= 3 && d.startsWith(w));
const shift = (w: Wall, days: number): Wall => {
  const t = new Date(Date.UTC(w.y, w.mo - 1, w.d + days));
  return { ...w, y: t.getUTCFullYear(), mo: t.getUTCMonth() + 1, d: t.getUTCDate() };
};
const weekday = (w: Wall) => new Date(Date.UTC(w.y, w.mo - 1, w.d)).getUTCDay();

/**
 * A go-to text in the given clock's wall time: "last Wednesday 09:30 NY", "yesterday 8pm",
 * "fri 14:00 feed", "2026-09-24 09:30", "24 Sep 3:15pm", "09:30" (today). A weekday alone is
 * the latest one (today included); "last" skips today. Without a time it's midnight. A
 * trailing "NY" or "feed" picks the clock; otherwise `clock`.
 */
export function parseWhen(text: string, now: Wall, clock: Clock): { wall: Wall; clock: Clock } | null {
  let s = text.trim().toLowerCase().replace(/,/g, " ").replace(/\s+/g, " ");
  const zone = /\s*\b(ny|new york|feed|broker|server)$/.exec(s);
  if (zone) {
    clock = zone[1] === "ny" || zone[1] === "new york" ? "ny" : "feed";
    s = s.slice(0, zone.index).trim();
  }
  // The time: 09:30, 9:30pm, 9pm.
  let time: { h: number; mi: number } | null = null;
  const tm = /(?:^|\s)(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$/.exec(s);
  if (tm && (tm[2] !== undefined || tm[3])) {
    let h = Number(tm[1]);
    const mi = Number(tm[2] ?? 0);
    if (tm[3] === "pm" && h < 12) h += 12;
    if (tm[3] === "am" && h === 12) h = 0;
    if (h > 23 || mi > 59) return null;
    time = { h, mi };
    s = s.slice(0, tm.index).trim();
  }
  let day: Wall | null = null;
  const today: Wall = { ...now, h: 0, mi: 0 };
  let iso: RegExpExecArray | null;
  let named: RegExpExecArray | null;
  if (!s || s === "today") day = today;
  else if (s === "yesterday") day = shift(today, -1);
  else if ((iso = /^(\d{4})-(\d{1,2})-(\d{1,2})$/.exec(s))) {
    day = { y: +iso[1], mo: +iso[2], d: +iso[3], h: 0, mi: 0 };
  } else if ((named = /^(?:(\d{1,2}) ([a-z]+)|([a-z]+) (\d{1,2}))(?: (\d{4}))?$/.exec(s)) && MONTHS.includes((named[2] ?? named[3]).slice(0, 3))) {
    const mo = MONTHS.indexOf((named[2] ?? named[3]).slice(0, 3)) + 1;
    const d = Number(named[1] ?? named[4]);
    let y = named[5] ? Number(named[5]) : now.y;
    if (!named[5] && (mo > now.mo || (mo === now.mo && d > now.d))) y -= 1; // "24 Dec" in January: last year's
    day = { y, mo, d, h: 0, mi: 0 };
  } else {
    const wd = /^(last )?([a-z]+)$/.exec(s);
    const i = wd ? dayIndex(wd[2]) : -1;
    if (i < 0) return null;
    let back = (weekday(today) - i + 7) % 7;
    if (wd![1] && back === 0) back = 7;
    day = shift(today, -back);
  }
  const check = new Date(Date.UTC(day.y, day.mo - 1, day.d));
  if (check.getUTCMonth() !== day.mo - 1 || check.getUTCDate() !== day.d) return null; // 31 Feb
  return { wall: { ...day, ...(time ?? { h: 0, mi: 0 }) }, clock };
}

/** Wall time as "YYYY-MM-DDTHH:MM". */
export const wallString = (w: Wall) =>
  `${w.y}-${String(w.mo).padStart(2, "0")}-${String(w.d).padStart(2, "0")}T${String(w.h).padStart(2, "0")}:${String(w.mi).padStart(2, "0")}`;

/** Feed-clock wall time as the chart's unix seconds (the feed clock's wall time read as UTC). */
export const feedUnix = (w: Wall) => Date.UTC(w.y, w.mo - 1, w.d, w.h, w.mi) / 1000;

/** The wall time now in New York (from the browser's time zone data) or on the feed clock. */
export function wallNow(clock: Clock, clockOffset: number, nowMs = Date.now()): Wall {
  if (clock === "feed") {
    const t = new Date(nowMs + clockOffset * 1000);
    return { y: t.getUTCFullYear(), mo: t.getUTCMonth() + 1, d: t.getUTCDate(), h: t.getUTCHours(), mi: t.getUTCMinutes() };
  }
  const parts = Object.fromEntries(new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York", year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "numeric", hourCycle: "h23",
  }).formatToParts(new Date(nowMs)).map((p) => [p.type, p.value]));
  return { y: +parts.year, mo: +parts.month, d: +parts.day, h: +parts.hour, mi: +parts.minute };
}

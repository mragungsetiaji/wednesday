import type { CalendarEvent } from "./api";

/**
 * What gold did after past releases of an event, as one line for the risk-time card and the
 * news-line tooltip: "CPI m/m: median 15m range 9.40 (last 12)". Null when nothing is stored.
 */
export function reactionLine(e: Pick<CalendarEvent, "title" | "reaction">): string | null {
  const r = e.reaction;
  if (!r || r.stored === 0) return null;
  if (r.count > 0) return r.summary;
  return `${e.title}: no M1 bars around its ${r.stored} past release${r.stored === 1 ? "" : "s"} yet`;
}

/** Releases at one minute, those with measured reactions first. */
export function reactionLines(events: Pick<CalendarEvent, "title" | "reaction">[], max = 3): string[] {
  const measured = events.filter((e) => (e.reaction?.count ?? 0) > 0);
  const rest = events.filter((e) => !measured.includes(e));
  return [...measured, ...rest].map(reactionLine).filter((l): l is string => l !== null).slice(0, max);
}

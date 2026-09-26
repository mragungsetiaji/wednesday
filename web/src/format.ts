import type { Level } from "./api";

export const DIGITS = 2;

export const fmtPrice = (v: number) =>
  v.toLocaleString("en-US", { minimumFractionDigits: DIGITS, maximumFractionDigits: DIGITS });

export const fmtSigned = (v: number) => `${v >= 0 ? "+" : "−"}${fmtPrice(Math.abs(v))}`;

/** "2,468.32" for a line, "2,468.32 – 2,471.79" for a zone. */
export const fmtLevelPrice = (lv: Level) =>
  lv.top === lv.bottom ? fmtPrice(lv.top) : `${fmtPrice(lv.bottom)} – ${fmtPrice(lv.top)}`;

/** Distance from price to the level's nearest edge (positive above, negative below, 0 inside). */
export function distance(lv: Level, price: number): number {
  if (lv.bottom > price) return lv.bottom - price;
  if (lv.top < price) return lv.top - price;
  return 0;
}

/** Short tag used next to a level. */
export const SHORT: Record<string, string> = { ob: "OB", liquidity: "LIQ", idm: "IDM" };

/** Visual role: which color/mark the level gets (see theme + zonesPrimitive). */
export type Role = "bull" | "bear" | "liquidity" | "idm";

export function roleOf(lv: Level): Role {
  if (lv.detector === "ob") return lv.kind === "bullish" ? "bull" : "bear";
  if (lv.detector === "liquidity") return "liquidity";
  return "idm";
}

export function detail(lv: Level): string {
  if (lv.detector === "ob") return lv.touches === 0 ? "untested" : `tested ${lv.touches}×`;
  if (lv.detector === "liquidity") return lv.meta.equal ? "equal pool" : lv.kind === "bsl" ? "buy stops" : "sell stops";
  return lv.kind === "bullish" ? "after bullish BOS" : "after bearish BOS";
}

export function eventVerb(lv: Level): string {
  if (lv.detector === "ob") return "mitigated";
  return lv.meta.grab ? "grabbed" : "broken";
}

/** Scan times are the feed's clock (broker server time); show them as-is. */
export const fmtFeedTime = (iso: string) => iso.replace("T", " ").slice(0, 16);

export function fmtAgo(iso: string | null, now: number): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s ago` : `${Math.floor(m / 60)}h ${m % 60}m ago`;
}

/** Time between two feed-clock ISO strings (both broker time, so the offset cancels out). */
export function fmtSpan(fromIso: string, toIso: string): string {
  // Parse naive times as UTC so a DST change in the viewer's timezone can't skew the gap.
  const utc = (iso: string) => Date.parse(/Z|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  const mins = Math.max(0, Math.round((utc(toIso) - utc(fromIso)) / 60000));
  if (mins < 60) return `${mins}m`;
  const h = Math.floor(mins / 60);
  if (h < 24) return `${h}h ${mins % 60}m`;
  return `${Math.floor(h / 24)}d ${h % 24}h`;
}

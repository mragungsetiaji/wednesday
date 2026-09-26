import type { OrderBlock } from "./api";

export const DIGITS = 2;

export const fmtPrice = (v: number) =>
  v.toLocaleString("en-US", { minimumFractionDigits: DIGITS, maximumFractionDigits: DIGITS });

export const fmtSigned = (v: number) => `${v >= 0 ? "+" : "−"}${fmtPrice(Math.abs(v))}`;

/** Distance from price to the zone's nearest edge (positive above, negative below, 0 inside). */
export function distance(ob: OrderBlock, price: number): number {
  if (ob.bottom > price) return ob.bottom - price;
  if (ob.top < price) return ob.top - price;
  return 0;
}

export const kindLabel = (ob: OrderBlock) => (ob.kind === "bullish" ? "BULL" : "BEAR");
export const kindArrow = (ob: OrderBlock) => (ob.kind === "bullish" ? "▲" : "▼");

/** Scan times are the feed's clock (broker server time); show them as-is. */
export const fmtFeedTime = (iso: string) => iso.replace("T", " ").slice(0, 16);

export function fmtAgo(iso: string | null, now: number): string {
  if (!iso) return "never";
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s ago` : `${Math.floor(m / 60)}h ${m % 60}m ago`;
}

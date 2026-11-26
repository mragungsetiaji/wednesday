/**
 * The browser tab's title: the market at a glance, like a quote board.
 * "XAUUSD 3,568.59 ▼ −0.42%", the change measured from the day's open.
 */
import { fmtPrice } from "./format.ts";

export const APP_TITLE = "Wednesday";

export function tabTitle(symbol: string, price: number | null, dayOpen: number | null): string {
  if (price === null || !Number.isFinite(price)) return APP_TITLE;
  const name = (symbol.split(":").pop() || "XAUUSD").toUpperCase();
  const head = `${name} ${fmtPrice(price)}`;
  if (dayOpen === null || !(dayOpen > 0)) return head;
  const pct = ((price - dayOpen) / dayOpen) * 100;
  const shown = Math.abs(pct).toFixed(2);
  if (shown === "0.00") return `${head} 0.00%`;
  return pct > 0 ? `${head} ▲ +${shown}%` : `${head} ▼ −${shown}%`;
}

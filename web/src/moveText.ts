/** The words for the move distribution: the value, and where it falls (percentile first). */
import type { DistPeriod, Distribution } from "./api";

const NOUNS: Record<DistPeriod, [string, string]> = {
  day: ["day", "days"],
  week: ["week", "weeks"],
  session: ["session", "sessions"],
  q90: ["90-minute block", "90-minute blocks"],
};

/** "−3.40%", "1.85%", "1.30 × ATR". */
export function fmtMove(v: number, measure: Distribution["measure"]): string {
  if (measure === "range_atr") return `${v.toFixed(2)} × ATR`;
  const abs = Math.abs(v).toFixed(2);
  const sign = measure === "change" && Number(abs) !== 0 ? (v > 0 ? "+" : "−") : ""; // no "−0.00%"
  return `${sign}${abs}%`;
}

const pct = (share: number) => {
  const p = share * 100;
  return p > 99.9 && p < 100 ? ">99.9%" : p < 0.1 && p > 0 ? "<0.1%" : `${p.toFixed(1)}%`;
};

/** What the samples are: "1,254 days", "212 Wednesdays", "88 NY AM sessions". */
export function samplesNoun(d: Distribution): string {
  const [one, many] = NOUNS[d.period];
  const n = d.samples;
  let noun = n === 1 ? one : many;
  if (d.filters.session) noun = `${d.filters.session} ${noun}`;
  if (d.filters.weekday && d.period === "day") noun = n === 1 ? d.filters.weekday : `${WEEKDAY_NAMES[d.filters.weekday] ?? d.filters.weekday}s`;
  else if (d.filters.weekday) noun = `${WEEKDAY_NAMES[d.filters.weekday] ?? d.filters.weekday} ${noun}`;
  return `${n.toLocaleString("en-US")} ${noun}`;
}

const WEEKDAY_NAMES: Record<string, string> = { Mon: "Monday", Tue: "Tuesday", Wed: "Wednesday", Thu: "Thursday", Fri: "Friday" };

/**
 * "−3.40% so far: lower than 99.6% of 1,254 days, about 1 in 250." Null without a
 * percentile (too few samples, or no current value).
 */
export function percentileSentence(d: Distribution): string | null {
  const x = d.current?.value;
  if (x === null || x === undefined || !d.percentile) return null;
  const p = d.percentile;
  const head = `${fmtMove(x, d.measure)}${d.current!.forming ? " so far" : ""}`;
  const where = p.side === "low" ? `lower than ${pct(p.above)}` : `higher than ${pct(p.below)}`;
  let rarity = "";
  if (p.tail === 0) rarity = `, beyond every one of them`;
  else if (1 / p.tail >= 3) rarity = `, about 1 in ${Math.round(1 / p.tail).toLocaleString("en-US")}`;
  return `${head}: ${where} of ${samplesNoun(d)}${rarity}.`;
}

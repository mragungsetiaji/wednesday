/** The chart's session and reference level toggles, and what each one shows. */
import type { RefLevel, RefLevelKind, SessionsResponse } from "./api";

export type LevelGroup = "killzones" | "asia" | "day" | "week" | "month" | "opens" | "quarter";

export const LEVEL_GROUPS: { id: LevelGroup; label: string; title: string }[] = [
  { id: "killzones", label: "Killzones", title: "Asia 20:00–00:00, London 02:00–05:00, NY AM 07:00–10:00, NY PM 13:30–16:00 New York" },
  { id: "asia", label: "Asia high / low", title: "The Asia killzone's range, until price takes it out" },
  { id: "day", label: "Previous day H / L", title: "PDH / PDL, faded once swept" },
  { id: "week", label: "Previous week H / L", title: "PWH / PWL, faded once swept" },
  { id: "month", label: "Previous month H / L", title: "PMH / PML, faded once swept" },
  { id: "opens", label: "Day, week and 00:00 opens", title: "The 18:00 day open, the Sunday week open and the midnight New York open" },
  { id: "quarter", label: "Quarter open", title: "The current 90-minute quarter's open" },
];

export const DEFAULT_LEVELS: LevelGroup[] = ["killzones", "asia", "day"];

const GROUP_OF: Record<RefLevelKind, LevelGroup> = {
  asia_high: "asia", asia_low: "asia", pdh: "day", pdl: "day", pwh: "week", pwl: "week", pmh: "month", pml: "month",
  day_open: "opens", week_open: "opens", midnight_open: "opens", quarter_open: "quarter",
};

export const groupOf = (kind: RefLevelKind): LevelGroup => GROUP_OF[kind];

/** A saved list of groups, with anything unknown dropped (an older or newer build's). */
export function levelGroups(saved: unknown): LevelGroup[] {
  const ids = new Set(LEVEL_GROUPS.map((g) => g.id));
  return Array.isArray(saved) ? saved.filter((g): g is LevelGroup => ids.has(g)) : DEFAULT_LEVELS;
}

export interface LevelView {
  killzones: SessionsResponse["killzones"];
  lines: RefLevel[];
}

export const NO_LEVELS: LevelView = { killzones: [], lines: [] };

/** What the chart draws for the switched-on groups. */
export function levelView(data: SessionsResponse | null, groups: LevelGroup[]): LevelView {
  if (!data || !groups.length) return NO_LEVELS;
  const on = new Set(groups);
  return {
    killzones: on.has("killzones") ? data.killzones : [],
    lines: data.lines.filter((l) => on.has(GROUP_OF[l.kind])),
  };
}

/**
 * Right-edge labels that don't overlap: each keeps its y when it can, else moves down (or up,
 * at the bottom) by whole label heights. Input and output are in the same order.
 */
export function spreadLabels(ys: number[], height: number, bottom: number): number[] {
  const order = ys.map((y, i) => [y, i] as const).sort((a, b) => a[0] - b[0]);
  const out = new Array<number>(ys.length);
  let last = -Infinity;
  for (const [y, i] of order) {
    const at = Math.max(y, last + height);
    out[i] = at;
    last = at;
  }
  // Pushed past the bottom: move the run back up, keeping the spacing.
  let limit = bottom - height / 2;
  for (let k = order.length - 1; k >= 0; k--) {
    const i = order[k][1];
    out[i] = Math.min(out[i], limit);
    limit = out[i] - height;
  }
  return out;
}

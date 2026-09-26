import type { DetectorInfo, Level, Scan } from "./api";
import { distance, roleOf } from "./format";
import type { Zone } from "./zonesPrimitive";

/** One row of the price ladder; every row is also pinned on the chart under the same tag. */
export interface RailItem {
  id: string;
  tag: string; // shown in the list and as the chart pill: "S1", "B2", "5M BSL"
  side: "above" | "below";
  tf: string;
  level: Level;
  setup: { rank: number; side: "sell" | "buy" } | null;
  price: number; // the price the row stands for: entry for setups, nearest edge otherwise
  distance: number; // signed, from current price
}

const MAX_SETUPS = 3;

export const levelKey = (tf: string, lv: Level) => `${tf}|${lv.detector}|${lv.kind}|${lv.time}|${lv.top}`;

/** Setups (S1.. above, B1.. below) plus the nearest non-OB level per detector on each side. */
export function buildRail(scan: Scan, detectors: DetectorInfo[]): RailItem[] {
  const enabled = new Set(detectors.map((d) => d.name));
  const items: RailItem[] = [];
  const seen = new Set<string>();

  if (enabled.has("ob")) {
    for (const side of ["sell", "buy"] as const) {
      scan.setups[side].slice(0, MAX_SETUPS).forEach((s, i) => {
        const tag = `${side === "sell" ? "S" : "B"}${i + 1}`;
        seen.add(levelKey(s.timeframe, s));
        items.push({
          id: tag,
          tag,
          side: side === "sell" ? "above" : "below",
          tf: s.timeframe,
          level: s,
          setup: { rank: i + 1, side },
          price: s.meta.entry!,
          distance: side === "sell" ? s.distance : -s.distance,
        });
      });
    }
  }

  for (const d of detectors) {
    if (d.name === "ob") continue; // order blocks are covered by the setups
    for (const side of ["above", "below"] as const) {
      const lv = scan.nearest[d.name]?.[side];
      if (!lv || seen.has(levelKey(lv.timeframe, lv))) continue;
      seen.add(levelKey(lv.timeframe, lv));
      items.push({
        id: levelKey(lv.timeframe, lv),
        tag: `${lv.timeframe} ${lv.label}`,
        side,
        tf: lv.timeframe,
        level: lv,
        setup: null,
        price: side === "above" ? lv.bottom : lv.top,
        distance: distance(lv, scan.price),
      });
    }
  }
  // Ladder order mirrors the price axis: highest first.
  return items.sort((a, b) => b.price - a.price);
}

export function railZone(item: RailItem): Zone {
  const lv = item.level;
  if (item.setup) {
    return {
      id: item.id, role: roleOf(lv), mark: "setup", top: lv.top, bottom: lv.bottom, startTime: lv.time_unix,
      label: item.tag, tag: item.tag, entry: lv.meta.entry, stop: lv.meta.sl, pinned: true,
    };
  }
  return {
    id: item.id, role: roleOf(lv), mark: "line", top: lv.top, bottom: lv.bottom, startTime: lv.time_unix,
    label: item.tag, tag: item.tag, strong: Boolean(lv.meta.equal), pinned: true,
  };
}

/** A level of the charted timeframe (or a higher one) drawn as context, without a pill. */
export function contextZone(tf: string, lv: Level, faded: boolean): Zone {
  const role = roleOf(lv);
  const isOb = lv.detector === "ob";
  return {
    id: levelKey(tf, lv),
    role,
    mark: isOb ? "box" : "line",
    top: lv.top,
    bottom: lv.bottom,
    startTime: lv.time_unix,
    label: `${tf} ${lv.label}`,
    faded,
    weak: lv.meta.priority === "middle",
    strong: Boolean(lv.meta.equal),
    stop: isOb && lv.meta.sl_capped ? lv.meta.sl : undefined,
  };
}

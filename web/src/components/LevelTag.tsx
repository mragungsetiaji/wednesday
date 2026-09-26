import type { Level } from "../api";
import { roleOf } from "../format";

/** Marker + text label. The marker mirrors the chart mark (box / line / dotted line). */
export function LevelTag({ level }: { level: Level }) {
  return <span className={`tag role-${roleOf(level)}`}>{level.label}</span>;
}

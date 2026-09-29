// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import type { SessionsResponse } from "./api.ts";
import { DEFAULT_LEVELS, LEVEL_GROUPS, levelGroups, levelView, spreadLabels } from "./refLevels.ts";

const line = (kind: SessionsResponse["lines"][number]["kind"]) =>
  ({ kind, label: kind, price: 1, start_unix: 0, end_unix: 1, swept_unix: null, current: true });
const data: SessionsResponse = {
  clock: "UTC",
  killzones: [{ name: "London", day: "2026-03-10", start_unix: 0, end_unix: 1 }],
  lines: [line("pdh"), line("pdl"), line("asia_high"), line("pwh"), line("week_open"), line("quarter_open")],
};

test("each toggle shows its own lines", () => {
  assert.deepEqual(levelView(data, ["day"]).lines.map((l) => l.kind), ["pdh", "pdl"]);
  assert.deepEqual(levelView(data, ["opens", "quarter"]).lines.map((l) => l.kind), ["week_open", "quarter_open"]);
  assert.equal(levelView(data, ["asia"]).killzones.length, 0);
  assert.equal(levelView(data, ["killzones"]).killzones.length, 1);
  assert.deepEqual(levelView(data, []), { killzones: [], lines: [] });
  assert.deepEqual(levelView(null, DEFAULT_LEVELS), { killzones: [], lines: [] });
});

test("saved toggles drop what this build doesn't know", () => {
  assert.deepEqual(levelGroups(["day", "nonsense", "week"]), ["day", "week"]);
  assert.deepEqual(levelGroups(undefined), DEFAULT_LEVELS);
  assert.ok(LEVEL_GROUPS.length === 7);
});

test("labels at the right edge never overlap", () => {
  assert.deepEqual(spreadLabels([10, 50, 100], 14, 200), [10, 50, 100]); // apart already
  assert.deepEqual(spreadLabels([50, 52, 51], 14, 200), [50, 78, 64]); // stacked down, same order in
  assert.deepEqual(spreadLabels([195, 196], 14, 200), [179, 193]); // pushed back up at the bottom
});

import assert from "node:assert/strict";
import { test } from "node:test";

import type { NewsReaction } from "./api.ts";
import { reactionLine, reactionLines } from "./newsReaction.ts";

const reaction = (count: number, stored: number, summary = ""): NewsReaction => ({
  title: "CPI m/m", currency: "USD", count, stored, last: null, move: { "5": null, "15": null, "60": null },
  move_atr: { "5": null, "15": null, "60": null }, range15: null, range15_atr: null, reversed: 0, up15: 0,
  surprise: { above: { count: 0, move15: null }, below: { count: 0, move15: null } }, summary,
});

test("a reaction line says what was measured, or why nothing was", () => {
  assert.equal(reactionLine({ title: "CPI m/m", reaction: reaction(12, 14, "CPI m/m: median 15m range 9.40 (last 12)") }),
    "CPI m/m: median 15m range 9.40 (last 12)");
  assert.equal(reactionLine({ title: "NFP", reaction: reaction(0, 1) }), "NFP: no M1 bars around its 1 past release yet");
  assert.equal(reactionLine({ title: "NFP", reaction: reaction(0, 3) }), "NFP: no M1 bars around its 3 past releases yet");
  assert.equal(reactionLine({ title: "NFP", reaction: reaction(0, 0) }), null);
  assert.equal(reactionLine({ title: "NFP" }), null);
});

test("measured releases come first", () => {
  const lines = reactionLines([
    { title: "Core CPI m/m", reaction: reaction(0, 2) },
    { title: "CPI m/m", reaction: reaction(5, 5, "CPI m/m: median 15m range 7.00 (last 5)") },
    { title: "CPI y/y" },
  ]);
  assert.deepEqual(lines, ["CPI m/m: median 15m range 7.00 (last 5)", "Core CPI m/m: no M1 bars around its 2 past releases yet"]);
});

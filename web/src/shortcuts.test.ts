// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { feedUnix, parseTimeframe, parseWhen, SHORTCUTS, wallNow, wallString } from "./shortcuts.ts";

test("no two actions share a shortcut", () => {
  const keys = SHORTCUTS.flatMap((s) => s.keys.split(/,\s*|\s*\/\s*/)).map((k) => k.trim().toLowerCase());
  assert.equal(new Set(keys).size, keys.length, keys.join(" | "));
});

test("typed timeframes", () => {
  const known = ["4H", "1H", "30M", "15M", "5M", "1M"];
  assert.equal(parseTimeframe("5", known), "5M");
  assert.equal(parseTimeframe("15", known), "15M");
  assert.equal(parseTimeframe("1h", known), "1H");
  assert.equal(parseTimeframe("4H", known), "4H");
  assert.equal(parseTimeframe("30m", known), "30M");
  assert.equal(parseTimeframe("h", known), "1H");
  assert.equal(parseTimeframe("2h", known), null);
  assert.equal(parseTimeframe("d", known), null);
  assert.equal(parseTimeframe("d", [...known, "D1"]), "D1");
  assert.equal(parseTimeframe("", known), null);
  assert.equal(parseTimeframe("5x", known), null);
});

// Wednesday 30 September 2026, 14:05.
const NOW = { y: 2026, mo: 9, d: 30, h: 14, mi: 5 };
const when = (text: string, clock: "ny" | "feed" = "ny") => {
  const r = parseWhen(text, NOW, clock);
  return r && `${wallString(r.wall)} ${r.clock}`;
};

test("go-to text", () => {
  assert.equal(when("last Wednesday 09:30 NY"), "2026-09-23T09:30 ny");
  assert.equal(when("wednesday 09:30"), "2026-09-30T09:30 ny"); // today counts
  assert.equal(when("mon 3pm feed"), "2026-09-28T15:00 feed");
  assert.equal(when("yesterday 8:15am", "feed"), "2026-09-29T08:15 feed");
  assert.equal(when("09:30"), "2026-09-30T09:30 ny");
  assert.equal(when("12am"), "2026-09-30T00:00 ny");
  assert.equal(when("2026-03-08 02:30"), "2026-03-08T02:30 ny");
  assert.equal(when("24 Sep 3:15pm new york"), "2026-09-24T15:15 ny");
  assert.equal(when("Dec 24"), "2025-12-24T00:00 ny"); // a later date this year means last year's
  assert.equal(when("last friday"), "2026-09-25T00:00 ny");
  assert.equal(when("2026-02-31"), null);
  assert.equal(when("25:00"), null);
  assert.equal(when("someday"), null);
});

test("clocks", () => {
  assert.equal(feedUnix({ y: 2026, mo: 9, d: 30, h: 1, mi: 0 }), Date.parse("2026-09-30T01:00Z") / 1000);
  const ms = Date.parse("2026-09-30T13:30Z");
  assert.deepEqual(wallNow("ny", 0, ms), { y: 2026, mo: 9, d: 30, h: 9, mi: 30 }); // EDT
  assert.deepEqual(wallNow("ny", 0, Date.parse("2026-01-15T14:30Z")), { y: 2026, mo: 1, d: 15, h: 9, mi: 30 }); // EST
  assert.deepEqual(wallNow("feed", 3 * 3600, ms), { y: 2026, mo: 9, d: 30, h: 16, mi: 30 });
});

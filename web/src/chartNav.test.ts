// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import type { QuarterBlock } from "./api";
import { countdown, fmtDuration, measure, presetStart } from "./chartNav.ts";

const block = (row: "week" | "session", day: string, start: number, label = ""): QuarterBlock =>
  ({ row, label, day, start_unix: start, end_unix: start + 3600, open: 1, high: 1, low: 1, close: 1, change: 0, live: false });

// Feed clock "NY+7" in September: 18:00 New York is 01:00 on the feed clock the next day.
const feed = (s: string) => Date.parse(`${s}Z`) / 1000;
const days = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-07", "2026-09-08",
  "2026-09-09", "2026-09-10", "2026-09-11", "2026-09-14", "2026-09-15", "2026-09-16", "2026-09-17", "2026-09-18",
  "2026-09-21", "2026-09-22", "2026-09-23", "2026-09-24", "2026-09-25", "2026-09-28", "2026-09-29", "2026-09-30"];
// Each trading day opens 18:00 New York the evening before: 01:00 feed on its own date.
const week = days.map((d) => block("week", d, feed(`${d}T01:00`)));
const sessions = [block("session", "2026-09-30", feed("2026-09-30T01:00"), "Tokyo"),
  block("session", "2026-09-30", feed("2026-09-30T07:00"), "London")];

test("presets start on New York boundaries, given in the feed clock", () => {
  const rows = { week, session: sessions };
  assert.equal(presetStart(rows, "day"), feed("2026-09-30T01:00"));
  assert.equal(presetStart(rows, "session"), feed("2026-09-30T07:00"));
  // Wednesday 30 Sep: the week's first trading day is Monday 28 Sep, open Sunday 18:00 New York.
  assert.equal(presetStart(rows, "week"), feed("2026-09-28T01:00"));
  assert.equal(presetStart(rows, "5d"), feed("2026-09-24T01:00"));
  // A month back from 30 Sep: from 31 Aug on.
  assert.equal(presetStart(rows, "1m"), feed("2026-08-31T01:00"));
  // A history shorter than the range starts at its first day.
  assert.equal(presetStart({ week: week.slice(-3) }, "1m"), feed("2026-09-28T01:00"));
  assert.equal(presetStart({}, "day"), null);
  assert.equal(presetStart({ week }, "session"), null);
});

test("duration, countdown and measure", () => {
  assert.equal(fmtDuration(45 * 60), "45m");
  assert.equal(fmtDuration(155 * 60), "2h 35m");
  assert.equal(fmtDuration(2 * 3600), "2h");
  assert.equal(fmtDuration(3 * 86400 + 4 * 3600 + 59), "3d 4h");
  assert.equal(countdown(1000, 300, 1001), "04:59");
  assert.equal(countdown(0, 14400, 3600.5), "3:00:00");
  assert.equal(countdown(0, 300, 300), null); // closed
  assert.equal(countdown(1000, 300, 900), null); // not started: the clock or the bar is off
  const m = measure(2400, 2412.5, 25, 125 * 60, 0.1);
  assert.equal(m.bars, 25);
  assert.equal(m.duration, "2h 5m");
  assert.ok(Math.abs(m.pips! - 125) < 1e-9 && Math.abs(m.pct! - 0.5208333) < 1e-6);
});

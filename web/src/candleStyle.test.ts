// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { DEFAULT_CHART_SETTINGS } from "./chartSettings.ts";
import { barColors, candleLook, CLEAR, parseColor, seriesOptions, withAlpha } from "./candleStyle.ts";

const theme = { bull: "#199e70", bear: "#e66767" };

test("colours carry their opacity", () => {
  assert.deepEqual(parseColor("#ABC"), { hex: "#aabbcc", alpha: 1 });
  assert.deepEqual(parseColor("rgba(25, 158, 112, 0.5)"), { hex: "#199e70", alpha: 0.5 });
  assert.deepEqual(parseColor("#199e7080"), { hex: "#199e70", alpha: 0.5 });
  assert.equal(withAlpha("#199e70", 1), "#199e70");
  assert.equal(withAlpha("rgba(25, 158, 112, 0.2)", 0.4), "rgba(25, 158, 112, 0.4)");
});

test("the defaults look as before: filled bodies, wicks, no borders", () => {
  const o = seriesOptions(candleLook(DEFAULT_CHART_SETTINGS, theme, false));
  assert.equal(o.upColor, theme.bull);
  assert.equal(o.downColor, theme.bear);
  assert.equal(o.borderVisible, false);
  assert.equal(o.wickVisible, true);
  assert.equal(o.wickUpColor, theme.bull);
});

test("borders and wicks take their own colours, bodies can be turned off", () => {
  const s = { ...DEFAULT_CHART_SETTINGS, body: false, borders: true, borderUp: "#000000", wickDown: "#ffffff" };
  const o = seriesOptions(candleLook(s, theme, false));
  assert.equal(o.upColor, CLEAR);
  assert.equal(o.downColor, CLEAR);
  assert.equal(o.borderUpColor, "#000000");
  assert.equal(o.borderDownColor, theme.bear);
  assert.equal(o.wickDownColor, "#ffffff");
  // Hollow keeps the rising bodies empty and shows the borders.
  const h = seriesOptions(candleLook(DEFAULT_CHART_SETTINGS, theme, true));
  assert.equal(h.upColor, CLEAR);
  assert.equal(h.borderVisible, true);
});

test("by the previous close: a down candle above the last close is painted up", () => {
  const off = candleLook(DEFAULT_CHART_SETTINGS, theme, false);
  assert.equal(barColors(off, { open: 10, close: 9 }, 8), null);
  const on = candleLook({ ...DEFAULT_CHART_SETTINGS, prevClose: true }, theme, false);
  assert.equal(barColors(on, { open: 10, close: 9 }, 8)?.color, theme.bull);
  assert.equal(barColors(on, { open: 8, close: 9 }, 10)?.color, theme.bear);
  assert.equal(barColors(on, { open: 8, close: 9 }, null), null); // the first candle: by its open
  const hollow = candleLook({ ...DEFAULT_CHART_SETTINGS, prevClose: true }, theme, true);
  assert.equal(barColors(hollow, { open: 8, close: 9 }, 10)?.color, CLEAR);
});

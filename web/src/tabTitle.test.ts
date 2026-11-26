// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { APP_TITLE, tabTitle } from "./tabTitle.ts";

test("the tab shows the symbol, the price and the change since the day's open", () => {
  assert.equal(tabTitle("FX:XAUUSD", null, 3500), APP_TITLE);
  assert.match(tabTitle("FX:XAUUSD", 3568.59, null), /^XAUUSD 3,?568\.59$/);
  assert.match(tabTitle("FX:XAUUSD", 3568.59, 3583.64), /^XAUUSD 3,?568\.59 ▼ −0\.42%$/);
  assert.match(tabTitle("OANDA:XAGUSD", 31.5, 31), /^XAGUSD .* ▲ \+1\.61%$/);
  assert.match(tabTitle("XAUUSD", 3500, 3500), / 0\.00%$/);
});

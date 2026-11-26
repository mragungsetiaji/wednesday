// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { initials } from "./account.ts";

test("the avatar shows the name's initials", () => {
  assert.equal(initials("A. Setiaji"), "AS");
  assert.equal(initials("aji"), "AJ");
  assert.equal(initials("mr_agung"), "MA");
  assert.equal(initials("  "), "?");
});

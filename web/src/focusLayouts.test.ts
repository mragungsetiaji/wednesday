// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { boundaries, LAYOUTS, layoutOf, MAX_PANES, moveBoundary, paneCells, paneCount, sizesFor } from "./focusLayouts.ts";

const close = (a: number[], b: number[]) => assert.ok(a.length === b.length && a.every((x, i) => Math.abs(x - b[i]) < 1e-9), `${a} != ${b}`);

test("every layout's panes fill its grid once", () => {
  for (const l of LAYOUTS) {
    const cells = paneCells(l);
    assert.equal(cells.length, paneCount(l), l.id);
    assert.ok(cells.length <= MAX_PANES);
    const taken = new Set<string>();
    for (const c of cells) for (let r = c.row; r < c.row + c.rowSpan; r++) {
      const key = `${c.col},${r}`;
      assert.ok(!taken.has(key), `${l.id}: ${key} twice`);
      taken.add(key);
    }
    assert.equal(taken.size, l.cols * l.rows, l.id);
  }
  assert.deepEqual(LAYOUTS.map((l) => paneCount(l)), [1, 2, 3, 3, 3, 4, 4, 6]);
});

test("old saved layouts still load", () => {
  assert.equal(layoutOf(4).id, "4");
  assert.equal(layoutOf(1).id, "1");
  assert.equal(layoutOf("1+3").id, "1+3");
  assert.equal(layoutOf("nonsense").id, "2");
});

test("sizes are saved per layout and checked against it", () => {
  const three = LAYOUTS.find((l) => l.id === "3c")!;
  const big = LAYOUTS.find((l) => l.id === "1+2")!;
  close(sizesFor(three, {}).cols, [1 / 3, 1 / 3, 1 / 3]);
  close(sizesFor(big, {}).cols, [0.62, 0.38]);
  const saved = { "3c": { cols: [0.5, 0.25, 0.25], rows: [1] } };
  close(sizesFor(three, saved).cols, [0.5, 0.25, 0.25]);
  close(sizesFor(three, { "3c": { cols: [0.5, 0.5], rows: [1] } }).cols, [1 / 3, 1 / 3, 1 / 3]); // wrong length
  close(sizesFor(three, { "3c": { cols: [0.95, 0.03, 0.02], rows: [1] } }).cols, [1 / 3, 1 / 3, 1 / 3]); // too narrow
});

test("moving a line resizes only its two neighbours", () => {
  const s = [1 / 3, 1 / 3, 1 / 3];
  close(boundaries(s), [1 / 3, 2 / 3]);
  close(moveBoundary(s, 0, 0.5), [0.5, 1 / 6, 1 / 3]);
  close(moveBoundary(s, 1, 0.9), [1 / 3, 0.9 - 1 / 3, 0.1]);
  close(moveBoundary(s, 0, 0.01), [0.1, 2 / 3 - 0.1, 1 / 3]); // no narrower than 10%
  close(moveBoundary(s, 0, 0.99), [2 / 3 - 0.1, 0.1, 1 / 3]); // nor pushing past the next line
});

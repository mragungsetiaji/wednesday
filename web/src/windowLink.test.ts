// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { crosshairLink, LinkBus, type Channel, type GroupOptions, type LinkGroup, type LinkMessage } from "./windowLink.ts";

/** Two windows' channels wired to each other, delivering at once. */
function pair(): [Channel, Channel] {
  const a: Channel = { onmessage: null, postMessage: (d) => b.onmessage?.({ data: structuredClone(d) }) };
  const b: Channel = { onmessage: null, postMessage: (d) => a.onmessage?.({ data: structuredClone(d) }) };
  return [a, b];
}

const opts = (o: Partial<Record<LinkGroup, GroupOptions>>) => (g: LinkGroup) => o[g] ?? { range: true, tf: false };

test("a crosshair reaches the same group in another window, not the sender or other groups", () => {
  const [ca, cb] = pair();
  const one = new LinkBus(ca, opts({}));
  const two = new LinkBus(cb, opts({}));
  const got: Record<string, LinkMessage[]> = { self: [], local: [], remote: [], other: [] };
  one.subscribe("self", "A", (m) => got.self.push(m));
  one.subscribe("local", "A", (m) => got.local.push(m));
  two.subscribe("remote", "A", (m) => got.remote.push(m));
  two.subscribe("other", "B", (m) => got.other.push(m));
  one.publish("A", "self", { kind: "crosshair", point: { time: 100, price: 2400 } });
  assert.equal(got.self.length, 0);
  assert.deepEqual(got.local, [{ kind: "crosshair", point: { time: 100, price: 2400 } }]);
  assert.deepEqual(got.remote, got.local);
  assert.equal(got.other.length, 0);
});

test("range and timeframe only travel when the group shares them", () => {
  const [ca, cb] = pair();
  const options = opts({ A: { range: false, tf: true } });
  const one = new LinkBus(ca, options);
  const two = new LinkBus(cb, options);
  const got: LinkMessage[] = [];
  two.subscribe("x", "A", (m) => got.push(m));
  one.publish("A", "y", { kind: "range", from: 1, to: 2 });
  one.publish("A", "y", { kind: "tf", tf: "5M" });
  assert.deepEqual(got, [{ kind: "tf", tf: "5M" }]);
});

test("the crosshair adapter has PriceChart's bus shape", () => {
  const bus = new LinkBus(null, opts({}));
  const a = crosshairLink("C", "a", bus);
  const b = crosshairLink("C", "b", bus);
  const seen: unknown[] = [];
  b.subscribe(0, (p) => seen.push(p));
  a.publish(0, null);
  a.publish(0, { time: 5, price: null });
  assert.deepEqual(seen, [null, { time: 5, price: null }]);
});

test("without a channel the bus still links charts in this window", () => {
  const bus = new LinkBus(null, opts({}));
  let n = 0;
  bus.subscribe("a", "A", () => n++);
  bus.publish("A", "b", { kind: "crosshair", point: null });
  assert.equal(n, 1);
});

// Run with `npm test` (Node 22.18+ runs TypeScript directly).
import assert from "node:assert/strict";
import { test } from "node:test";

import { applyPrefs, clearLocal, hasLocalData, type KeyStore, localAnswer, localData, LOCAL_KEYS, prefsDiff, prefsOf } from "./localData.ts";

function memory(init: Record<string, string> = {}): KeyStore {
  const m = new Map(Object.entries(init));
  return {
    getItem: (k) => m.get(k) ?? null,
    setItem: (k, v) => void m.set(k, v),
    removeItem: (k) => void m.delete(k),
    get length() { return m.size; },
    key: (i) => [...m.keys()][i] ?? null,
  };
}

const call = async (s: KeyStore, method: string, url: string, body?: unknown, server?: (u: string) => Promise<Response>) => {
  const res = await localAnswer(url, { method, body: body === undefined ? undefined : JSON.stringify(body) }, s, server);
  return res && { status: res.status, body: await res.json() };
};

test("drawings stay in the browser, per symbol", async () => {
  const s = memory();
  assert.deepEqual((await call(s, "GET", "/api/drawings"))?.body.drawings, []);
  const d = { kind: "hline", points: [{ time: 1, price: 2000 }] };
  assert.equal((await call(s, "PUT", "/api/drawings/a1", d))?.body.id, "a1");
  await call(s, "PUT", "/api/drawings/b2?symbol=fx%3Axagusd", d);
  assert.deepEqual((await call(s, "GET", "/api/drawings"))?.body.drawings.map((x: { id: string }) => x.id), ["a1"]);
  assert.deepEqual((await call(s, "GET", "/api/drawings?symbol=FX:XAGUSD"))?.body.drawings.map((x: { id: string }) => x.id), ["b2"]);
  assert.equal((await call(s, "DELETE", "/api/drawings/zz"))?.status, 404);
  assert.equal((await call(s, "DELETE", "/api/drawings/a1"))?.status, 200);
  assert.deepEqual((await call(s, "GET", "/api/drawings"))?.body.drawings, []);
  // Drawing alerts are the admin's: the server answers (and refuses) them.
  assert.equal(await call(s, "PUT", "/api/drawings/b2/alert", {}), null);
  assert.deepEqual((await call(s, "GET", "/api/drawings/alerts"))?.body, { alerts: [] });
});

test("templates and workspaces stay in the browser", async () => {
  const s = memory();
  await call(s, "PUT", "/api/drawing-templates/line/H1%20%20OB", { style: { color: "#ff0000", width: 2, dash: "solid" } });
  const t = (await call(s, "GET", "/api/drawing-templates"))?.body.templates;
  assert.equal(t.length, 1);
  assert.equal(t[0].name, "H1 OB");
  assert.equal((await call(s, "DELETE", "/api/drawing-templates/line/H1%20OB"))?.status, 200);

  await call(s, "PUT", "/api/workspaces/London", { layout: { "wed.tf": "M15" }, windows: [{ route: "#" }] });
  assert.deepEqual((await call(s, "GET", "/api/workspaces"))?.body.workspaces.map((w: { name: string; windows: number }) => [w.name, w.windows]),
    [["London", 1]]);
  assert.deepEqual((await call(s, "GET", "/api/workspaces/London"))?.body.layout, { "wed.tf": "M15" });
  assert.equal((await call(s, "GET", "/api/workspaces/Nope"))?.status, 404);
});

test("the terms are accepted in the browser, with the server's text", async () => {
  const s = memory();
  const server = async () => new Response(JSON.stringify({ version: "v2", text: "T", accepted: false, accepted_at: null }));
  assert.equal((await call(s, "GET", "/api/terms", undefined, server))?.body.accepted, false);
  assert.equal((await call(s, "POST", "/api/terms/accept", undefined, server))?.body.accepted, true);
  assert.equal((await call(s, "GET", "/api/terms", undefined, server))?.body.text, "T");
  const newer = async () => new Response(JSON.stringify({ version: "v3", text: "T", accepted: false, accepted_at: null }));
  assert.equal((await call(s, "GET", "/api/terms", undefined, newer))?.body.accepted, false);
});

test("the server answers everything else", async () => {
  assert.equal(await call(memory(), "GET", "/api/scan"), null);
  assert.equal(await call(memory(), "GET", "/api/journals"), null);
});

test("what the preview kept goes to the import, then is cleared", async () => {
  const s = memory({ "wed.tf": JSON.stringify("H1"), "other": "x" });
  assert.equal(hasLocalData(localData(s)), false);
  await call(s, "PUT", "/api/drawings/a1", { kind: "hline" });
  await call(s, "PUT", "/api/drawings/b2?symbol=FX:XAGUSD", { kind: "hline" });
  await call(s, "PUT", "/api/workspaces/W", { layout: {}, windows: [] });
  const d = localData(s);
  assert.equal(hasLocalData(d), true);
  assert.deepEqual(d.drawings.map((x) => [x.symbol, x.drawing.id]), [[undefined, "a1"], ["FX:XAGUSD", "b2"]]);
  assert.deepEqual(d.workspaces.map((w) => w.name), ["W"]);
  assert.deepEqual(d.prefs, { "wed.tf": "H1" });
  clearLocal(s);
  assert.equal(s.getItem(LOCAL_KEYS.drawings), null);
  assert.equal(s.getItem("wed.tf"), JSON.stringify("H1")); // preferences stay
});

test("preferences: snapshot, diff, apply", () => {
  const s = memory({ "wed.a": "1", "xau.b": JSON.stringify({ x: 1 }), "wedlocal.drawings": "{}" });
  const before = prefsOf(s);
  assert.deepEqual(before, { "wed.a": 1, "xau.b": { x: 1 } });
  assert.equal(prefsDiff(before, before), null);
  s.setItem("wed.a", "2");
  s.removeItem("xau.b");
  assert.deepEqual(prefsDiff(before, prefsOf(s)), { "wed.a": 2, "xau.b": null });
  applyPrefs({ "wed.c": [1, 2], "nope": 1 }, s);
  assert.equal(s.getItem("wed.c"), "[1,2]");
  assert.equal(s.getItem("nope"), null);
});

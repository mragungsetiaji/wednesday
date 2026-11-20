"""The API contract (#96): every route, every request body and the shape of every GET response.

The dashboard and the Tauri shell only talk to the server through ``/api``, so these are what a
replacement server (a Rust one, say) must answer the same way. ``tests/api_contract.json`` holds
the contract; this test fails with a list of what changed when the API no longer matches it.

After a deliberate API change, rewrite the file and review its diff:

    UPDATE_API_CONTRACT=1 uv run pytest tests/test_api_contract.py

A shape keeps the JSON's keys and types, not its values: ``"number"``, ``"string"``, ``"bool"``,
``"null"``, a list of its items' merged shape, an object of its keys' shapes (``"key?"`` when only
some items have it). Values that depend on the clock (a null now, a value later; an empty list) are
not differences.
"""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from wednesday.alerts import AlertManager
from wednesday.auth import Auth, hash_password
from wednesday.brief import BriefRunner
from wednesday.detectors import DetectorParams
from wednesday.engine import Runtime
from wednesday.journal.service import Journals
from wednesday.lab.service import Lab
from wednesday.news import Calendar
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.settings import DataSettings
from wednesday.storage import Store

CONTRACT = Path(__file__).with_name("api_contract.json")

# GET routes that reach another service (TradingView, Telegram): listed, but not called here.
NETWORK = {"/api/markets/candles", "/api/markets/tick", "/api/alerts/chats"}
# Query strings for GET routes that need one.
DRAWING_ID = "0f6c1d2e-8b4a-4c7e-9a51-3d2f7e6b1c90"
QUERIES = {
    "/api/clock/feed": {"ny": "2026-03-02T09:30"},
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    # Nothing here may reach the internet: a route that tries fails the test.
    connect = socket.socket.connect

    def local_only(sock, address):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise OSError(f"the API contract test blocked a connection to {address[0]}")
        return connect(sock, address)

    monkeypatch.setattr(socket.socket, "connect", local_only)
    monkeypatch.chdir(tmp_path)
    store = Store(f"sqlite:///{tmp_path / 'contract.db'}")
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store, alerts=AlertManager(store, None),
                      calendar=Calendar(store),
                      brief=BriefRunner(store), lab=Lab(store, str(tmp_path / "models"), cfg.params),
                      journals=Journals(store, sample=True), poll=False)
    runtime.engine.step()
    # With the login on, so its routes answer too; the client stays logged in.
    auth = Auth(hash_password("contract", n=2**10), store)
    client = TestClient(create_app(runtime, source="synthetic", ui_dir=tmp_path, auth=auth))
    assert client.post("/api/auth/login", json={"password": "contract"}).status_code == 200
    return client


def _api_routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _api_routes(r.original_router.routes)


# ---- shapes ----

DATA_KEY = re.compile(r"^-?\d+(\.\d+)?$|^\d{4}-\d\d-\d\d")  # keys that are data (times, ids), not names


def shape(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        items = [shape(v) for v in value]
        if not items:
            return []
        merged = items[0]
        for s in items[1:]:
            merged = merge(merged, s)
        return [merged]
    if isinstance(value, dict):
        if value and all(DATA_KEY.match(k) for k in value):
            merged = None
            for v in value.values():
                merged = shape(v) if merged is None else merge(merged, shape(v))
            return {"*": merged}
        return {k: shape(v) for k, v in sorted(value.items())}
    return type(value).__name__


def _types(s: str) -> set[str]:
    return set(s.split("|"))


def merge(a, b):
    """One shape for two items of the same list."""
    if a == b:
        return a
    if isinstance(a, dict) and isinstance(b, dict):
        names = {k.rstrip("?") for k in a} | {k.rstrip("?") for k in b}
        out = {}
        for n in sorted(names):
            ka = n if n in a else f"{n}?" if f"{n}?" in a else None
            kb = n if n in b else f"{n}?" if f"{n}?" in b else None
            if ka and kb:
                s = merge(a[ka], b[kb])
                out[n if ka == n and kb == n else f"{n}?"] = s
            else:
                out[f"{n}?"] = a[ka] if ka else b[kb]
        return out
    if isinstance(a, list) and isinstance(b, list):
        if not a or not b:
            return a or b
        return [merge(a[0], b[0])]
    if a == "null":
        return b
    if b == "null":
        return a
    if isinstance(a, str) and isinstance(b, str):
        return "|".join(sorted(_types(a) | _types(b)))
    return "any"


def differences(expected, actual, path="") -> list[str]:
    """What differs between the contract's shape and this run's, ignoring what the clock decides."""
    if expected == actual or "null" in (expected, actual) or "any" in (expected, actual):
        return []
    if isinstance(expected, list) and isinstance(actual, list):
        if not expected or not actual:
            return []
        return differences(expected[0], actual[0], path + "[]")
    if isinstance(expected, dict) and isinstance(actual, dict):
        out = []
        exp = {k.rstrip("?"): (k, v) for k, v in expected.items()}
        act = {k.rstrip("?"): (k, v) for k, v in actual.items()}
        if "*" in exp and "*" in act:
            return differences(exp["*"][1], act["*"][1], path + ".*")
        for name in sorted(exp.keys() | act.keys()):
            where = f"{path}.{name}"
            if name not in act:
                if not exp[name][0].endswith("?"):
                    out.append(f"{where}: missing")
            elif name not in exp:
                out.append(f"{where}: new ({json.dumps(act[name][1])[:80]})")
            else:
                out += differences(exp[name][1], act[name][1], where)
        return out
    if isinstance(expected, str) and isinstance(actual, str) and (_types(expected) | {"null"}) == (_types(actual) | {"null"}):
        return []
    return [f"{path or '.'}: {json.dumps(expected)[:80]} became {json.dumps(actual)[:80]}"]


# ---- the contract ----

def build(client) -> dict:
    app = client.app
    routes = sorted({(m, r.path) for r in _api_routes(app.routes) if r.path.startswith("/api")
                     for m in r.methods - {"HEAD", "OPTIONS"}}, key=lambda mp: (mp[1], mp[0]))
    openapi = app.openapi()
    requests = {}
    for path, methods in openapi["paths"].items():
        for method, op in methods.items():
            body = op.get("requestBody", {}).get("content", {})
            params = sorted(f"{p['name']}{'' if p.get('required') else '?'}" for p in op.get("parameters", [])
                            if p["in"] == "query")
            entry = {}
            if body:
                entry["body"] = {ct: c.get("schema", {}) for ct, c in sorted(body.items())}
            if params:
                entry["query"] = params
            if entry:
                requests[f"{method.upper()} {path}"] = entry

    # Something in the lists whose items have a shape: a drawing and a workspace (the sample journal
    # is there already), and the ids that fill the routes' path parameters.
    last = client.get("/api/replay/range").json()["last"]
    drawing = {"kind": "trendline", "points": [{"t": last - 7200, "p": 2470.5}, {"t": last - 3600, "p": 2480}],
               "style": {"color": None, "width": 2}}
    assert client.put(f"/api/drawings/{DRAWING_ID}", json=drawing).status_code == 200
    window = {"route": "#", "x": 0, "y": 0, "width": 1440, "height": 900, "maximized": True}
    assert client.put("/api/workspaces/Contract", json={"layout": {"xau.tf": "15M"}, "windows": [window]}).status_code == 200
    ids = {"name": "Contract", "journal_id": client.get("/api/journals").json()["journals"][0]["id"]}
    queries = dict(QUERIES)
    queries["/api/replay"] = {"at": last - 60}
    responses = {}
    for method, path in routes:
        if method != "GET" or path in NETWORK or path == "/api/stream":
            continue
        names = re.findall(r"\{(\w+)\}", path)
        if any(n not in ids for n in names):
            continue
        res = client.get(re.sub(r"\{(\w+)\}", lambda m: str(ids[m.group(1)]), path), params=queries.get(path))
        # A route that needs more set up (an active Lab model) answers 409 here: its status is the contract.
        assert res.status_code in (200, 409), (path, res.status_code, res.text[:200])
        kind = res.headers["content-type"].split(";")[0]
        if kind == "application/json":
            body = shape(res.json())
            responses[f"GET {path}"] = body if res.status_code == 200 else {"status": res.status_code, "body": body}
        else:  # a file (CSV, a model): its type is the contract
            responses[f"GET {path}"] = {"status": res.status_code, "content-type": kind}
    return {
        "routes": [f"{m} {p}" for m, p in routes],
        "requests": requests,
        "schemas": openapi.get("components", {}).get("schemas", {}),
        "responses": responses,
    }


def test_the_api_matches_its_contract(client):
    now = build(client)
    if os.environ.get("UPDATE_API_CONTRACT") or not CONTRACT.exists():
        CONTRACT.write_text(json.dumps(now, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        if not os.environ.get("UPDATE_API_CONTRACT"):
            pytest.fail(f"wrote {CONTRACT.name}: review and commit it")
        return
    saved = json.loads(CONTRACT.read_text(encoding="utf-8"))
    problems = []
    gone, added = set(saved["routes"]) - set(now["routes"]), set(now["routes"]) - set(saved["routes"])
    problems += [f"route removed: {r}" for r in sorted(gone)] + [f"route added: {r}" for r in sorted(added)]
    for key in sorted(saved["requests"].keys() | now["requests"].keys()):
        if saved["requests"].get(key) != now["requests"].get(key):
            problems.append(f"request changed: {key}")
    for name in sorted(saved["schemas"].keys() | now["schemas"].keys()):
        if saved["schemas"].get(name) != now["schemas"].get(name):
            problems.append(f"request schema changed: {name}")
    for key in sorted(saved["responses"].keys() | now["responses"].keys()):
        if key not in now["responses"]:
            problems.append(f"response no longer checked: {key}")
        elif key not in saved["responses"]:
            problems.append(f"response not in the contract: {key}")
        else:
            problems += [f"{key} {d}" for d in differences(saved["responses"][key], now["responses"][key])]
    assert not problems, (
        "The API no longer matches tests/api_contract.json. If the change is meant, run\n"
        "  UPDATE_API_CONTRACT=1 uv run pytest tests/test_api_contract.py\n"
        "and review the diff (the dashboard and any other server must follow):\n  " + "\n  ".join(problems))


def test_shapes_merge_list_items_and_ignore_the_clock():
    rows = [{"a": 1, "b": None}, {"a": 2, "b": "x", "c": True}]
    assert shape(rows) == [{"a": "number", "b": "string", "c?": "bool"}]
    assert shape({"1767225600": {"x": 1}, "1767229200": {"x": None}}) == {"*": {"x": "number"}}
    assert differences({"a": "number", "b": []}, {"a": "null", "b": [{"x": "number"}]}) == []
    assert differences({"a": "number"}, {"a": "string"}) == ['.a: "number" became "string"']
    assert differences({"a": "number"}, {}) == [".a: missing"]
    assert differences({}, {"z": "bool"}) == ['.z: new ("bool")']
    assert differences({"a?": "number"}, {}) == []

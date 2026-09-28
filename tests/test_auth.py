import argparse
import re

import pandas as pd
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from sqlalchemy import inspect, text

from wednesday import cli
from wednesday.auth import PUBLIC, SESSION_COOKIE, Auth, RateLimiter, check_password, cross_site, hash_password
from wednesday.detectors import DetectorParams
from wednesday.engine import Engine
from wednesday.feeds import SyntheticFeed
from wednesday.scanner import ScanConfig
from wednesday.server import create_app
from wednesday.storage import Store

PASSWORD = "correct horse battery"


@pytest.fixture
def store(tmp_path):
    return Store(f"sqlite:///{tmp_path / 'auth.db'}")


@pytest.fixture
def engine(store):
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    feed = SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100, end=pd.Timestamp("2026-03-02 12:00"))
    return Engine(feed, cfg, "XAUUSD", store)


@pytest.fixture
def auth(store):
    return Auth(hash_password(PASSWORD, n=2**10), store)


@pytest.fixture
def client(engine, auth, tmp_path):
    return TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path, auth=auth))


def test_password_hash_round_trip():
    encoded = hash_password(PASSWORD, n=2**10)
    assert encoded.startswith("scrypt$1024$8$1$") and PASSWORD not in encoded
    assert check_password(PASSWORD, encoded)
    assert not check_password("wrong", encoded)
    assert not check_password(PASSWORD, "garbage")
    assert hash_password(PASSWORD, n=2**10) != encoded  # salted


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _api_routes(routes):
    """Every APIRoute, including those of included routers (newer FastAPI wraps them)."""
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _api_routes(r.original_router.routes)


def test_every_api_route_needs_a_login(client):
    """Walks the app's routes: all of /api is closed without a login, except health and the login itself."""
    seen = 0
    routes = list(_api_routes(client.app.routes))
    assert {p for p in client.app.openapi()["paths"]} <= {r.path for r in routes}
    for route in routes:
        if not route.path.startswith("/api"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            res = client.request(method, _concrete(route.path), json={})
            guarded = res.status_code == 401 and res.json().get("detail") == "Log in first"
            assert guarded == (route.path not in PUBLIC), (method, route.path, res.status_code)
            seen += guarded
    assert seen > 50  # lab, journal and the core routes are all in the walk
    for path in ("/api/docs", "/api/openapi.json", "/api/no-such-route"):
        assert client.get(path).status_code == 401
    assert client.get("/api/health").json()["ok"] is True
    assert client.get("/api/auth/status").json() == {"enabled": True, "logged_in": False}


def test_login_session_and_logout(client):
    assert client.post("/api/auth/login", json={"password": "nope"}).status_code == 401
    res = client.post("/api/auth/login", json={"password": PASSWORD})
    assert res.status_code == 200
    cookie = res.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE}=" in cookie and "httponly" in cookie and "samesite=strict" in cookie
    assert "secure" not in cookie  # plain http (the test client); https sets Secure
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/auth/status").json() == {"enabled": True, "logged_in": True}
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/status").status_code == 401


def test_cookie_is_secure_over_https(engine, auth, tmp_path):
    client = TestClient(create_app(engine, ui_dir=tmp_path, auth=auth), base_url="https://dash.example")
    res = client.post("/api/auth/login", json={"password": PASSWORD})
    assert "secure" in res.headers["set-cookie"].lower()


def test_login_is_rate_limited(engine, store, tmp_path):
    now = [0.0]
    auth = Auth(hash_password(PASSWORD, n=2**10), store, RateLimiter(limit=3, window=60, clock=lambda: now[0]))
    client = TestClient(create_app(engine, ui_dir=tmp_path, auth=auth))
    for _ in range(3):
        assert client.post("/api/auth/login", json={"password": "bad"}).status_code == 401
    res = client.post("/api/auth/login", json={"password": PASSWORD})  # even the right one waits
    assert res.status_code == 429 and int(res.headers["retry-after"]) > 0
    now[0] = 61
    assert client.post("/api/auth/login", json={"password": PASSWORD}).status_code == 200


def test_api_tokens(client):
    client.post("/api/auth/login", json={"password": PASSWORD})
    assert client.post("/api/auth/tokens", json={"name": " "}).status_code == 422
    made = client.post("/api/auth/tokens", json={"name": "backup script"}).json()
    secret = made["token"]
    assert secret.startswith("wed_") and made["name"] == "backup script"
    listed = client.get("/api/auth/tokens").json()["tokens"]
    assert [t["name"] for t in listed] == ["backup script"] and "token" not in listed[0]
    client.post("/api/auth/logout")

    headers = {"Authorization": f"Bearer {secret}"}
    assert client.get("/api/status", headers=headers).status_code == 200
    assert client.get("/api/auth/tokens", headers=headers).json()["tokens"][0]["last_used_at"]
    # A bad token is judged on its own, even next to a valid session cookie.
    client.post("/api/auth/login", json={"password": PASSWORD})
    assert client.get("/api/status", headers={"Authorization": "Bearer wed_wrong"}).status_code == 401
    assert client.delete(f"/api/auth/tokens/{made['id']}").status_code == 200
    assert client.delete(f"/api/auth/tokens/{made['id']}").status_code == 404
    client.post("/api/auth/logout")
    assert client.get("/api/status", headers=headers).status_code == 401


def test_secrets_never_stored(client, store):
    client.post("/api/auth/login", json={"password": PASSWORD})
    session = client.cookies.get(SESSION_COOKIE)
    token = client.post("/api/auth/tokens", json={"name": "x"}).json()["token"]
    dump = []
    with store.engine.connect() as conn:
        for table in inspect(store.engine).get_table_names():
            dump += [repr(tuple(r)) for r in conn.execute(text(f'SELECT * FROM "{table}"'))]
    everything = "\n".join(dump)
    assert session and token
    for secret in (PASSWORD, session, token):
        assert secret not in everything


def test_cross_site_writes_are_refused(engine, auth, tmp_path):
    for a in (auth, None):  # with and without the login
        client = TestClient(create_app(engine, ui_dir=tmp_path, auth=a))
        if a:
            client.post("/api/auth/login", json={"password": PASSWORD})
        evil = client.put("/api/bias", json={}, headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403
        assert client.put("/api/bias", json={}, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
        same = client.put("/api/bias", json={}, headers={"Origin": "http://testserver"})
        assert same.status_code != 403
        assert client.get("/api/status", headers={"Origin": "https://evil.example"}).status_code == 200  # reads


def test_cross_site_rules():
    assert not cross_site("GET", {"origin": "https://evil.example", "host": "a"})
    assert cross_site("POST", {"origin": "null", "host": "a"})
    assert not cross_site("POST", {"origin": "https://dash.example", "host": "dash.example"})
    assert not cross_site("POST", {"origin": "https://dash.example", "host": "127.0.0.1:8000",
                                   "x-forwarded-host": "dash.example"})
    assert not cross_site("POST", {"host": "a"})  # scripts and curl send no Origin
    assert not cross_site("POST", {"host": "a", "sec-fetch-site": "same-origin"})


def test_without_password_everything_is_open(engine, tmp_path):
    client = TestClient(create_app(engine, ui_dir=tmp_path))
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/auth/status").json() == {"enabled": False, "logged_in": False}
    assert client.post("/api/auth/login", json={"password": "x"}).status_code == 409


def test_auth_from_env():
    assert not Auth.from_env(lambda name: None).enabled
    plain = Auth.from_env({"XAU_AUTH_PASSWORD": "pw"}.get)
    assert plain.enabled and check_password("pw", plain.password_hash)
    hashed = hash_password("pw2", n=2**10)
    assert Auth.from_env({"XAU_AUTH_PASSWORD_HASH": hashed, "XAU_AUTH_PASSWORD": "ignored"}.get).password_hash == hashed
    with pytest.raises(ValueError):
        Auth.from_env({"XAU_AUTH_PASSWORD_HASH": "plaintext"}.get)


def test_cli_refuses_open_network_dashboard(monkeypatch):
    monkeypatch.delenv("XAU_AUTH_PASSWORD", raising=False)
    monkeypatch.delenv("XAU_AUTH_PASSWORD_HASH", raising=False)
    args = lambda host, insecure=False: argparse.Namespace(host=host, insecure=insecure)  # noqa: E731
    assert not cli.dashboard_auth(args("127.0.0.1"), None).enabled
    assert not cli.dashboard_auth(args("localhost"), None).enabled
    assert not cli.dashboard_auth(args("::1"), None).enabled
    with pytest.raises(SystemExit, match="without a login"):
        cli.dashboard_auth(args("0.0.0.0"), None)
    assert not cli.dashboard_auth(args("0.0.0.0", insecure=True), None).enabled
    monkeypatch.setenv("XAU_AUTH_PASSWORD", "pw")
    assert cli.dashboard_auth(args("0.0.0.0"), None).enabled


def test_cli_hash_password(capsys):
    answers = iter(["s3cret", "s3cret"])
    cli.hash_password_command(read=lambda _prompt: next(answers))
    line = capsys.readouterr().out.strip()
    assert line.startswith("XAU_AUTH_PASSWORD_HASH=scrypt$")
    assert check_password("s3cret", line.split("=", 1)[1])
    answers = iter(["a", "b"])
    with pytest.raises(SystemExit, match="differ"):
        cli.hash_password_command(read=lambda _prompt: next(answers))

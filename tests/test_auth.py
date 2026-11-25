"""The login for a network-reachable dashboard (auth.py): accounts, sessions, the route guard."""

import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from wednesday.auth import (PUBLIC, SESSION_COOKIE, Auth, AuthError, RateLimiter, check_password, device_name,
                            hash_password, now_iso)
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
def auth(store):
    a = Auth(store)
    a.create_user("Trader", PASSWORD)
    return a


@pytest.fixture
def client(store, auth, tmp_path):
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    feed = SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100, end=pd.Timestamp("2026-03-02 12:00"))
    engine = Engine(feed, cfg, "XAUUSD", store)
    return TestClient(create_app(engine, source="synthetic", ui_dir=tmp_path, auth=auth))


def login(client, username="trader", password=PASSWORD, **headers):
    return client.post("/api/auth/login", json={"username": username, "password": password}, headers=headers)


def test_passwords_are_argon2id_and_old_scrypt_hashes_still_work():
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("$argon2id$v=19$m=19456,t=2,p=1$") and PASSWORD not in encoded
    assert check_password(PASSWORD, encoded) and not check_password("wrong", encoded)
    assert not check_password(PASSWORD, "garbage")
    # wednesday --hash-password of the old login: scrypt$n$r$p$salt$hash
    import base64
    import hashlib
    want = base64.b64encode(hashlib.scrypt(PASSWORD.encode(), salt=b"saltsaltsaltsalt", n=1024, r=8, p=1,
                                           dklen=32)).decode()
    old = f"scrypt$1024$8$1$c2FsdHNhbHRzYWx0c2FsdA==${want}"
    assert check_password(PASSWORD, old) and not check_password("wrong", old)


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def _api_routes(routes):
    for r in routes:
        if isinstance(r, APIRoute):
            yield r
        elif hasattr(r, "original_router"):
            yield from _api_routes(r.original_router.routes)


def test_every_api_route_needs_a_session(client):
    """Walks the app's routes, so a new route is guarded without anyone remembering to."""
    seen = 0
    for route in _api_routes(client.app.routes):
        if not route.path.startswith("/api"):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            res = client.request(method, _concrete(route.path), json={})
            guarded = res.status_code == 401 and res.json().get("detail") == "Log in first"
            assert guarded == (route.path not in PUBLIC), (method, route.path, res.status_code)
            seen += guarded
    assert seen > 50
    for path in ("/api/docs", "/api/openapi.json", "/api/no-such-route"):
        assert client.get(path).status_code == 401
    assert client.get("/").status_code != 401  # the dashboard itself loads, to show the login


def test_login_cookie_logout(client):
    assert client.get("/api/auth/status").json() == {"login": True, "logged_in": False, "setup": False,
                                                     "user": None}
    assert login(client, password="wrong").status_code == 401
    res = login(client, username=" TRADER ", **{"user-agent": "Mozilla/5.0 (Windows NT 10.0) Chrome/130.0"})
    assert res.status_code == 200, res.text
    assert res.json()["user"] == {"username": "trader", "name": None, "email": None, "role": "owner"}
    cookie = res.headers["set-cookie"]
    assert SESSION_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert "Secure" not in cookie  # plain http; behind https it is
    assert client.get("/api/status").status_code == 200
    sessions = client.get("/api/auth/sessions").json()["sessions"]
    assert [(s["device"], s["current"]) for s in sessions] == [("Chrome on Windows", True)]
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/status").status_code == 401


def test_secure_cookie_behind_https(client):
    res = login(client, **{"x-forwarded-proto": "https"})
    assert "Secure" in res.headers["set-cookie"]


def test_only_the_token_hash_is_stored(client, store):
    login(client)
    token = client.cookies[SESSION_COOKIE]
    (row,) = store.sessions(store.user(username="trader")["id"])
    assert row["token_hash"] != token and len(row["token_hash"]) == 64
    assert PASSWORD not in str(store.user(username="trader"))


def test_password_change_logs_out_the_other_devices(client, store, tmp_path):
    other = TestClient(client.app)
    login(other)
    login(client)
    assert len(client.get("/api/auth/sessions").json()["sessions"]) == 2
    bad = client.put("/api/auth/password", json={"current": "nope", "new": "a new password"})
    assert bad.status_code == 403
    assert client.put("/api/auth/password", json={"current": PASSWORD, "new": "short"}).status_code == 422
    ok = client.put("/api/auth/password", json={"current": PASSWORD, "new": "a new password"})
    assert ok.status_code == 200 and len(ok.json()["sessions"]) == 1
    assert client.get("/api/status").status_code == 200
    assert other.get("/api/status").status_code == 401
    assert login(other).status_code == 401 and login(other, password="a new password").status_code == 200


def test_log_out_another_device(client):
    other = TestClient(client.app)
    login(other)
    login(client)
    theirs = next(s for s in client.get("/api/auth/sessions").json()["sessions"] if not s["current"])
    assert client.delete(f"/api/auth/sessions/{theirs['id']}").status_code == 200
    assert other.get("/api/status").status_code == 401
    assert client.delete("/api/auth/sessions/nope").status_code == 404


def test_expired_or_disabled_sessions_end(client, store):
    login(client)
    user = store.user(username="trader")
    (row,) = store.sessions(user["id"])
    store.touch_session(row["token_hash"], row["last_seen_at"], now_iso(datetime.now(timezone.utc) - timedelta(1)))
    assert client.get("/api/status").status_code == 401
    login(client)
    store.update_user(user["id"], disabled=True)
    assert client.get("/api/status").status_code == 401
    assert login(client).status_code == 401


def test_sessions_are_renewed_as_they_are_used(client, store):
    login(client)
    (row,) = store.sessions(store.user(username="trader")["id"])
    old = datetime.now(timezone.utc) - timedelta(days=2)
    store.touch_session(row["token_hash"], now_iso(old), now_iso(old + timedelta(days=30)))
    assert client.get("/api/status").status_code == 200
    (row,) = store.sessions(store.user(username="trader")["id"])
    assert row["expires_at"] > now_iso(datetime.now(timezone.utc) + timedelta(days=29))


def test_failed_logins_are_limited(store, tmp_path):
    clock = [0.0]
    auth = Auth(store, limiter=RateLimiter(limit=3, window=60, clock=lambda: clock[0]))
    auth.create_user("trader", PASSWORD)
    for _ in range(3):
        with pytest.raises(AuthError, match="Wrong"):
            auth.login("trader", "wrong", "1.2.3.4", None)
    with pytest.raises(AuthError, match="Too many") as exc:
        auth.login("trader", PASSWORD, "5.6.7.8", None)  # per username, from any address
    assert exc.value.status == 429 and exc.value.retry_after
    clock[0] = 61
    assert auth.login("trader", PASSWORD, "5.6.7.8", None)


def test_first_account_from_the_setup_code(store, tmp_path, caplog):
    auth = Auth(store)
    auth.start()
    code = auth.code
    assert code and code in caplog.text
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    feed = SyntheticFeed(seed=7, history=cfg.required_m1_bars() + 100, end=pd.Timestamp("2026-03-02 12:00"))
    client = TestClient(create_app(Engine(feed, cfg, "XAUUSD", store), ui_dir=tmp_path, auth=auth))
    assert client.get("/api/auth/status").json()["setup"] is True
    body = {"code": "WRONG-CODE-XXXX", "username": "trader", "password": PASSWORD}
    assert client.post("/api/auth/setup", json=body).status_code == 401
    res = client.post("/api/auth/setup", json={**body, "code": code.lower().replace("-", " ")})
    assert res.status_code == 200 and res.json()["logged_in"] and res.json()["user"]["username"] == "trader"
    assert client.get("/api/status").status_code == 200
    assert client.post("/api/auth/setup", json={**body, "code": code}).status_code == 409  # once
    assert client.get("/api/auth/status").json()["setup"] is False


def test_first_account_from_the_old_password_variable(store):
    Auth(store).start({"XAU_AUTH_PASSWORD": PASSWORD}.get)
    assert check_password(PASSWORD, store.user(username="admin")["password_hash"])
    # An old scrypt hash is kept, then rehashed to argon2id at the first login.
    store2 = Store("sqlite://")
    import base64
    import hashlib
    digest = base64.b64encode(hashlib.scrypt(PASSWORD.encode(), salt=b"0123456789abcdef", n=1024, r=8, p=1,
                                             dklen=32)).decode()
    auth = Auth(store2)
    auth.start({"XAU_AUTH_PASSWORD_HASH": f"scrypt$1024$8$1${base64.b64encode(b'0123456789abcdef').decode()}${digest}"}.get)
    assert store2.user(username="admin")["password_hash"].startswith("scrypt$")
    auth.login("admin", PASSWORD, "1.2.3.4", None)
    assert store2.user(username="admin")["password_hash"].startswith("$argon2id$")


def test_create_user_resets_a_password(store):
    auth = Auth(store)
    assert auth.create_user("trader", PASSWORD) is True
    token = auth.login("trader", PASSWORD, "1.2.3.4", None)
    assert auth.create_user("trader", "another password") is False
    assert auth.who(token) is None
    with pytest.raises(AuthError, match="username"):
        auth.create_user("x", PASSWORD)
    with pytest.raises(AuthError, match="password"):
        auth.create_user("trader2", "short")


def test_the_login_needs_a_database():
    with pytest.raises(AuthError, match="database"):
        Auth(None)


def test_device_names():
    assert device_name("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit Version/17 Safari/605") \
        == "Safari on macOS"
    assert device_name("Mozilla/5.0 (Windows NT 10.0) Chrome/130 Safari/537 Edg/130") == "Edge on Windows"
    assert device_name(None) is None

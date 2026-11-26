"""The login for a network-reachable dashboard (auth.py): accounts, sessions, the route guard."""

import re
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from wednesday.auth import (GUEST_COOKIE, PUBLIC, SESSION_COOKIE, Auth, AuthError, RateLimiter, admin_only,
                            check_password, device_name, guest_may, hash_password, now_iso)
from wednesday.detectors import DetectorParams
from wednesday.engine import Engine, Runtime
from wednesday.journal.service import Journals
from wednesday.settings import DataSettings
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
            client.cookies.clear()  # POST /api/auth/guest gives this client a preview pass
            guarded = res.status_code == 401 and res.json().get("detail") == "Log in first"
            assert guarded == (route.path not in PUBLIC), (method, route.path, res.status_code)
            seen += guarded
    assert seen > 50
    for path in ("/api/docs", "/api/openapi.json", "/api/no-such-route"):
        assert client.get(path).status_code == 401
    assert client.get("/").status_code != 401  # the dashboard itself loads, to show the login


def test_login_cookie_logout(client):
    assert client.get("/api/auth/status").json() == {"login": True, "logged_in": False, "setup": False,
                                                     "user": None, "signup": True, "guest": None,
                                                     "guest_available": True}
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


# ---- guests, sign-up and accounts (docs/design-guest-accounts.md) ----

def test_the_guest_pass(client, store):
    assert client.get("/api/status").status_code == 401
    res = client.post("/api/auth/guest")
    assert res.status_code == 200, res.text
    cookie = res.headers["set-cookie"]
    assert GUEST_COOKIE in cookie and "HttpOnly" in cookie and "SameSite=strict" in cookie
    until = res.json()["guest"]["until"]
    assert res.json()["guest_available"] is False and not res.json()["logged_in"]
    token = client.cookies[GUEST_COOKIE]
    assert store.guest(token, now_iso()) is None  # only the hash is stored
    # The market reads, empty lists of an account's data, nothing else.
    assert client.get("/api/status").status_code == 200
    assert client.get("/api/drawings").json()["drawings"] == []
    assert client.get("/api/workspaces").json()["workspaces"] == []
    assert client.get("/api/users").status_code == 403
    refused = client.put("/api/workspaces/Mine", json={"layout": {}, "windows": []})
    assert refused.status_code == 403 and refused.json()["detail"] == "Log in or sign up for this"
    # The same browser again: its pass runs, no new one.
    assert client.post("/api/auth/guest").json()["guest"]["until"] == until
    # Another browser at the same address: a pass ending at the same time.
    other = TestClient(client.app)
    assert other.get("/api/auth/status").json()["guest_available"] is True
    assert other.post("/api/auth/guest").json()["guest"]["until"] == until
    # Once it ended, the address gets none until the week is over.
    with store.engine.begin() as conn:
        conn.exec_driver_sql("UPDATE guests SET expires_at = ?", (now_iso(datetime.now(timezone.utc) - timedelta(1)),))
    assert client.get("/api/status").status_code == 401
    assert client.get("/api/auth/status").json()["guest_available"] is False
    ended = TestClient(client.app).post("/api/auth/guest")
    assert ended.status_code == 403 and "preview ended" in ended.json()["detail"]


def test_guest_and_admin_routes():
    assert guest_may("GET", "/api/scan") and guest_may("GET", "/api/journals")
    assert not guest_may("PUT", "/api/drawings/x") and not guest_may("GET", "/api/users")
    assert not guest_may("GET", "/api/journals/x")
    assert admin_only("GET", "/api/users") and admin_only("PUT", "/api/settings")
    assert admin_only("PUT", "/api/drawings/x/alert") and admin_only("POST", "/api/engine/scan")
    assert not admin_only("GET", "/api/settings") and not admin_only("PUT", "/api/drawings/x")


def signup(client, username="newbie", password=PASSWORD, **extra):
    return client.post("/api/auth/signup", json={"username": username, "password": password, **extra})


def test_signup_waits_for_the_admin(client, store):
    res = signup(client, email="new@example.com")
    assert res.status_code == 200 and res.json() == {"username": "newbie", "pending": True}
    assert signup(TestClient(client.app)).status_code == 409  # the name is taken
    assert signup(client, "other", email="nope").status_code == 422
    pending = login(client, "newbie")
    assert pending.status_code == 403 and "approval" in pending.json()["detail"]
    assert login(client, "newbie", "wrong").status_code == 401
    admin = TestClient(client.app)
    login(admin)
    users = admin.get("/api/users").json()["users"]
    new = next(u for u in users if u["username"] == "newbie")
    assert new["role"] == "pending" and new["email"] == "new@example.com" and "password_hash" not in new
    assert admin.patch(f"/api/users/{new['id']}", json={"role": "boss"}).status_code == 422
    res = admin.patch(f"/api/users/{new['id']}", json={"role": "user"})
    assert res.status_code == 200 and {u["username"]: u["role"] for u in res.json()["users"]}["newbie"] == "user"
    assert login(client, "newbie").status_code == 200
    assert client.get("/api/status").status_code == 200
    only = client.get("/api/users")
    assert only.status_code == 403 and only.json()["detail"] == "Only the admin can change this"
    assert client.put("/api/bias", json={}).status_code == 403
    # Disabling ends their sessions.
    assert admin.patch(f"/api/users/{new['id']}", json={"disabled": True}).status_code == 200
    assert client.get("/api/status").status_code == 401


def test_signup_needs_a_first_account(store):
    with pytest.raises(AuthError, match="setup code"):
        Auth(store).signup({"username": "early", "password": PASSWORD}, "1.2.3.4")


def test_the_last_admin_stays(client, store):
    login(client)
    me = store.user(username="trader")["id"]
    for change in ({"role": "user"}, {"disabled": True}):
        res = client.patch(f"/api/users/{me}", json=change)
        assert res.status_code == 409 and "at least one admin" in res.json()["detail"]
    assert client.delete(f"/api/users/{me}").status_code == 409
    assert client.patch("/api/users/nope", json={"role": "user"}).status_code == 404
    signup(client, "second")
    second = store.user(username="second")["id"]
    assert client.patch(f"/api/users/{second}", json={"role": "owner"}).status_code == 200
    res = client.patch(f"/api/users/{me}", json={"role": "user"})  # another admin now, but it's me
    assert res.status_code == 409 and "your own" in res.json()["detail"]
    assert client.delete(f"/api/users/{me}").status_code == 409
    res = client.delete(f"/api/users/{second}")
    assert res.status_code == 200 and [u["username"] for u in res.json()["users"]] == ["trader"]


D1, D2 = "0f6c1d2e-8b4a-4c7e-9a51-3d2f7e6b1c90", "1f6c1d2e-8b4a-4c7e-9a51-3d2f7e6b1c91"
LAST = int(pd.Timestamp("2026-03-02 10:00").timestamp())


@pytest.fixture
def full(store, auth, tmp_path):
    """A client of a :class:`Runtime`, which has the journal."""
    cfg = ScanConfig(lookback=100, params=DetectorParams(swing_length=3))
    runtime = Runtime(cfg, DataSettings(source="synthetic"), store, journals=Journals(store), poll=False)
    return TestClient(create_app(runtime, source="synthetic", ui_dir=tmp_path, auth=auth))


def two_users(client, store) -> tuple[TestClient, TestClient]:
    signup(client, "bob")
    store.update_user(store.user(username="bob")["id"], role="user")
    alice, bob = TestClient(client.app), TestClient(client.app)
    assert login(alice).status_code == 200 and login(bob, "bob").status_code == 200
    return alice, bob


def test_each_account_has_its_own_data(full, store):
    alice, bob = two_users(full, store)
    drawing = {"kind": "hline", "points": [{"t": LAST, "p": 2400}]}
    assert alice.put(f"/api/drawings/{D1}", json=drawing).status_code == 200
    assert [d["id"] for d in alice.get("/api/drawings").json()["drawings"]] == [D1]
    assert "user_id" not in alice.get("/api/drawings").json()["drawings"][0]
    assert bob.get("/api/drawings").json()["drawings"] == []
    assert bob.put(f"/api/drawings/{D1}", json=drawing).status_code == 404  # not theirs to overwrite
    assert bob.delete(f"/api/drawings/{D1}").status_code == 404
    assert alice.put("/api/workspaces/London", json={"layout": {}, "windows": []}).status_code == 200
    assert bob.get("/api/workspaces").json()["workspaces"] == []
    assert bob.get("/api/workspaces/London").status_code == 404
    journal = alice.post("/api/journals", json={"name": "Alice"}).json()
    assert "user_id" not in journal
    assert [j["name"] for j in alice.get("/api/journals").json()["journals"]] == ["Alice"]
    assert bob.get("/api/journals").json()["journals"] == []
    assert bob.get(f"/api/journals/{journal['id']}").status_code == 404
    assert bob.post("/api/journals", json={"name": "Bob"}).status_code == 200  # his one free journal
    assert alice.post("/api/terms/accept").json()["accepted"] is True
    assert bob.get("/api/terms").json()["accepted"] is False


def test_preferences_merge(client):
    login(client)
    assert client.get("/api/prefs").json() == {"prefs": {}}
    assert client.put("/api/prefs", json={"wed.tf": "15M", "xau.theme": "dark"}).status_code == 200
    res = client.put("/api/prefs", json={"wed.tf": None, "wed.chart": "candles"})
    assert res.json() == {"prefs": {"xau.theme": "dark", "wed.chart": "candles"}}
    assert client.get("/api/prefs").json() == res.json()
    bad = client.put("/api/prefs", json={"theme": "dark"})
    assert bad.status_code == 422 and "start with wed." in bad.json()["detail"]


def test_import_keeps_what_the_account_has(client, store):
    login(client)
    line = {"kind": "hline", "points": [{"t": LAST, "p": 2400}]}
    client.put(f"/api/drawings/{D1}", json=line)
    client.put("/api/workspaces/London", json={"layout": {"xau.tf": "1H"}, "windows": []})
    client.put("/api/drawing-templates/line/OB", json={"style": {"color": "#ff0000", "width": 2}})
    client.put("/api/prefs", json={"wed.tf": "1H"})
    body = {
        "drawings": [{"drawing": {**line, "id": D1}}, {"drawing": {**line, "id": D2}},
                     {"drawing": {"kind": "nope", "id": "bad"}}, {"symbol": 3}],
        "templates": [{"family": "line", "name": " OB ", "style": {"color": "#00ff00"}},
                      {"family": "rect", "name": "Zone", "style": {"color": "#00ff00"}}],
        "workspaces": [{"name": "London", "layout": {}, "windows": []}, {"name": "New York", "layout": {}}],
        "prefs": {"wed.tf": "4H", "wed.chart": "bars", "other": 1, "wed.gone": None},
    }
    res = client.post("/api/me/import", json=body)
    assert res.json() == {"drawings": 1, "templates": 1, "workspaces": 1, "prefs": 1}, res.text
    assert sorted(d["id"] for d in client.get("/api/drawings").json()["drawings"]) == sorted([D1, D2])
    templates = {(t["family"], t["name"]): t for t in client.get("/api/drawing-templates").json()["templates"]}
    assert templates[("line", "OB")]["style"]["color"] == "#ff0000"
    assert client.get("/api/workspaces/London").json()["layout"] == {"xau.tf": "1H"}
    assert client.get("/api/prefs").json()["prefs"] == {"wed.tf": "1H", "wed.chart": "bars"}


def test_what_was_saved_without_a_login_goes_to_the_admin(store):
    store.set_setting("workspaces", {"London": {"layout": {}, "windows": [], "saved_at": "x"}})
    store.drawing_put({"id": D1, "source": "synthetic", "symbol": "XAUUSD", "kind": "hline",
                       "points": [{"t": 1, "p": 2}], "style": {}, "props": None, "timeframes": None,
                       "locked": False, "hidden": False, "updated_at": now_iso()})
    auth = Auth(store)
    auth.create_user("trader", PASSWORD)
    me = store.user(username="trader")["id"]
    assert store.get_setting("workspaces") is None and "London" in store.get_setting(f"workspaces:{me}")
    assert [d["id"] for d in store.drawings("synthetic", "XAUUSD", me)] == [D1]
    # A later start claims what was saved without the login in between.
    store.set_setting("prefs", {"wed.tf": "1H"})
    auth.start()
    assert store.get_setting(f"prefs:{me}") == {"wed.tf": "1H"} and store.get_setting("prefs") is None

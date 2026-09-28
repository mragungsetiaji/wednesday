"""Optional dashboard login.

Off unless ``XAU_AUTH_PASSWORD`` (or ``XAU_AUTH_PASSWORD_HASH``, made with
``wednesday --hash-password``) is set. With it on, every ``/api`` route except
``/api/health`` and the login routes needs either

* the session cookie a browser gets from ``POST /api/auth/login`` (HttpOnly,
  SameSite=Strict, Secure when the request came over HTTPS), or
* ``Authorization: Bearer <token>`` with an API token made in Settings > Access.

Sessions and API tokens are random secrets; only their SHA-256 is stored. The
password lives in ``.env`` (hashed with scrypt in memory at start), never in the
database. Failed logins are rate limited per client address.

Cross-site requests are refused on state-changing routes whether the login is on
or not: a browser request whose ``Origin`` (or ``Sec-Fetch-Site``) says it came
from another site gets a 403, so a web page can't drive a dashboard on localhost.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from fastapi import Body, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

SESSION_COOKIE = "wednesday_session"
SESSION_DAYS = 30
PUBLIC = frozenset({"/api/health", "/api/auth/status", "/api/auth/login"})
UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})
MAX_FAILURES = 5  # failed logins per client address ...
FAILURE_WINDOW = 300.0  # ... within this many seconds, then wait


class AuthError(ValueError):
    pass


class TooManyAttempts(AuthError):
    def __init__(self, retry_after: int):
        super().__init__(f"Too many failed logins. Try again in {retry_after} s.")
        self.retry_after = retry_after


# ---- passwords -----------------------------------------------------------------------------

def hash_password(password: str, *, n: int = 2**14, r: int = 8, p: int = 1, salt: bytes | None = None) -> str:
    """``scrypt$n$r$p$salt$hash`` (base64), the form ``XAU_AUTH_PASSWORD_HASH`` takes."""
    if not password:
        raise AuthError("The password is empty")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)
    b64 = lambda b: base64.b64encode(b).decode()  # noqa: E731
    return f"scrypt${n}${r}${p}${b64(salt)}${b64(digest)}"


def check_password(password: str, encoded: str) -> bool:
    try:
        algo, n, r, p, salt, digest = encoded.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def _sha(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---- token storage (the database, or memory with --db none) -------------------------------

class MemoryTokens:
    """The Store's auth_token_* methods, in memory: sessions end when the app stops."""

    def __init__(self):
        self.rows: dict[str, dict] = {}

    def auth_token_add(self, row: dict) -> None:
        self.rows[row["id"]] = dict(row)

    def auth_token_find(self, hashed: str) -> dict | None:
        return next((dict(r) for r in self.rows.values() if r["hash"] == hashed), None)

    def auth_token_touch(self, token_id: str, when: str) -> None:
        if token_id in self.rows:
            self.rows[token_id]["last_used_at"] = when

    def auth_tokens(self, kind: str) -> list[dict]:
        return sorted((dict(r) for r in self.rows.values() if r["kind"] == kind), key=lambda r: r["created_at"])

    def auth_token_delete(self, token_id=None, kind=None, expired_before=None) -> int:
        gone = [i for i, r in self.rows.items()
                if (token_id is None or i == token_id) and (kind is None or r["kind"] == kind)
                and (expired_before is None or (r.get("expires_at") and r["expires_at"] < expired_before))]
        for i in gone:
            del self.rows[i]
        return len(gone)


class RateLimiter:
    def __init__(self, limit: int = MAX_FAILURES, window: float = FAILURE_WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.failures: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def _trim(self, key: str) -> deque:
        q = self.failures[key]
        while q and self.clock() - q[0] > self.window:
            q.popleft()
        return q

    def check(self, key: str) -> None:
        with self.lock:
            q = self._trim(key)
            if len(q) >= self.limit:
                raise TooManyAttempts(max(1, int(self.window - (self.clock() - q[0])) + 1))

    def fail(self, key: str) -> None:
        with self.lock:
            self._trim(key).append(self.clock())

    def clear(self, key: str) -> None:
        with self.lock:
            self.failures.pop(key, None)


# ---- the login -----------------------------------------------------------------------------

class Auth:
    def __init__(self, password_hash: str | None = None, store=None, limiter: RateLimiter | None = None):
        self.password_hash = password_hash
        self.tokens = store if store is not None else MemoryTokens()
        self.limiter = limiter or RateLimiter()

    @classmethod
    def from_env(cls, env, store=None) -> "Auth":
        """``env(name)`` reads a setting (``.env`` / environment). A plain password is hashed right away."""
        hashed = (env("XAU_AUTH_PASSWORD_HASH") or "").strip()
        plain = env("XAU_AUTH_PASSWORD") or ""
        if hashed and not hashed.startswith("scrypt$"):
            raise AuthError("XAU_AUTH_PASSWORD_HASH must come from: wednesday --hash-password")
        return cls(hashed or (hash_password(plain) if plain else None), store)

    @property
    def enabled(self) -> bool:
        return self.password_hash is not None

    def login(self, password: str, client: str) -> str:
        """A new session secret for the cookie; raises AuthError on a wrong password."""
        if not self.enabled:
            raise AuthError("The dashboard has no login (set XAU_AUTH_PASSWORD to turn it on)")
        self.limiter.check(client)
        if not check_password(password or "", self.password_hash):
            self.limiter.fail(client)
            raise AuthError("Wrong password")
        self.limiter.clear(client)
        now = _now()
        self.tokens.auth_token_delete(kind="session", expired_before=now.isoformat())
        secret = secrets.token_urlsafe(32)
        self.tokens.auth_token_add({
            "id": str(uuid.uuid4()), "kind": "session", "name": None, "hash": _sha(secret),
            "created_at": now.isoformat(), "last_used_at": now.isoformat(),
            "expires_at": (now + timedelta(days=SESSION_DAYS)).isoformat(),
        })
        return secret

    def logout(self, secret: str | None) -> None:
        row = self._find(secret, "session")
        if row:
            self.tokens.auth_token_delete(token_id=row["id"])

    def _find(self, secret: str | None, kind: str) -> dict | None:
        if not secret:
            return None
        row = self.tokens.auth_token_find(_sha(secret))
        if not row or row["kind"] != kind:
            return None
        if row.get("expires_at") and row["expires_at"] < _now().isoformat():
            return None
        return row

    def who(self, session: str | None, bearer: str | None) -> dict | None:
        """The API token (when the request sends one) or else the session a request carries, or None.
        A request with a bearer token is judged by that token alone, never by its cookie."""
        if bearer is not None:
            row = self._find(bearer, "api")
            if row:
                self.tokens.auth_token_touch(row["id"], _now().isoformat())
            return row
        return self._find(session, "session")

    def create_token(self, name: str) -> dict:
        name = name.strip()[:120]
        if not name:
            raise AuthError("Name the token (what uses it)")
        secret = "wed_" + secrets.token_urlsafe(32)
        row = {"id": str(uuid.uuid4()), "kind": "api", "name": name, "hash": _sha(secret),
               "created_at": _now().isoformat(), "last_used_at": None, "expires_at": None}
        self.tokens.auth_token_add(row)
        return {**_public(row), "token": secret}

    def list_tokens(self) -> list[dict]:
        return [_public(r) for r in self.tokens.auth_tokens("api")]

    def revoke(self, token_id: str) -> bool:
        return self.tokens.auth_token_delete(token_id=token_id, kind="api") > 0


def _public(row: dict) -> dict:
    return {k: row.get(k) for k in ("id", "name", "created_at", "last_used_at")}


def cross_site(method: str, headers) -> bool:
    """A state-changing browser request that came from another site."""
    if method not in UNSAFE:
        return False
    origin = headers.get("origin")
    if origin:
        host = headers.get("x-forwarded-host") or headers.get("host") or ""
        return origin == "null" or urlsplit(origin).netloc.lower() != host.split(",")[0].strip().lower()
    return headers.get("sec-fetch-site") in ("cross-site", "same-site")


def install(app, auth: Auth) -> None:
    """Add the login routes and the middleware that guards ``/api`` to a FastAPI app."""

    def bearer(request: Request) -> str | None:
        value = request.headers.get("authorization") or ""
        return value[7:].strip() if value[:7].lower() == "bearer " else None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api"):
            token = bearer(request)
            session = request.cookies.get(SESSION_COOKIE)
            who = await run_in_threadpool(auth.who, session, token) if auth.enabled else None
            # A valid API token isn't sent by a browser on its own; everything else must be same-site.
            if not (who and who["kind"] == "api") and cross_site(request.method, request.headers):
                return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
            if auth.enabled and path not in PUBLIC:
                if who is None:
                    return JSONResponse({"detail": "Log in first"}, status_code=401,
                                        headers={"WWW-Authenticate": "Bearer"})
                request.state.auth = who
        return await call_next(request)

    def client_key(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    def need_enabled() -> None:
        if not auth.enabled:
            raise HTTPException(409, "The dashboard has no login (set XAU_AUTH_PASSWORD to turn it on)")

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict:
        """Whether the dashboard needs a login and whether this browser has one."""
        logged_in = bool(auth.enabled and auth.who(request.cookies.get(SESSION_COOKIE), bearer(request)))
        return {"enabled": auth.enabled, "logged_in": logged_in}

    @app.post("/api/auth/login")
    def login(request: Request, response: Response, body: dict = Body(...)) -> dict:
        need_enabled()
        try:
            secret = auth.login(str(body.get("password") or ""), client_key(request))
        except TooManyAttempts as exc:
            raise HTTPException(429, str(exc), headers={"Retry-After": str(exc.retry_after)}) from exc
        except AuthError as exc:
            raise HTTPException(401, str(exc)) from exc
        response.set_cookie(SESSION_COOKIE, secret, max_age=SESSION_DAYS * 86400, httponly=True,
                            samesite="strict", secure=request.url.scheme == "https", path="/")
        return {"enabled": True, "logged_in": True}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response) -> dict:
        auth.logout(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return {"enabled": auth.enabled, "logged_in": False}

    @app.get("/api/auth/tokens")
    def list_tokens() -> dict:
        need_enabled()
        return {"tokens": auth.list_tokens()}

    @app.post("/api/auth/tokens")
    def create_token(body: dict = Body(...)) -> dict:
        """A new API token; the secret is in this response only."""
        need_enabled()
        try:
            return auth.create_token(str(body.get("name") or ""))
        except AuthError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/auth/tokens/{token_id}")
    def revoke_token(token_id: str) -> dict:
        need_enabled()
        if not auth.revoke(token_id):
            raise HTTPException(404, "No such token")
        return {"tokens": auth.list_tokens()}

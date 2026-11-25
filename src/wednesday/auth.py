"""The login for a dashboard reached over the network (docs/design-storage-users.md, Part 2).

On ``127.0.0.1`` (the desktop app, ``make serve``) there is no login: nobody else reaches the
port. On any other host the login is on (``--insecure`` turns it off, for development), and every
``/api`` route but :data:`PUBLIC` needs a session.

* **Accounts** live in the ``users`` table. Passwords are argon2id hashes; a scrypt hash from the
  old ``XAU_AUTH_PASSWORD_HASH`` still works and is rehashed at the next login.
* **The first account** comes from a one-time setup code printed in the log, from
  ``wednesday --create-user``, or once from ``XAU_AUTH_PASSWORD(_HASH)``.
* **Sessions** are random tokens in an HttpOnly, SameSite=Strict cookie; the ``sessions`` table
  keeps only their SHA-256. One per device, 30 days, renewed as it is used, void once the
  password changes.
* **Failed logins** are limited per client address and per username.

The Rust server (``wed-server/src/auth.rs``) does the same on the same tables.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import threading
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

from fastapi import Body, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

log = logging.getLogger(__name__)

SESSION_COOKIE = "wednesday_session"
SESSION_DAYS = 30
RENEW_SECONDS = 3600  # a session's expiry moves at most this often (one write an hour, not per request)
PUBLIC = frozenset({"/api/health", "/api/auth/status", "/api/auth/login", "/api/auth/setup"})
MAX_FAILURES = 10  # failed logins per client address, and per username, ...
FAILURE_WINDOW = 15 * 60.0  # ... in this many seconds
SETUP_SECONDS = 3600  # the setup code's life
MIN_PASSWORD, MAX_PASSWORD = 8, 256
USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{1,63}")
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
NO_LOGIN = "This dashboard has no login: it is served on this computer only (127.0.0.1)"

# argon2id with OWASP's minimum (19 MiB, 2 passes, 1 lane); the Rust server uses the same.
_TIME_COST, _MEMORY_COST, _PARALLELISM = 2, 19456, 1


class AuthError(ValueError):
    """A refused auth request: ``status`` is the HTTP status, the message says what to do."""

    def __init__(self, message: str, status: int = 400, retry_after: int | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def now_iso(when: datetime | None = None) -> str:
    """The timestamps both servers write, so they compare as text."""
    return (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# ---- passwords -----------------------------------------------------------------------------

def _hasher():
    from argon2 import PasswordHasher, Type

    return PasswordHasher(time_cost=_TIME_COST, memory_cost=_MEMORY_COST, parallelism=_PARALLELISM,
                          hash_len=32, salt_len=16, type=Type.ID)


def hash_password(password: str) -> str:
    """An argon2id hash in the PHC form (``$argon2id$v=19$m=...``)."""
    return _hasher().hash(password)


def check_password(password: str, encoded: str) -> bool:
    """Whether ``password`` matches an argon2id hash or an old ``scrypt$n$r$p$salt$hash``."""
    if encoded.startswith("$argon2"):
        from argon2.exceptions import Argon2Error, InvalidHashError

        try:
            return _hasher().verify(encoded, password)
        except (Argon2Error, InvalidHashError):
            return False
    try:
        algo, n, r, p, salt, digest = encoded.split("$")
        if algo != "scrypt":
            return False
        want = base64.b64decode(digest)
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                             dklen=len(want), maxmem=2**26)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, want)


def needs_rehash(encoded: str) -> bool:
    return not encoded.startswith("$argon2id$")


def clean_username(value) -> str:
    name = str(value or "").strip().lower()
    if not USERNAME.fullmatch(name):
        raise AuthError("Use 2 to 64 letters, digits, dots, dashes or underscores for the username", 422)
    return name


def clean_password(value) -> str:
    password = value if isinstance(value, str) else ""
    if not MIN_PASSWORD <= len(password) <= MAX_PASSWORD:
        raise AuthError(f"Use a password of {MIN_PASSWORD} to {MAX_PASSWORD} characters", 422)
    return password


def device_name(user_agent: str | None) -> str | None:
    """"Chrome on Windows" from a User-Agent, for the devices list."""
    ua = user_agent or ""
    os_name = next((name for key, name in (("Windows", "Windows"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                                            ("Android", "Android"), ("Mac OS", "macOS"), ("Linux", "Linux"))
                    if key in ua), None)
    browser = next((name for key, name in (("Edg/", "Edge"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                                            ("Safari/", "Safari")) if key in ua), None)
    if browser and os_name:
        return f"{browser} on {os_name}"
    return browser or os_name or (ua[:100] or None)


def setup_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


def _same_code(given: str, code: str) -> bool:
    norm = lambda s: re.sub(r"[\s-]", "", s).upper()  # noqa: E731
    return hmac.compare_digest(norm(given).encode(), norm(code).encode())


# ---- failed logins -------------------------------------------------------------------------

class RateLimiter:
    """Failed attempts per key (``ip:...``, ``user:...``); ``check`` refuses once a key has too many."""

    def __init__(self, limit: int = MAX_FAILURES, window: float = FAILURE_WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.failures: dict[str, deque] = defaultdict(deque)
        self.lock = threading.Lock()

    def _trim(self, key: str) -> deque:
        q = self.failures[key]
        while q and self.clock() - q[0] > self.window:
            q.popleft()
        return q

    def check(self, *keys: str) -> None:
        with self.lock:
            for key in keys:
                q = self._trim(key)
                if len(q) >= self.limit:
                    wait = max(1, int(self.window - (self.clock() - q[0])) + 1)
                    raise AuthError(f"Too many failed attempts. Try again in {(wait + 59) // 60} min.", 429, wait)

    def fail(self, *keys: str) -> None:
        with self.lock:
            for key in keys:
                self._trim(key).append(self.clock())

    def clear(self, *keys: str) -> None:
        with self.lock:
            for key in keys:
                self.failures.pop(key, None)


# ---- the login -----------------------------------------------------------------------------

def _public_user(user: dict) -> dict:
    return {k: user.get(k) for k in ("username", "name", "email", "role")}


class Auth:
    """Accounts and sessions on ``store``; ``enabled`` is False on loopback (no login)."""

    def __init__(self, store, enabled: bool = True, limiter: RateLimiter | None = None, clock=time.monotonic):
        if enabled and store is None:
            raise AuthError("The login needs a database: remove XAU_DB_URL=none from .env", 500)
        self.store = store
        self.enabled = enabled
        self.limiter = limiter or RateLimiter()
        self.clock = clock
        self.code: str | None = None
        self.code_until = 0.0
        self.lock = threading.Lock()

    # -- start -----------------------------------------------------------------------------
    def start(self, env=lambda name: None) -> None:
        """With no account yet: create it from ``XAU_AUTH_PASSWORD(_HASH)``, else log a setup code."""
        if not self.enabled or self.store.users_count():
            return
        hashed = (env("XAU_AUTH_PASSWORD_HASH") or "").strip()
        plain = env("XAU_AUTH_PASSWORD") or ""
        if hashed or plain:
            self._add_user("admin", hashed or hash_password(plain))
            log.warning("created the account 'admin' from %s; remove it from .env and log in as admin",
                        "XAU_AUTH_PASSWORD_HASH" if hashed else "XAU_AUTH_PASSWORD")
            return
        self.code, self.code_until = setup_code(), self.clock() + SETUP_SECONDS
        log.warning("no account yet: open the dashboard, choose Create account and enter the setup code %s "
                    "(it works once, for an hour)", self.code)

    def _add_user(self, username: str, password_hash: str) -> dict:
        now = now_iso()
        row = {"id": str(uuid.uuid4()), "username": username, "email": None, "name": None,
               "password_hash": password_hash, "role": "owner", "disabled": False,
               "created_at": now, "password_at": now, "last_login_at": None}
        self.store.add_user(row)
        return row

    def create_user(self, username: str, password: str) -> bool:
        """``wednesday --create-user``: add the account, or set a new password on it (True when new).
        A new password ends the account's sessions."""
        username, password = clean_username(username), clean_password(password)
        user = self.store.user(username=username)
        if user is None:
            self._add_user(username, hash_password(password))
            return True
        self.store.update_user(user["id"], password_hash=hash_password(password), password_at=now_iso(),
                               disabled=False)
        self.store.delete_sessions(user_id=user["id"])
        return False

    # -- sessions --------------------------------------------------------------------------
    def _new_session(self, user: dict, device: str | None) -> str:
        now = datetime.now(timezone.utc)
        self.store.delete_sessions(expired_before=now_iso(now))
        token = secrets.token_urlsafe(32)
        self.store.add_session({
            "token_hash": _sha(token), "user_id": user["id"], "device": (device or "")[:100] or None,
            "created_at": now_iso(now), "expires_at": now_iso(now + timedelta(days=SESSION_DAYS)),
            "last_seen_at": now_iso(now)})
        self.store.update_user(user["id"], last_login_at=now_iso(now))
        return token

    def who(self, token: str | None) -> tuple[dict, dict] | None:
        """The (user, session) a cookie belongs to, renewing it; None when it is no session."""
        if not self.enabled or not token:
            return None
        session = self.store.session(_sha(token))
        if session is None:
            return None
        now = datetime.now(timezone.utc)
        user = self.store.user(user_id=session["user_id"])
        if (session["expires_at"] < now_iso(now) or user is None or user["disabled"]
                or session["created_at"] < user["password_at"]):
            self.store.delete_sessions(token_hash=session["token_hash"])
            return None
        seen = session.get("last_seen_at") or session["created_at"]
        if seen < now_iso(now - timedelta(seconds=RENEW_SECONDS)):
            self.store.touch_session(session["token_hash"], now_iso(now),
                                     now_iso(now + timedelta(days=SESSION_DAYS)))
        return user, session

    def status(self, token: str | None) -> dict:
        found = self.who(token)
        return {"login": self.enabled, "logged_in": found is not None,
                "setup": bool(self.enabled and self.code and self.store.users_count() == 0),
                "user": _public_user(found[0]) if found else None}

    def _need_enabled(self) -> None:
        if not self.enabled:
            raise AuthError(NO_LOGIN, 409)

    def login(self, username, password, client: str, device: str | None) -> str:
        """A new session token; AuthError on a wrong name or password, or too many tries."""
        self._need_enabled()
        name = str(username or "").strip().lower()
        keys = (f"ip:{client}", f"user:{name}")
        self.limiter.check(*keys)
        user = self.store.user(username=name) if name else None
        given = password if isinstance(password, str) else ""
        ok = user is not None and not user["disabled"] and check_password(given, user["password_hash"])
        if user is None:
            check_password(given, _dummy_hash())  # the same time whether the name exists or not
        if not ok:
            self.limiter.fail(*keys)
            raise AuthError("Wrong username or password", 401)
        self.limiter.clear(*keys)
        if needs_rehash(user["password_hash"]):
            self.store.update_user(user["id"], password_hash=hash_password(given))
        return self._new_session(user, device)

    def setup(self, code, username, password, client: str, device: str | None) -> str:
        """Create the first account with the setup code from the log, and log it in."""
        self._need_enabled()
        self.limiter.check(f"ip:{client}", "setup")
        with self.lock:
            if self.store.users_count():
                raise AuthError("The account exists already: log in", 409)
            if not self.code or self.clock() > self.code_until:
                raise AuthError("The setup code expired: restart Wednesday for a new one in its log", 410)
            if not _same_code(str(code or ""), self.code):
                self.limiter.fail(f"ip:{client}", "setup")
                raise AuthError("Wrong setup code: copy it from the server's log", 401)
            user = self._add_user(clean_username(username), hash_password(clean_password(password)))
            self.code = None
        log.info("created the account %s", user["username"])
        return self._new_session(user, device)

    def logout(self, token: str | None) -> None:
        if self.enabled and token:
            self.store.delete_sessions(token_hash=_sha(token))

    def sessions(self, token: str | None) -> list[dict]:
        """The devices logged in to this account, this one marked ``current``."""
        found = self.who(token)
        if found is None:
            return []
        user, current = found
        return [{"id": s["token_hash"][:16], "device": s.get("device"), "created_at": s["created_at"],
                 "last_seen_at": s.get("last_seen_at"), "current": s["token_hash"] == current["token_hash"]}
                for s in self.store.sessions(user["id"])]

    def end_session(self, token: str | None, session_id: str) -> None:
        self._need_enabled()
        found = self.who(token)
        if found is None:
            raise AuthError("Log in first", 401)
        user, _ = found
        hit = [s for s in self.store.sessions(user["id"]) if session_id and s["token_hash"].startswith(session_id)]
        if len(hit) != 1:
            raise AuthError("No such device: reload the list", 404)
        self.store.delete_sessions(token_hash=hit[0]["token_hash"])

    def change_password(self, token: str | None, current, new) -> None:
        """Set a new password; every other device is logged out."""
        self._need_enabled()
        found = self.who(token)
        if found is None:
            raise AuthError("Log in first", 401)
        user, session = found
        keys = (f"user:{user['username']}",)
        self.limiter.check(*keys)
        if not check_password(current if isinstance(current, str) else "", user["password_hash"]):
            self.limiter.fail(*keys)
            raise AuthError("The current password is wrong", 403)
        new = clean_password(new)
        # A session made now still counts: it is created after password_at.
        self.store.update_user(user["id"], password_hash=hash_password(new),
                               password_at=session["created_at"])
        self.store.delete_sessions(user_id=user["id"], keep=session["token_hash"])


_dummy: list[str] = []


def _dummy_hash() -> str:
    """A hash to check a wrong username against, so it takes as long as a wrong password."""
    if not _dummy:
        _dummy.append(hash_password(secrets.token_urlsafe(16)))
    return _dummy[0]


# ---- the FastAPI side ----------------------------------------------------------------------

def client_address(request) -> str:
    """The client's address; behind a proxy on this machine, the address the proxy saw."""
    peer = request.client.host if request.client else "unknown"
    if peer in ("127.0.0.1", "::1"):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[-1].strip() or peer
    return peer


def is_https(request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def guarded(path: str) -> bool:
    """Whether a path needs a session when the login is on."""
    return path.startswith("/api") and path not in PUBLIC


def install(app, auth: Auth) -> None:
    """The ``/api/auth`` routes and the middleware that asks for a session (when the login is on)."""
    @app.middleware("http")
    async def need_session(request: Request, call_next):
        if auth.enabled and guarded(request.url.path):
            found = await run_in_threadpool(auth.who, request.cookies.get(SESSION_COOKIE))
            if found is None:
                return JSONResponse({"detail": "Log in first"}, status_code=401)
        return await call_next(request)

    def refuse(exc: AuthError) -> HTTPException:
        headers = {"Retry-After": str(exc.retry_after)} if exc.retry_after else None
        return HTTPException(exc.status, str(exc), headers=headers)

    def set_cookie(request: Request, response: Response, token: str) -> None:
        response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_DAYS * 86400, httponly=True,
                            samesite="strict", secure=is_https(request), path="/")

    def device(request: Request, body: dict) -> str | None:
        given = body.get("device")
        return given.strip()[:100] if isinstance(given, str) and given.strip() else \
            device_name(request.headers.get("user-agent"))

    @app.get("/api/auth/status")
    def auth_status(request: Request) -> dict:
        """Whether the dashboard has a login, whether this browser is logged in, and as whom."""
        return auth.status(request.cookies.get(SESSION_COOKIE))

    @app.post("/api/auth/login")
    def auth_login(request: Request, response: Response, body: dict = Body(...)) -> dict:
        try:
            token = auth.login(body.get("username"), body.get("password"), client_address(request),
                               device(request, body))
        except AuthError as exc:
            raise refuse(exc) from exc
        set_cookie(request, response, token)
        return auth.status(token)

    @app.post("/api/auth/setup")
    def auth_setup(request: Request, response: Response, body: dict = Body(...)) -> dict:
        """The first account, with the setup code from the server's log."""
        try:
            token = auth.setup(body.get("code"), body.get("username"), body.get("password"),
                               client_address(request), device(request, body))
        except AuthError as exc:
            raise refuse(exc) from exc
        set_cookie(request, response, token)
        return auth.status(token)

    @app.post("/api/auth/logout")
    def auth_logout(request: Request, response: Response) -> dict:
        auth.logout(request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/")
        return auth.status(None)

    @app.get("/api/auth/sessions")
    def auth_sessions(request: Request) -> dict:
        """The devices logged in to this account (none without a login)."""
        return {"sessions": auth.sessions(request.cookies.get(SESSION_COOKIE))}

    @app.delete("/api/auth/sessions/{session_id}")
    def auth_end_session(session_id: str, request: Request) -> dict:
        """Log a device out (a lost laptop, say)."""
        token = request.cookies.get(SESSION_COOKIE)
        try:
            auth.end_session(token, session_id)
        except AuthError as exc:
            raise refuse(exc) from exc
        return {"sessions": auth.sessions(token)}

    @app.put("/api/auth/password")
    def auth_password(request: Request, body: dict = Body(...)) -> dict:
        """A new password; the other devices are logged out."""
        token = request.cookies.get(SESSION_COOKIE)
        try:
            auth.change_password(token, body.get("current"), body.get("new"))
        except AuthError as exc:
            raise refuse(exc) from exc
        return {"sessions": auth.sessions(token)}

"""Refuse cross-site writes to the dashboard's API.

The dashboard has no login: it runs on the trader's own machine, on ``127.0.0.1``. What it
does need is protection from other web pages in the same browser: a page on another site
could otherwise send ``PUT``/``POST``/``DELETE`` requests to ``http://127.0.0.1:8000/api``
and change settings or delete data. A browser request whose ``Origin`` (or
``Sec-Fetch-Site``) says it came from another site gets a 403. Scripts and ``curl`` send
no ``Origin`` and pass; reads (``GET``) always pass.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi.responses import JSONResponse
from starlette.requests import Request

UNSAFE = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def cross_site(method: str, headers) -> bool:
    """A state-changing browser request that came from another site."""
    if method not in UNSAFE:
        return False
    origin = headers.get("origin")
    if origin:
        host = headers.get("x-forwarded-host") or headers.get("host") or ""
        return origin == "null" or urlsplit(origin).netloc.lower() != host.split(",")[0].strip().lower()
    return headers.get("sec-fetch-site") in ("cross-site", "same-site")


def install(app) -> None:
    """Add the middleware that refuses cross-site writes to ``/api``."""

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if request.url.path.startswith("/api") and cross_site(request.method, request.headers):
            return JSONResponse({"detail": "Cross-site request refused"}, status_code=403)
        return await call_next(request)

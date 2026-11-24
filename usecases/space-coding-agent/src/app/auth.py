# Copyright (C) 2025-2026 Intel Corporation
# SPDX-License-Identifier: Apache-2.0
"""One shared app token: Bearer header for API clients, a signed cookie for the browser.

The cookie lets EventSource (which cannot send headers) authenticate. The route
opts out of the gateway's LiteLLM auth, so this token is the only gate.
"""

import hashlib
import hmac
import time

from fastapi import HTTPException, Request

from .config import settings

COOKIE = "sca_session"
COOKIE_TTL_S = 12 * 3600
_failures: dict[str, list[float]] = {}


def _sign(value: str) -> str:
    return hmac.new(settings.app_auth_token.encode(), value.encode(), hashlib.sha256).hexdigest()


def make_cookie() -> str:
    issued = str(int(time.time()))
    return f"{issued}.{_sign(issued)}"


def _cookie_ok(cookie: str) -> bool:
    try:
        issued, mac = cookie.split(".", 1)
        fresh = time.time() - int(issued) < COOKIE_TTL_S
    except ValueError:
        return False
    return fresh and hmac.compare_digest(mac, _sign(issued))


def token_ok(token: str) -> bool:
    return bool(settings.app_auth_token) and hmac.compare_digest(token.encode(), settings.app_auth_token.encode())


def throttled(client: str) -> bool:
    """At most 10 failed logins per client per 5 minutes."""
    now = time.time()
    recent = [t for t in _failures.get(client, []) if now - t < 300]
    _failures[client] = recent
    return len(recent) >= 10


def record_failure(client: str) -> None:
    _failures.setdefault(client, []).append(time.time())


async def require_auth(request: Request) -> None:
    if not settings.app_auth_token:
        raise HTTPException(503, "APP_AUTH_TOKEN is not configured")
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer ") and token_ok(header[7:].strip()):
        return
    cookie = request.cookies.get(COOKIE)
    if cookie and _cookie_ok(cookie):
        return
    raise HTTPException(401, "unauthorized")

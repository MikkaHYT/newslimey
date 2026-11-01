"""Password gate for the web dashboard.

The dashboard is a single-admin surface: one password from the environment,
kept in a signed cookie afterwards so the browser never re-sends it. The HMAC
key is derived from the password itself, so restarting the bot does not log
everyone out, and there is no session table to keep in sync.

Nothing here stores or logs the password.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from typing import Any

from aiohttp import web

#: Cookie carrying the signed session token.
COOKIE_NAME = "slimey_dashboard"

#: How long a login lasts before the password has to be entered again.
SESSION_TTL = 7 * 24 * 60 * 60

#: ``ip -> [failure timestamps]``. Five bad passwords in a minute is a lockout.
MAX_FAILURES = 5
FAILURE_WINDOW = 60.0

#: Paths reachable without a session. Everything else under /api needs one.
_PUBLIC_PATHS = frozenset({"/api/login", "/api/session", "/api/health"})


class DashboardAuth:
    """Verify the shared password and mint/validate session cookies."""

    def __init__(self, password: str) -> None:
        if not password:
            raise ValueError("A dashboard password is required")
        self._password = password
        self._secret = hashlib.sha256(
            b"slimey-dashboard-session:" + password.encode("utf-8")
        ).digest()
        self._failures: dict[str, list[float]] = {}

    # -- password ---------------------------------------------------------
    def check_password(self, candidate: Any) -> bool:
        if not isinstance(candidate, str) or not candidate:
            return False
        return hmac.compare_digest(candidate, self._password)

    # -- sessions ---------------------------------------------------------
    def _sign(self, stamp: str) -> str:
        return hmac.new(self._secret, stamp.encode("ascii"), hashlib.sha256).hexdigest()

    def issue_token(self) -> str:
        """A fresh ``<issued-at>.<signature>`` token for the cookie."""
        stamp = str(int(time.time()))
        return f"{stamp}.{self._sign(stamp)}"

    def token_is_valid(self, token: Any) -> bool:
        if not isinstance(token, str) or "." not in token:
            return False
        stamp, _, signature = token.partition(".")
        if not stamp.isdigit() or not signature:
            return False
        try:
            issued = int(stamp)
        except ValueError:
            return False
        age = time.time() - issued
        # A little clock skew in the future is tolerated; a long way is not.
        if age < -300 or age > SESSION_TTL:
            return False
        return hmac.compare_digest(signature, self._sign(stamp))

    def authenticated(self, request: web.Request) -> bool:
        return self.token_is_valid(request.cookies.get(COOKIE_NAME))

    # -- throttling -------------------------------------------------------
    def is_locked_out(self, address: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(address, []) if now - t < FAILURE_WINDOW]
        if recent:
            self._failures[address] = recent
        else:
            self._failures.pop(address, None)
        return len(recent) >= MAX_FAILURES

    def record_failure(self, address: str) -> None:
        self._failures.setdefault(address, []).append(time.time())

    def clear_failures(self, address: str) -> None:
        self._failures.pop(address, None)

    def retry_after(self, address: str) -> int:
        stamps = self._failures.get(address) or []
        if not stamps:
            return 0
        return max(0, int(FAILURE_WINDOW - (time.time() - min(stamps))))


def _client_address(request: web.Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


def set_session_cookie(response: web.StreamResponse, token: str) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=SESSION_TTL,
        httponly=True,
        samesite="Lax",
        path="/",
    )


def clear_session_cookie(response: web.StreamResponse) -> None:
    response.del_cookie(COOKIE_NAME, path="/")


def make_auth_middleware(auth: DashboardAuth):
    """Reject unauthenticated API calls before they reach a handler."""

    @web.middleware
    async def middleware(request: web.Request, handler):
        path = request.path
        # The live feed carries message traffic, so it needs the same gate as
        # the JSON API even though it does not live under /api.
        protected = path == "/ws" or (path.startswith("/api/") and path not in _PUBLIC_PATHS)
        if protected:
            if not auth.authenticated(request):
                return web.json_response(
                    {"error": "Not signed in", "code": "unauthenticated"}, status=401
                )
        return await handler(request)

    return middleware


def generate_password() -> str:
    """A random fallback password, used when the env var is missing."""
    return secrets.token_urlsafe(18)

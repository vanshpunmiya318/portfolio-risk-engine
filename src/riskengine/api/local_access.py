"""Loopback HTTP authorization for the launcher-managed application."""

import asyncio
import os
import re
import secrets
from time import monotonic

from starlette.requests import Request
from starlette.responses import JSONResponse, Response


SESSION_PATH = "/api/local-session"
BOOTSTRAP_SECONDS = 180
SESSION_SECONDS = 12 * 60 * 60
LEGACY_MODE = "legacy-read-only"
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}", re.ASCII)
_SAFE_METHODS = {"GET", "HEAD"}


def _port(name: str) -> int:
    value = os.environ.get(name, "")
    if not re.fullmatch(r"[0-9]{4,5}", value) or not 1024 <= int(value) <= 65535:
        raise RuntimeError("Invalid protected local API configuration. Start with dev.cmd.")
    return int(value)


def load_local_access() -> "LocalAccess | bool":
    mode = os.environ.get("RISKENGINE_API_MODE", "protected")
    if mode == LEGACY_MODE:
        return False
    code = os.environ.get("RISKENGINE_BOOTSTRAP_CODE", "")
    if mode != "protected" or not _TOKEN.fullmatch(code):
        raise RuntimeError("Missing or invalid local session configuration. Start with dev.cmd.")
    return LocalAccess(_port("RISKENGINE_API_PORT"), _port("RISKENGINE_VITE_PORT"), code)


def deny(status: int, detail: str) -> Response:
    return JSONResponse({"detail": detail}, status, headers={"Cache-Control": "no-store"})


class LocalAccess:
    def __init__(self, api_port: int, browser_port: int, bootstrap: str):
        self.listener = ("127.0.0.1", api_port)
        self.browser_host = f"127.0.0.1:{browser_port}"
        self.cookie_name = f"riskengine_local_session_{browser_port}"
        self.origin = f"http://{self.browser_host}"
        self.hosts = {self.browser_host, f"127.0.0.1:{api_port}"}
        self.bootstrap = bootstrap
        self.bootstrap_until = monotonic() + BOOTSTRAP_SECONDS
        self.session = None
        self.session_until = 0.0
        self.generation = secrets.token_urlsafe(16)
        self.browser_confirmed = asyncio.Event()

    def confirm_browser(self, request: Request, path: str) -> None:
        if path == SESSION_PATH and request.method == "GET" and request.headers["host"] == self.browser_host:
            self.browser_confirmed.set()

    def guard(self, request: Request, path: str) -> Response | None:
        headers = request.headers
        hosts = headers.getlist("host")
        if (
            request.client is None or request.client.host != "127.0.0.1"
            or request.scope.get("server") != self.listener
            or len(hosts) != 1 or hosts[0] not in self.hosts
            or any(name in headers for name in ("forwarded", "x-forwarded-host", "x-forwarded-for", "x-forwarded-proto"))
        ):
            return deny(400, "Invalid local request host.")

        origins = headers.getlist("origin")
        site = headers.getlist("sec-fetch-site")
        if (origins and (origins != [self.origin] or hosts != [self.browser_host])) or (
            site and site not in (["same-origin"], ["none"])
        ):
            return deny(403, "Invalid local request origin.")

        method = request.method
        if path in ("/health", "/ready") and method in _SAFE_METHODS:
            return None
        if path.rstrip("/") in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"):
            return deny(404, "Not found.")

        if path == SESSION_PATH and method in {"HEAD", "POST"}:
            if origins != [self.origin] or hosts != [self.browser_host]:
                return deny(403, "Invalid local request origin.")
            codes = headers.getlist("x-riskengine-bootstrap")
            if (
                len(codes) != 1 or not _TOKEN.fullmatch(codes[0]) or self.bootstrap is None
                or monotonic() >= self.bootstrap_until or not secrets.compare_digest(codes[0], self.bootstrap)
            ):
                return deny(401, "Local launch code is invalid or expired. Restart dev.cmd.")
            response = Response(status_code=204, headers={"Cache-Control": "no-store"})
            if method == "POST":
                # The synchronous check and consumption have no intervening await.
                self.bootstrap = None
                self.session = secrets.token_urlsafe(32)
                self.session_until = monotonic() + SESSION_SECONDS
                response.set_cookie(self.cookie_name, self.session, path="/api", httponly=True, samesite="strict")
            return response

        if method not in _SAFE_METHODS and (
            origins != [self.origin] or headers.getlist("x-riskengine-request") != ["1"]
        ):
            return deny(403, "Local write request verification failed.")
        cookie = request.cookies.get(self.cookie_name, "")
        if (
            self.session is None or monotonic() >= self.session_until
            or not _TOKEN.fullmatch(cookie) or not secrets.compare_digest(cookie, self.session)
        ):
            return deny(401, "Local session is missing or expired. Restart dev.cmd.")
        return None

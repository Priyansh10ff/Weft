"""HTTP hardening: request IDs and logging, security headers, optional API key.

* Every response carries ``X-Request-ID`` (the caller's, or a new one) and a
  log line with method, path, status and duration. An unhandled exception
  becomes a JSON 500 with the request ID instead of a stack trace.
* Security headers on every response. The website gets a strict Content
  Security Policy (everything from this origin, no inline scripts); the
  interactive API docs, which load Swagger UI from a CDN, are left out.
* When ``WEFT_API_KEY`` is set, every request that changes data or calls a
  model (all ``POST``, ``PUT``, ``PATCH`` and ``DELETE``) must send it as
  ``X-API-Key`` or ``Authorization: Bearer``. Reads stay open so media can be
  played by the browser; put the server behind your own auth if the data
  itself is private.
"""

from __future__ import annotations

import hmac
import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.config import get_settings

_log = logging.getLogger("weft.http")

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DOCS_PATHS = ("/docs", "/redoc", "/openapi.json")

CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "media-src 'self' blob:",
        "font-src 'self'",
        "connect-src 'self'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
    ]
)
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    "Cross-Origin-Opener-Policy": "same-origin",
}


def presented_key(request: Request) -> str | None:
    key = request.headers.get("x-api-key")
    if key:
        return key.strip()
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return None


def key_is_valid(expected: str, presented: str | None) -> bool:
    return bool(presented) and hmac.compare_digest(expected.encode(), presented.encode())


class HardeningMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        started = time.perf_counter()

        settings = get_settings()
        if (
            settings.api_key
            and request.method in WRITE_METHODS
            and not key_is_valid(settings.api_key, presented_key(request))
        ):
            response: Response = JSONResponse(
                {"detail": "This server requires an API key for this request. Send it as X-API-Key."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            try:
                response = await call_next(request)
            except Exception:  # anything a route did not turn into an HTTP error
                _log.exception("Unhandled error on %s %s [%s]", request.method, request.url.path, request_id)
                response = JSONResponse(
                    {"detail": "Internal server error.", "request_id": request_id}, status_code=500
                )

        response.headers["X-Request-ID"] = request_id
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if not request.url.path.startswith(_DOCS_PATHS):
            response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)

        elapsed = (time.perf_counter() - started) * 1000
        if not request.url.path.startswith(("/static/", "/uploads/", "/derived/", "/frames/")):
            _log.info("%s %s %d %.0fms [%s]", request.method, request.url.path, response.status_code, elapsed, request_id)
        return response

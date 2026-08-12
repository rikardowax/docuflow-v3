"""
DocuFlow v3.0 - Security middleware

Changes vs v2.2:
  + Content-Security-Policy header (was absent)
  + /docs and /redoc disabled in ENV=production (guard in middleware)
  RequestIDMiddleware and SecurityHeadersMiddleware unchanged otherwise.
"""
import time
import uuid
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── CSP policy ────────────────────────────────────────────────────────
# Strict for API routes; relaxed for Swagger/ReDoc (needs CDN scripts/styles).
_CSP_API = (
    "default-src 'none'; "
    "script-src 'none'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none';"
)

_CSP_DOCS = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "img-src 'self' data: blob: https://fastapi.tiangolo.com; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'self';"
)

_DOCS_PATHS = frozenset({"/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"})


def _is_docs_path(path: str) -> bool:
    return path in _DOCS_PATHS or path.startswith("/docs/")


def _is_app_path(path: str) -> bool:
    return path == "/app" or path.startswith("/app/")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # Block Swagger/ReDoc in production before the request reaches FastAPI
        if settings.is_production and request.url.path in ("/docs", "/redoc", "/openapi.json"):
            return JSONResponse(
                status_code=404,
                content={"error": "NOT_FOUND", "message": "Not found"},
            )

        response = await call_next(request)

        response.headers["X-Content-Type-Options"]   = "nosniff"
        response.headers["X-Frame-Options"]           = "DENY"
        response.headers["X-XSS-Protection"]          = "1; mode=block"
        response.headers["Referrer-Policy"]            = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"]         = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"]    = (
            _CSP_DOCS if (_is_docs_path(request.url.path) or _is_app_path(request.url.path))
            else _CSP_API
        )
        response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"

        if request.url.scheme == "https" or settings.is_production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

        # Never cache API responses (contains PII)
        if request.url.path.startswith("/v2/"):
            response.headers["Cache-Control"] = "no-store"
            response.headers["Pragma"]        = "no-cache"

        return response


class RequestIDMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        start    = time.time()
        response = await call_next(request)
        elapsed  = round((time.time() - start) * 1000, 1)

        response.headers["X-Request-ID"]    = request_id
        response.headers["X-Response-Time"] = f"{elapsed}ms"

        logger.info(
            f"{request.method} {request.url.path} → {response.status_code} ({elapsed}ms)",
            extra={"request_id": request_id},
        )
        return response

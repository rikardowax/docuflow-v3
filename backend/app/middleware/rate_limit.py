"""DocuFlow - Rate Limiting Middleware (sliding window via Redis)"""
from fastapi import Request, HTTPException, status
from starlette.middleware.base import BaseHTTPMiddleware
from app.core.config import settings
from app.core.redis_client import check_rate_limit
from app.core.logging import get_logger

logger = get_logger(__name__)

ENDPOINT_LIMITS = {
    "/v2/auth/token":     (5,  60),
    "/v2/process/batch":  (10, 60),
    "/v2/process":        (30, 60),
    "/v2/templates":      (60, 60),
    "/v2/stats":          (60, 60),
    "/v2/health":         (120, 60),
}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if not settings.RATE_LIMIT_ENABLED:
            return await call_next(request)

        # Identify client
        client_ip = request.headers.get("X-Forwarded-For", request.client.host if request.client else "unknown").split(",")[0].strip()
        auth = request.headers.get("Authorization", "")
        client_key = auth[-16:] if auth else client_ip

        # Find applicable limit
        path = request.url.path
        limit, window = ENDPOINT_LIMITS.get(path, (60, 60))

        rate_key = f"{path}:{client_key}"
        allowed, remaining = await check_rate_limit(rate_key, limit, window)

        if not allowed:
            logger.warning(f"Rate limit exceeded: {client_key} on {path}")
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Rate limit exceeded. Please slow down.",
                headers={
                    "X-RateLimit-Limit": str(limit),
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(window),
                    "Retry-After": str(window),
                }
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"]     = str(limit)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response

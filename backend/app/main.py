"""DocuFlow v3.0 - Production FastAPI Application"""
import time
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.logging import setup_logging, get_logger
from app.core.database import init_db
from app.core.redis_client import init_redis
from app.middleware.rate_limit import RateLimitMiddleware
from app.middleware.security import SecurityHeadersMiddleware, RequestIDMiddleware
from app.api.v2 import router as v2_router

setup_logging()
logger = get_logger(__name__)
_start_time = time.time()


async def _seed_demo_client():
    """Seed a demo_client / demo_secret user for dev. Idempotent."""
    from app.core.database import AsyncSessionLocal
    from app.core.db_repositories import ClientRepository
    from app.core.security import hash_password

    async with AsyncSessionLocal() as session:
        repo = ClientRepository(session)
        existing = await repo.get_by_client_id("demo_client")
        if not existing:
            await repo.create(
                client_id="demo_client",
                name="Demo Client",
                secret_hash=hash_password("demo_secret"),
                role="admin",
            )
            logger.info("Seeded demo_client (admin)")
        else:
            logger.info("demo_client already exists — skipping seed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting DocuFlow v{settings.VERSION} [{settings.ENV}]")

    for name, coro in [
        ("Database", init_db()),
        ("Redis",    init_redis()),
    ]:
        try:
            await coro
            logger.info(f"{name} ready")
        except Exception as e:
            logger.warning(f"{name}: {e} — degraded mode")

    # Seed built-in templates (idempotent — skips if already present)
    try:
        from app.core.db_repositories import seed_builtin_templates
        await seed_builtin_templates()
        logger.info("Built-in templates seeded")
    except Exception as e:
        logger.error(f"Template seeding failed: {e}")

    # Seed demo client for dev login (idempotent)
    try:
        await _seed_demo_client()
    except Exception as e:
        logger.error(f"Demo client seeding failed: {e}")

    try:
        from app.services.queue import queue_service
        await queue_service.connect()
    except Exception as e:
        logger.warning(f"Queue: {e} — in-memory fallback")

    try:
        from app.tasks.background import task_manager
        await task_manager.start()
    except Exception as e:
        logger.warning(f"Background tasks: {e}")

    # Pre-load ArcFace model so first biometric request doesn't timeout
    try:
        import asyncio
        from app.services.biometric import _load_face_model
        await asyncio.get_event_loop().run_in_executor(None, _load_face_model)
        logger.info("ArcFace model pre-loaded")
    except Exception as e:
        logger.warning(f"ArcFace pre-load: {e} — will lazy-load on first request")

    if settings.SENTRY_DSN:
        try:
            import sentry_sdk
            sentry_sdk.init(
                dsn=settings.SENTRY_DSN,
                traces_sample_rate=0.1,
                environment=settings.ENV,
                release=settings.VERSION,
            )
            logger.info("Sentry initialized")
        except ImportError:
            pass

    logger.info(f"Platform ready in {time.time() - _start_time:.2f}s")
    yield

    logger.info("Shutting down...")
    try:
        from app.tasks.background import task_manager
        await task_manager.stop()
    except Exception:
        pass


# Swagger/ReDoc only in non-production
_docs_url  = None if settings.is_production else "/docs"
_redoc_url = None if settings.is_production else "/redoc"

app = FastAPI(
    title="DocuFlow Platform API",
    version=settings.VERSION,
    docs_url=_docs_url,
    redoc_url=_redoc_url,
    lifespan=lifespan,
    description="Extraction · Biometric · Validation · Parallel Processing",
    contact={"name": "DocuFlow Support", "email": "support@docuflow.io"},
)

app.add_middleware(GZipMiddleware, minimum_size=1000)
app.add_middleware(SecurityHeadersMiddleware)   # CSP + docs guard
app.add_middleware(RequestIDMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID"],
    expose_headers=["X-Request-ID", "X-Response-Time", "X-RateLimit-Remaining"],
    max_age=600,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.ALLOWED_HOSTS)

if settings.METRICS_ENABLED:
    try:
        from prometheus_fastapi_instrumentator import Instrumentator
        Instrumentator(
            excluded_handlers=["/health", "/metrics"]
        ).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)
        logger.info("Prometheus metrics at /metrics")
    except ImportError:
        logger.warning("prometheus_fastapi_instrumentator not installed")

app.include_router(v2_router, prefix="/v2")


@app.get("/health", include_in_schema=False)
async def health():
    return {
        "status": "healthy",
        "version": settings.VERSION,
        "uptime_seconds": round(time.time() - _start_time),
    }


@app.get("/", include_in_schema=False)
async def root():
    return {
        "name": settings.APP_NAME,
        "version": settings.VERSION,
        "env": settings.ENV,
        "docs": _docs_url or "disabled in production",
    }


@app.exception_handler(404)
async def not_found(request: Request, exc):
    return JSONResponse(
        status_code=404,
        content={"error": "NOT_FOUND", "message": f"'{request.url.path}' not found", "docs": _docs_url or "n/a"},
    )


@app.exception_handler(500)
async def server_error(request: Request, exc):
    logger.error(f"Unhandled: {exc}", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "error": "INTERNAL_SERVER_ERROR",
            "message": "Unexpected error occurred",
            "request_id": getattr(request.state, "request_id", "unknown"),
        },
    )

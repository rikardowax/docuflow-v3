"""
DocuFlow v3.0 — API v2

Bloquant 1 résolu : _CLIENTS et _TEMPLATES remplacés par DB (PostgreSQL)
via ClientRepository et TemplateRepository. Zéro dict in-process.

Bloquant 2 (liveness) résolu dans biometric.py — MiniFASNet.

Partiels résolus :
  - /docs /redoc désactivés en production (SecurityHeadersMiddleware)
  - CSP header ajouté (SecurityHeadersMiddleware)
  - DocumentRepository : chaque traitement est persisté en base
  - AuditRepository    : chaque opération sensible est tracée
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
from fastapi import (
    APIRouter, BackgroundTasks, Depends, File, Form,
    HTTPException, Request, UploadFile, status,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_db
from app.core.db_repositories import (
    AuditRepository,
    ClientRepository,
    DocumentRepository,
    TemplateRepository,
)
from app.core.logging import get_logger
from app.core.redis_client import (
    cache_result, delete_result, get_avg_latency,
    get_cached_result, get_stat, increment_stat,
)
from app.core.security import (
    CurrentUser, Permission, create_access_token,
    generate_api_key, get_current_user, hash_password,
    require_permission, verify_password,
)
from app.models.schemas import (
    BatchRequest, BatchResponse, ProcessResponse,
    StatsResponse, TemplateCreate, TemplateResponse, TemplateUpdate, TokenRequest, TokenResponse,
)
from app.services.queue import orchestrator, queue_service
from app.services.storage import storage_service
from app.utils.helpers import validate_file_magic

# ── Imports for new OCR endpoint ──────────────────────────────────────
from app.services.ocr.gemini_ocr_service import GeminiOCRService

logger = get_logger(__name__)
router = APIRouter()


# ── Helpers ────────────────────────────────────────────────────────────

def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _user_agent(request: Request) -> str:
    return request.headers.get("User-Agent", "")[:256]


# ── Auth ───────────────────────────────────────────────────────────────

@router.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
async def get_token(
    req: TokenRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    Authenticate a client and return a JWT.
    Reads from PostgreSQL (clients table) — not from in-memory dict.
    """
    repo   = ClientRepository(db)
    client = await repo.get_by_client_id(req.client_id)

    if not client or not client.active:
        raise HTTPException(status_code=401, detail="Invalid client_id or inactive client")

    if not verify_password(req.client_secret, client.secret_hash):
        logger.warning(
            f"Failed auth for {req.client_id} from {_client_ip(request)}"
        )
        # Audit failed attempt
        await AuditRepository(db).log(
            action="AUTH_FAIL",
            client_id=req.client_id,
            ip_address=_client_ip(request),
            detail={"reason": "bad_secret"},
        )
        raise HTTPException(status_code=401, detail="Invalid credentials")

    # Update last_seen (fast UPDATE — no need for background task)
    try:
        await repo.touch_last_seen(req.client_id)
    except Exception:
        pass  # non-critical

    from app.core.security import ROLE_PERMISSIONS
    token = create_access_token({
        "sub":    req.client_id,
        "role":   client.role,
        "scopes": [p.value for p in ROLE_PERMISSIONS.get(client.role, [])],
    })

    await increment_stat("auth_success")
    return TokenResponse(
        access_token=token,
        expires_in=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES * 60,
    )


# ── Process ────────────────────────────────────────────────────────────

@router.post("/process", response_model=ProcessResponse, tags=["Processing"])
async def process_document(
    background_tasks: BackgroundTasks,
    request: Request,
    file: UploadFile            = File(...),
    verso: Optional[UploadFile] = File(None),
    selfie: Optional[UploadFile] = File(None),
    template_id: str            = Form("CNI_FR_v2"),
    modules: str                = Form("extraction,validation,fuzzy"),
    webhook_url: Optional[str]  = Form(None),
    priority: str               = Form("normal"),
    reference_data: Optional[str] = Form(None),
    selfie_source: str            = Form("upload"),
    user: CurrentUser           = Depends(require_permission(Permission.PROCESS_WRITE)),
    db: AsyncSession            = Depends(get_db),
):
    # ── 1. File validation ────────────────────────────────────────
    if file.content_type not in settings.ALLOWED_MIME_TYPES:
        raise HTTPException(400, f"Unsupported format: {file.content_type}")
    file_bytes = await file.read()
    if len(file_bytes) > settings.max_file_size_bytes:
        raise HTTPException(413, f"File too large (max {settings.MAX_FILE_SIZE_MB}MB)")
    valid, detected = validate_file_magic(file_bytes, file.content_type)
    if not valid:
        logger.warning(
            f"Magic bytes mismatch: declared={file.content_type} detected={detected} "
            f"client={user.client_id}"
        )
        raise HTTPException(
            400,
            f"File content ({detected}) does not match declared type ({file.content_type}). "
            "Polyglot files are not accepted.",
        )

    # ── 2. Template from DB ───────────────────────────────────────
    tmpl_repo = TemplateRepository(db)
    tmpl = await tmpl_repo.get(template_id)
    if not tmpl:
        raise HTTPException(404, f"Template '{template_id}' not found")

    # ── 3. Parse reference data ───────────────────────────────────
    ref_data = None
    if reference_data:
        try:
            ref_data = json.loads(reference_data)
        except json.JSONDecodeError:
            raise HTTPException(400, "Invalid reference_data JSON")

    fmt_map  = {"image/jpeg": "jpg", "image/png": "png",
                "application/pdf": "pdf", "image/tiff": "tiff", "image/webp": "webp"}
    fmt      = fmt_map.get(file.content_type, "jpg")
    trace_id = f"doc_{uuid.uuid4().hex[:16]}"
    mod_list = [m.strip() for m in modules.split(",")]

    # ── 4. Store binary ───────────────────────────────────────────
    storage_key = await storage_service.upload(file_bytes, fmt, trace_id)

    # ── 4b. Read verso if provided ────────────────────────────────
    verso_bytes = None
    if verso:
        if verso.content_type not in settings.ALLOWED_MIME_TYPES:
            raise HTTPException(400, f"Unsupported verso format: {verso.content_type}")
        verso_bytes = await verso.read()
        if len(verso_bytes) > settings.max_file_size_bytes:
            raise HTTPException(413, f"Verso file too large (max {settings.MAX_FILE_SIZE_MB}MB)")
        valid_v, detected_v = validate_file_magic(verso_bytes, verso.content_type)
        if not valid_v:
            raise HTTPException(400, f"Verso file content mismatch ({detected_v} vs {verso.content_type})")

    # ── 4c. Read selfie if provided (biometric) ───────────────────
    selfie_bytes = None
    if selfie:
        if selfie.content_type not in ("image/jpeg", "image/png", "image/webp"):
            raise HTTPException(400, f"Unsupported selfie format: {selfie.content_type}")
        selfie_bytes = await selfie.read()
        if len(selfie_bytes) > settings.max_file_size_bytes:
            raise HTTPException(413, f"Selfie file too large (max {settings.MAX_FILE_SIZE_MB}MB)")
        valid_s, detected_s = validate_file_magic(selfie_bytes, selfie.content_type)
        if not valid_s:
            raise HTTPException(400, f"Selfie file content mismatch ({detected_s} vs {selfie.content_type})")

    # ── 5. Persist document record ────────────────────────────────
    purge_at = datetime.now(timezone.utc) + timedelta(
        hours=settings.DOCUMENT_RETENTION_HOURS
    )
    doc_repo = DocumentRepository(db)
    await doc_repo.create(
        trace_id=trace_id,
        template_id=template_id,
        storage_key=storage_key,
        file_format=fmt,
        file_size_bytes=len(file_bytes),
        modules_used=mod_list,
        webhook_url=webhook_url,
        client_id_str=user.client_id,
        source_ip=_client_ip(request),
        user_agent=_user_agent(request),
        purge_at=purge_at,
    )

    # ── 6. Run pipeline ───────────────────────────────────────────
    start  = time.time()
    result = await orchestrator.process(file_bytes, fmt, tmpl, mod_list, selfie_bytes, ref_data, verso_bytes, selfie_source)
    elapsed = int((time.time() - start) * 1000)

    # ── 7. Persist result ─────────────────────────────────────────
    await doc_repo.complete(
        trace_id=trace_id,
        result=result,
        global_decision=result.get("global_decision"),
        processing_time_ms=elapsed,
    )

    # ── 8. Audit ──────────────────────────────────────────────────
    await AuditRepository(db).log(
        action="PROCESS",
        client_id=user.client_id,
        ip_address=_client_ip(request),
        detail={
            "trace_id":    trace_id,
            "template_id": template_id,
            "modules":     mod_list,
            "decision":    result.get("global_decision"),
            "elapsed_ms":  elapsed,
        },
        document_trace_id=trace_id,
    )

    # ── 9. Stats ──────────────────────────────────────────────────
    await increment_stat("processed_total")
    if result.get("global_decision") == "VALIDATED":
        await increment_stat("validated_total")

    # ── 10. Build response ────────────────────────────────────────
    ext  = result.get("extraction", {})
    resp = ProcessResponse(
        document_id=trace_id,
        trace_id=trace_id,
        status="completed",
        template_id=template_id,
        document_type=ext.get("document_type"),
        processing_time_ms=elapsed,
        overall_confidence=ext.get("overall_confidence"),
        global_decision=result.get("global_decision"),
        fields=ext.get("fields"),
        biometric_check=result.get("biometric"),
        validation=result.get("validation"),
        fuzzy_matching=result.get("fuzzy"),
        alerts=ext.get("alerts", []),
        mrz_decoded=ext.get("mrz_decoded"),
        created_at=datetime.now(timezone.utc),
    )

    result_dict = resp.model_dump(mode="json")
    await cache_result(trace_id, result_dict)

    if webhook_url:
        background_tasks.add_task(_deliver_webhook, webhook_url, result_dict, trace_id)
    background_tasks.add_task(_schedule_purge, trace_id, storage_key)

    return resp


@router.get("/results/{document_id}", response_model=ProcessResponse, tags=["Processing"])
async def get_result(
    document_id: str,
    user: CurrentUser = Depends(require_permission(Permission.PROCESS_READ)),
):
    cached = await get_cached_result(document_id)
    if cached:
        return cached
    raise HTTPException(404, "Document not found or result expired")


@router.delete("/results/{document_id}", status_code=204, tags=["Processing"])
async def delete_result_endpoint(
    document_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    user: CurrentUser    = Depends(require_permission(Permission.PROCESS_WRITE)),
    db: AsyncSession     = Depends(get_db),
):
    """GDPR: explicit deletion of a document result."""
    await delete_result(document_id)
    await increment_stat("deleted_total")
    await AuditRepository(db).log(
        action="DELETE",
        client_id=user.client_id,
        ip_address=_client_ip(request),
        detail={"document_id": document_id},
    )


# ── OCR Endpoints ──────────────────────────────────────────────────────

@router.post("/ocr/gemini", tags=["OCR"])
async def gemini_ocr_process_image(
    file: UploadFile = File(...),
    verso: Optional[UploadFile] = File(None),
    document_type: Optional[str] = Form(None),
    user: CurrentUser = Depends(require_permission(Permission.PROCESS_WRITE)),
):
    """
    Process uploaded image(s) using Google Gemini for structured document extraction.
    Supports optional verso (back side) for dual-side analysis.
    Returns both raw text and structured fields (name, date, ID number, etc.).
    """
    if file.content_type not in ["image/jpeg", "image/png", "image/tiff", "image/webp"]:
        raise HTTPException(
            status_code=400,
            detail="Unsupported file format. Only JPEG, PNG, TIFF, WEBP images are allowed.",
        )

    file_bytes = await file.read()
    if len(file_bytes) > settings.max_file_size_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large (max {settings.MAX_FILE_SIZE_MB}MB)",
        )

    verso_bytes = None
    if verso:
        if verso.content_type not in ["image/jpeg", "image/png", "image/tiff", "image/webp"]:
            raise HTTPException(400, "Unsupported verso format.")
        verso_bytes = await verso.read()
        if len(verso_bytes) > settings.max_file_size_bytes:
            raise HTTPException(413, f"Verso file too large (max {settings.MAX_FILE_SIZE_MB}MB)")

    try:
        ocr_service = GeminiOCRService.get_instance()
        if verso_bytes:
            result = await ocr_service.extract_recto_verso(file_bytes, verso_bytes, document_type)
        else:
            result = await ocr_service.extract_structured(file_bytes, document_type)
        return {
            "raw_text": result.get("raw_text", ""),
            "fields": result.get("fields", {}),
            "engine": "gemini",
            "dual_side": result.get("dual_side", False),
        }
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    except Exception as e:
        logger.error(f"Error in Gemini OCR endpoint: {e}")
        raise HTTPException(
            status_code=500, detail="Error processing image with Gemini OCR"
        )


# ── Batch ──────────────────────────────────────────────────────────────

@router.post("/process/batch", tags=["Batch"])
async def process_batch(
    request: Request,
    files: list[UploadFile]     = File(...),
    template_id: str            = Form("CNI_FR_v2"),
    modules: str                = Form("extraction,validation,fuzzy"),
    user: CurrentUser           = Depends(require_permission(Permission.BATCH_WRITE)),
    db: AsyncSession            = Depends(get_db),
):
    """Accept multiple file uploads, process each through the pipeline, track progress."""
    if not files:
        raise HTTPException(400, "No files provided")

    batch_id = f"batch_{uuid.uuid4().hex[:12]}"
    mod_list = [m.strip() for m in modules.split(",")]

    # Persist batch job
    from app.core.db_repositories import BatchRepository
    batch_repo = BatchRepository(db)
    await batch_repo.create(
        batch_id=batch_id,
        total_documents=len(files),
        modules=mod_list,
        client_id=user.client_id,
    )

    # Resolve template
    tmpl_repo = TemplateRepository(db)
    tmpl = await tmpl_repo.get(template_id)
    if not tmpl:
        raise HTTPException(404, f"Template '{template_id}' not found")

    fmt_map = {"image/jpeg": "jpg", "image/png": "png",
               "application/pdf": "pdf", "image/tiff": "tiff", "image/webp": "webp"}

    # Process each file sequentially (avoids overwhelming Gemini rate limits)
    results = []
    for f in files:
        doc_result = {"filename": f.filename, "status": "failed", "fields": None, "error": None}
        try:
            if f.content_type not in settings.ALLOWED_MIME_TYPES:
                doc_result["error"] = f"Unsupported format: {f.content_type}"
                await batch_repo.increment(batch_id, success=False)
                results.append(doc_result)
                continue

            file_bytes = await f.read()
            if len(file_bytes) > settings.max_file_size_bytes:
                doc_result["error"] = "File too large"
                await batch_repo.increment(batch_id, success=False)
                results.append(doc_result)
                continue

            fmt = fmt_map.get(f.content_type, "jpg")
            trace_id = f"doc_{uuid.uuid4().hex[:16]}"

            pipeline_result = await orchestrator.process(
                file_bytes, fmt, tmpl, mod_list
            )

            ext = pipeline_result.get("extraction", {})
            doc_result["status"] = "completed"
            doc_result["trace_id"] = trace_id
            doc_result["fields"] = ext.get("fields")
            doc_result["overall_confidence"] = ext.get("overall_confidence")
            doc_result["global_decision"] = pipeline_result.get("global_decision")
            doc_result["processing_time_ms"] = pipeline_result.get("total_ms")
            doc_result["validation"] = pipeline_result.get("validation")
            await batch_repo.increment(batch_id, success=True)

        except Exception as e:
            logger.error(f"Batch doc {f.filename}: {e}")
            doc_result["error"] = str(e)
            await batch_repo.increment(batch_id, success=False)

        results.append(doc_result)

    return {
        "batch_id": batch_id,
        "total_documents": len(files),
        "processed": sum(1 for r in results if r["status"] == "completed"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "status": "completed",
        "results": results,
    }


@router.get("/batch/{batch_id}/status", tags=["Batch"])
async def batch_status(
    batch_id: str,
    user: CurrentUser = Depends(require_permission(Permission.PROCESS_READ)),
    db: AsyncSession  = Depends(get_db),
):
    from app.core.db_repositories import BatchRepository
    job = await BatchRepository(db).get(batch_id)
    if not job:
        q = queue_service.get_stats()
        return {
            "batch_id": batch_id,
            "status": "unknown",
            "queue_stats": q,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    return {
        "batch_id": job["batch_id"],
        "status": job["status"],
        "total_documents": job["total_documents"],
        "processed_documents": job["processed_documents"],
        "failed_documents": job["failed_documents"],
        "progress_pct": round((job["processed_documents"] + job["failed_documents"]) / max(1, job["total_documents"]) * 100, 1),
        "created_at": job["created_at"],
        "completed_at": job["completed_at"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/batches", tags=["Batch"])
async def list_batches(
    user: CurrentUser = Depends(require_permission(Permission.PROCESS_READ)),
    db: AsyncSession  = Depends(get_db),
):
    from app.core.db_repositories import BatchRepository
    return await BatchRepository(db).list_all()


# ── Templates ──────────────────────────────────────────────────────────

@router.get("/templates", response_model=list[TemplateResponse], tags=["Templates"])
async def list_templates(
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_READ)),
    db: AsyncSession  = Depends(get_db),
):
    templates = await TemplateRepository(db).list_active()
    return [
        TemplateResponse(
            id=t["id"], name=t["name"], document_type=t["document_type"],
            country=t.get("country"), version=t["version"], active=t["active"],
            fields_count=len(t["fields"]),
            created_at=datetime.fromisoformat(t["created_at"]) if t.get("created_at") else datetime.now(timezone.utc),
        )
        for t in templates
    ]


@router.get("/templates/{tid}", tags=["Templates"])
async def get_template(
    tid: str,
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_READ)),
    db: AsyncSession  = Depends(get_db),
):
    tmpl = await TemplateRepository(db).get(tid)
    if not tmpl:
        raise HTTPException(404, f"Template '{tid}' not found")
    return tmpl


@router.post("/templates", status_code=201, tags=["Templates"])
async def create_template(
    req: TemplateCreate,
    request: Request,
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_WRITE)),
    db: AsyncSession  = Depends(get_db),
):
    repo = TemplateRepository(db)
    if await repo.exists(req.id):
        raise HTTPException(409, f"Template '{req.id}' already exists")

    t = await repo.create(
        template_id=req.id,
        name=req.name,
        document_type=req.document_type,
        fields_config=[f.model_dump() for f in req.fields],
        country=req.country,
        created_by=user.client_id,
    )
    await AuditRepository(db).log(
        action="TEMPLATE_CREATE",
        client_id=user.client_id,
        ip_address=_client_ip(request),
        detail={"template_id": req.id, "name": req.name},
    )
    return TemplateResponse(
        id=t.id, name=t.name, document_type=t.document_type,
        country=t.country, version=t.version, active=t.active,
        fields_count=len(t.fields_config),
        created_at=t.created_at,
    )


@router.delete("/templates/{tid}", status_code=204, tags=["Templates"])
async def delete_template(
    tid: str,
    request: Request,
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_WRITE)),
    db: AsyncSession  = Depends(get_db),
):
    repo = TemplateRepository(db)
    deleted = await repo.soft_delete(tid)
    if not deleted:
        raise HTTPException(404, "Template not found")
    await AuditRepository(db).log(
        action="TEMPLATE_DELETE",
        client_id=user.client_id,
        ip_address=_client_ip(request),
        detail={"template_id": tid},
    )


@router.put("/templates/{tid}", tags=["Templates"])
async def update_template(
    tid: str,
    req: TemplateUpdate,
    request: Request,
    user: CurrentUser = Depends(require_permission(Permission.TEMPLATE_WRITE)),
    db: AsyncSession  = Depends(get_db),
):
    repo = TemplateRepository(db)
    kwargs = {}
    if req.name is not None:
        kwargs["name"] = req.name
    if req.document_type is not None:
        kwargs["document_type"] = req.document_type
    if req.fields is not None:
        kwargs["fields_config"] = [f.model_dump() for f in req.fields]
    if req.country is not None:
        kwargs["country"] = req.country
    if req.active is not None:
        kwargs["active"] = req.active

    t = await repo.update(tid, **kwargs)
    if not t:
        raise HTTPException(404, f"Template '{tid}' not found")

    await AuditRepository(db).log(
        action="TEMPLATE_UPDATE",
        client_id=user.client_id,
        ip_address=_client_ip(request),
        detail={"template_id": tid, "updated_fields": list(kwargs.keys())},
    )
    return TemplateResponse(
        id=t.id, name=t.name, document_type=t.document_type,
        country=t.country, version=t.version, active=t.active,
        fields_count=len(t.fields_config),
        created_at=t.created_at,
    )


# ── Stats & Health ─────────────────────────────────────────────────────

@router.get("/stats", response_model=StatsResponse, tags=["Monitoring"])
async def get_stats(
    user: CurrentUser = Depends(require_permission(Permission.STATS_READ)),
):
    total     = await get_stat("processed_total")
    validated = await get_stat("validated_total")
    avg_ms    = await get_avg_latency()
    q         = queue_service.get_stats()
    return StatsResponse(
        total_documents=total,
        today_documents=total,
        success_rate=round(validated / total, 3) if total else 0.0,
        avg_processing_time_ms=round(avg_ms, 1),
        active_workers=q["active_workers"],
        queue_depth=q["queue_depth"],
        documents_by_status={"completed": validated, "review": 0, "failed": max(0, total - validated)},
        documents_by_type={},
    )


@router.get("/health", tags=["Monitoring"])
async def health_detailed():
    from app.core.database import check_db_health
    from app.core.redis_client import check_redis_health
    db_ok      = await check_db_health()
    redis_ok   = await check_redis_health()
    storage_ok = await storage_service.health_check()
    q          = queue_service.get_stats()
    all_ok     = db_ok and redis_ok
    return {
        "status":    "healthy" if all_ok else "degraded",
        "version":   settings.VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "components": {
            "api":              "healthy",
            "database":         "healthy" if db_ok    else "unhealthy",
            "redis":            "healthy" if redis_ok else "unhealthy",
            "storage":          "healthy" if storage_ok else "degraded",
            "queue":            "healthy",
            "ocr_engine":       "healthy",
            "biometric_engine": "healthy",
        },
        "queue": q,
    }


# ── Background helpers ─────────────────────────────────────────────────

async def _deliver_webhook(url: str, payload: dict, trace_id: str) -> None:
    import asyncio
    from app.core.security import sign_webhook_payload
    body    = json.dumps(payload, default=str)
    sig     = sign_webhook_payload(body)
    headers = {
        "Content-Type":         "application/json",
        "X-DocuFlow-Event":     "document.processed",
        "X-DocuFlow-Signature": f"sha256={sig}",
        "X-DocuFlow-TraceID":   trace_id,
    }
    for attempt in range(1, settings.WEBHOOK_MAX_RETRIES + 1):
        try:
            async with httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT) as client:
                r = await client.post(url, content=body, headers=headers)
                if r.status_code < 400:
                    logger.info(f"Webhook delivered to {url} (attempt {attempt})")
                    return
                logger.warning(f"Webhook {url} → {r.status_code} (attempt {attempt})")
        except Exception as e:
            logger.error(f"Webhook error attempt {attempt}: {e}")
        if attempt < settings.WEBHOOK_MAX_RETRIES:
            await asyncio.sleep(settings.QUEUE_RETRY_DELAY * (2 ** (attempt - 1)))
    logger.error(f"Webhook failed after {settings.WEBHOOK_MAX_RETRIES} attempts: {url}")


async def _schedule_purge(trace_id: str, storage_key: str) -> None:
    import asyncio
    await asyncio.sleep(settings.DOCUMENT_RETENTION_HOURS * 3600)
    await storage_service.delete(storage_key)
    logger.info(f"GDPR purge completed for: {trace_id}")

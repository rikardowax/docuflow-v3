"""
DocuFlow v3.0 - Database repositories (replaces in-memory dicts in v2.py)

Provides async CRUD for:
  - ClientRepository   : authenticate, get, create
  - TemplateRepository : get, list active, create, soft-delete
  - DocumentRepository : create, update status, get by trace_id

All operations go through SQLAlchemy AsyncSession obtained via get_db().
Redis caching is applied on hot-path reads (template lookup, client auth).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.logging import get_logger
from app.models.models import (
    Client, Template, DocumentRecord, BatchJob,
    AuditLog, ProcessingStatus, GlobalDecision,
)

logger = get_logger(__name__)


def _sanitize_for_json(obj: Any) -> Any:
    """Recursively convert numpy types to native Python types for JSON serialization."""
    import numpy as np
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj


# ── Redis cache helpers (optional — degrade gracefully if Redis is down) ─
_TEMPLATE_CACHE_TTL = 300   # 5 min
_CLIENT_CACHE_TTL   = 60    # 1 min


async def _redis_get(key: str) -> Optional[dict]:
    try:
        from app.core.redis_client import get_redis
        r = await get_redis()
        if r is None:
            return None
        raw = await r.get(key)
        return json.loads(raw) if raw else None
    except Exception:
        return None


async def _redis_set(key: str, value: dict, ttl: int) -> None:
    try:
        from app.core.redis_client import get_redis
        r = await get_redis()
        if r is not None:
            await r.setex(key, ttl, json.dumps(value, default=str))
    except Exception:
        pass


async def _redis_del(key: str) -> None:
    try:
        from app.core.redis_client import get_redis
        r = await get_redis()
        if r is not None:
            await r.delete(key)
    except Exception:
        pass


# ── Client Repository ─────────────────────────────────────────────────
class ClientRepository:
    """
    All auth reads go DB-first with a short Redis cache.
    The cache key is 'client:<client_id>' → JSON dict.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    async def get_by_client_id(self, client_id: str) -> Optional[Client]:
        cache_key = f"client:{client_id}"
        cached = await _redis_get(cache_key)
        if cached:
            # Re-hydrate a minimal object from cache (no ORM tracking needed for auth)
            obj = Client()
            for k, v in cached.items():
                setattr(obj, k, v)
            return obj

        result = await self.session.execute(
            select(Client).where(Client.client_id == client_id)
        )
        client = result.scalar_one_or_none()
        if client:
            await _redis_set(cache_key, {
                "client_id":   client.client_id,
                "secret_hash": client.secret_hash,
                "role":        client.role,
                "active":      client.active,
                "rate_limit":  client.rate_limit,
            }, _CLIENT_CACHE_TTL)
        return client

    async def touch_last_seen(self, client_id: str) -> None:
        await self.session.execute(
            update(Client)
            .where(Client.client_id == client_id)
            .values(last_seen_at=datetime.now(timezone.utc))
        )
        await self.session.commit()

    async def create(
        self,
        client_id: str,
        name: str,
        secret_hash: str,
        role: str = "api_client",
    ) -> Client:
        client = Client(
            id=uuid4(),
            client_id=client_id,
            name=name,
            secret_hash=secret_hash,
            role=role,
            active=True,
        )
        self.session.add(client)
        await self.session.commit()
        await self.session.refresh(client)
        return client


# ── Template Repository ───────────────────────────────────────────────
class TemplateRepository:
    """
    Templates are read-heavy (every /process call fetches one).
    We cache each template JSON in Redis under 'template:<id>'.
    Write operations (create / soft-delete) invalidate the cache.
    """

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _orm_to_dict(t: Template) -> dict:
        return {
            "id":            t.id,
            "name":          t.name,
            "document_type": t.document_type,
            "country":       t.country,
            "version":       t.version,
            "active":        t.active,
            "fields":        t.fields_config,
            "created_by":    t.created_by,
            "created_at":    t.created_at.isoformat() if t.created_at else None,
        }

    async def get(self, template_id: str) -> Optional[dict]:
        cache_key = f"template:{template_id}"
        cached = await _redis_get(cache_key)
        if cached:
            return cached

        result = await self.session.execute(
            select(Template).where(
                Template.id == template_id,
                Template.active == True,  # noqa: E712
            )
        )
        t = result.scalar_one_or_none()
        if not t:
            return None
        d = self._orm_to_dict(t)
        await _redis_set(cache_key, d, _TEMPLATE_CACHE_TTL)
        return d

    async def list_active(self) -> list[dict]:
        result = await self.session.execute(
            select(Template).where(Template.active == True)  # noqa: E712
        )
        return [self._orm_to_dict(t) for t in result.scalars().all()]

    async def create(
        self,
        template_id: str,
        name: str,
        document_type: str,
        fields_config: list,
        country: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> Template:
        t = Template(
            id=template_id,
            name=name,
            document_type=document_type,
            country=country,
            version="1",
            fields_config=fields_config,
            active=True,
            created_by=created_by,
        )
        self.session.add(t)
        await self.session.commit()
        await self.session.refresh(t)
        await _redis_set(f"template:{template_id}", self._orm_to_dict(t), _TEMPLATE_CACHE_TTL)
        return t

    async def soft_delete(self, template_id: str) -> bool:
        result = await self.session.execute(
            select(Template).where(Template.id == template_id)
        )
        t = result.scalar_one_or_none()
        if not t:
            return False
        t.active = False
        await self.session.commit()
        await _redis_del(f"template:{template_id}")
        return True

    async def exists(self, template_id: str) -> bool:
        result = await self.session.execute(
            select(Template.id).where(Template.id == template_id)
        )
        return result.scalar_one_or_none() is not None

    async def update(
        self,
        template_id: str,
        name: Optional[str] = None,
        document_type: Optional[str] = None,
        fields_config: Optional[list] = None,
        country: Optional[str] = ...,
        active: Optional[bool] = None,
    ) -> Optional[Template]:
        result = await self.session.execute(
            select(Template).where(Template.id == template_id)
        )
        t = result.scalar_one_or_none()
        if not t:
            return None
        if name is not None:
            t.name = name
        if document_type is not None:
            t.document_type = document_type
        if fields_config is not None:
            t.fields_config = fields_config
        if country is not ...:
            t.country = country
        if active is not None:
            t.active = active
        await self.session.commit()
        await self.session.refresh(t)
        await _redis_del(f"template:{template_id}")
        return t


# ── Document Repository ───────────────────────────────────────────────
class DocumentRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        trace_id: str,
        template_id: str,
        storage_key: str,
        file_format: str,
        file_size_bytes: int,
        modules_used: list[str],
        webhook_url: Optional[str],
        client_id_str: Optional[str],
        source_ip: Optional[str],
        user_agent: Optional[str],
        purge_at: datetime,
    ) -> DocumentRecord:
        # Resolve client FK from client_id string
        client_fk = None
        if client_id_str:
            result = await self.session.execute(
                select(Client.id).where(Client.client_id == client_id_str)
            )
            client_fk = result.scalar_one_or_none()

        doc = DocumentRecord(
            id=uuid4(),
            trace_id=trace_id,
            client_fk=client_fk,
            template_id=template_id,
            storage_key=storage_key,
            file_format=file_format,
            file_size_bytes=file_size_bytes,
            modules_used=modules_used,
            webhook_url=webhook_url,
            status=ProcessingStatus.PROCESSING.value,
            source_ip=source_ip,
            user_agent=user_agent,
            purge_at=purge_at,
        )
        self.session.add(doc)
        await self.session.commit()
        await self.session.refresh(doc)
        return doc

    async def complete(
        self,
        trace_id: str,
        result: dict,
        global_decision: str,
        processing_time_ms: int,
    ) -> None:
        result = _sanitize_for_json(result)
        ext = result.get("extraction", {})
        await self.session.execute(
            update(DocumentRecord)
            .where(DocumentRecord.trace_id == trace_id)
            .values(
                status=ProcessingStatus.COMPLETED.value,
                global_decision=global_decision if global_decision else None,
                extracted_fields=ext.get("fields"),
                overall_confidence=ext.get("overall_confidence"),
                alerts=ext.get("alerts", []),
                biometric_result=result.get("biometric"),
                validation_result=result.get("validation"),
                fuzzy_result=result.get("fuzzy"),
                mrz_decoded=ext.get("mrz_decoded"),
                document_type=ext.get("document_type"),
                processing_time_ms=processing_time_ms,
                processed_at=datetime.now(timezone.utc),
            )
        )
        await self.session.commit()

    async def mark_failed(self, trace_id: str, error: str) -> None:
        await self.session.execute(
            update(DocumentRecord)
            .where(DocumentRecord.trace_id == trace_id)
            .values(
                status=ProcessingStatus.FAILED.value,
                error_message=error[:2048],
                processed_at=datetime.now(timezone.utc),
            )
        )
        await self.session.commit()


# ── Audit Repository ──────────────────────────────────────────────────
class AuditRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def log(
        self,
        action: str,
        client_id: Optional[str],
        ip_address: Optional[str],
        detail: dict,
        document_trace_id: Optional[str] = None,
    ) -> None:
        document_id = None
        if document_trace_id:
            result = await self.session.execute(
                select(DocumentRecord.id).where(
                    DocumentRecord.trace_id == document_trace_id
                )
            )
            document_id = result.scalar_one_or_none()

        log = AuditLog(
            id=uuid4(),
            document_id=document_id,
            client_id=client_id,
            action=action,
            detail=detail,
            ip_address=ip_address,
        )
        self.session.add(log)
        await self.session.commit()


# ── Batch Repository ──────────────────────────────────────────────────
class BatchRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create(
        self,
        batch_id: str,
        total_documents: int,
        modules: list,
        webhook_url: Optional[str] = None,
        client_id: Optional[str] = None,
    ) -> BatchJob:
        # Resolve client FK
        client_fk = None
        if client_id:
            result = await self.session.execute(
                select(Client.id).where(Client.client_id == client_id)
            )
            client_fk = result.scalar_one_or_none()
        job = BatchJob(
            batch_id=batch_id,
            client_fk=client_fk,
            total_documents=total_documents,
            processed_documents=0,
            failed_documents=0,
            status="pending",
            webhook_url=webhook_url,
            modules=modules,
        )
        self.session.add(job)
        await self.session.commit()
        await self.session.refresh(job)
        return job

    async def get(self, batch_id: str) -> Optional[dict]:
        result = await self.session.execute(
            select(BatchJob).where(BatchJob.batch_id == batch_id)
        )
        job = result.scalar_one_or_none()
        if not job:
            return None
        return {
            "batch_id": job.batch_id,
            "total_documents": job.total_documents,
            "processed_documents": job.processed_documents,
            "failed_documents": job.failed_documents,
            "status": job.status,
            "modules": job.modules,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }

    async def list_all(self, limit: int = 50) -> list[dict]:
        result = await self.session.execute(
            select(BatchJob).order_by(BatchJob.created_at.desc()).limit(limit)
        )
        jobs = result.scalars().all()
        return [
            {
                "batch_id": j.batch_id,
                "total_documents": j.total_documents,
                "processed_documents": j.processed_documents,
                "failed_documents": j.failed_documents,
                "status": j.status,
                "created_at": j.created_at.isoformat() if j.created_at else None,
                "completed_at": j.completed_at.isoformat() if j.completed_at else None,
            }
            for j in jobs
        ]

    async def increment(self, batch_id: str, success: bool = True) -> None:
        result = await self.session.execute(
            select(BatchJob).where(BatchJob.batch_id == batch_id)
        )
        job = result.scalar_one_or_none()
        if not job:
            return
        if success:
            job.processed_documents += 1
        else:
            job.failed_documents += 1
        total_done = job.processed_documents + job.failed_documents
        if total_done >= job.total_documents:
            job.status = "completed"
            from datetime import datetime, timezone
            job.completed_at = datetime.now(timezone.utc)
        else:
            job.status = "processing"
        await self.session.commit()


# ── DB seed: built-in templates ───────────────────────────────────────
# Called once at startup (main.py lifespan). Idempotent: skips if already present.

_BUILTIN_TEMPLATES: list[dict] = [
    {
        "id": "CNI_FR_v2", "name": "CNI France v2",
        "document_type": "identity_card", "country": "FR",
        "fields": [
            {"id": "last_name",   "label": "Nom",               "type": "string", "zone": {"x": 0.05, "y": 0.25, "w": 0.45, "h": 0.12}, "validation": {"required": True, "min_length": 2},               "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90},
            {"id": "first_name",  "label": "Prénom",            "type": "string", "zone": {"x": 0.05, "y": 0.33, "w": 0.45, "h": 0.12}, "validation": {"required": True},                                "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90},
            {"id": "birth_date",  "label": "Date de naissance", "type": "date",   "zone": {"x": 0.05, "y": 0.44, "w": 0.35, "h": 0.10}, "validation": {"required": True, "min_age": 0, "not_future": True}, "ocr_tolerance": 0.95, "fuzzy_threshold": 1.00},
            {"id": "id_number",   "label": "Numéro CNI",        "type": "string", "zone": {"x": 0.05, "y": 0.54, "w": 0.40, "h": 0.10}, "validation": {"required": True, "min_length": 12, "max_length": 15}, "ocr_tolerance": 0.95, "fuzzy_threshold": 0.95},
            {"id": "expiry_date", "label": "Date d'expiration", "type": "date",   "zone": {"x": 0.05, "y": 0.64, "w": 0.35, "h": 0.10}, "validation": {"required": True, "not_past": True},              "ocr_tolerance": 0.95, "fuzzy_threshold": 0.95},
            {"id": "nationality", "label": "Nationalité",       "type": "string", "zone": {"x": 0.05, "y": 0.72, "w": 0.30, "h": 0.08}, "validation": {},                                               "ocr_tolerance": 0.85, "fuzzy_threshold": 0.85},
            # ── Verso fields (back side) ──
            {"id": "father_name",          "label": "Nom du père",              "type": "string", "validation": {},               "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90, "side": "verso"},
            {"id": "mother_name",          "label": "Nom de la mère",           "type": "string", "validation": {},               "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90, "side": "verso"},
            {"id": "address",              "label": "Adresse",                  "type": "string", "validation": {},               "ocr_tolerance": 0.80, "fuzzy_threshold": 0.85, "side": "verso"},
            {"id": "registration_place",   "label": "Lieu d'enregistrement",    "type": "string", "validation": {},               "ocr_tolerance": 0.80, "fuzzy_threshold": 0.85, "side": "verso"},
            {"id": "birth_place",          "label": "Lieu de naissance",         "type": "string", "validation": {},               "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90, "side": "verso"},
            {"id": "gender",               "label": "Sexe",                      "type": "string", "validation": {},               "ocr_tolerance": 0.95, "fuzzy_threshold": 1.00, "side": "verso"},
            {"id": "height",               "label": "Taille",                    "type": "string", "validation": {},               "ocr_tolerance": 0.90, "fuzzy_threshold": 0.95, "side": "verso"},
            {"id": "issue_date",           "label": "Date de délivrance",        "type": "date",   "validation": {"not_future": True},  "ocr_tolerance": 0.95, "fuzzy_threshold": 1.00, "side": "verso"},
            {"id": "issuing_authority",    "label": "Autorité de délivrance",    "type": "string", "validation": {},               "ocr_tolerance": 0.80, "fuzzy_threshold": 0.85, "side": "verso"},
        ],
    },
    {
        "id": "PASSPORT_INT_v1", "name": "Passeport International v1",
        "document_type": "passport", "country": None,
        "fields": [
            {"id": "last_name",       "label": "Nom",               "type": "string", "validation": {"required": True},               "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90},
            {"id": "first_name",      "label": "Prénom",            "type": "string", "validation": {"required": True},               "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90},
            {"id": "birth_date",      "label": "Date de naissance", "type": "date",   "validation": {"required": True, "not_future": True}, "ocr_tolerance": 0.95, "fuzzy_threshold": 1.00},
            {"id": "passport_number", "label": "Numéro passeport",  "type": "string", "validation": {"required": True},               "ocr_tolerance": 0.95, "fuzzy_threshold": 0.95},
            {"id": "nationality",     "label": "Nationalité",       "type": "string", "validation": {},                               "ocr_tolerance": 0.85, "fuzzy_threshold": 0.85},
            {"id": "expiry_date",     "label": "Date d'expiration", "type": "date",   "validation": {"required": True, "not_past": True}, "ocr_tolerance": 0.95, "fuzzy_threshold": 0.95},
        ],
    },
]


async def seed_builtin_templates() -> None:
    """Insert or update built-in templates. Idempotent."""
    async with AsyncSessionLocal() as session:
        repo = TemplateRepository(session)
        for tmpl in _BUILTIN_TEMPLATES:
            if not await repo.exists(tmpl["id"]):
                await repo.create(
                    template_id=tmpl["id"],
                    name=tmpl["name"],
                    document_type=tmpl["document_type"],
                    fields_config=tmpl["fields"],
                    country=tmpl.get("country"),
                    created_by="system",
                )
                logger.info(f"Seeded built-in template: {tmpl['id']}")
            else:
                # Update fields_config if template already exists
                result = await session.execute(
                    select(Template).where(Template.id == tmpl["id"])
                )
                t = result.scalar_one_or_none()
                if t and len(t.fields_config or []) != len(tmpl["fields"]):
                    t.fields_config = tmpl["fields"]
                    await session.commit()
                    await _redis_del(f"template:{tmpl['id']}")
                    logger.info(f"Updated built-in template fields: {tmpl['id']}")

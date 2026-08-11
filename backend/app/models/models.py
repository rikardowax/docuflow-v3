"""DocuFlow - Database Models: Documents, Templates, Clients, Audit"""
from sqlalchemy import (Column, String, Float, Boolean, DateTime, JSON,
                         Integer, Enum, Text, ForeignKey, Index, BigInteger,
                         TypeDecorator, CHAR)
from sqlalchemy.dialects.postgresql import ENUM as PgEnum
from sqlalchemy.orm import relationship
from app.core.database import Base
from datetime import datetime, timezone
import uuid, enum


# ── Cross-DB UUID type (works with both PostgreSQL and SQLite) ────────
class UUIDType(TypeDecorator):
    """Platform-independent UUID type. Uses CHAR(36) on SQLite, native UUID on PG."""
    impl = CHAR(36)
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import UUID
            return dialect.type_descriptor(UUID())
        return dialect.type_descriptor(CHAR(36))

    def process_bind_param(self, value, dialect):
        if value is not None:
            if dialect.name == "postgresql":
                return uuid.UUID(str(value)) if not isinstance(value, uuid.UUID) else value
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is not None:
            if not isinstance(value, uuid.UUID):
                return uuid.UUID(value)
        return value


class ProcessingStatus(str, enum.Enum):
    PENDING    = "pending"
    PROCESSING = "processing"
    COMPLETED  = "completed"
    FAILED     = "failed"
    REVIEW     = "review"
    EXPIRED    = "expired"


class GlobalDecision(str, enum.Enum):
    VALIDATED = "VALIDATED"
    REVIEW    = "REVIEW"
    REJECTED  = "REJECTED"


def utcnow():
    return datetime.now(timezone.utc)


class Client(Base):
    """API Client / Tenant"""
    __tablename__ = "clients"
    id            = Column(UUIDType(), primary_key=True, default=uuid.uuid4)
    name          = Column(String(128), nullable=False)
    client_id     = Column(String(64), unique=True, nullable=False, index=True)
    secret_hash   = Column(String(256), nullable=False)
    role          = Column(String(32), default="api_client")
    active        = Column(Boolean, default=True)
    rate_limit    = Column(Integer, default=60)
    allowed_ips   = Column(JSON, default=list)
    metadata_     = Column("metadata", JSON, default=dict)
    created_at    = Column(DateTime(timezone=True), default=utcnow)
    last_seen_at  = Column(DateTime(timezone=True), nullable=True)
    api_keys      = relationship("ApiKey", back_populates="client", cascade="all, delete")
    documents     = relationship("DocumentRecord", back_populates="client")


class ApiKey(Base):
    __tablename__ = "api_keys"
    id            = Column(UUIDType(), primary_key=True, default=uuid.uuid4)
    client_id_fk  = Column(UUIDType(), ForeignKey("clients.id"), nullable=False)
    name          = Column(String(128), nullable=False)
    key_hash      = Column(String(256), unique=True, nullable=False)
    scopes        = Column(JSON, default=list)
    active        = Column(Boolean, default=True)
    expires_at    = Column(DateTime(timezone=True), nullable=True)
    created_at    = Column(DateTime(timezone=True), default=utcnow)
    last_used_at  = Column(DateTime(timezone=True), nullable=True)
    client        = relationship("Client", back_populates="api_keys")


class Template(Base):
    __tablename__ = "templates"
    id            = Column(String(64), primary_key=True)
    name          = Column(String(128), nullable=False)
    document_type = Column(String(64))
    country       = Column(String(8), nullable=True)
    version       = Column(String(16), default="1")
    fields_config = Column(JSON, nullable=False)
    active        = Column(Boolean, default=True)
    created_by    = Column(String(128), nullable=True)
    created_at    = Column(DateTime(timezone=True), default=utcnow)
    updated_at    = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DocumentRecord(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_trace_id",  "trace_id"),
        Index("ix_documents_status",    "status"),
        Index("ix_documents_created",   "created_at"),
        Index("ix_documents_client",    "client_fk"),
    )
    id              = Column(UUIDType(), primary_key=True, default=uuid.uuid4)
    trace_id        = Column(String(64), unique=True, nullable=False)
    client_fk       = Column(UUIDType(), ForeignKey("clients.id"), nullable=True)
    template_id     = Column(String(64), nullable=True)
    document_type   = Column(String(64), nullable=True)
    status          = Column(PgEnum("pending","processing","completed","failed","review","expired", name="processingstatus", create_type=False), default=ProcessingStatus.PENDING.value, index=True)
    priority        = Column(String(16), default="normal")
    modules_used    = Column(JSON, default=list)

    # Storage
    storage_key     = Column(String(256), nullable=True)
    file_format     = Column(String(16), nullable=True)
    file_size_bytes = Column(BigInteger, nullable=True)

    # Results
    extracted_fields    = Column(JSON, nullable=True)
    overall_confidence  = Column(Float, nullable=True)
    processing_time_ms  = Column(Integer, nullable=True)
    alerts              = Column(JSON, default=list)
    biometric_result    = Column(JSON, nullable=True)
    validation_result   = Column(JSON, nullable=True)
    fuzzy_result        = Column(JSON, nullable=True)
    mrz_decoded         = Column(JSON, nullable=True)
    global_decision     = Column(PgEnum("VALIDATED","REVIEW","REJECTED", name="globaldecision", create_type=False), nullable=True)
    error_message       = Column(Text, nullable=True)
    retry_count         = Column(Integer, default=0)

    # Webhook
    webhook_url         = Column(String(512), nullable=True)
    webhook_delivered   = Column(Boolean, default=False)
    webhook_attempts    = Column(Integer, default=0)

    # Audit / GDPR
    created_at          = Column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at          = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    processed_at        = Column(DateTime(timezone=True), nullable=True)
    purge_at            = Column(DateTime(timezone=True), nullable=True, index=True)
    created_by          = Column(String(128), nullable=True)
    source_ip           = Column(String(64), nullable=True)
    user_agent          = Column(String(256), nullable=True)

    client = relationship("Client", back_populates="documents")
    audit_logs = relationship("AuditLog", back_populates="document", cascade="all, delete")


class BatchJob(Base):
    __tablename__ = "batch_jobs"
    id                  = Column(UUIDType(), primary_key=True, default=uuid.uuid4)
    batch_id            = Column(String(128), unique=True, nullable=False, index=True)
    client_fk           = Column(UUIDType(), ForeignKey("clients.id"), nullable=True)
    total_documents     = Column(Integer, default=0)
    processed_documents = Column(Integer, default=0)
    failed_documents    = Column(Integer, default=0)
    status              = Column(PgEnum("pending","processing","completed","failed","review","expired", name="processingstatus", create_type=False), default=ProcessingStatus.PENDING.value)
    webhook_url         = Column(String(512), nullable=True)
    modules             = Column(JSON, default=list)
    created_at          = Column(DateTime(timezone=True), default=utcnow)
    completed_at        = Column(DateTime(timezone=True), nullable=True)
    error_summary       = Column(JSON, default=dict)


class AuditLog(Base):
    """Immutable audit trail for all operations (GDPR / compliance)."""
    __tablename__ = "audit_logs"
    __table_args__ = (Index("ix_audit_created", "created_at"),)
    id          = Column(UUIDType(), primary_key=True, default=uuid.uuid4)
    document_id = Column(UUIDType(), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    client_id   = Column(String(128), nullable=True)
    action      = Column(String(64), nullable=False)   # PROCESS | DELETE | PURGE | ACCESS
    detail      = Column(JSON, default=dict)
    ip_address  = Column(String(64), nullable=True)
    created_at  = Column(DateTime(timezone=True), default=utcnow, index=True)
    document    = relationship("DocumentRecord", back_populates="audit_logs")
"""Initial migration - create all DocuFlow tables

Revision ID: 0001_initial
Revises: 
Create Date: 2026-04-07
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # clients
    op.create_table("clients",
        sa.Column("id",           postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name",         sa.String(128), nullable=False),
        sa.Column("client_id",    sa.String(64),  nullable=False),
        sa.Column("secret_hash",  sa.String(256), nullable=False),
        sa.Column("role",         sa.String(32),  default="api_client"),
        sa.Column("active",       sa.Boolean,     default=True),
        sa.Column("rate_limit",   sa.Integer,     default=60),
        sa.Column("allowed_ips",  sa.JSON,        default=list),
        sa.Column("metadata",     sa.JSON,        default=dict),
        sa.Column("created_at",   sa.DateTime(timezone=True)),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("client_id"),
    )
    op.create_index("ix_clients_client_id", "clients", ["client_id"])

    # api_keys
    op.create_table("api_keys",
        sa.Column("id",            postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("client_id_fk",  postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id"), nullable=False),
        sa.Column("name",          sa.String(128), nullable=False),
        sa.Column("key_hash",      sa.String(256), nullable=False),
        sa.Column("scopes",        sa.JSON,        default=list),
        sa.Column("active",        sa.Boolean,     default=True),
        sa.Column("expires_at",    sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at",    sa.DateTime(timezone=True)),
        sa.Column("last_used_at",  sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("key_hash"),
    )

    # templates
    op.create_table("templates",
        sa.Column("id",            sa.String(64),  primary_key=True),
        sa.Column("name",          sa.String(128), nullable=False),
        sa.Column("document_type", sa.String(64)),
        sa.Column("country",       sa.String(8),   nullable=True),
        sa.Column("version",       sa.String(16),  default="1"),
        sa.Column("fields_config", sa.JSON,        nullable=False),
        sa.Column("active",        sa.Boolean,     default=True),
        sa.Column("created_by",    sa.String(128), nullable=True),
        sa.Column("created_at",    sa.DateTime(timezone=True)),
        sa.Column("updated_at",    sa.DateTime(timezone=True)),
    )

    # documents
    op.create_table("documents",
        sa.Column("id",                 postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("trace_id",           sa.String(64),  nullable=False),
        sa.Column("client_fk",          postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id"), nullable=True),
        sa.Column("template_id",        sa.String(64),  nullable=True),
        sa.Column("document_type",      sa.String(64),  nullable=True),
        sa.Column("status",             sa.Enum("pending","processing","completed","failed","review","expired", name="processingstatus")),
        sa.Column("priority",           sa.String(16),  default="normal"),
        sa.Column("modules_used",       sa.JSON,        default=list),
        sa.Column("storage_key",        sa.String(256), nullable=True),
        sa.Column("file_format",        sa.String(16),  nullable=True),
        sa.Column("file_size_bytes",    sa.BigInteger,  nullable=True),
        sa.Column("extracted_fields",   sa.JSON,        nullable=True),
        sa.Column("overall_confidence", sa.Float,       nullable=True),
        sa.Column("processing_time_ms", sa.Integer,     nullable=True),
        sa.Column("alerts",             sa.JSON,        default=list),
        sa.Column("biometric_result",   sa.JSON,        nullable=True),
        sa.Column("validation_result",  sa.JSON,        nullable=True),
        sa.Column("fuzzy_result",       sa.JSON,        nullable=True),
        sa.Column("mrz_decoded",        sa.JSON,        nullable=True),
        sa.Column("global_decision",    sa.Enum("VALIDATED","REVIEW","REJECTED", name="globaldecision"), nullable=True),
        sa.Column("error_message",      sa.Text,        nullable=True),
        sa.Column("retry_count",        sa.Integer,     default=0),
        sa.Column("webhook_url",        sa.String(512), nullable=True),
        sa.Column("webhook_delivered",  sa.Boolean,     default=False),
        sa.Column("webhook_attempts",   sa.Integer,     default=0),
        sa.Column("created_at",         sa.DateTime(timezone=True)),
        sa.Column("updated_at",         sa.DateTime(timezone=True)),
        sa.Column("processed_at",       sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_at",           sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by",         sa.String(128), nullable=True),
        sa.Column("source_ip",          sa.String(64),  nullable=True),
        sa.Column("user_agent",         sa.String(256), nullable=True),
        sa.UniqueConstraint("trace_id"),
    )
    op.create_index("ix_documents_trace_id", "documents", ["trace_id"])
    op.create_index("ix_documents_status",   "documents", ["status"])
    op.create_index("ix_documents_created",  "documents", ["created_at"])
    op.create_index("ix_documents_client",   "documents", ["client_fk"])
    op.create_index("ix_documents_purge",    "documents", ["purge_at"])

    # batch_jobs
    op.create_table("batch_jobs",
        sa.Column("id",                  postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("batch_id",            sa.String(128), nullable=False),
        sa.Column("client_fk",           postgresql.UUID(as_uuid=True), sa.ForeignKey("clients.id"), nullable=True),
        sa.Column("total_documents",     sa.Integer, default=0),
        sa.Column("processed_documents", sa.Integer, default=0),
        sa.Column("failed_documents",    sa.Integer, default=0),
        sa.Column("status",              sa.Enum("pending","processing","completed","failed","review","expired", name="processingstatus")),
        sa.Column("webhook_url",         sa.String(512), nullable=True),
        sa.Column("modules",             sa.JSON,    default=list),
        sa.Column("created_at",          sa.DateTime(timezone=True)),
        sa.Column("completed_at",        sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_summary",       sa.JSON,    default=dict),
        sa.UniqueConstraint("batch_id"),
    )

    # audit_logs
    op.create_table("audit_logs",
        sa.Column("id",          postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="SET NULL"), nullable=True),
        sa.Column("client_id",   sa.String(128), nullable=True),
        sa.Column("action",      sa.String(64),  nullable=False),
        sa.Column("detail",      sa.JSON,        default=dict),
        sa.Column("ip_address",  sa.String(64),  nullable=True),
        sa.Column("created_at",  sa.DateTime(timezone=True)),
    )
    op.create_index("ix_audit_created", "audit_logs", ["created_at"])


def downgrade() -> None:
    op.drop_table("audit_logs")
    op.drop_table("batch_jobs")
    op.drop_table("documents")
    op.drop_table("templates")
    op.drop_table("api_keys")
    op.drop_table("clients")
    op.execute("DROP TYPE IF EXISTS processingstatus")
    op.execute("DROP TYPE IF EXISTS globaldecision")

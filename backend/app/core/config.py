"""DocuFlow v3.0 - Production Configuration"""
from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional
from functools import lru_cache
import os, secrets


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", case_sensitive=True, extra="ignore"
    )

    APP_NAME:    str  = "DocuFlow Platform"
    VERSION:     str  = "3.0.0"
    ENV:         str  = "development"
    DEBUG:       bool = False
    SECRET_KEY:  str  = os.getenv("SECRET_KEY", secrets.token_hex(32))

    ALLOWED_HOSTS:   List[str] = ["*"]
    ALLOWED_ORIGINS: List[str] = [
        "http://localhost:3000", "http://localhost:5173"
    ]

    # ── Database ──────────────────────────────────────────────────
    DATABASE_URL:    str = "postgresql+asyncpg://docuflow:docuflow@localhost:5432/docuflow"
    DB_POOL_SIZE:    int = 20
    DB_MAX_OVERFLOW: int = 40

    # ── Redis ─────────────────────────────────────────────────────
    REDIS_URL:            str = "redis://localhost:6379/0"
    REDIS_RESULT_TTL:     int = 86400
    REDIS_RATE_LIMIT_TTL: int = 60

    # ── MinIO ─────────────────────────────────────────────────────
    MINIO_ENDPOINT:   str  = "localhost:9000"
    MINIO_ACCESS_KEY: str  = "minioadmin"
    MINIO_SECRET_KEY: str  = "minioadmin123"
    MINIO_BUCKET:     str  = "docuflow-docs"
    MINIO_SECURE:     bool = False

    # ── Queue ─────────────────────────────────────────────────────
    QUEUE_TYPE:              str = "rabbitmq"
    RABBITMQ_URL:            str = "amqp://guest:guest@localhost:5672/"
    KAFKA_BOOTSTRAP_SERVERS: str = "localhost:9092"
    KAFKA_TOPIC:             str = "docuflow.process"
    QUEUE_MAX_RETRIES:       int = 3
    QUEUE_RETRY_DELAY:       int = 5

    # ── JWT / Security ────────────────────────────────────────────
    JWT_ALGORITHM:                   str           = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int           = 60
    JWT_REFRESH_TOKEN_EXPIRE_DAYS:   int           = 7
    JWT_SECRET:                      str           = os.getenv("JWT_SECRET", secrets.token_hex(32))
    JWT_PRIVATE_KEY_PATH:            Optional[str] = None
    JWT_PUBLIC_KEY_PATH:             Optional[str] = None
    API_KEY_LENGTH:                  int           = 32
    BCRYPT_ROUNDS:                   int           = 12
    GEMINI_API_KEY:                  Optional[str] = None

    # ── Rate limiting ─────────────────────────────────────────────
    RATE_LIMIT_ENABLED: bool = True
    RATE_LIMIT_DEFAULT: str  = "60/minute"
    RATE_LIMIT_PROCESS: str  = "30/minute"
    RATE_LIMIT_BATCH:   str  = "10/minute"
    RATE_LIMIT_AUTH:    str  = "5/minute"

    # ── OCR ───────────────────────────────────────────────────────
    TESSERACT_CMD:            str   = "tesseract"
    OCR_LANGUAGE:             str   = "fra+eng+ara"
    OCR_DPI:                  int   = 300
    OCR_TIMEOUT:              int   = 45
    OCR_CONFIDENCE_THRESHOLD: float = 0.70
    OCR_ALERT_THRESHOLD:      float = 0.75

    # ── Biometric ─────────────────────────────────────────────────
    FACE_MODEL_PATH:           str   = "/models/arcface"
    LIVENESS_MODEL_PATH:       str   = "/models/liveness"
    FACE_SIMILARITY_THRESHOLD: float = 0.45
    LIVENESS_THRESHOLD:        float = 0.75
    GPU_ENABLED:               bool  = False
    GPU_DEVICE_ID:             int   = 0

    # ── Processing ────────────────────────────────────────────────
    MAX_PARALLEL_WORKERS: int       = 50
    WORKER_TIMEOUT:       int       = 120
    MAX_FILE_SIZE_MB:     int       = 20
    ALLOWED_MIME_TYPES:   List[str] = [
        "image/jpeg", "image/png", "application/pdf",
        "image/tiff", "image/webp",
    ]

    # ── GDPR / Retention ──────────────────────────────────────────
    DOCUMENT_RETENTION_HOURS:  int = 24
    RESULT_RETENTION_DAYS:     int = 30
    AUDIT_LOG_RETENTION_DAYS:  int = 365

    # ── Monitoring ────────────────────────────────────────────────
    SENTRY_DSN:      Optional[str] = None
    METRICS_ENABLED: bool          = True
    LOG_LEVEL:       str           = "INFO"
    LOG_FORMAT:      str           = "json"

    # ── Webhooks ──────────────────────────────────────────────────
    WEBHOOK_TIMEOUT:     int           = 10
    WEBHOOK_MAX_RETRIES: int           = 5
    WEBHOOK_SECRET:      Optional[str] = None

    # ── RBAC ──────────────────────────────────────────────────────
    RBAC_ENABLED: bool = True

    # ── PII encryption ────────────────────────────────────────────
    PII_ENCRYPTION_ENABLED: bool          = False
    PII_MASTER_KEY:         Optional[str] = None

    @property
    def is_production(self) -> bool:
        return self.ENV == "production"

    @property
    def max_file_size_bytes(self) -> int:
        return self.MAX_FILE_SIZE_MB * 1024 * 1024


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()

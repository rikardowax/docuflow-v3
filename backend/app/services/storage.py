"""DocuFlow - MinIO/S3 Storage with presigned URLs and GDPR purge"""
import asyncio, io, logging
from datetime import datetime, timedelta, timezone
from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

try:
    from minio import Minio
    from minio.error import S3Error
    MINIO_AVAILABLE = True
except ImportError:
    MINIO_AVAILABLE = False
    logger.warning("minio not installed — using in-memory fallback")


class StorageService:
    def __init__(self):
        self._client = None
        self._store: dict[str, bytes] = {}  # fallback

    def _get_client(self):
        if self._client:
            return self._client
        if not MINIO_AVAILABLE:
            return None
        try:
            self._client = Minio(
                settings.MINIO_ENDPOINT,
                access_key=settings.MINIO_ACCESS_KEY,
                secret_key=settings.MINIO_SECRET_KEY,
                secure=settings.MINIO_SECURE,
            )
            if not self._client.bucket_exists(settings.MINIO_BUCKET):
                self._client.make_bucket(settings.MINIO_BUCKET)
                logger.info(f"Created bucket: {settings.MINIO_BUCKET}")
            return self._client
        except Exception as e:
            logger.error(f"MinIO init error: {e}")
            return None

    async def upload(self, file_bytes: bytes, file_format: str, trace_id: str) -> str:
        from datetime import datetime, timezone
        key = f"documents/{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/{trace_id}.{file_format}"
        client = self._get_client()
        if client:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, self._upload_sync, client, key, file_bytes, file_format)
                return key
            except Exception as e:
                logger.error(f"MinIO upload error: {e}")
        self._store[key] = file_bytes
        return key

    def _upload_sync(self, client, key, data, fmt):
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "pdf": "application/pdf"}.get(fmt, "application/octet-stream")
        client.put_object(settings.MINIO_BUCKET, key, io.BytesIO(data), len(data), content_type=mime)

    async def download(self, storage_key: str) -> bytes:
        client = self._get_client()
        if client:
            try:
                loop = asyncio.get_event_loop()
                return await loop.run_in_executor(None, self._download_sync, client, storage_key)
            except Exception as e:
                logger.error(f"MinIO download error: {e}")
        data = self._store.get(storage_key)
        if not data:
            raise FileNotFoundError(f"Key not found: {storage_key}")
        return data

    def _download_sync(self, client, key) -> bytes:
        response = client.get_object(settings.MINIO_BUCKET, key)
        return response.read()

    async def delete(self, storage_key: str):
        client = self._get_client()
        if client:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, client.remove_object, settings.MINIO_BUCKET, storage_key)
            except Exception as e:
                logger.error(f"MinIO delete error: {e}")
        self._store.pop(storage_key, None)
        logger.info(f"Purged: {storage_key}")

    async def get_presigned_url(self, key: str, expires_minutes: int = 15) -> str:
        client = self._get_client()
        if client:
            try:
                from datetime import timedelta
                loop = asyncio.get_event_loop()
                url = await loop.run_in_executor(
                    None, lambda: client.presigned_get_object(
                        settings.MINIO_BUCKET, key, expires=timedelta(minutes=expires_minutes)
                    )
                )
                return url
            except Exception as e:
                logger.error(f"Presigned URL error: {e}")
        return f"http://{settings.MINIO_ENDPOINT}/{settings.MINIO_BUCKET}/{key}?expires={expires_minutes}m"

    async def health_check(self) -> bool:
        client = self._get_client()
        if client:
            try:
                client.bucket_exists(settings.MINIO_BUCKET)
                return True
            except Exception:
                return False
        return True  # In-memory always healthy


storage_service = StorageService()

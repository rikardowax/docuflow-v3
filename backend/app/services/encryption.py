"""
DocuFlow - PII Field-Level Encryption
AES-256-GCM encryption for sensitive fields at rest.
Transparent encrypt/decrypt for document extracted fields.
"""
import base64
import hashlib
import json
import os
from typing import Any
from app.core.logging import get_logger

logger = get_logger(__name__)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    CRYPTO_AVAILABLE = True
except ImportError:
    CRYPTO_AVAILABLE = False
    logger.warning("cryptography not installed — PII encryption disabled")


# Fields considered PII and subject to encryption at rest
PII_FIELDS = frozenset({
    "last_name", "first_name", "full_name",
    "birth_date", "birth_place",
    "id_number", "passport_number", "doc_number",
    "address", "phone", "email",
    "mrz_line1", "mrz_line2",
})


class PIIEncryptionService:
    """
    AES-256-GCM field-level encryption for PII data.
    Each field gets a unique nonce; the key is derived from the master key.
    """

    def __init__(self):
        self._key: bytes | None = None
        self._enabled = False

    def initialize(self, master_key: str, salt: str = "docuflow-pii-v1"):
        """Initialize with master key from environment."""
        if not CRYPTO_AVAILABLE:
            logger.warning("PII encryption unavailable — cryptography not installed")
            return
        if not master_key or len(master_key) < 32:
            logger.warning("PII_MASTER_KEY too short — encryption disabled")
            return
        # Derive AES-256 key from master key using PBKDF2
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt.encode(),
            iterations=100_000,
        )
        self._key = kdf.derive(master_key.encode())
        self._enabled = True
        logger.info("PII field-level encryption initialized (AES-256-GCM)")

    @property
    def enabled(self) -> bool:
        return self._enabled and CRYPTO_AVAILABLE and self._key is not None

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a string value. Returns base64-encoded ciphertext."""
        if not self.enabled or plaintext is None:
            return plaintext
        try:
            aesgcm = AESGCM(self._key)
            nonce = os.urandom(12)  # 96-bit nonce
            ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
            # Prefix with nonce for storage
            return "enc:" + base64.b64encode(nonce + ct).decode("ascii")
        except Exception as e:
            logger.error(f"PII encryption failed: {e}")
            return plaintext

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a previously encrypted value."""
        if not self.enabled or not ciphertext or not ciphertext.startswith("enc:"):
            return ciphertext
        try:
            raw = base64.b64decode(ciphertext[4:])
            nonce, ct = raw[:12], raw[12:]
            aesgcm = AESGCM(self._key)
            return aesgcm.decrypt(nonce, ct, None).decode("utf-8")
        except Exception as e:
            logger.error(f"PII decryption failed: {e}")
            return ciphertext

    def encrypt_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Encrypt all PII fields in an extracted fields dict."""
        if not self.enabled:
            return fields
        result = {}
        for field_id, field_data in fields.items():
            if field_id in PII_FIELDS and isinstance(field_data, dict):
                value = field_data.get("value")
                if value is not None:
                    encrypted_value = self.encrypt(str(value))
                    result[field_id] = {**field_data, "value": encrypted_value, "encrypted": True}
                else:
                    result[field_id] = field_data
            else:
                result[field_id] = field_data
        return result

    def decrypt_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        """Decrypt all PII fields for API response."""
        result = {}
        for field_id, field_data in fields.items():
            if isinstance(field_data, dict) and field_data.get("encrypted"):
                value = field_data.get("value")
                if value:
                    decrypted = self.decrypt(str(value))
                    result[field_id] = {**field_data, "value": decrypted, "encrypted": False}
                else:
                    result[field_id] = field_data
            else:
                result[field_id] = field_data
        return result

    def pseudonymize(self, value: str) -> str:
        """One-way pseudonymization for analytics (HMAC-SHA256)."""
        if not self._key or not value:
            return "***"
        import hmac as hmac_mod
        h = hmac_mod.new(self._key, value.encode(), hashlib.sha256)
        return "pseudo_" + h.hexdigest()[:16]


pii_service = PIIEncryptionService()


def init_pii_encryption():
    """Initialize from environment variables."""
    import os
    from app.core.config import settings
    master_key = os.getenv("PII_MASTER_KEY", "")
    if getattr(settings, "PII_ENCRYPTION_ENABLED", False) and master_key:
        pii_service.initialize(master_key)
    else:
        logger.info("PII encryption disabled (set PII_ENCRYPTION_ENABLED=true and PII_MASTER_KEY)")

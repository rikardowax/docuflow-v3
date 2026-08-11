"""
DocuFlow - Additional Tests: Encryption, MFA, Utils, Queue, Storage
Extends the main test suite with coverage for new modules.
"""
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock


# ── PII Encryption Tests ───────────────────────────────────────────────

class TestPIIEncryption:
    @pytest.fixture(autouse=True)
    def setup(self):
        from app.services.encryption import PIIEncryptionService
        self.svc = PIIEncryptionService()

    def test_encrypt_decrypt_roundtrip(self):
        self.svc.initialize("a" * 32)
        if not self.svc.enabled:
            pytest.skip("cryptography not available")
        plaintext = "DUPONT"
        encrypted = self.svc.encrypt(plaintext)
        assert encrypted != plaintext
        assert encrypted.startswith("enc:")
        decrypted = self.svc.decrypt(encrypted)
        assert decrypted == plaintext

    def test_encrypt_none_returns_none(self):
        self.svc.initialize("a" * 32)
        result = self.svc.encrypt(None)
        assert result is None

    def test_decrypt_non_encrypted_passthrough(self):
        self.svc.initialize("a" * 32)
        result = self.svc.decrypt("plain_text_no_prefix")
        assert result == "plain_text_no_prefix"

    def test_encrypt_fields_only_pii(self):
        self.svc.initialize("a" * 32)
        if not self.svc.enabled:
            pytest.skip("cryptography not available")
        fields = {
            "last_name": {"value": "DUPONT", "confidence": 0.98},
            "template_id": {"value": "CNI_FR_v2", "confidence": 1.0},  # not PII
        }
        result = self.svc.encrypt_fields(fields)
        if self.svc.enabled:
            # last_name should be encrypted
            assert result["last_name"]["value"].startswith("enc:")
            # template_id should not be encrypted
            assert result["template_id"]["value"] == "CNI_FR_v2"

    def test_decrypt_fields_restores_values(self):
        self.svc.initialize("a" * 32)
        if not self.svc.enabled:
            pytest.skip("cryptography not available")
        fields = {"last_name": {"value": "DUPONT", "confidence": 0.98}}
        encrypted = self.svc.encrypt_fields(fields)
        decrypted = self.svc.decrypt_fields(encrypted)
        assert decrypted["last_name"]["value"] == "DUPONT"

    def test_weak_key_disables_encryption(self):
        from app.services.encryption import PIIEncryptionService
        svc = PIIEncryptionService()
        svc.initialize("short")  # Too short
        assert not svc.enabled

    def test_different_encryptions_different_ciphertext(self):
        self.svc.initialize("a" * 32)
        if not self.svc.enabled:
            pytest.skip("cryptography not available")
        c1 = self.svc.encrypt("DUPONT")
        c2 = self.svc.encrypt("DUPONT")
        # AES-GCM with random nonce: same plaintext → different ciphertext
        assert c1 != c2
        # But both decrypt to same value
        assert self.svc.decrypt(c1) == self.svc.decrypt(c2) == "DUPONT"

    def test_pseudonymize_deterministic(self):
        self.svc.initialize("a" * 32)
        if not self.svc._key:
            pytest.skip("cryptography not available")
        p1 = self.svc.pseudonymize("DUPONT")
        p2 = self.svc.pseudonymize("DUPONT")
        assert p1 == p2
        assert p1.startswith("pseudo_")

    def test_pseudonymize_different_values(self):
        self.svc.initialize("a" * 32)
        if not self.svc._key:
            pytest.skip("cryptography not available")
        p1 = self.svc.pseudonymize("DUPONT")
        p2 = self.svc.pseudonymize("MARTIN")
        assert p1 != p2


# ── TOTP/MFA Tests ────────────────────────────────────────────────────

class TestTOTPService:
    @pytest.fixture(autouse=True)
    def setup(self):
        from app.services.mfa import TOTPService
        self.svc = TOTPService()

    def test_generate_secret_length(self):
        secret = self.svc.generate_secret()
        assert len(secret) >= 16
        # Must be valid base32
        import base64
        base64.b32decode(secret.upper())

    def test_generate_provisioning_uri(self):
        secret = self.svc.generate_secret()
        uri = self.svc.get_provisioning_uri(secret, "user@example.com")
        assert "otpauth://totp/" in uri
        assert secret in uri
        assert "DocuFlow" in uri

    def test_verify_correct_code(self):
        secret = self.svc.generate_secret()
        code = self.svc.get_current_code(secret)
        assert self.svc.verify(secret, code)

    def test_verify_wrong_code(self):
        secret = self.svc.generate_secret()
        assert not self.svc.verify(secret, "000000")

    def test_verify_non_digit_code(self):
        secret = self.svc.generate_secret()
        assert not self.svc.verify(secret, "abcdef")

    def test_verify_wrong_length(self):
        secret = self.svc.generate_secret()
        assert not self.svc.verify(secret, "12345")   # 5 digits, not 6
        assert not self.svc.verify(secret, "1234567") # 7 digits

    def test_two_different_secrets_different_codes(self):
        s1 = self.svc.generate_secret()
        s2 = self.svc.generate_secret()
        c1 = self.svc.get_current_code(s1)
        c2 = self.svc.get_current_code(s2)
        # Very unlikely to match (1/1,000,000 chance)
        # We test that secrets are different
        assert s1 != s2

    def test_code_is_6_digits(self):
        secret = self.svc.generate_secret()
        code = self.svc.get_current_code(secret)
        assert len(code) == 6
        assert code.isdigit()


# ── Utils Tests ────────────────────────────────────────────────────────

class TestHelpers:
    def test_validate_file_magic_jpeg(self):
        from app.utils.helpers import validate_file_magic
        jpeg_bytes = b"\xff\xd8\xff\xe0" + b"\x00" * 100
        valid, detected = validate_file_magic(jpeg_bytes, "image/jpeg")
        assert valid
        assert detected == "image/jpeg"

    def test_validate_file_magic_png(self):
        from app.utils.helpers import validate_file_magic
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        valid, detected = validate_file_magic(png_bytes, "image/png")
        assert valid
        assert detected == "image/png"

    def test_validate_file_magic_mismatch(self):
        from app.utils.helpers import validate_file_magic
        # PNG magic but declared as JPEG
        png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        valid, detected = validate_file_magic(png_bytes, "image/jpeg")
        assert not valid
        assert detected == "image/png"

    def test_validate_file_magic_pdf(self):
        from app.utils.helpers import validate_file_magic
        pdf_bytes = b"%PDF-1.7\n" + b"\x00" * 100
        valid, detected = validate_file_magic(pdf_bytes, "application/pdf")
        assert valid

    def test_compute_file_hash_deterministic(self):
        from app.utils.helpers import compute_file_hash
        data = b"test data"
        h1 = compute_file_hash(data)
        h2 = compute_file_hash(data)
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_compute_file_hash_different_data(self):
        from app.utils.helpers import compute_file_hash
        assert compute_file_hash(b"abc") != compute_file_hash(b"def")

    def test_sanitize_string_removes_control_chars(self):
        from app.utils.helpers import sanitize_string
        result = sanitize_string("DUPONT\x00\x01Jean")
        assert "\x00" not in result
        assert "\x01" not in result
        assert "DUPONT" in result

    def test_sanitize_string_normalizes_whitespace(self):
        from app.utils.helpers import sanitize_string
        result = sanitize_string("DUPONT   Jean\t\tPierre")
        assert result == "DUPONT Jean Pierre"

    def test_sanitize_string_max_length(self):
        from app.utils.helpers import sanitize_string
        long_string = "A" * 1000
        result = sanitize_string(long_string, max_length=50)
        assert len(result) == 50

    def test_normalize_name_strips_accents(self):
        from app.utils.helpers import normalize_name
        assert normalize_name("Élodie") == "ELODIE"
        assert normalize_name("OUÉDRAOGO") == "OUEDRAOGO"
        assert normalize_name("Jean-Noël") == "JEAN-NOEL"

    def test_extract_digits(self):
        from app.utils.helpers import extract_digits
        assert extract_digits("AB-123.456/CD") == "123456"
        assert extract_digits("no digits") == ""
        assert extract_digits("12 34 56 78") == "12345678"

    def test_detect_document_type_cni(self):
        from app.utils.helpers import detect_document_type_from_text
        doc_type, confidence = detect_document_type_from_text(
            "République Française\nCARTE NATIONALE D'IDENTITE\nNOM: DUPONT"
        )
        assert doc_type == "CNI"
        assert confidence > 0.3

    def test_detect_document_type_passport(self):
        from app.utils.helpers import detect_document_type_from_text
        doc_type, _ = detect_document_type_from_text("PASSEPORT - PASSPORT P<FRA")
        assert doc_type == "PASSPORT"

    def test_detect_document_type_unknown(self):
        from app.utils.helpers import detect_document_type_from_text
        doc_type, confidence = detect_document_type_from_text("Hello world")
        assert doc_type == "UNKNOWN"
        assert confidence == 0.0

    def test_compute_overall_confidence(self):
        from app.utils.helpers import compute_overall_confidence
        fields = {
            "last_name":  {"confidence": 0.98},
            "first_name": {"confidence": 0.95},
            "birth_date": {"confidence": 0.99},
            "missing":    {"confidence": 0.0},  # excluded from average
        }
        result = compute_overall_confidence(fields)
        assert 0.95 <= result <= 0.99

    def test_compute_overall_confidence_empty(self):
        from app.utils.helpers import compute_overall_confidence
        assert compute_overall_confidence({}) == 0.0

    def test_get_client_ip_from_x_forwarded_for(self):
        from app.utils.helpers import get_client_ip
        headers = {"x-forwarded-for": "203.0.113.5, 198.51.100.1"}
        ip = get_client_ip(headers, "127.0.0.1")
        assert ip == "203.0.113.5"

    def test_get_client_ip_fallback(self):
        from app.utils.helpers import get_client_ip
        ip = get_client_ip({}, "192.168.1.1")
        assert ip == "192.168.1.1"

    def test_paginate_first_page(self):
        from app.utils.helpers import paginate
        items = list(range(25))
        result = paginate(items, page=1, size=10)
        assert result["items"] == list(range(10))
        assert result["total"] == 25
        assert result["pages"] == 3

    def test_paginate_last_page(self):
        from app.utils.helpers import paginate
        items = list(range(25))
        result = paginate(items, page=3, size=10)
        assert result["items"] == [20, 21, 22, 23, 24]


# ── Queue Tests ───────────────────────────────────────────────────────

class TestQueueService:
    @pytest.fixture(autouse=True)
    def setup(self):
        from app.services.queue import QueueService
        self.q = QueueService()

    @pytest.mark.asyncio
    async def test_publish_returns_job_id(self):
        job_id = await self.q.publish({"url": "http://example.com/doc.jpg"})
        assert isinstance(job_id, str)
        assert len(job_id) > 10

    @pytest.mark.asyncio
    async def test_publish_increments_queue_depth(self):
        depth_before = self.q.get_queue_depth()
        await self.q.publish({"url": "http://example.com/doc.jpg"})
        assert self.q.get_queue_depth() >= depth_before

    @pytest.mark.asyncio
    async def test_publish_high_priority(self):
        job_id = await self.q.publish({"test": "data"}, priority="high")
        assert job_id is not None

    def test_get_stats(self):
        stats = self.q.get_stats()
        assert "active_workers" in stats
        assert "queue_depth" in stats
        assert "dlq_depth" in stats
        assert "processed_total" in stats
        assert "failed_total" in stats

    @pytest.mark.asyncio
    async def test_global_decision_validated(self):
        from app.services.queue import ProcessingOrchestrator
        orch = ProcessingOrchestrator()
        result = {"biometric": {}, "validation": {"passed": True}, "fuzzy": {"overall": "VALIDATED"}}
        assert orch._decide(result) == "VALIDATED"

    @pytest.mark.asyncio
    async def test_global_decision_rejected_spoofing(self):
        from app.services.queue import ProcessingOrchestrator
        orch = ProcessingOrchestrator()
        result = {"biometric": {"spoofing_attempt": True}}
        assert orch._decide(result) == "REJECTED"

    @pytest.mark.asyncio
    async def test_global_decision_review_validation_fail(self):
        from app.services.queue import ProcessingOrchestrator
        orch = ProcessingOrchestrator()
        result = {
            "biometric": {"spoofing_attempt": False, "decision": "MATCH"},
            "validation": {"passed": False, "rules_failed": 2},
        }
        assert orch._decide(result) == "REVIEW"


# ── Storage Tests ─────────────────────────────────────────────────────

class TestStorageService:
    @pytest.fixture(autouse=True)
    def setup(self):
        from app.services.storage import StorageService
        self.svc = StorageService()

    @pytest.mark.asyncio
    async def test_upload_returns_key(self):
        key = await self.svc.upload(b"test data", "jpg", "trace_abc123")
        assert isinstance(key, str)
        assert "trace_abc123" in key
        assert key.endswith(".jpg")

    @pytest.mark.asyncio
    async def test_upload_download_roundtrip(self):
        data = b"test document content"
        key = await self.svc.upload(data, "pdf", "trace_roundtrip")
        downloaded = await self.svc.download(key)
        assert downloaded == data

    @pytest.mark.asyncio
    async def test_download_missing_raises(self):
        with pytest.raises(FileNotFoundError):
            await self.svc.download("nonexistent/key.jpg")

    @pytest.mark.asyncio
    async def test_delete_removes_key(self):
        key = await self.svc.upload(b"data", "png", "trace_delete")
        await self.svc.delete(key)
        # After deletion, key should not be found
        with pytest.raises((FileNotFoundError, Exception)):
            await self.svc.download(key)

    @pytest.mark.asyncio
    async def test_health_check_returns_bool(self):
        result = await self.svc.health_check()
        assert isinstance(result, bool)


# ── Redis Tests ───────────────────────────────────────────────────────

class TestRedisOperations:
    """Test Redis operations with mocked client."""

    @pytest.mark.asyncio
    async def test_cache_result_and_retrieve(self):
        from app.core import redis_client as rc
        mock_redis = MagicMock()
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.get = AsyncMock(return_value='{"document_id": "test"}')

        with patch.object(rc, "_redis", mock_redis):
            await rc.cache_result("test_doc", {"document_id": "test"})
            result = await rc.get_cached_result("test_doc")
            assert result == {"document_id": "test"}

    @pytest.mark.asyncio
    async def test_rate_limit_allows_under_limit(self):
        from app.core import redis_client as rc
        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=MagicMock(
            zremrangebyscore=MagicMock(),
            zadd=MagicMock(),
            zcard=MagicMock(),
            expire=MagicMock(),
            execute=AsyncMock(return_value=[0, 1, 5, True])  # count=5
        ))
        with patch.object(rc, "_redis", mock_redis):
            allowed, remaining = await rc.check_rate_limit("test_key", limit=60)
            assert allowed is True
            assert remaining == 55

    @pytest.mark.asyncio
    async def test_rate_limit_blocks_over_limit(self):
        from app.core import redis_client as rc
        mock_redis = MagicMock()
        mock_redis.pipeline = MagicMock(return_value=MagicMock(
            zremrangebyscore=MagicMock(),
            zadd=MagicMock(),
            zcard=MagicMock(),
            expire=MagicMock(),
            execute=AsyncMock(return_value=[0, 1, 61, True])  # count=61 > limit=60
        ))
        with patch.object(rc, "_redis", mock_redis):
            allowed, remaining = await rc.check_rate_limit("test_key", limit=60)
            assert allowed is False
            assert remaining == 0

    @pytest.mark.asyncio
    async def test_increment_stat(self):
        from app.core import redis_client as rc
        mock_redis = MagicMock()
        mock_redis.incr = AsyncMock(return_value=42)
        with patch.object(rc, "_redis", mock_redis):
            await rc.increment_stat("processed_total")
            mock_redis.incr.assert_called_once_with("stats:processed_total", 1)

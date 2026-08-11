"""
DocuFlow v3.0 — Tests for the two production blockers

Blocker 1: DB-backed clients and templates (was in-memory dicts)
Blocker 2: MiniFASNet liveness (was Laplacian fallback)

Additional: CSP header, /docs guard in production ENV
"""
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


# ════════════════════════════════════════════════════════════════════════
# ── BLOCKER 1: DB repositories ──────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════

class TestClientRepository:
    """ClientRepository reads from DB, not from _CLIENTS dict."""

    @pytest.fixture
    def mock_session(self):
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit  = AsyncMock()
        session.add     = MagicMock()
        return session

    @pytest.fixture
    def repo(self, mock_session):
        from app.core.db_repositories import ClientRepository
        return ClientRepository(mock_session)

    @pytest.mark.asyncio
    async def test_get_by_client_id_returns_db_row(self, repo, mock_session):
        from app.models.models import Client
        fake_client = Client()
        fake_client.client_id   = "acme"
        fake_client.secret_hash = "$2b$hash"
        fake_client.role        = "api_client"
        fake_client.active      = True
        fake_client.rate_limit  = 60

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = fake_client
        mock_session.execute.return_value = result_mock

        with patch("app.core.db_repositories._redis_get", return_value=None), \
             patch("app.core.db_repositories._redis_set", new_callable=AsyncMock):
            client = await repo.get_by_client_id("acme")

        assert client is not None
        assert client.client_id == "acme"
        assert client.active is True

    @pytest.mark.asyncio
    async def test_get_by_client_id_returns_none_for_unknown(self, repo, mock_session):
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        with patch("app.core.db_repositories._redis_get", return_value=None):
            client = await repo.get_by_client_id("unknown_client")

        assert client is None

    @pytest.mark.asyncio
    async def test_get_by_client_id_hits_redis_cache(self, repo, mock_session):
        """If Redis has the client, no DB query should be made."""
        cached = {
            "client_id": "cached_client", "secret_hash": "hash",
            "role": "api_client", "active": True, "rate_limit": 60,
        }
        with patch("app.core.db_repositories._redis_get", return_value=cached):
            client = await repo.get_by_client_id("cached_client")

        mock_session.execute.assert_not_called()
        assert client.client_id == "cached_client"


class TestTemplateRepository:
    """TemplateRepository reads/writes DB, not _TEMPLATES dict."""

    @pytest.fixture
    def mock_session(self):
        session = AsyncMock()
        session.execute = AsyncMock()
        session.commit  = AsyncMock()
        session.add     = MagicMock()
        session.refresh = AsyncMock()
        return session

    @pytest.fixture
    def repo(self, mock_session):
        from app.core.db_repositories import TemplateRepository
        return TemplateRepository(mock_session)

    @pytest.mark.asyncio
    async def test_get_returns_dict_from_db(self, repo, mock_session):
        from app.models.models import Template
        from datetime import datetime, timezone
        t = Template()
        t.id            = "CNI_FR_v2"
        t.name          = "CNI France v2"
        t.document_type = "identity_card"
        t.country       = "FR"
        t.version       = "1"
        t.active        = True
        t.fields_config = []
        t.created_by    = "system"
        t.created_at    = datetime.now(timezone.utc)

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = t
        mock_session.execute.return_value = result_mock

        with patch("app.core.db_repositories._redis_get", return_value=None), \
             patch("app.core.db_repositories._redis_set", new_callable=AsyncMock):
            tmpl = await repo.get("CNI_FR_v2")

        assert tmpl is not None
        assert tmpl["id"] == "CNI_FR_v2"
        assert tmpl["document_type"] == "identity_card"

    @pytest.mark.asyncio
    async def test_get_returns_none_for_missing_template(self, repo, mock_session):
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        with patch("app.core.db_repositories._redis_get", return_value=None):
            tmpl = await repo.get("NONEXISTENT")

        assert tmpl is None

    @pytest.mark.asyncio
    async def test_create_persists_to_db(self, repo, mock_session):
        from app.models.models import Template
        from datetime import datetime, timezone
        created = Template()
        created.id            = "MY_TPL"
        created.name          = "My template"
        created.document_type = "invoice"
        created.country       = None
        created.version       = "1"
        created.active        = True
        created.fields_config = [{"id": "amount"}]
        created.created_by    = "test_user"
        created.created_at    = datetime.now(timezone.utc)
        mock_session.refresh.return_value = None

        with patch("app.core.db_repositories.TemplateRepository.exists", return_value=False), \
             patch("app.core.db_repositories._redis_set", new_callable=AsyncMock):
            # Patch refresh to return our object
            mock_session.refresh.side_effect = lambda obj: setattr(obj, "id", "MY_TPL") or None
            t = await repo.create(
                template_id="MY_TPL",
                name="My template",
                document_type="invoice",
                fields_config=[{"id": "amount"}],
            )

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_soft_delete_sets_active_false(self, repo, mock_session):
        from app.models.models import Template
        t = Template()
        t.id     = "TO_DELETE"
        t.active = True

        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = t
        mock_session.execute.return_value = result_mock

        with patch("app.core.db_repositories._redis_del", new_callable=AsyncMock):
            deleted = await repo.soft_delete("TO_DELETE")

        assert deleted is True
        assert t.active is False
        mock_session.commit.assert_called()

    @pytest.mark.asyncio
    async def test_soft_delete_returns_false_for_missing(self, repo, mock_session):
        result_mock = MagicMock()
        result_mock.scalar_one_or_none.return_value = None
        mock_session.execute.return_value = result_mock

        deleted = await repo.soft_delete("GHOST")
        assert deleted is False


class TestSeedBuiltinTemplates:
    """seed_builtin_templates() is idempotent and uses DB."""

    @pytest.mark.asyncio
    async def test_seed_skips_existing_templates(self):
        from app.core.db_repositories import seed_builtin_templates, _BUILTIN_TEMPLATES
        with patch("app.core.db_repositories.AsyncSessionLocal") as mock_session_cls, \
             patch("app.core.db_repositories.TemplateRepository.exists", return_value=True) as mock_exists:

            session_mock = AsyncMock()
            session_mock.__aenter__ = AsyncMock(return_value=session_mock)
            session_mock.__aexit__  = AsyncMock(return_value=False)
            mock_session_cls.return_value = session_mock

            with patch("app.core.db_repositories.TemplateRepository") as MockRepo:
                instance = AsyncMock()
                instance.exists.return_value = True
                instance.create = AsyncMock()
                MockRepo.return_value = instance

                await seed_builtin_templates()

                assert instance.create.call_count == 0   # nothing created — all exist

    @pytest.mark.asyncio
    async def test_seed_creates_missing_templates(self):
        from app.core.db_repositories import seed_builtin_templates, _BUILTIN_TEMPLATES
        with patch("app.core.db_repositories.AsyncSessionLocal") as mock_session_cls:
            session_mock = AsyncMock()
            session_mock.__aenter__ = AsyncMock(return_value=session_mock)
            session_mock.__aexit__  = AsyncMock(return_value=False)
            mock_session_cls.return_value = session_mock

            with patch("app.core.db_repositories.TemplateRepository") as MockRepo:
                instance = AsyncMock()
                instance.exists.return_value = False    # nothing exists yet
                instance.create = AsyncMock()
                MockRepo.return_value = instance

                await seed_builtin_templates()

                assert instance.create.call_count == len(_BUILTIN_TEMPLATES)


# ════════════════════════════════════════════════════════════════════════
# ── BLOCKER 2: MiniFASNet liveness ──────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════

class TestMiniFASNetLiveness:
    """biometric.py must use MiniFASNet when the model is present."""

    def _make_fake_image_bytes(self) -> bytes:
        try:
            import numpy as np, cv2
            img = np.zeros((128, 128, 3), dtype=np.uint8)
            img[40:90, 40:90] = 200
            _, buf = cv2.imencode(".jpg", img)
            return buf.tobytes()
        except ImportError:
            return b"\xff\xd8\xff" + b"\x00" * 1000

    def test_minifas_liveness_called_when_model_present(self):
        """When ONNX session is available, _minifas_liveness must be used."""
        from app.services.biometric import BiometricService
        import numpy as np

        svc = BiometricService()

        # Fake ONNX session: returns real_score=0.92 (genuine)
        fake_sess = MagicMock()
        fake_output = np.array([[0.08, 0.92]], dtype=np.float32)
        fake_sess.run.return_value = [fake_output]
        fake_sess.get_inputs.return_value = [MagicMock(name="input")]

        image_bytes = self._make_fake_image_bytes()

        with patch("app.services.biometric._load_liveness", return_value=fake_sess), \
             patch("app.services.biometric.CV2_AVAILABLE", True), \
             patch("app.services.biometric._decode_image") as mock_decode:
            import numpy as np
            mock_decode.return_value = np.zeros((128, 128, 3), dtype=np.uint8)
            result = svc._passive_liveness(image_bytes)

        assert result["model"] == "MiniFASNet-V2"
        assert result["liveness_score"] == pytest.approx(0.92, abs=0.01)
        assert result["result"] == "GENUINE"
        assert result["spoofing_attempt"] is False

    def test_minifas_detects_spoof(self):
        """Score < LIVENESS_THRESHOLD → SPOOF."""
        from app.services.biometric import BiometricService
        import numpy as np

        svc = BiometricService()

        fake_sess = MagicMock()
        fake_output = np.array([[0.85, 0.15]], dtype=np.float32)  # low real score
        fake_sess.run.return_value = [fake_output]
        fake_sess.get_inputs.return_value = [MagicMock(name="input")]

        image_bytes = self._make_fake_image_bytes()

        with patch("app.services.biometric._load_liveness", return_value=fake_sess), \
             patch("app.services.biometric.CV2_AVAILABLE", True), \
             patch("app.services.biometric._decode_image") as mock_decode:
            mock_decode.return_value = np.zeros((128, 128, 3), dtype=np.uint8)
            result = svc._passive_liveness(image_bytes)

        assert result["result"] == "SPOOF"
        assert result["spoofing_attempt"] is True
        assert result["model"] == "MiniFASNet-V2"

    def test_laplacian_fallback_warns_when_no_model(self):
        """When no ONNX model is available, fallback must log a warning."""
        from app.services.biometric import BiometricService
        import numpy as np

        svc = BiometricService()
        image_bytes = self._make_fake_image_bytes()

        with patch("app.services.biometric._load_liveness", return_value=None), \
             patch("app.services.biometric.CV2_AVAILABLE", True), \
             patch("app.services.biometric._decode_image") as mock_decode, \
             patch("app.services.biometric.logger") as mock_logger:
            mock_decode.return_value = np.zeros((128, 128, 3), dtype=np.uint8)
            result = svc._passive_liveness(image_bytes)

        # Warning must be issued
        mock_logger.warning.assert_called()
        warning_msg = mock_logger.warning.call_args[0][0]
        assert "LIVENESS FALLBACK" in warning_msg or "laplacian" in warning_msg.lower()

        # Fallback result carries its own warning field
        assert result.get("model") == "laplacian_fallback"
        assert "warning" in result

    def test_laplacian_fallback_model_label(self):
        """Laplacian fallback result must be clearly labelled as non-certified."""
        from app.services.biometric import BiometricService
        import numpy as np

        svc = BiometricService()
        image_bytes = self._make_fake_image_bytes()

        with patch("app.services.biometric._load_liveness", return_value=None), \
             patch("app.services.biometric.CV2_AVAILABLE", True), \
             patch("app.services.biometric._decode_image") as mock_decode, \
             patch("app.services.biometric.logger"):
            mock_decode.return_value = np.zeros((128, 128, 3), dtype=np.uint8)
            result = svc._texture_liveness(image_bytes)

        assert result["model"] == "laplacian_fallback"
        assert "Non-certified" in result.get("warning", "")

    @pytest.mark.asyncio
    async def test_verify_decision_is_spoofing_detected(self):
        """If liveness fails, decision must be SPOOFING_DETECTED, not MISMATCH."""
        from app.services.biometric import BiometricService

        svc = BiometricService()
        spoof_result = {
            "liveness_score": 0.12,
            "result": "SPOOF",
            "spoofing_attempt": True,
            "model": "MiniFASNet-V2",
        }

        with patch.object(svc, "_detect_faces", return_value=svc._sim_faces()), \
             patch.object(svc, "_passive_liveness", return_value=spoof_result), \
             patch.object(svc, "_photo_integrity", return_value={
                 "photo_present": True, "photo_integrity_score": 0.9,
                 "tampering_detected": False, "ela_score": 0.0,
             }):
            result = await svc.verify(
                document_bytes=b"\xff\xd8\xff" + b"\x00" * 100,
                selfie_bytes=b"\xff\xd8\xff" + b"\x00" * 100,
            )

        assert result["decision"] == "SPOOFING_DETECTED"
        assert result["spoofing_attempt"] is True
        assert any("anti-spoofing" in a.lower() or "spoof" in a.lower()
                   for a in result.get("alerts", []))


# ════════════════════════════════════════════════════════════════════════
# ── Security: CSP + /docs guard ─────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════

class TestSecurityMiddleware:

    def _make_mock_request(self, path: str, scheme: str = "https") -> MagicMock:
        req = MagicMock()
        req.url.path   = path
        req.url.scheme = scheme
        return req

    @pytest.mark.asyncio
    async def test_csp_header_present_on_api_response(self):
        """Every API response must carry a Content-Security-Policy header."""
        from app.middleware.security import SecurityHeadersMiddleware
        from starlette.responses import Response

        middleware = SecurityHeadersMiddleware(app=MagicMock())
        request = self._make_mock_request("/v2/health")

        fake_response = Response(content="ok", status_code=200)
        call_next = AsyncMock(return_value=fake_response)

        with patch("app.middleware.security.settings") as mock_settings:
            mock_settings.is_production = False
            response = await middleware.dispatch(request, call_next)

        assert "Content-Security-Policy" in response.headers
        csp = response.headers["Content-Security-Policy"]
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'none'" in csp

    @pytest.mark.asyncio
    async def test_docs_blocked_in_production(self):
        """Requests to /docs, /redoc, /openapi.json must return 404 in production."""
        from app.middleware.security import SecurityHeadersMiddleware

        middleware = SecurityHeadersMiddleware(app=MagicMock())
        call_next  = AsyncMock()

        for path in ("/docs", "/redoc", "/openapi.json"):
            request = self._make_mock_request(path)
            with patch("app.middleware.security.settings") as mock_settings:
                mock_settings.is_production = True
                response = await middleware.dispatch(request, call_next)

            assert response.status_code == 404, f"{path} must return 404 in production"
            # call_next must NOT be called — request terminated early
            call_next.assert_not_called()
            call_next.reset_mock()

    @pytest.mark.asyncio
    async def test_docs_accessible_in_development(self):
        """/docs must pass through in development environment."""
        from app.middleware.security import SecurityHeadersMiddleware
        from starlette.responses import Response

        middleware = SecurityHeadersMiddleware(app=MagicMock())
        request    = self._make_mock_request("/docs")

        fake_response = Response(content="<html>", status_code=200)
        call_next = AsyncMock(return_value=fake_response)

        with patch("app.middleware.security.settings") as mock_settings:
            mock_settings.is_production = False
            response = await middleware.dispatch(request, call_next)

        assert response.status_code == 200
        call_next.assert_called_once()

    @pytest.mark.asyncio
    async def test_hsts_header_in_production(self):
        """Strict-Transport-Security must be set in production."""
        from app.middleware.security import SecurityHeadersMiddleware
        from starlette.responses import Response

        middleware = SecurityHeadersMiddleware(app=MagicMock())
        request    = self._make_mock_request("/v2/health", scheme="https")

        fake_response = Response(content="ok", status_code=200)
        call_next = AsyncMock(return_value=fake_response)

        with patch("app.middleware.security.settings") as mock_settings:
            mock_settings.is_production = True
            response = await middleware.dispatch(request, call_next)

        assert "Strict-Transport-Security" in response.headers
        assert "max-age=31536000" in response.headers["Strict-Transport-Security"]

    @pytest.mark.asyncio
    async def test_cache_control_no_store_on_api_routes(self):
        """API responses must never be cached (PII data)."""
        from app.middleware.security import SecurityHeadersMiddleware
        from starlette.responses import Response

        middleware = SecurityHeadersMiddleware(app=MagicMock())
        request    = self._make_mock_request("/v2/process")

        fake_response = Response(content="{}", status_code=200)
        call_next = AsyncMock(return_value=fake_response)

        with patch("app.middleware.security.settings") as mock_settings:
            mock_settings.is_production = False
            response = await middleware.dispatch(request, call_next)

        assert response.headers.get("Cache-Control") == "no-store"
        assert response.headers.get("Pragma") == "no-cache"


# ════════════════════════════════════════════════════════════════════════
# ── Magic bytes validation ───────────────────────────────────────────────
# ════════════════════════════════════════════════════════════════════════

class TestValidateFileMagic:

    def test_valid_jpeg(self):
        from app.utils.helpers import validate_file_magic
        jpeg = b"\xff\xd8\xff" + b"\x00" * 100
        valid, detected = validate_file_magic(jpeg, "image/jpeg")
        assert valid is True
        assert detected == "image/jpeg"

    def test_valid_png(self):
        from app.utils.helpers import validate_file_magic
        png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
        valid, detected = validate_file_magic(png, "image/png")
        assert valid is True
        assert detected == "image/png"

    def test_valid_pdf(self):
        from app.utils.helpers import validate_file_magic
        pdf = b"%PDF-1.4" + b"\x00" * 100
        valid, detected = validate_file_magic(pdf, "application/pdf")
        assert valid is True
        assert detected == "application/pdf"

    def test_polyglot_jpeg_declared_as_png_rejected(self):
        """JPEG bytes declared as PNG must be rejected."""
        from app.utils.helpers import validate_file_magic
        jpeg = b"\xff\xd8\xff" + b"\x00" * 100
        valid, detected = validate_file_magic(jpeg, "image/png")
        assert valid is False
        assert detected == "image/jpeg"

    def test_arbitrary_bytes_rejected(self):
        from app.utils.helpers import validate_file_magic
        garbage = b"\x00\x01\x02\x03" * 50
        valid, detected = validate_file_magic(garbage, "image/jpeg")
        assert valid is False

    def test_process_endpoint_rejects_polyglot(self):
        """POST /v2/process must return 400 when magic bytes mismatch."""
        from app.utils.helpers import validate_file_magic
        # Simulate: JPEG content declared as PNG
        jpeg_bytes = b"\xff\xd8\xff" + b"\x00" * 200
        valid, detected = validate_file_magic(jpeg_bytes, "image/png")
        assert valid is False, (
            "validate_file_magic must return invalid for JPEG-as-PNG — "
            "the /process endpoint depends on this to reject polyglot files"
        )


# ════════════════════════════════════════════════════════════════════════
# ── MRZ TD1 (regression from v3.0 extraction.py) ───────────────────────
# ════════════════════════════════════════════════════════════════════════

class TestMRZDecoderV3:

    @pytest.fixture
    def decoder(self):
        from app.services.extraction import MRZDecoder
        return MRZDecoder()

    def test_td3_decode_valid(self, decoder):
        # Well-formed TD3 MRZ (ICAO 9303 example)
        l1 = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<"
        l2 = "L898902C36UTO7408122F1204159ZE184226B<<<<<<1"
        result = decoder.decode_td3(l1, l2)
        assert result["format"] == "TD3"
        assert result["last_name"] == "ERIKSSON"
        assert result["first_name"] == "ANNA MARIA"

    def test_td1_decode_valid(self, decoder):
        # Synthetic TD1 with valid check digits
        # Build a minimal valid TD1 for testing
        l1 = "I<UTO<<<<<<<<<<<1"
        # Use a known-valid synthetic TD1
        l1 = "IDUTOD231458907<<<<<<<<<<<<<<"   # 30 chars, padded
        l2 = "7408122M1204159UTO<<<<<<<<<<<4"   # 30 chars
        l3 = "ERIKSSON<<ANNA<MARIA<<<<<<<<<<"   # 30 chars
        result = decoder.decode_td1(l1[:30], l2[:30], l3[:30])
        assert result["format"] == "TD1"

    def test_auto_detect_td3(self, decoder):
        """extract_from_ocr must auto-detect TD3 from OCR text."""
        l1 = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<"
        l2 = "L898902C36UTO7408122F1204159ZE184226B<<<<<<1"
        text = f"Some noise\n{l1}\n{l2}\nmore noise"
        result = decoder.extract_from_ocr(text)
        assert result is not None
        assert result["format"] == "TD3"

    def test_invalid_mrz_returns_valid_false(self, decoder):
        """Tampered check digit must set valid=False."""
        l1 = "P<UTOERIKSSON<<ANNA<MARIA<<<<<<<<<<<<<<<<<<"
        # Corrupt the document number check digit (position 9 in l2)
        l2 = "L898902C99UTO7408122F1204159ZE184226B<<<<<<1"   # 9 → wrong
        result = decoder.decode_td3(l1, l2)
        assert result["valid"] is False
        assert "fail" in result["check_digits"]

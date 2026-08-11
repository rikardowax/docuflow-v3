"""
DocuFlow - Complete Test Suite
Unit tests, integration tests, and E2E tests.
Run: pytest tests/ -v --cov=app --cov-report=html
"""
import asyncio
import io
import json
import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import AsyncClient, ASGITransport

# ── Fixtures ───────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest.fixture
def sample_image_bytes():
    """Minimal valid PNG bytes."""
    import struct, zlib
    def png_chunk(chunk_type, data):
        c = chunk_type + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xffffffff)
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr = png_chunk(b"IHDR", ihdr_data)
    raw = b"\x00\xff\x00\x00"
    idat = png_chunk(b"IDAT", zlib.compress(raw))
    iend = png_chunk(b"IEND", b"")
    return signature + ihdr + idat + iend


@pytest.fixture
def sample_template():
    return {
        "id": "TEST_v1",
        "name": "Test Template",
        "document_type": "identity_card",
        "fields": [
            {"id": "last_name",  "label": "Nom",   "type": "string", "validation": {"required": True, "min_length": 2}, "ocr_tolerance": 0.80, "fuzzy_threshold": 0.90},
            {"id": "birth_date", "label": "Date",  "type": "date",   "validation": {"required": True, "not_future": True}, "ocr_tolerance": 0.90, "fuzzy_threshold": 1.00},
            {"id": "id_number",  "label": "Num",   "type": "string", "validation": {"required": False}, "ocr_tolerance": 0.90, "fuzzy_threshold": 0.95},
        ]
    }


@pytest_asyncio.fixture
async def auth_token():
    """Get a valid auth token."""
    from app.core.security import create_access_token
    return create_access_token({
        "sub": "test_client",
        "role": "admin",
        "scopes": ["process:read", "process:write", "batch:write",
                   "template:read", "template:write", "stats:read", "admin"],
    })


@pytest_asyncio.fixture
async def client(auth_token):
    """HTTP test client."""
    from app.main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.headers["Authorization"] = f"Bearer {auth_token}"
        yield c


# ── Unit: Security ─────────────────────────────────────────────────────

class TestSecurity:
    def test_hash_and_verify_password(self):
        from app.core.security import hash_password, verify_password
        hashed = hash_password("mysecret")
        assert verify_password("mysecret", hashed)
        assert not verify_password("wrong", hashed)

    def test_create_and_decode_token(self):
        from app.core.security import create_access_token, decode_token
        token = create_access_token({"sub": "client123", "role": "operator"})
        payload = decode_token(token)
        assert payload["sub"] == "client123"
        assert payload["role"] == "operator"

    def test_expired_token_raises(self):
        from app.core.security import create_access_token, decode_token
        from datetime import timedelta
        from jose import JWTError
        token = create_access_token({"sub": "x"}, expires_delta=timedelta(seconds=-1))
        with pytest.raises(JWTError):
            decode_token(token)

    def test_generate_api_key(self):
        from app.core.security import generate_api_key, hash_api_key
        raw, hashed = generate_api_key()
        assert raw.startswith("df_")
        assert len(raw) > 20
        assert hashed == hash_api_key(raw)

    def test_permission_check(self):
        from app.core.security import CurrentUser, Permission
        user = CurrentUser("test", role="operator")
        assert user.has_permission(Permission.PROCESS_WRITE)
        assert not user.has_permission(Permission.ADMIN)

    def test_role_admin_has_all_permissions(self):
        from app.core.security import CurrentUser, Permission
        admin = CurrentUser("admin", role="admin")
        for perm in Permission:
            assert admin.has_permission(perm)

    def test_webhook_signature(self):
        from app.core.security import sign_webhook_payload
        sig1 = sign_webhook_payload('{"event":"test"}')
        sig2 = sign_webhook_payload('{"event":"test"}')
        assert sig1 == sig2
        assert len(sig1) == 64  # hex sha256


# ── Unit: Fuzzy Matching ───────────────────────────────────────────────

class TestFuzzyMatcher:
    @pytest.fixture(autouse=True)
    def setup(self):
        from app.services.validation import FuzzyMatcher
        self.fm = FuzzyMatcher()

    def test_exact_match(self):
        score, algo = self.fm.best_score("DUPONT", "DUPONT")
        assert score == 1.0
        assert algo == "exact"

    def test_case_insensitive(self):
        score, _ = self.fm.best_score("dupont", "DUPONT")
        assert score >= 0.95

    def test_accent_normalization(self):
        score, _ = self.fm.best_score("OUEDRAOGO", "OUÉDRAOGO")
        assert score >= 0.90

    def test_typo_tolerance(self):
        score, _ = self.fm.best_score("DUPONT", "DUPONTT")
        assert score >= 0.85

    def test_phonetic_similarity(self):
        score, _ = self.fm.best_score("FANTA", "PHANTA")
        assert score >= 0.75

    def test_completely_different(self):
        score, _ = self.fm.best_score("DUPONT", "ZZZZZZZ")
        assert score < 0.50

    def test_date_uses_levenshtein(self):
        score, algo = self.fm.best_score("1985-03-14", "1985-03-14", ftype="date")
        assert score == 1.0
        assert algo in ("exact", "levenshtein")

    def test_levenshtein_distance(self):
        score = self.fm.levenshtein("ABCDE", "ABCDF")
        assert 0.75 <= score <= 1.0

    def test_jaro_winkler_names(self):
        score = self.fm.jaro_winkler("MARIE", "MARIA")
        assert score >= 0.85

    def test_ngram_similarity(self):
        score = self.fm.ngram("MARTIN", "MARTINE")
        assert score >= 0.75

    @pytest.mark.parametrize("s1,s2,expected_min", [
        ("Mohamed", "Mohammed", 0.88),
        ("Jean-Pierre", "Jean Pierre", 0.90),
        ("OUÉDRAOGO", "OUEDRAOGO", 0.92),
        ("14/03/1985", "1985-03-14", 0.50),
    ])
    def test_parametrized_pairs(self, s1, s2, expected_min):
        score, _ = self.fm.best_score(s1, s2)
        assert score >= expected_min, f"{s1} vs {s2}: got {score}, expected >= {expected_min}"


# ── Unit: Validation ───────────────────────────────────────────────────

class TestValidation:
    @pytest.fixture(autouse=True)
    def setup(self):
        from app.services.validation import ValidationService
        self.svc = ValidationService()

    def test_required_field_missing(self):
        errors = self.svc.validate_field("name", None, {"required": True})
        assert len(errors) == 1
        assert "required" in errors[0]["message"]

    def test_required_field_empty_string(self):
        errors = self.svc.validate_field("name", "", {"required": True})
        assert len(errors) == 1

    def test_min_length_pass(self):
        errors = self.svc.validate_field("name", "DUPONT", {"min_length": 2})
        assert len(errors) == 0

    def test_min_length_fail(self):
        errors = self.svc.validate_field("name", "A", {"min_length": 2})
        assert len(errors) == 1

    def test_max_length_fail(self):
        errors = self.svc.validate_field("id", "A" * 20, {"max_length": 12})
        assert len(errors) == 1

    def test_regex_pass(self):
        errors = self.svc.validate_field("id", "123456789012", {"regex": r"^\d{12}$"})
        assert len(errors) == 0

    def test_regex_fail(self):
        errors = self.svc.validate_field("id", "ABC", {"regex": r"^\d{12}$"})
        assert len(errors) == 1

    def test_not_future_date_fails_for_future(self):
        errors = self.svc.validate_field("birth_date", "2099-01-01", {"not_future": True})
        assert len(errors) == 1

    def test_not_future_date_passes_for_past(self):
        errors = self.svc.validate_field("birth_date", "1985-03-14", {"not_future": True})
        assert len(errors) == 0

    def test_not_past_warns_for_expired(self):
        errors = self.svc.validate_field("expiry_date", "2000-01-01", {"not_past": True})
        assert any(e["severity"] == "warning" for e in errors)

    def test_min_age(self):
        errors = self.svc.validate_field("birth_date", "2020-01-01", {"min_age": 18})
        assert len(errors) == 1

    @pytest.mark.asyncio
    async def test_full_validation_pass(self, sample_template):
        from app.services.validation import validation_service
        fields = {
            "last_name":  {"value": "DUPONT",      "confidence": 0.98},
            "birth_date": {"value": "1985-03-14",  "confidence": 0.99},
            "id_number":  {"value": "123456789012","confidence": 0.97},
        }
        result = await validation_service.validate(fields, sample_template)
        assert result["passed"] is True
        assert result["rules_failed"] == 0

    @pytest.mark.asyncio
    async def test_full_validation_fail_required(self, sample_template):
        from app.services.validation import validation_service
        fields = {
            "last_name":  {"value": None,     "confidence": 0.0},
            "birth_date": {"value": "1985-03-14", "confidence": 0.99},
            "id_number":  {"value": None,     "confidence": 0.0},
        }
        result = await validation_service.validate(fields, sample_template)
        assert result["passed"] is False
        assert result["rules_failed"] >= 1


# ── Unit: MRZ Decoder ──────────────────────────────────────────────────

class TestMRZDecoder:
    @pytest.fixture(autouse=True)
    def setup(self):
        from app.services.extraction import MRZDecoder
        self.decoder = MRZDecoder()

    def test_check_digit_computation(self):
        # ICAO 9303 example: "232" → check digit 2
        assert self.decoder.check_digit("232") == 2

    def test_decode_names(self):
        last, first = self.decoder.decode_names("DUPONT<<JEAN<PIERRE<<<<<<<<<")
        assert last == "DUPONT"
        assert "JEAN" in first

    def test_valid_td3(self):
        # Synthetic valid MRZ (check digits computed correctly)
        # Using a known valid test vector
        line1 = "P<FRADUPONT<<JEAN<PIERRE<<<<<<<<<<<<<<<<<<<"
        line2 = "L8980DR34FRA8503140M3512315<<<<<<<<<<<<<<<6"
        result = self.decoder.decode_td3(line1, line2)
        assert result["last_name"] == "DUPONT"
        assert result["nationality"] == "FRA"


# ── Unit: OCR Extraction ───────────────────────────────────────────────

class TestExtractionService:
    @pytest.mark.asyncio
    async def test_extract_returns_required_keys(self, sample_image_bytes, sample_template):
        from app.services.extraction import extraction_service
        result = await extraction_service.extract(sample_image_bytes, "png", sample_template)
        assert "fields" in result
        assert "overall_confidence" in result
        assert "processing_time_ms" in result
        assert "alerts" in result

    @pytest.mark.asyncio
    async def test_extract_fields_match_template(self, sample_image_bytes, sample_template):
        from app.services.extraction import extraction_service
        result = await extraction_service.extract(sample_image_bytes, "png", sample_template)
        for field in sample_template["fields"]:
            assert field["id"] in result["fields"]

    def test_detect_document_type_cni(self):
        from app.services.extraction import ExtractionService
        svc = ExtractionService()
        assert svc._detect_type("CARTE NATIONALE D'IDENTITE") == "CNI"

    def test_detect_document_type_passport(self):
        from app.services.extraction import ExtractionService
        svc = ExtractionService()
        assert svc._detect_type("PASSEPORT FRANCAIS") == "PASSPORT"

    def test_date_coercion(self):
        from app.services.extraction import ExtractionService
        svc = ExtractionService()
        assert svc._coerce("14/03/1985", "date") == "1985-03-14"
        assert svc._coerce("1985-03-14", "date") == "1985-03-14"

    def test_number_coercion(self):
        from app.services.extraction import ExtractionService
        svc = ExtractionService()
        assert svc._coerce("ABC-123.45", "number") == "123.45"


# ── Integration: API Endpoints ─────────────────────────────────────────

class TestAuthEndpoints:
    @pytest.mark.asyncio
    async def test_login_success(self, client):
        resp = await client.post("/v2/auth/token", json={
            "client_id": "demo_client",
            "client_secret": "demo_secret",
            "grant_type": "client_credentials"
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "access_token" in data
        assert data["token_type"] == "Bearer"
        assert data["expires_in"] > 0

    @pytest.mark.asyncio
    async def test_login_invalid_credentials(self, client):
        resp = await client.post("/v2/auth/token", json={
            "client_id": "demo_client",
            "client_secret": "wrong_secret",
            "grant_type": "client_credentials"
        })
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_login_unknown_client(self, client):
        resp = await client.post("/v2/auth/token", json={
            "client_id": "nonexistent",
            "client_secret": "any",
            "grant_type": "client_credentials"
        })
        assert resp.status_code == 401


class TestProcessEndpoints:
    @pytest.mark.asyncio
    async def test_process_document_success(self, client, sample_image_bytes):
        resp = await client.post("/v2/process", data={
            "template_id": "CNI_FR_v2",
            "modules": "extraction,validation",
        }, files={"file": ("test.png", sample_image_bytes, "image/png")})
        assert resp.status_code == 200
        data = resp.json()
        assert "document_id" in data
        assert "global_decision" in data
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_process_returns_fields(self, client, sample_image_bytes):
        resp = await client.post("/v2/process", data={
            "template_id": "CNI_FR_v2",
            "modules": "extraction",
        }, files={"file": ("test.png", sample_image_bytes, "image/png")})
        assert resp.status_code == 200
        data = resp.json()
        assert data["fields"] is not None

    @pytest.mark.asyncio
    async def test_process_invalid_format(self, client):
        resp = await client.post("/v2/process", data={"template_id": "CNI_FR_v2"},
            files={"file": ("test.exe", b"MZ", "application/octet-stream")})
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_process_file_too_large(self, client):
        big = b"0" * (25 * 1024 * 1024)
        resp = await client.post("/v2/process", data={"template_id": "CNI_FR_v2"},
            files={"file": ("big.jpg", big, "image/jpeg")})
        assert resp.status_code == 413

    @pytest.mark.asyncio
    async def test_process_with_reference_data(self, client, sample_image_bytes):
        ref = json.dumps({"last_name": "DUPONT", "birth_date": "1985-03-14"})
        resp = await client.post("/v2/process", data={
            "template_id": "CNI_FR_v2", "modules": "extraction,fuzzy",
            "reference_data": ref,
        }, files={"file": ("test.png", sample_image_bytes, "image/png")})
        assert resp.status_code == 200
        data = resp.json()
        assert data["fuzzy_matching"] is not None

    @pytest.mark.asyncio
    async def test_get_result_after_process(self, client, sample_image_bytes):
        # Process first
        resp = await client.post("/v2/process", data={"template_id": "CNI_FR_v2"},
            files={"file": ("test.png", sample_image_bytes, "image/png")})
        doc_id = resp.json()["document_id"]
        # Then retrieve
        resp2 = await client.get(f"/v2/results/{doc_id}")
        assert resp2.status_code == 200
        assert resp2.json()["document_id"] == doc_id

    @pytest.mark.asyncio
    async def test_get_result_not_found(self, client):
        resp = await client.get("/v2/results/nonexistent_id")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_process_unknown_template(self, client, sample_image_bytes):
        resp = await client.post("/v2/process",
            data={"template_id": "NONEXISTENT_v99"},
            files={"file": ("test.png", sample_image_bytes, "image/png")})
        assert resp.status_code == 404


class TestBatchEndpoints:
    @pytest.mark.asyncio
    async def test_batch_submit(self, client):
        resp = await client.post("/v2/process/batch", json={
            "documents": [
                {"url": "https://example.com/doc1.jpg", "template_id": "CNI_FR_v2"},
                {"url": "https://example.com/doc2.jpg", "template_id": "PASSPORT_INT_v1"},
            ],
            "batch_id": "test_batch_001",
            "modules": ["extraction", "validation"],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["batch_id"] == "test_batch_001"
        assert data["total_documents"] == 2
        assert data["status"] == "queued"

    @pytest.mark.asyncio
    async def test_batch_requires_at_least_one_doc(self, client):
        resp = await client.post("/v2/process/batch", json={"documents": []})
        assert resp.status_code == 422


class TestTemplateEndpoints:
    @pytest.mark.asyncio
    async def test_list_templates(self, client):
        resp = await client.get("/v2/templates")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) >= 2
        ids = [t["id"] for t in data]
        assert "CNI_FR_v2" in ids
        assert "PASSPORT_INT_v1" in ids

    @pytest.mark.asyncio
    async def test_get_specific_template(self, client):
        resp = await client.get("/v2/templates/CNI_FR_v2")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == "CNI_FR_v2"
        assert len(data["fields"]) == 6

    @pytest.mark.asyncio
    async def test_create_template(self, client):
        tmpl = {
            "id": "TEST_CREATE_v1", "name": "Test Create", "document_type": "invoice",
            "fields": [{"id": "amount", "label": "Montant", "type": "number",
                        "validation": {"required": True}, "ocr_tolerance": 0.85, "fuzzy_threshold": 0.95}]
        }
        resp = await client.post("/v2/templates", json=tmpl)
        assert resp.status_code == 201
        assert resp.json()["id"] == "TEST_CREATE_v1"

    @pytest.mark.asyncio
    async def test_create_duplicate_template(self, client):
        tmpl = {"id": "CNI_FR_v2", "name": "Dup", "document_type": "identity_card",
                "fields": [{"id": "f", "label": "F", "type": "string",
                             "validation": {}, "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90}]}
        resp = await client.post("/v2/templates", json=tmpl)
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_get_nonexistent_template(self, client):
        resp = await client.get("/v2/templates/DOESNT_EXIST")
        assert resp.status_code == 404


class TestMonitoringEndpoints:
    @pytest.mark.asyncio
    async def test_health_check(self, client):
        resp = await client.get("/v2/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "status" in data
        assert "components" in data
        assert data["components"]["api"] == "healthy"

    @pytest.mark.asyncio
    async def test_root_health(self, client):
        resp = await client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_stats_endpoint(self, client):
        resp = await client.get("/v2/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_documents" in data
        assert "success_rate" in data
        assert "active_workers" in data
        assert "queue_depth" in data


# ── Security Tests ─────────────────────────────────────────────────────

class TestSecurityControls:
    @pytest.mark.asyncio
    async def test_endpoint_requires_auth(self):
        from app.main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            resp = await c.get("/v2/templates")
        assert resp.status_code in (401, 403)

    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self):
        from app.main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.headers["Authorization"] = "Bearer invalid.token.here"
            resp = await c.get("/v2/templates")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_response_has_security_headers(self, client):
        resp = await client.get("/health")
        assert "x-content-type-options" in resp.headers
        assert resp.headers["x-content-type-options"] == "nosniff"
        assert "x-frame-options" in resp.headers

    @pytest.mark.asyncio
    async def test_response_has_request_id(self, client):
        resp = await client.get("/health")
        assert "x-request-id" in resp.headers
        assert "x-response-time" in resp.headers

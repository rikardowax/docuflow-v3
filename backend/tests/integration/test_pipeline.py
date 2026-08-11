"""
DocuFlow - Integration Tests
Full pipeline tests: auth → process → result → delete (GDPR).
"""
import io
import json
import pytest
import pytest_asyncio
import struct
import zlib
from unittest.mock import AsyncMock, patch, MagicMock


def make_png() -> bytes:
    """Minimal valid PNG."""
    def chunk(t, d):
        c = t + d
        return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", 50, 50, 8, 2, 0, 0, 0))
    raw = b"\x00" + b"\xFF\x00\x00" * 50
    idat = chunk(b"IDAT", zlib.compress(raw * 50))
    iend = chunk(b"IEND", b"")
    return sig + ihdr + idat + iend


PNG_BYTES = make_png()


@pytest_asyncio.fixture
async def auth_token():
    from app.core.security import create_access_token
    return create_access_token({
        "sub": "integration_test_client",
        "role": "admin",
        "scopes": ["process:read", "process:write", "batch:write",
                   "template:read", "template:write", "stats:read", "admin"],
    })


@pytest_asyncio.fixture
async def client(auth_token):
    from httpx import AsyncClient, ASGITransport
    with patch("app.core.redis_client._redis") as mock_redis, \
         patch("app.core.redis_client.init_redis", new_callable=AsyncMock), \
         patch("app.core.database.init_db", new_callable=AsyncMock):
        mock_redis.ping = AsyncMock(return_value=True)
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.setex = AsyncMock(return_value=True)
        mock_redis.delete = AsyncMock(return_value=1)
        mock_redis.incr = AsyncMock(return_value=1)
        mock_redis.lpush = AsyncMock(return_value=1)
        mock_redis.ltrim = AsyncMock(return_value=True)
        mock_redis.lrange = AsyncMock(return_value=[])
        mock_redis.pipeline = MagicMock(return_value=MagicMock(
            zremrangebyscore=MagicMock(), zadd=MagicMock(),
            zcard=MagicMock(), expire=MagicMock(),
            execute=AsyncMock(return_value=[0, 1, 1, True])
        ))
        from app.main import app
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            c.headers["Authorization"] = f"Bearer {auth_token}"
            yield c


class TestFullPipeline:
    """End-to-end pipeline: auth → process → retrieve → delete."""

    @pytest.mark.asyncio
    async def test_complete_extraction_pipeline(self, client):
        """Full extraction pipeline with all modules."""
        resp = await client.post(
            "/v2/process",
            data={
                "template_id": "CNI_FR_v2",
                "modules": "extraction,validation,fuzzy",
                "reference_data": json.dumps({"last_name": "DUPONT", "birth_date": "1985-03-14"}),
            },
            files={"file": ("test.png", PNG_BYTES, "image/png")},
        )
        assert resp.status_code == 200
        data = resp.json()

        # Check structure
        assert data["document_id"].startswith("doc_")
        assert data["trace_id"] == data["document_id"]
        assert data["status"] == "completed"
        assert data["template_id"] == "CNI_FR_v2"
        assert data["global_decision"] in ("VALIDATED", "REVIEW", "REJECTED")
        assert isinstance(data["processing_time_ms"], int)
        assert data["processing_time_ms"] > 0
        assert isinstance(data["overall_confidence"], float)
        assert 0.0 <= data["overall_confidence"] <= 1.0

        # Fields
        assert data["fields"] is not None
        for field_id in ["last_name", "first_name", "birth_date", "id_number", "expiry_date"]:
            assert field_id in data["fields"]
            field = data["fields"][field_id]
            assert "value" in field
            assert "confidence" in field
            assert 0.0 <= field["confidence"] <= 1.0

        # Validation
        assert data["validation"] is not None
        assert "passed" in data["validation"]
        assert "rules_checked" in data["validation"]
        assert "errors" in data["validation"]
        assert "warnings" in data["validation"]

        # Fuzzy matching (reference data provided)
        assert data["fuzzy_matching"] is not None
        assert "global_score" in data["fuzzy_matching"]
        assert "overall" in data["fuzzy_matching"]
        assert data["fuzzy_matching"]["overall"] in ("VALIDATED", "REVIEW", "REJECTED")

        # MRZ (identity_card template)
        assert "mrz_decoded" in data  # may be None if not detected
        return data["document_id"]

    @pytest.mark.asyncio
    async def test_process_then_retrieve(self, client):
        """Process a document and retrieve the result."""
        # Process
        process_resp = await client.post(
            "/v2/process",
            data={"template_id": "CNI_FR_v2", "modules": "extraction"},
            files={"file": ("test.png", PNG_BYTES, "image/png")},
        )
        assert process_resp.status_code == 200
        doc_id = process_resp.json()["document_id"]

        # Retrieve
        get_resp = await client.get(f"/v2/results/{doc_id}")
        assert get_resp.status_code == 200
        result = get_resp.json()
        assert result["document_id"] == doc_id
        assert result["status"] == "completed"

    @pytest.mark.asyncio
    async def test_process_then_delete_gdpr(self, client):
        """Process a document then delete it (GDPR right to erasure)."""
        # Process
        resp = await client.post(
            "/v2/process",
            data={"template_id": "CNI_FR_v2", "modules": "extraction"},
            files={"file": ("test.png", PNG_BYTES, "image/png")},
        )
        assert resp.status_code == 200
        doc_id = resp.json()["document_id"]

        # Delete
        del_resp = await client.delete(f"/v2/results/{doc_id}")
        assert del_resp.status_code == 204

        # Verify it's gone
        get_resp = await client.get(f"/v2/results/{doc_id}")
        assert get_resp.status_code == 404

    @pytest.mark.asyncio
    async def test_extraction_only_module(self, client):
        """Test with only extraction module — no validation or fuzzy."""
        resp = await client.post(
            "/v2/process",
            data={"template_id": "CNI_FR_v2", "modules": "extraction"},
            files={"file": ("test.png", PNG_BYTES, "image/png")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["fields"] is not None
        assert data["validation"] is None
        assert data["fuzzy_matching"] is None

    @pytest.mark.asyncio
    async def test_passport_template(self, client):
        """Test with passport template."""
        resp = await client.post(
            "/v2/process",
            data={"template_id": "PASSPORT_INT_v1", "modules": "extraction,validation"},
            files={"file": ("passport.png", PNG_BYTES, "image/png")},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["template_id"] == "PASSPORT_INT_v1"
        # Passport template has different fields
        for field_id in ["last_name", "first_name", "passport_number", "expiry_date"]:
            assert field_id in data["fields"]

    @pytest.mark.asyncio
    async def test_high_priority_processing(self, client):
        """High priority docs should process normally."""
        resp = await client.post(
            "/v2/process",
            data={"template_id": "CNI_FR_v2", "modules": "extraction", "priority": "high"},
            files={"file": ("urgent.png", PNG_BYTES, "image/png")},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"

    @pytest.mark.asyncio
    async def test_batch_then_status(self, client):
        """Submit batch and check status endpoint."""
        batch_resp = await client.post("/v2/process/batch", json={
            "documents": [
                {"url": f"https://example.com/doc{i}.jpg", "template_id": "CNI_FR_v2"}
                for i in range(5)
            ],
            "batch_id": "integration_test_batch_001",
            "modules": ["extraction", "validation"],
        })
        assert batch_resp.status_code == 200
        batch_data = batch_resp.json()
        assert batch_data["batch_id"] == "integration_test_batch_001"
        assert batch_data["total_documents"] == 5
        assert batch_data["status"] == "queued"

        # Check status
        status_resp = await client.get(f"/v2/batch/integration_test_batch_001/status")
        assert status_resp.status_code == 200

    @pytest.mark.asyncio
    async def test_template_crud_lifecycle(self, client):
        """Create → Get → Update → Delete template."""
        template_data = {
            "id": "INTEGRATION_TEST_v1",
            "name": "Integration Test Template",
            "document_type": "invoice",
            "fields": [
                {"id": "amount", "label": "Montant", "type": "number",
                 "validation": {"required": True}, "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90},
                {"id": "invoice_number", "label": "Numéro", "type": "string",
                 "validation": {"required": True, "min_length": 3}, "ocr_tolerance": 0.90, "fuzzy_threshold": 0.95},
            ]
        }

        # Create
        create_resp = await client.post("/v2/templates", json=template_data)
        assert create_resp.status_code == 201
        created = create_resp.json()
        assert created["id"] == "INTEGRATION_TEST_v1"
        assert created["fields_count"] == 2
        assert created["active"] is True

        # Get
        get_resp = await client.get("/v2/templates/INTEGRATION_TEST_v1")
        assert get_resp.status_code == 200
        assert get_resp.json()["id"] == "INTEGRATION_TEST_v1"

        # Appears in list
        list_resp = await client.get("/v2/templates")
        ids = [t["id"] for t in list_resp.json()]
        assert "INTEGRATION_TEST_v1" in ids

        # Delete (deactivate)
        del_resp = await client.delete("/v2/templates/INTEGRATION_TEST_v1")
        assert del_resp.status_code == 204

        # Use it in process — template deactivated, should not appear in active list
        list_after = await client.get("/v2/templates")
        active_ids = [t["id"] for t in list_after.json() if t["active"]]
        assert "INTEGRATION_TEST_v1" not in active_ids


class TestErrorHandling:
    """Tests for error cases and edge conditions."""

    @pytest.mark.asyncio
    async def test_malformed_reference_data(self, client):
        resp = await client.post(
            "/v2/process",
            data={"template_id": "CNI_FR_v2", "reference_data": "not valid json"},
            files={"file": ("test.png", PNG_BYTES, "image/png")},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_process_without_file(self, client):
        resp = await client.post("/v2/process", data={"template_id": "CNI_FR_v2"})
        assert resp.status_code == 422  # Validation error — file required

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, client):
        """Multiple simultaneous requests should all succeed."""
        import asyncio
        tasks = [
            client.post(
                "/v2/process",
                data={"template_id": "CNI_FR_v2", "modules": "extraction"},
                files={"file": (f"doc{i}.png", PNG_BYTES, "image/png")},
            )
            for i in range(5)
        ]
        responses = await asyncio.gather(*tasks)
        for resp in responses:
            assert resp.status_code == 200
            assert resp.json()["status"] == "completed"

    @pytest.mark.asyncio
    async def test_duplicate_template_returns_409(self, client):
        await client.post("/v2/templates", json={
            "id": "DUP_TEST_v1", "name": "Dup", "document_type": "invoice",
            "fields": [{"id": "f", "label": "F", "type": "string",
                        "validation": {}, "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90}]
        })
        resp2 = await client.post("/v2/templates", json={
            "id": "DUP_TEST_v1", "name": "Dup2", "document_type": "invoice",
            "fields": [{"id": "f", "label": "F", "type": "string",
                        "validation": {}, "ocr_tolerance": 0.85, "fuzzy_threshold": 0.90}]
        })
        assert resp2.status_code == 409

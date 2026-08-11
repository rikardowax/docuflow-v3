import pytest
from unittest.mock import MagicMock, patch
from PIL import Image
import io
import json

from app.services.ocr.gemini_ocr_service import GeminiOCRService
from app.core.config import settings


def _make_image_bytes(color="red"):
    img = Image.new("RGB", (100, 50), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# Mock the settings for GEMINI_API_KEY
@pytest.fixture(autouse=True)
def mock_settings():
    with patch.object(settings, "GEMINI_API_KEY", "test_api_key"):
        # Reset singleton so each test gets a fresh instance
        GeminiOCRService._instance = None
        yield settings
        GeminiOCRService._instance = None


@pytest.mark.asyncio
async def test_gemini_ocr_service_extract_structured_success():
    """
    Test that GeminiOCRService correctly extracts structured fields.
    """
    gemini_response = json.dumps({
        "raw_text": "DUPONT\nJean-Pierre\n14/03/1985",
        "fields": {
            "last_name": "DUPONT",
            "first_name": "Jean-Pierre",
            "birth_date": "14/03/1985",
            "document_type": "CNI",
        },
    })

    with patch("google.generativeai.GenerativeModel") as MockModel:
        mock_instance = MockModel.return_value
        mock_response = MagicMock()
        mock_response.text = gemini_response
        mock_instance.generate_content = MagicMock(return_value=mock_response)

        service = GeminiOCRService()
        result = await service.extract_structured(_make_image_bytes())

        assert result["fields"]["last_name"] == "DUPONT"
        assert result["fields"]["first_name"] == "Jean-Pierre"
        assert result["fields"]["birth_date"] == "14/03/1985"
        assert result["fields"]["document_type"] == "CNI"
        assert "DUPONT" in result["raw_text"]


@pytest.mark.asyncio
async def test_gemini_ocr_service_process_image_compat():
    """
    Test that process_image still returns raw_text string for backward compat.
    """
    gemini_response = json.dumps({
        "raw_text": "Hello World",
        "fields": {"last_name": "TEST"},
    })

    with patch("google.generativeai.GenerativeModel") as MockModel:
        mock_instance = MockModel.return_value
        mock_response = MagicMock()
        mock_response.text = gemini_response
        mock_instance.generate_content = MagicMock(return_value=mock_response)

        service = GeminiOCRService()
        text = await service.process_image(_make_image_bytes())
        assert text == "Hello World"


@pytest.mark.asyncio
async def test_gemini_ocr_service_no_text_in_response():
    """
    Test that GeminiOCRService returns empty when no text is extracted.
    """
    with patch("google.generativeai.GenerativeModel") as MockModel:
        mock_instance = MockModel.return_value
        mock_response = MagicMock()
        mock_response.text = None
        mock_instance.generate_content = MagicMock(return_value=mock_response)

        service = GeminiOCRService()
        result = await service.extract_structured(_make_image_bytes())
        assert result["raw_text"] == ""
        assert result["fields"] == {}


@pytest.mark.asyncio
async def test_gemini_ocr_service_api_error():
    """
    Test that GeminiOCRService re-raises exceptions from the Gemini API.
    """
    with patch("google.generativeai.GenerativeModel") as MockModel:
        mock_instance = MockModel.return_value
        mock_instance.generate_content = MagicMock(side_effect=Exception("Gemini API Error"))

        service = GeminiOCRService()
        with pytest.raises(Exception, match="Gemini API Error"):
            await service.extract_structured(_make_image_bytes())


@pytest.mark.asyncio
async def test_gemini_ocr_service_no_api_key_configured():
    """
    Test that GeminiOCRService raises ValueError if GEMINI_API_KEY is not configured.
    """
    with patch.object(settings, "GEMINI_API_KEY", None):
        with pytest.raises(ValueError, match="GEMINI_API_KEY is not configured in settings."):
            GeminiOCRService()


@pytest.mark.asyncio
async def test_gemini_ocr_handles_markdown_code_fences():
    """
    Test that the parser strips markdown code fences from Gemini's response.
    """
    raw = '```json\n{"raw_text": "test", "fields": {"last_name": "MARTIN"}}\n```'

    with patch("google.generativeai.GenerativeModel") as MockModel:
        mock_instance = MockModel.return_value
        mock_response = MagicMock()
        mock_response.text = raw
        mock_instance.generate_content = MagicMock(return_value=mock_response)

        service = GeminiOCRService()
        result = await service.extract_structured(_make_image_bytes())
        assert result["fields"]["last_name"] == "MARTIN"


@pytest.mark.asyncio
async def test_gemini_ocr_handles_non_json_response():
    """
    Test that non-JSON response is returned as raw_text.
    """
    with patch("google.generativeai.GenerativeModel") as MockModel:
        mock_instance = MockModel.return_value
        mock_response = MagicMock()
        mock_response.text = "This is not JSON, just plain OCR text."
        mock_instance.generate_content = MagicMock(return_value=mock_response)

        service = GeminiOCRService()
        result = await service.extract_structured(_make_image_bytes())
        assert "not JSON" in result["raw_text"]
        assert result["fields"] == {}

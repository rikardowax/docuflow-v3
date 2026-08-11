"""
DocuFlow v3.0 — Gemini OCR Service
Uses Google Gemini 2.0 Flash for structured document field extraction.
Returns both raw text and parsed JSON fields from identity documents.
"""
import asyncio
import io
import json
import logging
import re
from typing import Any

import google.generativeai as genai
from PIL import Image

from app.core.config import settings

logger = logging.getLogger(__name__)

# ── Document-type-aware extraction prompts ────────────────────────────

_STRUCTURED_PROMPT = """\
You are a precise OCR engine for official documents. Analyze this image and return a JSON object with two keys:
1. "raw_text": all visible text in the image, preserving line breaks.
2. "fields": a JSON object with the following keys (set to null if not found):
   - "last_name": family name
   - "first_name": given name(s)
   - "birth_date": date of birth in DD/MM/YYYY format
   - "birth_place": place of birth
   - "nationality": nationality
   - "gender": M or F
   - "id_number": document number / numéro de pièce
   - "expiry_date": expiration date in DD/MM/YYYY format
   - "issue_date": issue date in DD/MM/YYYY format
   - "issuing_authority": issuing authority
   - "address": address if present
   - "document_type": one of CNI, PASSPORT, DRIVER_LICENSE, RESIDENCE_PERMIT, RIB, INVOICE, RCCM, UNKNOWN
   - "mrz_line1": first MRZ line if visible (raw characters)
   - "mrz_line2": second MRZ line if visible (raw characters)
   - "mrz_line3": third MRZ line if visible (raw characters, for TD1 cards)

IMPORTANT RULES:
- Return ONLY valid JSON, no markdown, no explanation, no code fences.
- Dates must always be DD/MM/YYYY.
- Preserve accented characters exactly as printed.
- For MRZ lines, transcribe < characters exactly.
- If a field is not visible in the document, set it to null.
"""

_INVOICE_PROMPT = """\
You are a precise OCR engine for invoices and business documents. Analyze this image and return a JSON object with two keys:
1. "raw_text": all visible text in the image, preserving line breaks.
2. "fields": a JSON object with the following keys (set to null if not found):
   - "invoice_number": numéro de facture
   - "invoice_date": date in DD/MM/YYYY
   - "due_date": date d'échéance in DD/MM/YYYY
   - "total_amount": montant total (number only)
   - "currency": devise (EUR, USD, XOF, etc.)
   - "tax_amount": montant TVA
   - "vendor_name": nom du fournisseur
   - "vendor_address": adresse du fournisseur
   - "client_name": nom du client
   - "client_address": adresse du client
   - "iban": IBAN if present
   - "bic": BIC/SWIFT if present
   - "document_type": "INVOICE"

IMPORTANT: Return ONLY valid JSON, no markdown, no code fences.
"""


_RECTO_VERSO_PROMPT = """\
You are a precise OCR engine for official documents. You are given TWO images of the SAME document:
- Image 1 = RECTO (front side)
- Image 2 = VERSO (back side)

Analyze BOTH images and return a SINGLE JSON object with two keys:
1. "raw_text": all visible text from BOTH sides, separated by "\\n--- VERSO ---\\n" between front and back.
2. "fields": a JSON object merging information from both sides. Use these keys (set to null if not found):
   - "last_name": family name
   - "first_name": given name(s)
   - "birth_date": date of birth in DD/MM/YYYY format
   - "birth_place": place of birth
   - "nationality": nationality
   - "gender": M or F
   - "id_number": document number / numéro de pièce
   - "expiry_date": expiration date in DD/MM/YYYY format
   - "issue_date": issue date in DD/MM/YYYY format
   - "issuing_authority": issuing authority
   - "address": address if present
   - "document_type": one of CNI, PASSPORT, DRIVER_LICENSE, RESIDENCE_PERMIT, RIB, INVOICE, RCCM, UNKNOWN
   - "mrz_line1": first MRZ line if visible (raw characters)
   - "mrz_line2": second MRZ line if visible (raw characters)
   - "mrz_line3": third MRZ line if visible (raw characters, for TD1 cards)
   - "father_name": father's full name (often on verso)
   - "mother_name": mother's full name (often on verso)
   - "registration_place": place of registration (often on verso)
   - "height": height in cm (often on verso, e.g. "175 cm")

IMPORTANT RULES:
- Return ONLY valid JSON, no markdown, no explanation, no code fences.
- Merge data from both sides into ONE fields object. If the same field appears on both sides, prefer the clearer/more complete version.
- Dates must always be DD/MM/YYYY.
- Preserve accented characters exactly as printed.
- For MRZ lines, transcribe < characters exactly.
- If a field is not visible on either side, set it to null.
"""


def _get_prompt(document_type: str | None) -> str:
    if document_type in ("invoice", "INVOICE"):
        return _INVOICE_PROMPT
    return _STRUCTURED_PROMPT


class GeminiOCRService:
    _instance = None

    def __init__(self):
        if not settings.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY is not configured in settings.")
        genai.configure(api_key=settings.GEMINI_API_KEY)
        # Use gemini-2.5-flash (best quality for OCR, 500 RPD free tier)
        model_name = getattr(settings, "GEMINI_MODEL", None) or "gemini-2.5-flash"
        self.model = genai.GenerativeModel(model_name)
        logger.info(f"Gemini OCR engine initialized with model: {model_name}")

    @classmethod
    def get_instance(cls) -> "GeminiOCRService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Raw text extraction (legacy compat) ───────────────────────
    async def process_image(self, image_data: bytes) -> str:
        result = await self.extract_structured(image_data)
        return result.get("raw_text", "")

    # ── Structured field extraction ───────────────────────────────
    async def extract_structured(
        self,
        image_data: bytes,
        document_type: str | None = None,
    ) -> dict[str, Any]:
        try:
            img = Image.open(io.BytesIO(image_data))
            if img.mode == "RGBA":
                img = img.convert("RGB")

            prompt = _get_prompt(document_type)

            # Try up to 2 times — gemini-2.5-flash-lite sometimes returns
            # non-JSON on first attempt
            for attempt in range(2):
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: self.model.generate_content(
                        [prompt, img],
                        generation_config=genai.GenerationConfig(
                            temperature=0.0,
                            max_output_tokens=4096,
                            response_mime_type="application/json",
                        ),
                    ),
                )

                if not response or not response.text:
                    logger.warning("Gemini API returned no text for the image.")
                    return {"raw_text": "", "fields": {}}

                result = self._parse_response(response.text)
                if result.get("fields"):
                    return result
                if attempt == 0:
                    logger.info("Gemini returned no fields, retrying once...")

            return result

        except Exception as e:
            logger.error(f"Error processing image with Gemini OCR: {e}")
            raise

    # ── Recto + Verso dual-image extraction ───────────────────────
    async def extract_recto_verso(
        self,
        recto_data: bytes,
        verso_data: bytes,
        document_type: str | None = None,
    ) -> dict[str, Any]:
        try:
            recto_img = Image.open(io.BytesIO(recto_data))
            verso_img = Image.open(io.BytesIO(verso_data))
            if recto_img.mode == "RGBA":
                recto_img = recto_img.convert("RGB")
            if verso_img.mode == "RGBA":
                verso_img = verso_img.convert("RGB")

            prompt = _RECTO_VERSO_PROMPT

            for attempt in range(2):
                loop = asyncio.get_event_loop()
                response = await loop.run_in_executor(
                    None,
                    lambda: self.model.generate_content(
                        [prompt, recto_img, verso_img],
                        generation_config=genai.GenerationConfig(
                            temperature=0.0,
                            max_output_tokens=4096,
                            response_mime_type="application/json",
                        ),
                    ),
                )

                if not response or not response.text:
                    logger.warning("Gemini API returned no text for recto/verso.")
                    return {"raw_text": "", "fields": {}, "dual_side": True}

                result = self._parse_response(response.text)
                result["dual_side"] = True
                if result.get("fields"):
                    return result
                if attempt == 0:
                    logger.info("Gemini recto/verso returned no fields, retrying once...")

            return result

        except Exception as e:
            logger.error(f"Error processing recto/verso with Gemini OCR: {e}")
            raise

    def _parse_response(self, text: str) -> dict[str, Any]:
        cleaned = text.strip()
        # Strip markdown code fences if Gemini returns them despite instructions
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip()

        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError:
            # Try to find a JSON object embedded in the response text
            match = re.search(r'\{[\s\S]*\}', cleaned)
            if match:
                try:
                    data = json.loads(match.group())
                except json.JSONDecodeError:
                    pass
                else:
                    logger.info("Extracted JSON from non-JSON Gemini response")
                    return self._normalize_parsed(data, cleaned)
            logger.warning(
                "Gemini response is not valid JSON, raw (first 500 chars): %s",
                cleaned[:500],
            )
            return {"raw_text": cleaned, "fields": {}}

        return self._normalize_parsed(data, text)

    @staticmethod
    def _normalize_parsed(data: Any, raw_fallback: str) -> dict[str, Any]:
        if not isinstance(data, dict):
            return {"raw_text": str(data), "fields": {}}

        raw_text = data.get("raw_text", "")
        fields = data.get("fields", {})

        if not isinstance(fields, dict):
            fields = {}

        # Clean null values
        fields = {k: v for k, v in fields.items() if v is not None}

        return {"raw_text": raw_text, "fields": fields}

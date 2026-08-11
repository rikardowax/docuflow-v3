"""
DocuFlow - Utility Functions
File validation, image helpers, response builders, sanitization.
"""
import hashlib
import io
import magic
import re
import unicodedata
from pathlib import Path
from typing import Optional

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)


# ── File Validation ────────────────────────────────────────────────────
MAGIC_SIGNATURES = {
    b"\xff\xd8\xff":                     "image/jpeg",
    b"\x89PNG\r\n\x1a\n":               "image/png",
    b"%PDF":                              "application/pdf",
    b"II*\x00":                          "image/tiff",
    b"MM\x00*":                          "image/tiff",
    b"RIFF":                              "image/webp",
}


def validate_file_magic(file_bytes: bytes, declared_type: str) -> tuple[bool, str]:
    """
    Validate file magic bytes against declared MIME type.
    Returns (is_valid, detected_type).
    Prevents polyglot file attacks.
    """
    for sig, mime in MAGIC_SIGNATURES.items():
        if file_bytes[:len(sig)] == sig:
            detected = mime
            break
    else:
        detected = "application/octet-stream"

    # Special: WebP check
    if file_bytes[:4] == b"RIFF" and len(file_bytes) >= 12 and file_bytes[8:12] == b"WEBP":
        detected = "image/webp"

    is_valid = detected == declared_type or (
        detected == "image/jpeg" and declared_type in ("image/jpeg", "image/jpg")
    )
    return is_valid, detected


def compute_file_hash(file_bytes: bytes) -> str:
    """SHA-256 hash of file content (for deduplication and audit)."""
    return hashlib.sha256(file_bytes).hexdigest()


def validate_file_size(file_bytes: bytes) -> bool:
    """Check file is within size limits."""
    return len(file_bytes) <= settings.max_file_size_bytes


def get_file_format(content_type: str) -> str:
    """Map MIME type to file extension."""
    return {
        "image/jpeg":      "jpg",
        "image/jpg":       "jpg",
        "image/png":       "png",
        "application/pdf": "pdf",
        "image/tiff":      "tiff",
        "image/webp":      "webp",
    }.get(content_type, "bin")


# ── Text Sanitization ──────────────────────────────────────────────────
def sanitize_string(value: str, max_length: int = 256) -> str:
    """Remove control characters and normalize whitespace."""
    if not value:
        return ""
    # Remove null bytes and control characters
    value = re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", "", value)
    # Normalize whitespace
    value = re.sub(r"\s+", " ", value).strip()
    return value[:max_length]


def normalize_name(name: str) -> str:
    """Normalize a person's name: remove accents, uppercase."""
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")
    return name.upper().strip()


def extract_digits(value: str) -> str:
    """Extract only digit characters from a string."""
    return re.sub(r"\D", "", value)


# ── IP / Network ───────────────────────────────────────────────────────
def get_client_ip(headers: dict, client_host: Optional[str]) -> str:
    """Extract real client IP from headers (respects X-Forwarded-For)."""
    xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    real_ip = headers.get("x-real-ip") or headers.get("X-Real-IP")
    if real_ip:
        return real_ip.strip()
    return client_host or "unknown"


def is_valid_ip(ip: str) -> bool:
    """Validate IPv4 or IPv6 address."""
    import ipaddress
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


# ── Response Builders ──────────────────────────────────────────────────
def build_error_response(code: str, message: str, details: dict = None) -> dict:
    """Standard error response format."""
    resp = {"error": code, "message": message}
    if details:
        resp["details"] = details
    return resp


def paginate(items: list, page: int, size: int) -> dict:
    """Paginate a list."""
    import math
    total = len(items)
    start = (page - 1) * size
    end   = start + size
    return {
        "items": items[start:end],
        "total": total,
        "page":  page,
        "size":  size,
        "pages": math.ceil(total / size) if size > 0 else 0,
    }


# ── Document Type Detection ────────────────────────────────────────────
DOCUMENT_KEYWORDS = {
    "CNI":      ["carte nationale", "national d'identite", "cni", "cin", "carte d'identite"],
    "PASSPORT": ["passport", "passeport", "travel document"],
    "RCCM":     ["rccm", "registre du commerce", "registre de commerce"],
    "NIU":      ["niu", "numero d'identifiant unique", "identifiant unique"],
    "RIB":      ["rib", "iban", "bic", "domiciliation bancaire", "releve d'identite"],
    "INVOICE":  ["facture", "invoice", "montant ttc", "tva", "numero de facture"],
    "CONTRACT": ["contrat", "agreement", "entre les soussignes"],
}


def detect_document_type_from_text(text: str) -> tuple[str, float]:
    """
    Detect document type from OCR text using keyword matching.
    Returns (type, confidence).
    """
    text_lower = text.lower()
    scores = {}
    for doc_type, keywords in DOCUMENT_KEYWORDS.items():
        matched = sum(1 for kw in keywords if kw in text_lower)
        if matched > 0:
            scores[doc_type] = matched / len(keywords)

    if not scores:
        return "UNKNOWN", 0.0

    best_type = max(scores, key=scores.get)
    return best_type, min(1.0, scores[best_type] * 2)  # Scale up confidence


# ── Confidence Scoring ─────────────────────────────────────────────────
def compute_overall_confidence(field_results: dict) -> float:
    """Compute weighted average confidence across all fields."""
    if not field_results:
        return 0.0
    confidences = [
        f["confidence"] if isinstance(f, dict) else getattr(f, "confidence", 0)
        for f in field_results.values()
        if (f["confidence"] if isinstance(f, dict) else getattr(f, "confidence", 0)) > 0
    ]
    return round(sum(confidences) / len(confidences), 3) if confidences else 0.0

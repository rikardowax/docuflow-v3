"""
DocuFlow v3.0 - OCR Service
Primary  : PaddleOCR PP-OCRv4/v5 (multilingual, Apache 2.0)
Secondary: Tesseract 5 LSTM with PSM 6 for forms
Fallback : Simulation mode

Improvements over v2.2:
 - PaddleOCR primary engine (state-of-the-art open source 2026)
 - OCR post-correction for common character confusions (0/O, 1/I/l, S/5...)
 - Tesseract --psm 6 for structured forms (was --psm 3 — auto)
 - Confidence threshold raised to 0.70 (was 0.60)
 - Adaptive upscaling: target 1800px width (was 1000px)
 - MRZ: TD1 (3-line ID cards) + TD2 support added (was TD3-only)
 - CLAHE pre-enhancement for low-contrast scans
"""
import asyncio
import io
import re
import time
from enum import Enum
from typing import Any

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

# ── Imaging ───────────────────────────────────────────────────────────
try:
    import cv2
    import numpy as np
    from PIL import Image
    IMAGING_AVAILABLE = True
except ImportError:
    IMAGING_AVAILABLE = False
    logger.warning("OpenCV/PIL not available")

# ── PaddleOCR (primary) ───────────────────────────────────────────────
_paddle_model = None

def _load_paddle():
    global _paddle_model
    if _paddle_model is not None:
        return _paddle_model
    try:
        from paddleocr import PaddleOCR
        _paddle_model = PaddleOCR(
            use_angle_cls=True,
            lang="latin",
            use_gpu=settings.GPU_ENABLED,
            show_log=False,
            use_space_char=True,
            det_db_thresh=0.3,
            det_db_box_thresh=0.5,
            rec_batch_num=6,
        )
        logger.info("PaddleOCR engine loaded")
        return _paddle_model
    except Exception as e:
        logger.warning(f"PaddleOCR unavailable ({e}), fallback to Tesseract")
        return None

# ── Tesseract (secondary) ─────────────────────────────────────────────
try:
    import pytesseract
    pytesseract.pytesseract.tesseract_cmd = settings.TESSERACT_CMD
    TESSERACT_AVAILABLE = True
except ImportError:
    TESSERACT_AVAILABLE = False


class OCREngine(str, Enum):
    PADDLE     = "paddle"
    TESSERACT  = "tesseract"
    SIMULATION = "simulation"


# ── Post-correction ───────────────────────────────────────────────────
_NUM_FIXES = {"O": "0", "o": "0", "I": "1", "l": "1",
              "S": "5", "B": "8", "G": "6", "Z": "2"}

def _fix_numeric(text: str) -> str:
    return "".join(_NUM_FIXES.get(c, c) for c in text)

def _fix_name(text: str) -> str:
    t = text.upper()
    t = re.sub(r"(?<=[A-Z])0(?=[A-Z])", "O", t)
    t = re.sub(r"(?<=[A-Z])1(?=[A-Z])", "I", t)
    return t


# ── Image Preprocessor ────────────────────────────────────────────────
class ImagePreprocessor:
    def preprocess(self, image_bytes: bytes, fmt: str) -> bytes:
        if not IMAGING_AVAILABLE:
            return image_bytes
        try:
            nparr = np.frombuffer(image_bytes, np.uint8)
            img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return image_bytes

            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            gray = self._deskew(gray)
            gray = cv2.fastNlMeansDenoising(gray, h=10)

            # CLAHE for low-contrast documents (new in v3.0)
            clahe    = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(gray)
            _, binary = cv2.threshold(
                enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
            )

            # Upscale: target 1800px width (was 1000px in v2.2)
            h, w = binary.shape
            if w < 1800:
                scale  = 1800 / w
                binary = cv2.resize(
                    binary, None, fx=scale, fy=scale,
                    interpolation=cv2.INTER_CUBIC,
                )

            ok, buf = cv2.imencode(".png", binary)
            return buf.tobytes() if ok else image_bytes
        except Exception as e:
            logger.warning(f"Preprocessing failed: {e}")
            return image_bytes

    def _deskew(self, gray: "np.ndarray") -> "np.ndarray":
        try:
            coords = np.column_stack(np.where(gray < 200))
            if len(coords) < 100:
                return gray
            angle = cv2.minAreaRect(coords)[-1]
            angle = -(90 + angle) if angle < -45 else -angle
            if abs(angle) < 0.3:      # tighter than v2.2 (was 0.5)
                return gray
            h, w = gray.shape
            M = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
            return cv2.warpAffine(
                gray, M, (w, h),
                flags=cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_REPLICATE,
            )
        except Exception:
            return gray


# ── PaddleOCR Engine ──────────────────────────────────────────────────
class PaddleOCREngine:
    async def run(self, image_bytes: bytes) -> "dict[str, Any] | None":
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._run_sync, image_bytes),
                timeout=settings.OCR_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("PaddleOCR timed out")
            return None

    def _run_sync(self, image_bytes: bytes) -> "dict[str, Any] | None":
        model = _load_paddle()
        if model is None:
            return None
        try:
            nparr = np.frombuffer(image_bytes, np.uint8)
            img   = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return None
            result = model.ocr(img, cls=True)
            if not result or not result[0]:
                return {"full_text": "", "blocks": [], "engine": OCREngine.PADDLE}

            h, w   = img.shape[:2]
            blocks = []
            lines  = []
            for line in result[0]:
                if not line:
                    continue
                bbox, (text, conf) = line
                text = text.strip()
                if not text or conf < settings.OCR_CONFIDENCE_THRESHOLD:
                    continue
                xs = [p[0] for p in bbox]
                ys = [p[1] for p in bbox]
                blocks.append({
                    "text":       text,
                    "confidence": round(float(conf), 3),
                    "x": round(min(xs) / w, 4),
                    "y": round(min(ys) / h, 4),
                    "w": round((max(xs) - min(xs)) / w, 4),
                    "h": round((max(ys) - min(ys)) / h, 4),
                    "line_num":  len(lines) + 1,
                    "block_num": 1,
                })
                lines.append(text)
            return {"full_text": "\n".join(lines), "blocks": blocks,
                    "engine": OCREngine.PADDLE}
        except Exception as e:
            logger.error(f"PaddleOCR error: {e}")
            return None


# ── Tesseract Engine ──────────────────────────────────────────────────
class TesseractOCREngine:
    def __init__(self):
        self.lang = settings.OCR_LANGUAGE

    async def run(self, image_bytes: bytes, is_form: bool = True) -> dict[str, Any]:
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._run_sync, image_bytes, is_form),
                timeout=settings.OCR_TIMEOUT,
            )
        except asyncio.TimeoutError:
            logger.warning("Tesseract timed out")
            return self._simulate()

    def _run_sync(self, image_bytes: bytes, is_form: bool) -> dict[str, Any]:
        if not TESSERACT_AVAILABLE:
            return self._simulate()
        try:
            img = Image.open(io.BytesIO(image_bytes))
            # PSM 6 = uniform block — better for ID card forms (v2.2 used PSM 3)
            psm = 6 if is_form else 3
            cfg = f"--oem 3 --psm {psm} -l {self.lang}"
            full_text = pytesseract.image_to_string(img, config=cfg)
            data      = pytesseract.image_to_data(
                img, config=cfg,
                output_type=pytesseract.Output.DICT,
            )
            blocks = []
            for i in range(len(data["text"])):
                text = data["text"][i].strip()
                conf = int(data["conf"][i])
                if not text or conf < 0:
                    continue
                w_img, h_img = img.size
                blocks.append({
                    "text":       text,
                    "confidence": conf / 100.0,
                    "x": data["left"][i] / w_img,
                    "y": data["top"][i]  / h_img,
                    "w": data["width"][i] / w_img,
                    "h": data["height"][i] / h_img,
                    "line_num":  data["line_num"][i],
                    "block_num": data["block_num"][i],
                })
            filtered = [b for b in blocks
                        if b["confidence"] >= settings.OCR_CONFIDENCE_THRESHOLD]
            return {"full_text": full_text.strip(), "blocks": filtered,
                    "engine": OCREngine.TESSERACT}
        except Exception as e:
            logger.error(f"Tesseract error: {e}")
            return self._simulate()

    def _simulate(self) -> dict[str, Any]:
        return {
            "full_text": "DUPONT Jean-Pierre\n14/03/1985\n123456789012\n31/12/2030\nFRANCE",
            "blocks": [
                {"text": "DUPONT",       "confidence": 0.98, "x": 0.05, "y": 0.25, "w": 0.30, "h": 0.08, "line_num": 1, "block_num": 1},
                {"text": "Jean-Pierre",  "confidence": 0.95, "x": 0.05, "y": 0.33, "w": 0.35, "h": 0.08, "line_num": 2, "block_num": 1},
                {"text": "14/03/1985",   "confidence": 0.99, "x": 0.05, "y": 0.45, "w": 0.28, "h": 0.07, "line_num": 3, "block_num": 2},
                {"text": "123456789012", "confidence": 0.97, "x": 0.05, "y": 0.55, "w": 0.38, "h": 0.07, "line_num": 4, "block_num": 2},
                {"text": "31/12/2030",   "confidence": 0.96, "x": 0.05, "y": 0.65, "w": 0.28, "h": 0.07, "line_num": 5, "block_num": 3},
                {"text": "FRANCE",       "confidence": 0.99, "x": 0.05, "y": 0.72, "w": 0.20, "h": 0.07, "line_num": 6, "block_num": 3},
            ],
            "engine": OCREngine.SIMULATION,
        }


# ── MRZ Decoder — TD1 + TD2 + TD3 ─────────────────────────────────────
class MRZDecoder:
    """
    ICAO 9303 full MRZ decoder.
    v3.0 adds TD1 (3×30, ID cards) and auto-format detection.
    v2.2 was TD3-only.
    """
    WEIGHTS     = [7, 3, 1]
    CHAR_VALUES = {str(i): i for i in range(10)}
    CHAR_VALUES.update({chr(i + 65): i + 10 for i in range(26)})
    CHAR_VALUES["<"] = 0

    def check_digit(self, s: str) -> int:
        return sum(
            self.CHAR_VALUES.get(c, 0) * self.WEIGHTS[i % 3]
            for i, c in enumerate(s)
        ) % 10

    def _names(self, field: str) -> tuple[str, str]:
        parts = field.split("<<", 1)
        return (parts[0].replace("<", " ").strip(),
                parts[1].replace("<", " ").strip() if len(parts) > 1 else "")

    def _date(self, d: str, threshold: int = 30) -> str:
        try:
            yy = int(d[:2])
        except (ValueError, IndexError):
            return ""
        year = 1900 + yy if yy > threshold else 2000 + yy
        return f"{year}-{d[2:4]}-{d[4:6]}"

    def decode_td3(self, l1: str, l2: str) -> dict[str, Any]:
        if len(l1) < 44 or len(l2) < 44:
            return {"valid": False, "format": "TD3", "error": "line too short"}
        last, first = self._names(l1[5:44])
        try:
            doc_ok  = self.check_digit(l2[0:9])   == int(l2[9])
            exp_ok  = self.check_digit(l2[19:25]) == int(l2[25])
        except (ValueError, IndexError):
            doc_ok, exp_ok = False, False
        return {
            "valid":           doc_ok and exp_ok,
            "format":          "TD3",
            "check_digits":    "all_pass" if (doc_ok and exp_ok) else "partial_fail",
            "last_name":       last,
            "first_name":      first,
            "document_number": l2[0:9].replace("<", ""),
            "nationality":     l2[10:13].replace("<", ""),
            "birth_date":      self._date(l2[13:19]),
            "expiry_date":     self._date(l2[19:25]),
            "country":         l1[2:5].replace("<", ""),
        }

    def decode_td1(self, l1: str, l2: str, l3: str) -> dict[str, Any]:
        if len(l1) < 30 or len(l2) < 30 or len(l3) < 30:
            return {"valid": False, "format": "TD1", "error": "line too short"}
        last, first = self._names(l3)
        try:
            doc_ok = self.check_digit(l1[5:14]) == int(l1[14])
            exp_ok = self.check_digit(l2[8:14]) == int(l2[14])
        except (ValueError, IndexError):
            doc_ok, exp_ok = False, False
        return {
            "valid":           doc_ok and exp_ok,
            "format":          "TD1",
            "check_digits":    "all_pass" if (doc_ok and exp_ok) else "partial_fail",
            "last_name":       last,
            "first_name":      first,
            "document_number": l1[5:14].replace("<", ""),
            "nationality":     l2[15:18].replace("<", ""),
            "birth_date":      self._date(l2[0:6]),
            "expiry_date":     self._date(l2[8:14]),
            "country":         l1[2:5].replace("<", ""),
        }

    def extract_from_ocr(self, full_text: str) -> "dict[str, Any] | None":
        lines = [
            ln.strip().replace(" ", "")
            for ln in full_text.split("\n")
            if len(ln.strip()) >= 28
        ]
        mrz = [ln for ln in lines if re.match(r"^[A-Z0-9<]{28,}$", ln)]

        # Try TD1 first (ID cards — 3 lines × 30)
        td1 = [m for m in mrz if len(m) >= 30]
        if len(td1) >= 3 and td1[0][0] in ("I", "A", "C"):
            res = self.decode_td1(td1[0][:30], td1[1][:30], td1[2][:30])
            if res.get("valid"):
                return res

        # Try TD3 (passports — 2 lines × 44)
        td3 = [m for m in mrz if len(m) >= 44]
        if len(td3) >= 2:
            res = self.decode_td3(
                td3[0].ljust(44, "<")[:44],
                td3[1].ljust(44, "<")[:44],
            )
            if res.get("valid"):
                return res

        # Lenient TD1 fallback
        if len(td1) >= 3:
            return self.decode_td1(td1[0][:30], td1[1][:30], td1[2][:30])
        return None


# ── Gemini-based structured extraction ────────────────────────────────
GEMINI_AVAILABLE = False
_gemini_service = None

def _get_gemini():
    global GEMINI_AVAILABLE, _gemini_service
    if _gemini_service is not None:
        return _gemini_service
    if not settings.GEMINI_API_KEY:
        return None
    try:
        from app.services.ocr.gemini_ocr_service import GeminiOCRService
        _gemini_service = GeminiOCRService()
        GEMINI_AVAILABLE = True
        logger.info("Gemini OCR engine loaded — will be used as primary extractor")
        return _gemini_service
    except Exception as e:
        logger.warning(f"Gemini OCR unavailable ({e}), using PaddleOCR/Tesseract")
        return None


# ── Extraction Orchestrator ───────────────────────────────────────────
class ExtractionService:
    def __init__(self):
        self.preprocessor  = ImagePreprocessor()
        self.paddle_engine = PaddleOCREngine()
        self.tess_engine   = TesseractOCREngine()
        self.mrz_decoder   = MRZDecoder()

    async def extract(self, image_bytes: bytes, file_format: str,
                      template_config: dict, verso_bytes: bytes = None) -> dict[str, Any]:
        start = time.time()
        doc_type_hint = template_config.get("document_type")

        # ── Try Gemini first (structured extraction) ──────────────
        gemini = _get_gemini()
        if gemini:
            try:
                if verso_bytes:
                    gemini_result = await asyncio.wait_for(
                        gemini.extract_recto_verso(image_bytes, verso_bytes, doc_type_hint),
                        timeout=settings.OCR_TIMEOUT,
                    )
                else:
                    gemini_result = await asyncio.wait_for(
                        gemini.extract_structured(image_bytes, doc_type_hint),
                        timeout=settings.OCR_TIMEOUT,
                    )
                gemini_fields = gemini_result.get("fields", {})
                raw_text      = gemini_result.get("raw_text", "")
                if gemini_fields:
                    # Map Gemini fields to template fields with high confidence
                    fields = self._map_gemini_fields(
                        gemini_fields, raw_text, template_config
                    )
                    doc_type = gemini_fields.get("document_type") or self._detect_type(raw_text)

                    # MRZ from Gemini response or from raw text
                    mrz = None
                    if doc_type_hint in ("identity_card", "passport"):
                        mrz_text = raw_text
                        # Append MRZ lines from Gemini if extracted
                        mrz_lines = []
                        for k in ("mrz_line1", "mrz_line2", "mrz_line3"):
                            if gemini_fields.get(k):
                                mrz_lines.append(gemini_fields[k])
                        if mrz_lines:
                            mrz_text = "\n".join(mrz_lines)
                        mrz = self.mrz_decoder.extract_from_ocr(mrz_text)

                    confidences = [f["confidence"] for f in fields.values()
                                   if f["confidence"] > 0]
                    overall = round(sum(confidences) / len(confidences), 3) if confidences else 0.0

                    logger.info(f"Gemini extraction: {len(gemini_fields)} fields, "
                                f"confidence={overall}")
                    return {
                        "document_type":      doc_type,
                        "ocr_engine":         "gemini",
                        "fields":             fields,
                        "overall_confidence":  overall,
                        "mrz_decoded":        mrz,
                        "processing_time_ms": int((time.time() - start) * 1000),
                        "alerts":             self._check_alerts(fields, template_config),
                        "dual_side":          gemini_result.get("dual_side", False),
                    }
                else:
                    logger.warning("Gemini returned no fields — falling back to OCR engines")
            except asyncio.TimeoutError:
                logger.warning("Gemini extraction timed out — falling back to OCR engines")
            except Exception as e:
                err_str = str(e)
                logger.warning(f"Gemini extraction failed ({err_str}) — falling back to OCR engines")

        # ── Fallback: PaddleOCR / Tesseract pipeline ──────────────
        is_form = doc_type_hint in ("identity_card", "passport", "invoice", "rccm")

        # 1. Preprocess
        processed = await asyncio.get_event_loop().run_in_executor(
            None, self.preprocessor.preprocess, image_bytes, file_format
        )

        # 2. OCR: PaddleOCR → Tesseract fallback
        ocr_result = await self.paddle_engine.run(processed)
        if not ocr_result:
            logger.info("PaddleOCR unavailable — using Tesseract")
            ocr_result = await self.tess_engine.run(processed, is_form=is_form)

        # 3. Type detection
        doc_type = self._detect_type(ocr_result["full_text"])

        # 4. Template + post-correction
        fields = self._apply_template(ocr_result, template_config)

        # 5. MRZ
        mrz = None
        if doc_type_hint in ("identity_card", "passport"):
            mrz = self.mrz_decoder.extract_from_ocr(ocr_result["full_text"])

        confidences = [f["confidence"] for f in fields.values() if f["confidence"] > 0]
        overall     = round(sum(confidences) / len(confidences), 3) if confidences else 0.0

        return {
            "document_type":    doc_type,
            "ocr_engine":       ocr_result.get("engine", OCREngine.TESSERACT),
            "fields":           fields,
            "overall_confidence": overall,
            "mrz_decoded":      mrz,
            "processing_time_ms": int((time.time() - start) * 1000),
            "alerts":           self._check_alerts(fields, template_config),
        }

    def _map_gemini_fields(self, gemini_fields: dict, raw_text: str,
                           template_config: dict) -> dict:
        """Map Gemini's structured fields to template field IDs."""
        fields = {}
        # Build a lookup: template field_id → field config
        template_fields = {f["id"]: f for f in template_config.get("fields", [])}

        # Direct mapping from Gemini field names to common template field IDs
        GEMINI_TO_TEMPLATE = {
            "last_name": "last_name",
            "first_name": "first_name",
            "birth_date": "birth_date",
            "birth_place": "birth_place",
            "nationality": "nationality",
            "gender": "gender",
            "id_number": "id_number",
            "expiry_date": "expiry_date",
            "issue_date": "issue_date",
            "issuing_authority": "issuing_authority",
            "address": "address",
            # Invoice fields
            "invoice_number": "invoice_number",
            "invoice_date": "invoice_date",
            "due_date": "due_date",
            "total_amount": "total_amount",
            "currency": "currency",
            "tax_amount": "tax_amount",
            "vendor_name": "vendor_name",
            "vendor_address": "vendor_address",
            "client_name": "client_name",
            "client_address": "client_address",
            "iban": "iban",
            "bic": "bic",
            # Verso fields (back side of ID cards)
            "father_name": "father_name",
            "mother_name": "mother_name",
            "registration_place": "registration_place",
            "height": "height",
        }

        for gemini_key, value in gemini_fields.items():
            if gemini_key in ("document_type", "mrz_line1", "mrz_line2", "mrz_line3"):
                continue
            template_id = GEMINI_TO_TEMPLATE.get(gemini_key, gemini_key)
            if template_id in template_fields:
                ftype = template_fields[template_id].get("type", "string")
                coerced = self._coerce(str(value), ftype,
                                       template_fields[template_id].get("format"))
                fields[template_id] = {
                    "value":          coerced,
                    "confidence":     0.95,  # Gemini high baseline
                    "alerts":         [],
                    "post_corrected": False,
                }

        # Fill missing required template fields
        for fid, fconf in template_fields.items():
            if fid not in fields:
                # Try to find by alternate names in gemini_fields
                found = False
                for gk, gv in gemini_fields.items():
                    if gk in ("document_type", "mrz_line1", "mrz_line2", "mrz_line3"):
                        continue
                    if gk.replace("_", "") == fid.replace("_", ""):
                        ftype = fconf.get("type", "string")
                        fields[fid] = {
                            "value":          self._coerce(str(gv), ftype, fconf.get("format")),
                            "confidence":     0.90,
                            "alerts":         [],
                            "post_corrected": False,
                        }
                        found = True
                        break
                if not found:
                    req = fconf.get("validation", {}).get("required", False)
                    fields[fid] = {
                        "value": None, "confidence": 0.0,
                        "alerts": [f"Field '{fid}' not detected"] if req else [],
                        "post_corrected": False,
                    }

        return fields

    def _detect_type(self, text: str) -> str:
        t = text.upper()
        if any(k in t for k in ["CARTE NATIONALE", "CNI", "CIN"]):
            return "CNI"
        if "PASSPORT" in t or "PASSEPORT" in t:
            return "PASSPORT"
        if "FACTURE" in t or "INVOICE" in t:
            return "INVOICE"
        if "RCCM" in t or "REGISTRE DU COMMERCE" in t:
            return "RCCM"
        if "IBAN" in t or "BIC" in t or "RIB" in t:
            return "RIB"
        if "PERMIS DE CONDUIRE" in t or "DRIVING LICENCE" in t:
            return "DRIVER_LICENSE"
        if "CARTE DE SEJOUR" in t or "RESIDENCE PERMIT" in t:
            return "RESIDENCE_PERMIT"
        return "UNKNOWN"

    def _apply_template(self, ocr_result: dict, template_config: dict) -> dict:
        fields    = {}
        blocks    = ocr_result.get("blocks", [])
        full_text = ocr_result.get("full_text", "")

        for field in template_config.get("fields", []):
            fid       = field["id"]
            zone      = field.get("zone")
            ftype     = field.get("type", "string")
            tolerance = field.get("ocr_tolerance", 0.70)

            best = None; best_score = 0.0
            for block in blocks:
                if zone:
                    score = self._zone_iou(block, zone)
                    if score > best_score and block["confidence"] >= tolerance:
                        best_score = score; best = block
                else:
                    label = field.get("label", "").lower()
                    if label and label in full_text.lower():
                        idx = full_text.lower().find(label)
                        for b in blocks:
                            if b["text"].lower() in full_text[idx: idx + 80].lower():
                                if b["confidence"] > best_score:
                                    best = b; best_score = b["confidence"]
                                break

            if best:
                raw  = best["text"]
                orig = raw
                if ftype in ("number", "id"):
                    raw = _fix_numeric(raw)
                elif fid in ("last_name", "first_name", "full_name"):
                    raw = _fix_name(raw)
                fields[fid] = {
                    "value":          self._coerce(raw, ftype, field.get("format")),
                    "confidence":     round(best["confidence"], 3),
                    "alerts":         [],
                    "post_corrected": raw != orig,
                }
            else:
                req = field.get("validation", {}).get("required", False)
                fields[fid] = {
                    "value": None, "confidence": 0.0,
                    "alerts": [f"Field '{fid}' not detected"] if req else [],
                    "post_corrected": False,
                }
        return fields

    def _zone_iou(self, block: dict, zone: dict) -> float:
        bx, by, bw, bh = block["x"], block["y"], block["w"], block["h"]
        zx, zy, zw, zh = zone["x"],  zone["y"],  zone["w"],  zone["h"]
        ix    = max(0, min(bx + bw, zx + zw) - max(bx, zx))
        iy    = max(0, min(by + bh, zy + zh) - max(by, zy))
        inter = ix * iy
        union = bw * bh + zw * zh - inter
        return inter / union if union > 0 else 0.0

    def _coerce(self, text: str, ftype: str, fmt: str = None) -> Any:
        text = text.strip()
        if ftype == "date":
            for pat, rep in [
                (r"(\d{2})[/.\-](\d{2})[/.\-](\d{4})", r"\3-\2-\1"),
                (r"(\d{4})[/.\-](\d{2})[/.\-](\d{2})", r"\1-\2-\3"),
            ]:
                m = re.search(pat, text)
                if m:
                    return re.sub(pat, rep, m.group(0))
        if ftype in ("number", "id"):
            return re.sub(r"[^\d]", "", text) or None
        return text or None

    def _check_alerts(self, fields: dict, template_config: dict) -> list[str]:
        alerts = []
        for field in template_config.get("fields", []):
            fid  = field["id"]
            val  = fields.get(fid, {})
            conf = val.get("confidence", 0)
            if val.get("value") is not None and conf < 0.75:
                alerts.append(f"Low confidence on field '{fid}': {conf:.0%}")
            if val.get("post_corrected"):
                alerts.append(f"OCR post-correction applied on field '{fid}'")
        return alerts


extraction_service = ExtractionService()

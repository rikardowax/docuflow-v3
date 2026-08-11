"""
DocuFlow - Validation & Fuzzy Matching
Multi-algorithm fuzzy comparison with confidence scoring.
"""
import re
import asyncio
import unicodedata
from datetime import datetime, date
from typing import Any
from app.core.logging import get_logger

logger = get_logger(__name__)

try:
    from rapidfuzz import fuzz, distance
    RAPIDFUZZ_AVAILABLE = True
except ImportError:
    RAPIDFUZZ_AVAILABLE = False
    logger.warning("rapidfuzz not available — using pure Python fallback")


def normalize(s: str) -> str:
    """Normalize for comparison: lowercase, strip accents, remove extra spaces."""
    s = s.strip().lower()
    s = unicodedata.normalize("NFD", s)
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    s = re.sub(r"\s+", " ", s)
    return s


class FuzzyMatcher:
    """Multi-algorithm fuzzy comparison with automatic best-score selection."""

    def levenshtein(self, s1: str, s2: str) -> float:
        if RAPIDFUZZ_AVAILABLE:
            return distance.Levenshtein.normalized_similarity(s1, s2)
        s1, s2 = s1.lower(), s2.lower()
        if s1 == s2: return 1.0
        if not s1 or not s2: return 0.0
        m, n = len(s1), len(s2)
        dp = list(range(n + 1))
        for i in range(1, m + 1):
            prev = dp[0]
            dp[0] = i
            for j in range(1, n + 1):
                temp = dp[j]
                dp[j] = (prev if s1[i-1] == s2[j-1]
                          else 1 + min(prev, dp[j], dp[j-1]))
                prev = temp
        return 1.0 - dp[n] / max(m, n)

    def jaro_winkler(self, s1: str, s2: str) -> float:
        if RAPIDFUZZ_AVAILABLE:
            return fuzz.WRatio(s1, s2, processor=normalize) / 100.0
        s1, s2 = normalize(s1), normalize(s2)
        if s1 == s2: return 1.0
        if not s1 or not s2: return 0.0
        l1, l2 = len(s1), len(s2)
        match_d = max(l1, l2) // 2 - 1
        s1m = [False] * l1; s2m = [False] * l2
        matches = 0
        for i in range(l1):
            for j in range(max(0, i - match_d), min(l2, i + match_d + 1)):
                if not s2m[j] and s1[i] == s2[j]:
                    s1m[i] = s2m[j] = True; matches += 1; break
        if not matches: return 0.0
        t = 0; k = 0
        for i in range(l1):
            if s1m[i]:
                while not s2m[k]: k += 1
                if s1[i] != s2[k]: t += 1
                k += 1
        jaro = (matches/l1 + matches/l2 + (matches - t/2)/matches) / 3
        prefix = sum(1 for i in range(min(4, l1, l2)) if s1[i] == s2[i])
        return jaro + prefix * 0.1 * (1 - jaro)

    def ngram(self, s1: str, s2: str, n: int = 2) -> float:
        if RAPIDFUZZ_AVAILABLE:
            return fuzz.partial_ratio(s1, s2, processor=normalize) / 100.0
        s1, s2 = normalize(s1), normalize(s2)
        if s1 == s2: return 1.0
        if len(s1) < n or len(s2) < n: return 1.0 if s1 == s2 else 0.0
        g1 = set(s1[i:i+n] for i in range(len(s1)-n+1))
        g2 = set(s2[i:i+n] for i in range(len(s2)-n+1))
        inter = len(g1 & g2)
        return 2 * inter / (len(g1) + len(g2))

    def phonetic(self, s1: str, s2: str) -> float:
        """Simple phonetic normalization (common substitutions)."""
        def ph(s: str) -> str:
            s = normalize(s)
            for a, b in [("ph", "f"), ("ou", "u"), ("eau", "o"), ("au", "o"),
                          ("ai", "e"), ("ei", "e"), ("oi", "ua"), ("qu", "k"),
                          ("ch", "sh"), ("th", "t"), ("ck", "k")]:
                s = s.replace(a, b)
            return re.sub(r"(.)\1+", r"\1", s)
        p1, p2 = ph(s1), ph(s2)
        return self.levenshtein(p1, p2)

    def best_score(self, s1: str, s2: str, ftype: str = "string") -> tuple[float, str]:
        s1n, s2n = normalize(str(s1)), normalize(str(s2))
        if s1n == s2n:
            return 1.0, "exact"
        if ftype in ("date", "number"):
            score = self.levenshtein(s1n, s2n)
            return round(score, 4), "levenshtein"
        scores = {
            "jaro_winkler": self.jaro_winkler(s1n, s2n),
            "levenshtein":  self.levenshtein(s1n, s2n),
            "ngram":        self.ngram(s1n, s2n),
            "phonetic":     self.phonetic(s1n, s2n),
        }
        best = max(scores, key=scores.get)
        return round(scores[best], 4), best


class ValidationService:
    DATE_FORMATS = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%Y%m%d", "%d.%m.%Y"]

    def _parse_date(self, value: str) -> date | None:
        for fmt in self.DATE_FORMATS:
            try:
                return datetime.strptime(str(value).strip(), fmt).date()
            except (ValueError, TypeError):
                continue
        return None

    def validate_field(self, fid: str, value: Any, rules: dict) -> list[dict]:
        errors = []
        if value is None or str(value).strip() == "":
            if rules.get("required"):
                errors.append({"field": fid, "message": f"'{fid}' is required", "severity": "error"})
            return errors
        sv = str(value).strip()
        if "min_length" in rules and len(sv) < rules["min_length"]:
            errors.append({"field": fid, "message": f"'{fid}' too short (min {rules['min_length']})", "severity": "error"})
        if "max_length" in rules and len(sv) > rules["max_length"]:
            errors.append({"field": fid, "message": f"'{fid}' too long (max {rules['max_length']})", "severity": "error"})
        if "regex" in rules and not re.fullmatch(rules["regex"], sv):
            errors.append({"field": fid, "message": f"'{fid}' format invalid", "severity": "error"})
        if "min_age" in rules:
            dob = self._parse_date(sv)
            if dob:
                age = (date.today() - dob).days // 365
                if age < rules["min_age"]:
                    errors.append({"field": fid, "message": f"'{fid}' age {age} below minimum {rules['min_age']}", "severity": "error"})
        if rules.get("not_future"):
            d = self._parse_date(sv)
            if d and d > date.today():
                errors.append({"field": fid, "message": f"'{fid}' cannot be a future date", "severity": "error"})
        if rules.get("not_past"):
            d = self._parse_date(sv)
            if d and d < date.today():
                errors.append({"field": fid, "message": f"'{fid}' has expired", "severity": "warning"})
        return errors

    async def validate(self, fields: dict, template_config: dict) -> dict[str, Any]:
        await asyncio.sleep(0)
        errors, warnings = [], []
        rules_checked = 0
        for fc in template_config.get("fields", []):
            rules = fc.get("validation", {})
            if not rules: continue
            rules_checked += 1
            fdata = fields.get(fc["id"], {})
            value = fdata.get("value") if isinstance(fdata, dict) else fdata
            for e in self.validate_field(fc["id"], value, rules):
                (errors if e["severity"] == "error" else warnings).append(e)
        # Cross-field: birth_date vs expiry_date
        bd = fields.get("birth_date", {}).get("value")
        ed = fields.get("expiry_date", {}).get("value")
        if bd and ed:
            d1, d2 = self._parse_date(str(bd)), self._parse_date(str(ed))
            if d1 and d2 and d1 >= d2:
                errors.append({"field": "cross_check", "message": "birth_date must be before expiry_date", "severity": "error"})
        return {
            "passed": len(errors) == 0,
            "rules_checked": rules_checked,
            "rules_failed": len(errors),
            "errors": errors,
            "warnings": warnings,
        }


class FuzzyService:
    FIELD_THRESHOLDS = {
        "last_name": 0.90, "first_name": 0.90, "full_name": 0.88,
        "birth_date": 1.00, "expiry_date": 0.98, "issue_date": 0.98,
        "id_number": 0.95, "passport_number": 0.95, "doc_number": 0.95,
        "nationality": 0.85, "country": 0.85,
    }
    FIELD_TYPES = {
        "birth_date": "date", "expiry_date": "date", "issue_date": "date",
        "id_number": "number", "passport_number": "number",
    }

    def __init__(self):
        self.matcher = FuzzyMatcher()

    async def compare(self, extracted: dict, reference: dict,
                      template_config: dict) -> dict[str, Any]:
        await asyncio.sleep(0)
        results = {}
        scores = []
        for fid, ref_val in reference.items():
            ext = extracted.get(fid, {})
            ext_val = ext.get("value") if isinstance(ext, dict) else ext
            if ext_val is None or ref_val is None:
                continue
            ftype  = self.FIELD_TYPES.get(fid, "string")
            score, algo = self.matcher.best_score(str(ext_val), str(ref_val), ftype)
            thresh = self.FIELD_THRESHOLDS.get(fid, 0.90)
            decision = "MATCH" if score >= thresh else ("REVIEW" if score >= thresh * 0.85 else "MISMATCH")
            results[fid] = {
                "extracted": str(ext_val),
                "reference": str(ref_val),
                "score": score,
                "algorithm": algo,
                "threshold": thresh,
                "decision": decision,
            }
            scores.append(score)
        global_score = round(sum(scores) / len(scores), 4) if scores else 1.0
        decisions = [r["decision"] for r in results.values()]
        if "MISMATCH" in decisions:
            overall = "REJECTED"
        elif "REVIEW" in decisions:
            overall = "REVIEW"
        else:
            overall = "VALIDATED"
        return {"fields": results, "global_score": global_score, "overall": overall}


validation_service = ValidationService()
fuzzy_service = FuzzyService()

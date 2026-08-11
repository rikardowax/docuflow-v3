"""
DocuFlow - Multi-Factor Authentication (TOTP)
RFC 6238 TOTP for admin and sensitive accounts.
"""
import base64
import hashlib
import hmac
import os
import struct
import time
from typing import Optional
from app.core.logging import get_logger

logger = get_logger(__name__)

try:
    import pyotp
    import qrcode
    import io
    TOTP_AVAILABLE = True
except ImportError:
    TOTP_AVAILABLE = False
    logger.warning("pyotp/qrcode not installed — MFA disabled")


class TOTPService:
    """TOTP-based MFA implementation."""

    ISSUER = "DocuFlow Platform"
    DIGITS = 6
    INTERVAL = 30   # seconds
    VALID_WINDOW = 1  # allow 1 step before/after for clock skew

    def generate_secret(self) -> str:
        """Generate a new TOTP secret (base32 encoded)."""
        if TOTP_AVAILABLE:
            return pyotp.random_base32()
        # Fallback: manual base32 generation
        return base64.b32encode(os.urandom(20)).decode("ascii")

    def get_provisioning_uri(self, secret: str, account_name: str) -> str:
        """Generate otpauth:// URI for QR code display."""
        if TOTP_AVAILABLE:
            totp = pyotp.TOTP(secret)
            return totp.provisioning_uri(
                name=account_name,
                issuer_name=self.ISSUER
            )
        # Manual URI construction
        return (
            f"otpauth://totp/{self.ISSUER}:{account_name}"
            f"?secret={secret}&issuer={self.ISSUER}"
            f"&digits={self.DIGITS}&period={self.INTERVAL}"
        )

    def generate_qr_code_base64(self, secret: str, account_name: str) -> Optional[str]:
        """Generate QR code as base64 PNG for embedding in HTML."""
        if not TOTP_AVAILABLE:
            return None
        try:
            uri = self.get_provisioning_uri(secret, account_name)
            qr = qrcode.QRCode(version=1, box_size=6, border=2)
            qr.add_data(uri)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return base64.b64encode(buf.getvalue()).decode("ascii")
        except Exception as e:
            logger.error(f"QR code generation failed: {e}")
            return None

    def verify(self, secret: str, code: str, window: int = None) -> bool:
        """
        Verify a TOTP code.
        Returns True if valid within the time window.
        """
        window = window or self.VALID_WINDOW
        code = code.strip().replace(" ", "")
        if not code.isdigit() or len(code) != self.DIGITS:
            return False
        if TOTP_AVAILABLE:
            totp = pyotp.TOTP(secret)
            return totp.verify(code, valid_window=window)
        # Manual TOTP verification
        return self._verify_manual(secret, code, window)

    def _verify_manual(self, secret: str, code: str, window: int) -> bool:
        """RFC 6238 TOTP verification without pyotp."""
        try:
            key = base64.b32decode(secret.upper())
            now = int(time.time()) // self.INTERVAL
            for offset in range(-window, window + 1):
                counter = struct.pack(">Q", now + offset)
                h = hmac.new(key, counter, hashlib.sha1).digest()
                offset_byte = h[-1] & 0x0F
                truncated = struct.unpack(">I", h[offset_byte:offset_byte + 4])[0] & 0x7FFFFFFF
                expected = str(truncated % (10 ** self.DIGITS)).zfill(self.DIGITS)
                if hmac.compare_digest(code, expected):
                    return True
        except Exception as e:
            logger.error(f"Manual TOTP verification error: {e}")
        return False

    def get_current_code(self, secret: str) -> str:
        """Get current TOTP code (for testing only)."""
        if TOTP_AVAILABLE:
            return pyotp.TOTP(secret).now()
        key = base64.b32decode(secret.upper())
        counter = struct.pack(">Q", int(time.time()) // self.INTERVAL)
        h = hmac.new(key, counter, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        truncated = struct.unpack(">I", h[offset:offset + 4])[0] & 0x7FFFFFFF
        return str(truncated % 1_000_000).zfill(6)


totp_service = TOTPService()

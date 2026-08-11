"""
DocuFlow - Security: RS256 JWT + RBAC + API Key hashing
"""
import hashlib
import hmac
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from enum import Enum

import bcrypt as _bcrypt

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import JWTError, jwt

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)
security = HTTPBearer(auto_error=False)


class Permission(str, Enum):
    PROCESS_READ   = "process:read"
    PROCESS_WRITE  = "process:write"
    BATCH_WRITE    = "batch:write"
    TEMPLATE_READ  = "template:read"
    TEMPLATE_WRITE = "template:write"
    STATS_READ     = "stats:read"
    ADMIN          = "admin"


ROLE_PERMISSIONS = {
    "viewer":    [Permission.PROCESS_READ, Permission.TEMPLATE_READ, Permission.STATS_READ],
    "operator":  [Permission.PROCESS_READ, Permission.PROCESS_WRITE, Permission.BATCH_WRITE,
                  Permission.TEMPLATE_READ, Permission.STATS_READ],
    "admin":     list(Permission),
    "api_client":[Permission.PROCESS_READ, Permission.PROCESS_WRITE, Permission.BATCH_WRITE,
                  Permission.TEMPLATE_READ, Permission.TEMPLATE_WRITE, Permission.STATS_READ],
}


def hash_password(password: str) -> str:
    rounds = getattr(settings, 'BCRYPT_ROUNDS', 12)
    return _bcrypt.hashpw(password.encode('utf-8'), _bcrypt.gensalt(rounds=rounds)).decode('utf-8')


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return _bcrypt.checkpw(plain.encode('utf-8'), hashed.encode('utf-8'))
    except Exception:
        return False


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key() -> tuple[str, str]:
    """Returns (raw_key, hashed_key)."""
    key = f"df_{secrets.token_urlsafe(settings.API_KEY_LENGTH)}"
    return key, hash_api_key(key)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    payload = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload.update({"exp": expire, "iat": datetime.now(timezone.utc), "type": "access"})

    if settings.JWT_PRIVATE_KEY_PATH:
        with open(settings.JWT_PRIVATE_KEY_PATH) as f:
            private_key = f.read()
        return jwt.encode(payload, private_key, algorithm="RS256")
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    if settings.JWT_PUBLIC_KEY_PATH:
        with open(settings.JWT_PUBLIC_KEY_PATH) as f:
            public_key = f.read()
        return jwt.decode(token, public_key, algorithms=["RS256"])
    return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])


class CurrentUser:
    def __init__(self, client_id: str, role: str = "api_client", scopes: list = None):
        self.client_id = client_id
        self.role = role
        self.scopes = scopes or ROLE_PERMISSIONS.get(role, [])

    def has_permission(self, permission: Permission) -> bool:
        return permission in self.scopes or Permission.ADMIN in self.scopes


def _extract_token(credentials: Optional[HTTPAuthorizationCredentials]) -> str:
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Authorization header missing",
                            headers={"WWW-Authenticate": "Bearer"})
    return credentials.credentials


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> CurrentUser:
    token = _extract_token(credentials)
    try:
        payload = decode_token(token)
        client_id: str = payload.get("sub")
        role: str = payload.get("role", "api_client")
        scopes: list = payload.get("scopes", [])
        if not client_id:
            raise HTTPException(status_code=401, detail="Invalid token payload")
        return CurrentUser(client_id=client_id, role=role, scopes=scopes)
    except JWTError as e:
        logger.warning(f"JWT validation failed: {e}")
        raise HTTPException(status_code=401, detail="Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})


def require_permission(permission: Permission):
    async def checker(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not user.has_permission(permission):
            raise HTTPException(status_code=403,
                                detail=f"Permission '{permission}' required")
        return user
    return checker


def sign_webhook_payload(payload: str) -> str:
    """HMAC-SHA256 signature for webhook delivery."""
    secret = settings.WEBHOOK_SECRET or settings.SECRET_KEY
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

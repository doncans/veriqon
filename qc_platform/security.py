import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from qc_platform.database import get_session
from qc_platform.models import APIKey


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    api_key_id: str
    key_name: str
    role: str


def generate_api_key() -> str:
    return f"qcp_{secrets.token_urlsafe(32)}"


def hash_api_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_principal(
    _request: Request,
    authorization: str | None = Header(default=None),
    session: Session = Depends(get_session),
) -> Principal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer API key required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = authorization.removeprefix("Bearer ").strip()
    if not token or len(token) > 256:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    api_key = session.scalar(select(APIKey).where(APIKey.key_hash == hash_api_key(token)))
    expires_at = api_key.expires_at if api_key is not None else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    if (
        api_key is None
        or api_key.revoked_at is not None
        or (expires_at is not None and expires_at <= now)
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key")
    return Principal(
        tenant_id=api_key.tenant_id,
        api_key_id=api_key.id,
        key_name=api_key.name,
        role=api_key.role,
    )


def require_roles(*roles: str):
    def dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if principal.role not in roles:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient role")
        return principal

    return dependency

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from qc_platform.config import API_KEY_TTL_DAYS
from qc_platform.database import get_session
from qc_platform.models import APIKey, AuditEvent, utc_now
from qc_platform.schemas import APIKeyCreate, APIKeyRead
from qc_platform.security import Principal, generate_api_key, hash_api_key, require_roles

router = APIRouter(tags=["authentication"])


@router.post("/api-keys", response_model=APIKeyRead, status_code=status.HTTP_201_CREATED)
def create_api_key(
    body: APIKeyCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    token = generate_api_key()
    expires_at = utc_now() + timedelta(days=API_KEY_TTL_DAYS)
    api_key = APIKey(
        tenant_id=principal.tenant_id,
        name=body.name,
        role=body.role,
        key_hash=hash_api_key(token),
        expires_at=expires_at,
    )
    session.add(api_key)
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="api_key_created",
            details={"name": body.name, "role": body.role},
        )
    )
    session.commit()
    return APIKeyRead(
        id=api_key.id,
        name=api_key.name,
        role=api_key.role,
        token=token,
        expires_at=expires_at,
    )


@router.delete("/api-keys/{key_id}", status_code=status.HTTP_204_NO_CONTENT)
def revoke_api_key(
    key_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    api_key = session.scalar(
        select(APIKey).where(APIKey.id == key_id, APIKey.tenant_id == principal.tenant_id)
    )
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if api_key.id == principal.api_key_id:
        raise HTTPException(status_code=400, detail="Cannot revoke the key used for this request")
    api_key.revoked_at = utc_now()
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="api_key_revoked",
            details={"api_key_id": api_key.id, "name": api_key.name},
        )
    )
    session.commit()

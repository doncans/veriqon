from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from qc_platform.database import get_session

router = APIRouter(tags=["system"])


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/ready")
def readiness(session: Session = Depends(get_session)):
    session.execute(select(1))
    return {"status": "ready", "database": "connected"}

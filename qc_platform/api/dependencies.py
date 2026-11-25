from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from qc_platform.models import Evidence, QCCase, Workflow
from qc_platform.qc import latest_checks
from qc_platform.schemas import CaseRead, CheckRead, EvidenceRead


def get_case(session: Session, tenant_id: str, case_id: str) -> QCCase:
    case = session.scalar(
        select(QCCase).where(QCCase.id == case_id, QCCase.tenant_id == tenant_id)
    )
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


def get_case_for_update(session: Session, tenant_id: str, case_id: str) -> QCCase:
    case = session.scalar(
        select(QCCase)
        .where(QCCase.id == case_id, QCCase.tenant_id == tenant_id)
        .with_for_update()
    )
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


def get_workflow(session: Session, tenant_id: str, workflow_id: str) -> Workflow:
    workflow = session.scalar(
        select(Workflow).where(Workflow.id == workflow_id, Workflow.tenant_id == tenant_id)
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


def serialize_case(session: Session, case: QCCase, tenant_id: str) -> CaseRead:
    workflow = get_workflow(session, tenant_id, case.workflow_id)
    checks = latest_checks(session, case.id, tenant_id)
    evidence = session.scalars(
        select(Evidence)
        .where(Evidence.case_id == case.id, Evidence.tenant_id == tenant_id)
        .order_by(Evidence.created_at.asc(), Evidence.id.asc())
    ).all()
    return CaseRead(
        id=case.id,
        workflow_id=workflow.id,
        workflow_slug=workflow.slug,
        workflow_version=workflow.version,
        external_reference=case.external_reference,
        source_system=case.source_system,
        data=case.data,
        status=case.status,
        created_at=case.created_at,
        updated_at=case.updated_at,
        checks=[CheckRead.model_validate(item, from_attributes=True) for item in checks],
        evidence=[EvidenceRead.model_validate(item, from_attributes=True) for item in evidence],
    )

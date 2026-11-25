import hashlib
from pathlib import PurePath

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from qc_platform.api.dependencies import (
    get_case,
    get_case_for_update,
    get_workflow,
    serialize_case,
)
from qc_platform.config import DOCUMENT_INGESTION_ENABLED
from qc_platform.database import get_session
from qc_platform.document_intake import MAX_DOCUMENT_BYTES, parse_source
from qc_platform.models import AgentRun, AuditEvent, Evidence, QCCase, Workflow
from qc_platform.qc import latest_checks, run_checks
from qc_platform.schemas import (
    AgentRecommendation,
    AgentRunRead,
    CaseIntakeCreate,
    CaseCreate,
    CaseRead,
    EvidenceCreate,
    ReviewCreate,
    AuditRead,
)
from qc_platform.security import Principal, get_principal, require_roles

router = APIRouter(tags=["cases"])


@router.get("/cases", response_model=list[CaseRead])
def list_cases(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    case_status: str | None = Query(
        default=None,
        alias="status",
        pattern=r"^(in_review|needs_information|approved|rejected)$",
    ),
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
):
    query = select(QCCase).where(QCCase.tenant_id == principal.tenant_id)
    if case_status:
        query = query.where(QCCase.status == case_status)
    cases = session.scalars(
        query
        .order_by(QCCase.created_at.desc(), QCCase.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [serialize_case(session, case, principal.tenant_id) for case in cases]


@router.post("/cases", response_model=CaseRead, status_code=status.HTTP_201_CREATED)
def create_case(
    body: CaseCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "operator")),
):
    case = _create_case(
        session,
        principal,
        body.workflow_slug,
        body.external_reference,
        body.source_system,
        body.data,
    )
    session.refresh(case)
    return serialize_case(session, case, principal.tenant_id)


def _create_case(
    session: Session,
    principal: Principal,
    workflow_slug: str,
    external_reference: str | None,
    source_system: str,
    data: dict,
    evidence_rows: list[Evidence] | None = None,
) -> QCCase:
    workflow = session.scalar(
        select(Workflow)
        .where(
            Workflow.tenant_id == principal.tenant_id,
            Workflow.slug == workflow_slug,
            Workflow.active.is_(True),
        )
        .order_by(Workflow.version.desc())
    )
    if workflow is None:
        raise HTTPException(status_code=404, detail="Active workflow not found")
    case = QCCase(
        tenant_id=principal.tenant_id,
        workflow_id=workflow.id,
        external_reference=external_reference,
        source_system=source_system,
        data=data,
    )
    session.add(case)
    session.flush()
    for evidence in evidence_rows or []:
        evidence.case_id = case.id
        session.add(evidence)
        session.add(
            AuditEvent(
                tenant_id=principal.tenant_id,
                case_id=case.id,
                actor_api_key_id=principal.api_key_id,
                actor_role=principal.role,
                action="intake_document_parsed",
                details={
                    "source_id": evidence.attributes["source_id"],
                    "filename": evidence.attributes["filename"],
                    "format": evidence.attributes["format"],
                    "sha256": evidence.sha256,
                    "extracted_field_count": evidence.attributes["extracted_field_count"],
                },
            )
        )
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            case_id=case.id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="case_created",
            details={
                "workflow_slug": workflow.slug,
                "workflow_version": workflow.version,
                "intake_sources": [
                    evidence.attributes["source_id"]
                    for evidence in evidence_rows or []
                ],
            },
        )
    )
    run_checks(session, case, workflow, principal)
    session.commit()
    return case


@router.post("/cases/intake", response_model=CaseRead, status_code=status.HTTP_201_CREATED)
async def intake_case(
    manifest: str = Form(...),
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "operator")),
):
    if not DOCUMENT_INGESTION_ENABLED:
        raise HTTPException(status_code=503, detail="Document intake is disabled in this environment")
    try:
        intake = CaseIntakeCreate.model_validate_json(manifest)
    except ValidationError as error:
        raise HTTPException(status_code=422, detail="Invalid cross-platform intake manifest") from error

    files_by_name = {}
    for upload in files:
        filename = upload.filename
        if not filename or "/" in filename or "\\" in filename:
            raise HTTPException(status_code=422, detail="Uploaded files must have simple filenames")
        if filename in files_by_name:
            raise HTTPException(status_code=422, detail="Uploaded filenames must be unique")
        content = await upload.read(MAX_DOCUMENT_BYTES + 1)
        if len(content) > MAX_DOCUMENT_BYTES:
            raise HTTPException(status_code=413, detail=f"{filename}: file exceeds the per-file size limit")
        files_by_name[filename] = content
        await upload.close()

    expected_names = {source.filename for source in intake.sources}
    if set(files_by_name) != expected_names:
        raise HTTPException(
            status_code=422,
            detail="Uploaded files must match the filenames declared in the intake manifest",
        )

    data: dict = {}
    evidence_rows = []
    for source in intake.sources:
        suffix = PurePath(source.filename).suffix.lower()
        expected_suffix = ".pdf" if source.format == "pdf" else ".xlsx" if source.format == "xlsx" else ".json"
        if suffix != expected_suffix:
            raise HTTPException(
                status_code=422,
                detail=f"{source.filename}: file extension does not match declared format",
            )
        content = files_by_name[source.filename]
        try:
            parsed = parse_source(source, content)
            for path, value in parsed.values.items():
                _set_field_value(data, path, value)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        digest = hashlib.sha256(content).hexdigest()
        evidence_rows.append(
            Evidence(
                tenant_id=principal.tenant_id,
                case_id="",
                evidence_type=source.evidence_type,
                source_system=source.source_id,
                reference=f"upload://sha256/{digest}",
                sha256=digest,
                attributes={
                    "source_id": source.source_id,
                    "filename": source.filename,
                    "format": source.format,
                    "mapped_fields": sorted(source.field_map),
                    "extracted_field_count": len(parsed.values),
                    "extracted_text_characters": parsed.extracted_text_characters,
                },
            )
        )

    case = _create_case(
        session,
        principal,
        intake.workflow_slug,
        intake.external_reference,
        intake.source_system,
        data,
        evidence_rows,
    )
    session.refresh(case)
    return serialize_case(session, case, principal.tenant_id)


def _set_field_value(data: dict, path: str, value: object) -> None:
    parts = path.split(".")
    current = data
    for part in parts[:-1]:
        if part not in current:
            current[part] = {}
        existing = current[part]
        if not isinstance(existing, dict):
            raise ValueError(f"Mapped field path conflicts with another field: {path}")
        current = existing
    final = parts[-1]
    if final in current:
        raise ValueError(f"Mapped field path was specified more than once: {path}")
    current[final] = value


@router.get("/cases/{case_id}", response_model=CaseRead)
def read_case(
    case_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
):
    return serialize_case(session, get_case(session, principal.tenant_id, case_id), principal.tenant_id)


@router.post(
    "/cases/{case_id}/evidence",
    response_model=CaseRead,
    status_code=status.HTTP_201_CREATED,
)
def add_evidence(
    case_id: str,
    body: EvidenceCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "operator")),
):
    case = get_case_for_update(session, principal.tenant_id, case_id)
    if case.status in {"approved", "rejected"}:
        raise HTTPException(status_code=409, detail="Finalized cases cannot be changed")
    workflow = get_workflow(session, principal.tenant_id, case.workflow_id)
    evidence = Evidence(
        tenant_id=principal.tenant_id,
        case_id=case.id,
        evidence_type=body.evidence_type,
        source_system=body.source_system,
        reference=body.reference,
        sha256=body.sha256.lower() if body.sha256 else None,
        attributes=body.attributes,
    )
    session.add(evidence)
    session.flush()
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            case_id=case.id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="evidence_attached",
            details={
                "evidence_id": evidence.id,
                "evidence_type": evidence.evidence_type,
                "source_system": evidence.source_system,
            },
        )
    )
    run_checks(session, case, workflow, principal)
    session.commit()
    session.refresh(case)
    return serialize_case(session, case, principal.tenant_id)


@router.post("/cases/{case_id}/revalidate", response_model=CaseRead)
def revalidate_case(
    case_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "operator")),
):
    case = get_case_for_update(session, principal.tenant_id, case_id)
    if case.status in {"approved", "rejected"}:
        raise HTTPException(status_code=409, detail="Finalized cases cannot be revalidated")
    workflow = get_workflow(session, principal.tenant_id, case.workflow_id)
    run_checks(session, case, workflow, principal)
    session.commit()
    session.refresh(case)
    return serialize_case(session, case, principal.tenant_id)


@router.get("/cases/{case_id}/agent-runs", response_model=list[AgentRunRead])
def list_agent_runs(
    case_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
):
    get_case(session, principal.tenant_id, case_id)
    runs = session.scalars(
        select(AgentRun)
        .where(AgentRun.tenant_id == principal.tenant_id, AgentRun.case_id == case_id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id)
        .limit(limit)
    ).all()
    return [
        AgentRunRead(
            id=run.id,
            case_id=run.case_id,
            provider=run.provider,
            profile_id=run.profile_id,
            model=run.model,
            status=run.status,
            input_digest=run.input_digest,
            recommendation=AgentRecommendation.model_validate(run.recommendation),
            created_at=run.created_at,
        )
        for run in runs
    ]


@router.post("/cases/{case_id}/review", response_model=CaseRead)
def review_case(
    case_id: str,
    body: ReviewCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "reviewer")),
):
    case = get_case_for_update(session, principal.tenant_id, case_id)
    if case.status in {"approved", "rejected"}:
        raise HTTPException(status_code=409, detail="Case has already received a final decision")
    checks = latest_checks(session, case.id, principal.tenant_id)
    evidence = session.scalars(
        select(Evidence)
        .where(Evidence.case_id == case.id, Evidence.tenant_id == principal.tenant_id)
        .order_by(Evidence.created_at.asc(), Evidence.id.asc())
    ).all()
    if body.decision == "approve":
        if not checks or any(check.status == "fail" for check in checks):
            raise HTTPException(
                status_code=409,
                detail="All latest QC checks must pass before approval",
            )
        if not body.confirm_qc_reviewed or not body.confirm_evidence_reviewed:
            raise HTTPException(
                status_code=422,
                detail="Approval requires confirmation that QC checks and evidence were reviewed",
            )
        case.status = "approved"
    elif body.decision == "reject":
        case.status = "rejected"
    else:
        case.status = "needs_information"
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            case_id=case.id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="human_review",
            details={
                "decision": body.decision,
                "note": body.note,
                "qc_check_ids": [check.id for check in checks],
                "evidence_ids": [item.id for item in evidence],
                "confirm_qc_reviewed": body.confirm_qc_reviewed,
                "confirm_evidence_reviewed": body.confirm_evidence_reviewed,
            },
        )
    )
    session.commit()
    session.refresh(case)
    return serialize_case(session, case, principal.tenant_id)


@router.get("/cases/{case_id}/audit", response_model=list[AuditRead])
def read_case_audit(
    case_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
):
    get_case(session, principal.tenant_id, case_id)
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.case_id == case_id, AuditEvent.tenant_id == principal.tenant_id)
        .order_by(AuditEvent.occurred_at.asc(), AuditEvent.id.asc())
    ).all()
    return [AuditRead.model_validate(event, from_attributes=True) for event in events]

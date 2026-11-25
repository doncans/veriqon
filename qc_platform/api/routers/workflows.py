import logging

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from qc_platform.api.dependencies import get_workflow
from qc_platform.agent import digest_input
from qc_platform.config import available_model_profiles, get_model_profile
from qc_platform.database import get_session
from qc_platform.models import (
    AuditEvent,
    BackgroundJob,
    Tenant,
    Workflow,
    WorkflowSOP,
)
from qc_platform.schemas import (
    ApproveRuleSuggestions,
    FieldRule,
    ModelProfileRead,
    RuleSuggestionRequest,
    WorkflowCreate,
    WorkflowRead,
    WorkflowSOPCreate,
    WorkflowSOPRead,
)
from qc_platform.security import Principal, get_principal, require_roles

logger = logging.getLogger(__name__)
router = APIRouter(tags=["workflows"])


def tenant_slug_for(session: Session, tenant_id: str) -> str:
    tenant_slug = session.scalar(select(Tenant.slug).where(Tenant.id == tenant_id))
    if tenant_slug is None:
        raise HTTPException(status_code=401, detail="Tenant is unavailable")
    return tenant_slug


@router.get("/workflows", response_model=list[WorkflowRead])
def list_workflows(
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
):
    return session.scalars(
        select(Workflow)
        .where(Workflow.tenant_id == principal.tenant_id)
        .order_by(Workflow.slug, Workflow.version.desc())
    ).all()


@router.get("/model-profiles", response_model=list[ModelProfileRead])
def list_model_profiles(
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    try:
        profiles = available_model_profiles(tenant_slug_for(session, principal.tenant_id))
    except ValueError as error:
        logger.error("Model profile configuration is invalid")
        raise HTTPException(status_code=503, detail="Model profile configuration is invalid") from error
    return [
        ModelProfileRead(profile_id=profile_id, provider=profile.provider, model=profile.model)
        for profile_id, profile in profiles
    ]


@router.post("/workflows", response_model=WorkflowRead, status_code=status.HTTP_201_CREATED)
def create_workflow(
    body: WorkflowCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    tenant_slug = tenant_slug_for(session, principal.tenant_id)
    try:
        get_model_profile(body.agent_profile, tenant_slug)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail="Model profile is not enabled for this tenant") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Configured model profile is unavailable") from error
    workflow = Workflow(tenant_id=principal.tenant_id, **body.model_dump())
    session.add(workflow)
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="workflow_created",
            details={"slug": body.slug, "version": body.version},
        )
    )
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Workflow version already exists") from error
    session.refresh(workflow)
    return workflow


@router.get("/workflows/{workflow_id}/sops", response_model=list[WorkflowSOPRead])
def list_workflow_sops(
    workflow_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    workflow = get_workflow(session, principal.tenant_id, workflow_id)
    return session.scalars(
        select(WorkflowSOP)
        .join(
            Workflow,
            (WorkflowSOP.workflow_id == Workflow.id)
            & (WorkflowSOP.tenant_id == Workflow.tenant_id),
        )
        .where(
            WorkflowSOP.tenant_id == principal.tenant_id,
            Workflow.slug == workflow.slug,
        )
        .order_by(WorkflowSOP.version.desc())
    ).all()


@router.post(
    "/workflows/{workflow_id}/sops",
    response_model=WorkflowSOPRead,
    status_code=status.HTTP_201_CREATED,
)
def create_workflow_sop(
    workflow_id: str,
    body: WorkflowSOPCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    workflow = get_workflow(session, principal.tenant_id, workflow_id)
    latest_version = session.scalar(
        select(WorkflowSOP.version)
        .join(
            Workflow,
            (WorkflowSOP.workflow_id == Workflow.id)
            & (WorkflowSOP.tenant_id == Workflow.tenant_id),
        )
        .where(WorkflowSOP.tenant_id == principal.tenant_id, Workflow.slug == workflow.slug)
        .order_by(WorkflowSOP.version.desc())
        .limit(1)
    ) or 0
    sop = WorkflowSOP(
        tenant_id=principal.tenant_id,
        workflow_id=workflow.id,
        created_by_api_key_id=principal.api_key_id,
        version=latest_version + 1,
        title=body.title,
        content=body.content,
    )
    session.add(sop)
    session.flush()
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="workflow_sop_created",
            details={
                "workflow_id": workflow.id,
                "sop_id": sop.id,
                "version": sop.version,
                "content_digest": digest_input({"content": sop.content}),
            },
        )
    )
    session.commit()
    session.refresh(sop)
    return sop


@router.post("/workflows/{workflow_id}/rule-suggestions", status_code=202)
def request_rule_suggestions(
    workflow_id: str,
    body: RuleSuggestionRequest,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    workflow = get_workflow(session, principal.tenant_id, workflow_id)
    sop = session.scalar(
        select(WorkflowSOP)
        .join(
            Workflow,
            (WorkflowSOP.workflow_id == Workflow.id)
            & (WorkflowSOP.tenant_id == Workflow.tenant_id),
        )
        .where(
            WorkflowSOP.id == body.sop_id,
            WorkflowSOP.tenant_id == principal.tenant_id,
            Workflow.slug == workflow.slug,
        )
    )
    if sop is None:
        raise HTTPException(status_code=404, detail="SOP not found")
    if len(sop.content.encode("utf-8")) > 40_000:
        raise HTTPException(status_code=422, detail="SOP exceeds the model input size limit")
    try:
        profile = get_model_profile(
            workflow.agent_profile,
            tenant_slug_for(session, principal.tenant_id),
        )
    except PermissionError as error:
        raise HTTPException(status_code=403, detail="Model profile is not enabled for this tenant") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Configured model profile is unavailable") from error
    if profile.provider == "rules":
        raise HTTPException(
            status_code=422,
            detail="Rule suggestions require a configured language model profile",
        )

    job = BackgroundJob(
        tenant_id=principal.tenant_id,
        actor_api_key_id=principal.api_key_id,
        case_id=None,
        job_type="rule_suggestion",
        payload={
            "workflow_id": workflow.id,
            "sop_id": sop.id,
            "profile_id": workflow.agent_profile,
            "candidate_fields": body.candidate_fields,
        },
    )
    session.add(job)
    session.flush()
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="rule_suggestions_queued",
            details={
                "job_id": job.id,
                "workflow_id": workflow.id,
                "sop_id": sop.id,
                "candidate_field_count": len(body.candidate_fields),
            },
        )
    )
    session.commit()
    session.refresh(job)
    return {"job_id": job.id, "status": job.status}


@router.post(
    "/workflows/{workflow_id}/rule-suggestions/{job_id}/approve",
    response_model=WorkflowRead,
)
def approve_rule_suggestions(
    workflow_id: str,
    job_id: str,
    body: ApproveRuleSuggestions,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    workflow = get_workflow(session, principal.tenant_id, workflow_id)
    latest_workflow_id = session.scalar(
        select(Workflow.id)
        .where(
            Workflow.tenant_id == principal.tenant_id,
            Workflow.slug == workflow.slug,
        )
        .order_by(Workflow.version.desc())
        .limit(1)
    )
    if latest_workflow_id != workflow.id:
        raise HTTPException(
            status_code=409,
            detail="Workflow changed after suggestions were generated; request suggestions again",
        )
    job = session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.id == job_id,
            BackgroundJob.tenant_id == principal.tenant_id,
        )
    )
    if (
        job is None
        or job.job_type != "rule_suggestion"
        or job.payload.get("workflow_id") != workflow.id
    ):
        raise HTTPException(status_code=404, detail="Rule suggestion job not found")
    if job.status != "succeeded" or not job.result:
        raise HTTPException(status_code=409, detail="Rule suggestion job is not complete")

    suggestions = job.result.get("suggestions")
    if not isinstance(suggestions, list):
        raise HTTPException(status_code=409, detail="Rule suggestion result is invalid")
    by_key = {
        suggestion.get("rule", {}).get("key"): suggestion
        for suggestion in suggestions
        if isinstance(suggestion, dict) and isinstance(suggestion.get("rule"), dict)
    }
    if not set(body.rule_keys).issubset(by_key):
        raise HTTPException(status_code=422, detail="Selected rule key was not suggested")
    existing_keys = {rule["key"] for rule in workflow.field_rules}
    if existing_keys.intersection(body.rule_keys):
        raise HTTPException(status_code=409, detail="A selected rule already exists in this workflow")
    try:
        selected_rules = [
            FieldRule.model_validate(by_key[key]["rule"]).model_dump(mode="json")
            for key in body.rule_keys
        ]
    except ValidationError as error:
        raise HTTPException(status_code=409, detail="A suggested rule is invalid") from error
    new_version = Workflow(
        tenant_id=workflow.tenant_id,
        slug=workflow.slug,
        name=workflow.name,
        version=workflow.version + 1,
        required_fields=list(workflow.required_fields),
        field_rules=[*workflow.field_rules, *selected_rules],
        require_evidence=workflow.require_evidence,
        required_evidence_types=list(workflow.required_evidence_types),
        agent_profile=workflow.agent_profile,
        active=True,
    )
    session.add(new_version)
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="rule_suggestions_approved",
            details={
                "job_id": job.id,
                "workflow_id": workflow.id,
                "new_workflow_version": new_version.version,
                "approved_rule_keys": body.rule_keys,
            },
        )
    )
    try:
        session.commit()
    except IntegrityError as error:
        session.rollback()
        raise HTTPException(status_code=409, detail="Workflow version already exists") from error
    session.refresh(new_version)
    return new_version

from datetime import timedelta

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from qc_platform.api.dependencies import get_case, get_workflow
from qc_platform.config import get_model_profile
from qc_platform.database import get_session
from qc_platform.models import (
    AuditEvent,
    AutomationSchedule,
    BackgroundJob,
    JobAttempt,
    Tenant,
    utc_now,
)
from qc_platform.schemas import (
    AutomationJobCreate,
    AutomationScheduleCreate,
    AutomationScheduleRead,
    AutomationScheduleUpdate,
    BackgroundJobRead,
    JobAttemptRead,
)
from qc_platform.security import Principal, get_principal, require_roles

router = APIRouter(tags=["automation"])


def _validate_agent_profile(session: Session, tenant_id: str, profile_id: str) -> None:
    tenant_slug = session.scalar(select(Tenant.slug).where(Tenant.id == tenant_id))
    if tenant_slug is None:
        raise HTTPException(status_code=401, detail="Tenant is unavailable")
    try:
        get_model_profile(profile_id, tenant_slug)
    except PermissionError as error:
        raise HTTPException(status_code=403, detail="Model profile is not enabled for this tenant") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Configured model profile is unavailable") from error


def _enqueue_job(
    case_id: str,
    job_type: str,
    idempotency_key: str | None,
    session: Session,
    principal: Principal,
    *,
    max_attempts: int = 3,
    audit_action: str = "automation_job_queued",
) -> BackgroundJobRead:
    if idempotency_key is not None and not idempotency_key.strip():
        raise HTTPException(status_code=422, detail="Idempotency-Key must not be blank")
    case = get_case(session, principal.tenant_id, case_id)
    workflow = get_workflow(session, principal.tenant_id, case.workflow_id)
    if job_type == "agent_review":
        _validate_agent_profile(session, principal.tenant_id, workflow.agent_profile)
    if idempotency_key:
        existing = session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id == principal.tenant_id,
                BackgroundJob.idempotency_key == idempotency_key,
            )
        )
        if existing is not None:
            if existing.case_id != case.id or existing.job_type != job_type:
                raise HTTPException(status_code=409, detail="Idempotency key already used")
            return BackgroundJobRead.model_validate(existing, from_attributes=True)
    job = BackgroundJob(
        tenant_id=principal.tenant_id,
        actor_api_key_id=principal.api_key_id,
        case_id=case.id,
        job_type=job_type,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
        payload={"profile_id": workflow.agent_profile} if job_type == "agent_review" else {},
    )
    session.add(job)
    session.flush()
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            case_id=case.id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action=audit_action,
            details=(
                {"job_id": job.id, "profile_id": workflow.agent_profile}
                if audit_action == "agent_review_queued"
                else {"job_id": job.id, "job_type": job.job_type}
            ),
        )
    )
    session.commit()
    session.refresh(job)
    return BackgroundJobRead.model_validate(job, from_attributes=True)


@router.post("/cases/{case_id}/agent-review", response_model=BackgroundJobRead, status_code=202)
def agent_review_case(
    case_id: str,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "operator", "reviewer")),
):
    return _enqueue_job(
        case_id,
        "agent_review",
        idempotency_key,
        session,
        principal,
        audit_action="agent_review_queued",
    )


@router.post("/automation/jobs", response_model=BackgroundJobRead, status_code=202)
def enqueue_automation_job(
    body: AutomationJobCreate,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=128),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "operator")),
):
    return _enqueue_job(
        body.case_id,
        body.job_type,
        idempotency_key,
        session,
        principal,
        max_attempts=body.max_attempts,
    )


@router.get("/automation/schedules", response_model=list[AutomationScheduleRead])
def list_automation_schedules(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "reviewer")),
):
    schedules = session.scalars(
        select(AutomationSchedule)
        .where(AutomationSchedule.tenant_id == principal.tenant_id)
        .order_by(AutomationSchedule.created_at.desc(), AutomationSchedule.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [AutomationScheduleRead.model_validate(item, from_attributes=True) for item in schedules]


@router.post(
    "/automation/schedules",
    response_model=AutomationScheduleRead,
    status_code=status.HTTP_201_CREATED,
)
def create_automation_schedule(
    body: AutomationScheduleCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    case = get_case(session, principal.tenant_id, body.case_id)
    workflow = get_workflow(session, principal.tenant_id, case.workflow_id)
    if body.job_type == "agent_review":
        _validate_agent_profile(session, principal.tenant_id, workflow.agent_profile)
    schedule = AutomationSchedule(
        tenant_id=principal.tenant_id,
        case_id=case.id,
        actor_api_key_id=principal.api_key_id,
        job_type=body.job_type,
        interval_seconds=body.interval_seconds,
        next_run_at=utc_now() + timedelta(seconds=body.interval_seconds),
    )
    session.add(schedule)
    session.flush()
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            case_id=case.id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="automation_schedule_created",
            details={
                "schedule_id": schedule.id,
                "job_type": schedule.job_type,
                "interval_seconds": schedule.interval_seconds,
            },
        )
    )
    session.commit()
    session.refresh(schedule)
    return AutomationScheduleRead.model_validate(schedule, from_attributes=True)


@router.patch("/automation/schedules/{schedule_id}", response_model=AutomationScheduleRead)
def update_automation_schedule(
    schedule_id: str,
    body: AutomationScheduleUpdate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin")),
):
    schedule = session.scalar(
        select(AutomationSchedule).where(
            AutomationSchedule.id == schedule_id,
            AutomationSchedule.tenant_id == principal.tenant_id,
        )
    )
    if schedule is None:
        raise HTTPException(status_code=404, detail="Automation schedule not found")
    schedule.enabled = body.enabled
    schedule.updated_at = utc_now()
    session.add(
        AuditEvent(
            tenant_id=principal.tenant_id,
            case_id=schedule.case_id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="automation_schedule_updated",
            details={"schedule_id": schedule.id, "enabled": schedule.enabled},
        )
    )
    session.commit()
    session.refresh(schedule)
    return AutomationScheduleRead.model_validate(schedule, from_attributes=True)


@router.get("/cases/{case_id}/agent-jobs", response_model=list[BackgroundJobRead])
@router.get("/cases/{case_id}/automation-jobs", response_model=list[BackgroundJobRead])
def list_agent_jobs(
    case_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
):
    get_case(session, principal.tenant_id, case_id)
    jobs = session.scalars(
        select(BackgroundJob)
        .where(BackgroundJob.tenant_id == principal.tenant_id, BackgroundJob.case_id == case_id)
        .order_by(BackgroundJob.created_at.desc(), BackgroundJob.id)
        .limit(limit)
    ).all()
    return [BackgroundJobRead.model_validate(job, from_attributes=True) for job in jobs]


@router.get("/agent-jobs/{job_id}", response_model=BackgroundJobRead)
def read_agent_job(
    job_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
):
    job = session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.id == job_id,
            BackgroundJob.tenant_id == principal.tenant_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")
    return BackgroundJobRead.model_validate(job, from_attributes=True)


@router.get("/agent-jobs/{job_id}/attempts", response_model=list[JobAttemptRead])
def list_job_attempts(
    job_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
):
    job = session.scalar(
        select(BackgroundJob).where(
            BackgroundJob.id == job_id,
            BackgroundJob.tenant_id == principal.tenant_id,
        )
    )
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    attempts = session.scalars(
        select(JobAttempt)
        .where(JobAttempt.job_id == job.id, JobAttempt.tenant_id == principal.tenant_id)
        .order_by(JobAttempt.attempt_number)
    ).all()
    return [JobAttemptRead.model_validate(attempt, from_attributes=True) for attempt in attempts]


@router.get("/automation/jobs", response_model=list[BackgroundJobRead])
def list_tenant_agent_jobs(
    job_status: str | None = Query(
        default=None,
        alias="status",
        pattern=r"^(queued|running|succeeded|failed|cancelled)$",
    ),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    principal: Principal = Depends(get_principal),
):
    query = select(BackgroundJob).where(BackgroundJob.tenant_id == principal.tenant_id)
    if job_status:
        query = query.where(BackgroundJob.status == job_status)
    jobs = session.scalars(
        query.order_by(BackgroundJob.created_at.desc(), BackgroundJob.id)
        .limit(limit)
        .offset(offset)
    ).all()
    return [BackgroundJobRead.model_validate(job, from_attributes=True) for job in jobs]

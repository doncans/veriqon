from datetime import timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from qc_platform.database import get_session
from qc_platform.models import BackgroundJob, JobAttempt, QCCase, utc_now
from qc_platform.schemas import JobAttemptRead, MonitoringSummary
from qc_platform.security import Principal, require_roles

router = APIRouter(tags=["monitoring"])


@router.get("/operations/jobs/summary")
def tenant_job_summary(
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "reviewer")),
):
    counts = session.execute(
        select(BackgroundJob.status, func.count(BackgroundJob.id))
        .where(BackgroundJob.tenant_id == principal.tenant_id)
        .group_by(BackgroundJob.status)
    ).all()
    return {
        "by_status": {job_status: count for job_status, count in counts},
        "tracked": sum(count for _, count in counts),
    }


@router.get("/monitoring/api/summary", response_model=MonitoringSummary)
def monitoring_summary(
    window_days: int = Query(default=7, ge=1, le=90),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "reviewer")),
):
    cutoff = utc_now() - timedelta(days=window_days)
    cases_by_status = dict(
        session.execute(
            select(QCCase.status, func.count(QCCase.id))
            .where(QCCase.tenant_id == principal.tenant_id)
            .group_by(QCCase.status)
        ).all()
    )
    jobs_by_status = dict(
        session.execute(
            select(BackgroundJob.status, func.count(BackgroundJob.id))
            .where(
                BackgroundJob.tenant_id == principal.tenant_id,
                BackgroundJob.created_at >= cutoff,
            )
            .group_by(BackgroundJob.status)
        ).all()
    )
    attempts_by_status = dict(
        session.execute(
            select(JobAttempt.status, func.count(JobAttempt.id))
            .where(
                JobAttempt.tenant_id == principal.tenant_id,
                JobAttempt.started_at >= cutoff,
            )
            .group_by(JobAttempt.status)
        ).all()
    )
    daily_cases = [
        {"date": str(day), "count": count}
        for day, count in session.execute(
            select(func.date(QCCase.created_at), func.count(QCCase.id))
            .where(QCCase.tenant_id == principal.tenant_id, QCCase.created_at >= cutoff)
            .group_by(func.date(QCCase.created_at))
            .order_by(func.date(QCCase.created_at))
        ).all()
    ]
    daily_jobs = [
        {"date": str(day), "count": count}
        for day, count in session.execute(
            select(func.date(BackgroundJob.created_at), func.count(BackgroundJob.id))
            .where(
                BackgroundJob.tenant_id == principal.tenant_id,
                BackgroundJob.created_at >= cutoff,
            )
            .group_by(func.date(BackgroundJob.created_at))
            .order_by(func.date(BackgroundJob.created_at))
        ).all()
    ]
    error_count = session.scalar(
        select(func.count(JobAttempt.id)).where(
            JobAttempt.tenant_id == principal.tenant_id,
            JobAttempt.started_at >= cutoff,
            JobAttempt.status.in_(("retrying", "failed")),
        )
    ) or 0
    return MonitoringSummary(
        window_days=window_days,
        cases_by_status=cases_by_status,
        jobs_by_status=jobs_by_status,
        attempts_by_status=attempts_by_status,
        daily_cases=daily_cases,
        daily_jobs=daily_jobs,
        error_count=error_count,
    )


@router.get("/monitoring/api/errors", response_model=list[JobAttemptRead])
def monitoring_errors(
    limit: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_roles("tenant_admin", "reviewer")),
):
    attempts = session.scalars(
        select(JobAttempt)
        .where(
            JobAttempt.tenant_id == principal.tenant_id,
            JobAttempt.status.in_(("retrying", "failed")),
        )
        .order_by(JobAttempt.started_at.desc(), JobAttempt.id)
        .limit(limit)
    ).all()
    return [JobAttemptRead.model_validate(attempt, from_attributes=True) for attempt in attempts]

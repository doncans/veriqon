import argparse
import logging
import os
import platform
import threading
import time
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from qc_platform.agent import (
    AgentUnavailableError,
    build_agent_input,
    digest_input,
    make_recommendation,
    make_rule_suggestions,
)
from qc_platform.config import APP_ENV, DATABASE_URL, get_model_profile
from qc_platform.database import make_engine, make_session_factory
from qc_platform.models import (
    AgentRun,
    APIKey,
    AuditEvent,
    AutomationSchedule,
    BackgroundJob,
    Evidence,
    JobAttempt,
    QCCase,
    Tenant,
    Workflow,
    WorkflowSOP,
    utc_now,
)
from qc_platform.qc import latest_checks, run_checks
from qc_platform.security import Principal

logger = logging.getLogger(__name__)
LEASE_SECONDS = 300
HEARTBEAT_SECONDS = 20


@dataclass(frozen=True)
class JobContext:
    job_id: str
    job_type: str
    tenant_id: str
    actor_api_key_id: str
    actor_role: str
    case_id: str | None
    workflow_id: str
    tenant_slug: str
    profile_id: str | None
    agent_input: dict
    attempts: int
    max_attempts: int


def _eligible_jobs(now):
    return or_(
        (BackgroundJob.status == "queued") & (BackgroundJob.run_after <= now),
        (BackgroundJob.status == "running")
        & (BackgroundJob.lease_until.is_not(None))
        & (BackgroundJob.lease_until <= now),
    )


def _claim_job(factory: sessionmaker[Session], worker_id: str) -> str | None:
    now = utc_now()
    with factory() as session:
        candidate = session.scalar(
            select(BackgroundJob)
            .where(_eligible_jobs(now))
            .order_by(BackgroundJob.run_after, BackgroundJob.created_at, BackgroundJob.id)
            .limit(1)
            .with_for_update(skip_locked=session.bind.dialect.name == "postgresql")
        )
        if candidate is None:
            return None
        candidate_id = candidate.id
        attempt_number = candidate.attempts + 1
        if candidate.status == "running":
            stale_attempt = session.scalar(
                select(JobAttempt).where(
                    JobAttempt.job_id == candidate.id,
                    JobAttempt.status == "running",
                ).order_by(JobAttempt.attempt_number.desc())
            )
            if stale_attempt is not None:
                stale_attempt.status = "retrying"
                stale_attempt.error_code = "worker_lease_expired"
                stale_attempt.finished_at = now
                started_at = stale_attempt.started_at
                if started_at.tzinfo is None:
                    started_at = started_at.replace(tzinfo=now.tzinfo)
                stale_attempt.duration_ms = max(
                    0, int((now - started_at).total_seconds() * 1000)
                )
        result = session.execute(
            update(BackgroundJob)
            .where(BackgroundJob.id == candidate_id, _eligible_jobs(now))
            .execution_options(synchronize_session=False)
            .values(
                status="running",
                progress=5,
                progress_message="Worker claimed job",
                lease_owner=worker_id,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                attempts=BackgroundJob.attempts + 1,
                updated_at=now,
            )
        )
        if result.rowcount != 1:
            session.rollback()
            return None
        session.add(
            JobAttempt(
                tenant_id=candidate.tenant_id,
                job_id=candidate.id,
                attempt_number=attempt_number,
                status="running",
                worker_id=worker_id,
            )
        )
        session.commit()
        return candidate_id


def _update_progress(
    factory: sessionmaker[Session],
    job_id: str,
    worker_id: str,
    progress: int,
    message: str,
) -> bool:
    now = utc_now()
    with factory() as session:
        result = session.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.id == job_id,
                BackgroundJob.status == "running",
                BackgroundJob.lease_owner == worker_id,
            )
            .values(
                progress=progress,
                progress_message=message,
                lease_until=now + timedelta(seconds=LEASE_SECONDS),
                updated_at=now,
            )
        )
        session.commit()
        return result.rowcount == 1


def _heartbeat(factory: sessionmaker[Session], job_id: str, worker_id: str, stop: threading.Event):
    while not stop.wait(HEARTBEAT_SECONDS):
        try:
            if not _update_progress(factory, job_id, worker_id, 40, "Model task is running"):
                return
        except Exception:
            logger.exception("Could not renew lease for background job %s", job_id)


def _finish_failure(
    factory: sessionmaker[Session],
    job_id: str,
    worker_id: str,
    attempts: int,
    max_attempts: int,
    error_code: str,
) -> None:
    now = utc_now()
    can_retry = attempts < max_attempts
    with factory() as session:
        attempt = session.scalar(
            select(JobAttempt).where(
                JobAttempt.job_id == job_id,
                JobAttempt.attempt_number == attempts,
                JobAttempt.worker_id == worker_id,
                JobAttempt.status == "running",
            )
        )
        if attempt is not None:
            attempt.status = "retrying" if can_retry else "failed"
            attempt.error_code = error_code
            attempt.finished_at = now
            started_at = attempt.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=now.tzinfo)
            attempt.duration_ms = max(0, int((now - started_at).total_seconds() * 1000))
        session.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.id == job_id,
                BackgroundJob.status == "running",
                BackgroundJob.lease_owner == worker_id,
            )
            .values(
                status="queued" if can_retry else "failed",
                progress=0 if can_retry else 100,
                progress_message="Retry scheduled" if can_retry else "Job failed",
                error_code=error_code,
                run_after=now + timedelta(seconds=min(300, 2 ** attempts * 5)),
                lease_owner=None,
                lease_until=None,
                updated_at=now,
                finished_at=None if can_retry else now,
            )
        )
        session.commit()


def enqueue_due_schedules(factory: sessionmaker[Session], batch_size: int = 100) -> int:
    now = utc_now()
    enqueued = 0
    with factory() as session:
        schedules = session.scalars(
            select(AutomationSchedule)
            .where(AutomationSchedule.enabled.is_(True), AutomationSchedule.next_run_at <= now)
            .order_by(AutomationSchedule.next_run_at, AutomationSchedule.id)
            .limit(batch_size)
            .with_for_update(skip_locked=session.bind.dialect.name == "postgresql")
        ).all()
        for schedule in schedules:
            actor = session.scalar(
                select(APIKey).where(
                    APIKey.id == schedule.actor_api_key_id,
                    APIKey.tenant_id == schedule.tenant_id,
                )
            )
            actor_expiry = actor.expires_at if actor is not None else None
            if actor_expiry is not None and actor_expiry.tzinfo is None:
                actor_expiry = actor_expiry.replace(tzinfo=now.tzinfo)
            if actor is None or actor.revoked_at is not None or (
                actor_expiry is not None and actor_expiry <= now
            ):
                schedule.enabled = False
                schedule.updated_at = now
                continue
            due_at = schedule.next_run_at
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=now.tzinfo)
            idempotency_key = f"schedule:{schedule.id}:{due_at.isoformat()}"
            existing = session.scalar(
                select(BackgroundJob.id).where(
                    BackgroundJob.tenant_id == schedule.tenant_id,
                    BackgroundJob.idempotency_key == idempotency_key,
                )
            )
            workflow_profile = None
            if schedule.job_type == "agent_review":
                workflow_profile = session.scalar(
                    select(Workflow.agent_profile)
                    .join(
                        QCCase,
                        (QCCase.workflow_id == Workflow.id)
                        & (QCCase.tenant_id == Workflow.tenant_id),
                    )
                    .where(
                        QCCase.id == schedule.case_id,
                        QCCase.tenant_id == schedule.tenant_id,
                    )
                )
                if workflow_profile is None:
                    schedule.enabled = False
                    continue
            if existing is None:
                session.add(
                    BackgroundJob(
                        tenant_id=schedule.tenant_id,
                        actor_api_key_id=schedule.actor_api_key_id,
                        case_id=schedule.case_id,
                        job_type=schedule.job_type,
                        idempotency_key=idempotency_key,
                        payload={"profile_id": workflow_profile} if workflow_profile else {},
                        run_after=now,
                    )
                )
                enqueued += 1
            schedule.last_run_at = due_at
            schedule.next_run_at = due_at + timedelta(seconds=schedule.interval_seconds)
            schedule.updated_at = now
            session.add(
                AuditEvent(
                    tenant_id=schedule.tenant_id,
                    case_id=schedule.case_id,
                    actor_api_key_id=schedule.actor_api_key_id,
                    actor_role="tenant_admin",
                    action="automation_schedule_fired",
                    details={
                        "schedule_id": schedule.id,
                        "job_type": schedule.job_type,
                        "scheduled_at": due_at.isoformat(),
                    },
                )
            )
        session.commit()
    return enqueued


def _load_job_context(
    factory: sessionmaker[Session], job_id: str, worker_id: str
) -> JobContext | str:
    with factory() as session:
        job = session.get(BackgroundJob, job_id)
        if job is None or job.status != "running" or job.lease_owner != worker_id:
            return "job_lease_lost"
        if job.job_type not in {"agent_review", "qc_revalidate", "rule_suggestion"}:
            return "unsupported_job"
        if job.job_type != "rule_suggestion" and not job.case_id:
            return "case_unavailable"
        actor = session.scalar(
            select(APIKey).where(
                APIKey.id == job.actor_api_key_id,
                APIKey.tenant_id == job.tenant_id,
            )
        )
        if actor is None or actor.revoked_at is not None:
            return "actor_key_inactive"
        actor_expiry = actor.expires_at
        if actor_expiry is not None and actor_expiry.tzinfo is None:
            actor_expiry = actor_expiry.replace(tzinfo=utc_now().tzinfo)
        if actor_expiry is not None and actor_expiry <= utc_now():
            return "actor_key_expired"
        if job.job_type == "rule_suggestion":
            workflow_id = job.payload.get("workflow_id")
            workflow = session.scalar(
                select(Workflow).where(
                    Workflow.id == workflow_id,
                    Workflow.tenant_id == job.tenant_id,
                )
            )
            if workflow is None:
                return "workflow_sop_unavailable"
            sop = session.scalar(
                select(WorkflowSOP)
                .join(
                    Workflow,
                    (WorkflowSOP.workflow_id == Workflow.id)
                    & (WorkflowSOP.tenant_id == Workflow.tenant_id),
                )
                .where(
                    WorkflowSOP.id == job.payload.get("sop_id"),
                    WorkflowSOP.tenant_id == job.tenant_id,
                    Workflow.slug == workflow.slug,
                )
            )
            if workflow is None or sop is None:
                return "workflow_sop_unavailable"
            agent_input = {
                "sop_text": sop.content,
                "candidate_fields": job.payload.get("candidate_fields", []),
                "existing_rule_keys": [rule["key"] for rule in workflow.field_rules],
            }
        else:
            case = session.scalar(
                select(QCCase).where(
                    QCCase.id == job.case_id,
                    QCCase.tenant_id == job.tenant_id,
                )
            )
            if case is None:
                return "case_unavailable"
            workflow_id = case.workflow_id
            workflow = session.scalar(
                select(Workflow).where(
                    Workflow.id == workflow_id,
                    Workflow.tenant_id == job.tenant_id,
                )
            )
            if workflow is None:
                return "workflow_unavailable"
            checks = latest_checks(session, case.id, job.tenant_id)
            evidence = session.scalars(
                select(Evidence).where(
                    Evidence.tenant_id == job.tenant_id,
                    Evidence.case_id == case.id,
                )
            ).all()
            agent_input = build_agent_input(case, workflow, checks, evidence)
        tenant_slug = session.scalar(select(Tenant.slug).where(Tenant.id == job.tenant_id))
        if workflow is None or tenant_slug is None:
            return "workflow_unavailable"
        profile_id = (
            str(job.payload["profile_id"])
            if job.job_type in {"agent_review", "rule_suggestion"}
            else None
        )
        return JobContext(
            job_id=job.id,
            job_type=job.job_type,
            tenant_id=job.tenant_id,
            actor_api_key_id=actor.id,
            actor_role=actor.role,
            case_id=job.case_id,
            workflow_id=workflow.id,
            tenant_slug=tenant_slug,
            profile_id=profile_id,
            agent_input=agent_input,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
        )


def _save_success(
    factory: sessionmaker[Session],
    context: JobContext,
    worker_id: str,
    result: dict,
    recommendation=None,
    profile=None,
) -> bool:
    with factory() as session:
        job = session.get(BackgroundJob, context.job_id)
        if job is None or job.lease_owner != worker_id or job.status != "running":
            session.rollback()
            return False
        if recommendation is not None and profile is not None:
            if context.case_id is None:
                raise ValueError("Agent review job has no case")
            run = AgentRun(
                tenant_id=context.tenant_id,
                case_id=context.case_id,
                actor_api_key_id=context.actor_api_key_id,
                provider=profile.provider,
                profile_id=context.profile_id,
                model=profile.model,
                status="completed",
                input_digest=digest_input(context.agent_input),
                recommendation=recommendation.model_dump(),
            )
            session.add(run)
            session.flush()
            result = {**result, "agent_run_id": run.id}
            session.add(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    case_id=context.case_id,
                    actor_api_key_id=context.actor_api_key_id,
                    actor_role=context.actor_role,
                    action="agent_review_completed",
                    details={
                        "agent_run_id": run.id,
                        "job_id": job.id,
                        "profile_id": context.profile_id,
                        "provider": profile.provider,
                        "model": profile.model,
                        "input_digest": run.input_digest,
                    },
                )
            )
        elif context.job_type == "rule_suggestion":
            session.add(
                AuditEvent(
                    tenant_id=context.tenant_id,
                    actor_api_key_id=context.actor_api_key_id,
                    actor_role=context.actor_role,
                    action="rule_suggestions_completed",
                    details={
                        "job_id": job.id,
                        "workflow_id": context.workflow_id,
                        "suggestion_count": len(result.get("suggestions", [])),
                        "input_digest": digest_input(context.agent_input),
                    },
                )
            )
        attempt = session.scalar(
            select(JobAttempt).where(
                JobAttempt.job_id == context.job_id,
                JobAttempt.attempt_number == job.attempts,
                JobAttempt.worker_id == worker_id,
            )
        )
        if attempt is not None:
            attempt.status = "succeeded"
            attempt.finished_at = utc_now()
            started_at = attempt.started_at
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=utc_now().tzinfo)
            attempt.duration_ms = max(
                0, int((attempt.finished_at - started_at).total_seconds() * 1000)
            )
        update_result = session.execute(
            update(BackgroundJob)
            .where(
                BackgroundJob.id == context.job_id,
                BackgroundJob.status == "running",
                BackgroundJob.lease_owner == worker_id,
            )
            .values(
                status="succeeded",
                progress=100,
                progress_message=(
                    "Recommendation ready for human review"
                    if recommendation is not None
                    else "QC checks completed"
                ),
                result=result,
                error_code=None,
                lease_owner=None,
                lease_until=None,
                updated_at=utc_now(),
                finished_at=utc_now(),
            )
        )
        if update_result.rowcount != 1:
            session.rollback()
            return False
        session.commit()
        return True


def _process_qc_job(factory: sessionmaker[Session], context: JobContext, worker_id: str) -> None:
    if context.case_id is None:
        raise LookupError("case_unavailable")
    if not _update_progress(factory, context.job_id, worker_id, 35, "Running configured QC rules"):
        return
    with factory() as session:
        case = session.scalar(
            select(QCCase).where(
                QCCase.id == context.case_id,
                QCCase.tenant_id == context.tenant_id,
            ).with_for_update()
        )
        workflow = (
            session.scalar(
                select(Workflow).where(
                    Workflow.id == case.workflow_id,
                    Workflow.tenant_id == context.tenant_id,
                )
            )
            if case
            else None
        )
        if case is None or workflow is None:
            raise LookupError("case_unavailable")
        if case.status in {"approved", "rejected"}:
            raise LookupError("case_finalized")
        principal = Principal(
            tenant_id=context.tenant_id,
            api_key_id=context.actor_api_key_id,
            key_name="background-job",
            role=context.actor_role,
        )
        checks = run_checks(session, case, workflow, principal)
        session.flush()
        result = {
            "check_count": len(checks),
            "failed_count": sum(check.status == "fail" for check in checks),
            "warning_count": sum(check.status == "warning" for check in checks),
        }
        session.add(
            AuditEvent(
                tenant_id=context.tenant_id,
                case_id=context.case_id,
                actor_api_key_id=context.actor_api_key_id,
                actor_role=context.actor_role,
                action="automation_qc_completed",
                details={"job_id": context.job_id, **result},
            )
        )
        session.commit()
    _save_success(factory, context, worker_id, result)


def process_one_job(factory: sessionmaker[Session], worker_id: str | None = None) -> bool:
    worker_id = worker_id or str(uuid4())
    job_id = _claim_job(factory, worker_id)
    if job_id is None:
        return False
    context = _load_job_context(factory, job_id, worker_id)
    if isinstance(context, str):
        with factory() as session:
            job = session.get(BackgroundJob, job_id)
            attempts = job.attempts if job else 1
        if context != "job_lease_lost":
            _finish_failure(factory, job_id, worker_id, attempts, attempts, context)
        return True

    if context.job_type == "qc_revalidate":
        try:
            _process_qc_job(factory, context, worker_id)
        except LookupError as error:
            _finish_failure(
                factory, job_id, worker_id, context.attempts, context.attempts, str(error)
            )
        except Exception:
            logger.exception("QC revalidation failed for background job %s", job_id)
            _finish_failure(
                factory,
                job_id,
                worker_id,
                context.attempts,
                context.max_attempts,
                "qc_execution_failed",
            )
        return True

    try:
        if context.profile_id is None:
            raise ValueError("Missing configured model profile")
        profile = get_model_profile(context.profile_id, context.tenant_slug)
        if not _update_progress(factory, job_id, worker_id, 35, "Requesting model recommendation"):
            return True
        heartbeat_stop = threading.Event()
        heartbeat_thread = threading.Thread(
            target=_heartbeat,
            args=(factory, job_id, worker_id, heartbeat_stop),
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            if context.job_type == "rule_suggestion":
                suggestions = make_rule_suggestions(context.agent_input, profile)
                result = suggestions.model_dump(mode="json")
                _save_success(factory, context, worker_id, result)
            else:
                recommendation = make_recommendation(context.agent_input, profile)
                _save_success(
                    factory,
                    context,
                    worker_id,
                    {},
                    recommendation=recommendation,
                    profile=profile,
                )
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=HEARTBEAT_SECONDS + 1)
    except PermissionError:
        _finish_failure(
            factory, job_id, worker_id, context.attempts, context.attempts, "model_profile_not_allowed"
        )
    except ValueError:
        _finish_failure(
            factory, job_id, worker_id, context.attempts, context.attempts, "model_profile_invalid"
        )
    except AgentUnavailableError:
        logger.warning("Agent provider unavailable for background job %s", job_id)
        _finish_failure(
            factory, job_id, worker_id, context.attempts, context.max_attempts, "model_unavailable"
        )
    except Exception:
        logger.exception("Unexpected error processing background job %s", job_id)
        _finish_failure(
            factory, job_id, worker_id, context.attempts, context.max_attempts, "processing_error"
        )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the durable QC background-job worker.")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--once", action="store_true", help="Process at most one available job.")
    args = parser.parse_args()
    if args.poll_seconds < 0.1:
        parser.error("--poll-seconds must be at least 0.1 seconds")
    logging.basicConfig(level=logging.INFO if APP_ENV != "development" else logging.WARNING)
    engine = make_engine(DATABASE_URL)
    factory = make_session_factory(engine)
    worker_id = f"{platform.node()}:{os.getpid()}:{uuid4()}"
    try:
        while True:
            schedules_created = enqueue_due_schedules(factory)
            did_work = process_one_job(factory, worker_id) or schedules_created > 0
            if args.once:
                return
            if not did_work:
                time.sleep(args.poll_seconds)
    except KeyboardInterrupt:
        logger.info("QC worker stopping")
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()

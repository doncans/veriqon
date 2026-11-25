from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from qc_platform.database import Base


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class APIKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (UniqueConstraint("tenant_id", "id", name="uq_api_keys_tenant_id_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(32))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Workflow(Base):
    __tablename__ = "workflows"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", "version", name="uq_workflow_tenant_slug_version"),
        UniqueConstraint("tenant_id", "id", name="uq_workflows_tenant_id_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    slug: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer)
    required_fields: Mapped[list[str]] = mapped_column(JSON)
    field_rules: Mapped[list[dict]] = mapped_column(JSON, default=list)
    require_evidence: Mapped[bool] = mapped_column(Boolean, default=False)
    required_evidence_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    agent_profile: Mapped[str] = mapped_column(String(100), default="default")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class WorkflowSOP(Base):
    __tablename__ = "workflow_sops"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "workflow_id",
            "version",
            name="uq_workflow_sop_tenant_workflow_version",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
            name="fk_workflow_sops_tenant_workflow",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "created_by_api_key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_workflow_sops_tenant_actor",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workflow_id: Mapped[str] = mapped_column(String(36), index=True)
    created_by_api_key_id: Mapped[str] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(String(20000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QCCase(Base):
    __tablename__ = "qc_cases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_qc_cases_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "workflow_id"],
            ["workflows.tenant_id", "workflows.id"],
            name="fk_qc_cases_tenant_workflow",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    workflow_id: Mapped[str] = mapped_column(String(36), index=True)
    external_reference: Mapped[str | None] = mapped_column(String(160), nullable=True)
    source_system: Mapped[str] = mapped_column(String(120))
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(32), default="in_review", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class Evidence(Base):
    __tablename__ = "evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["qc_cases.tenant_id", "qc_cases.id"],
            name="fk_evidence_tenant_case",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    evidence_type: Mapped[str] = mapped_column(String(100))
    source_system: Mapped[str] = mapped_column(String(120))
    reference: Mapped[str] = mapped_column(String(2048))
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attributes: Mapped[dict] = mapped_column(JSON, default=dict)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QCCheck(Base):
    __tablename__ = "qc_checks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["qc_cases.tenant_id", "qc_cases.id"],
            name="fk_qc_checks_tenant_case",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    run_id: Mapped[str] = mapped_column(String(36), index=True)
    rule_key: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(24))
    result: Mapped[dict] = mapped_column(JSON)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["qc_cases.tenant_id", "qc_cases.id"],
            name="fk_agent_runs_tenant_case",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_api_key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_agent_runs_tenant_actor",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_api_key_id: Mapped[str] = mapped_column(String(36))
    provider: Mapped[str] = mapped_column(String(40))
    profile_id: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24))
    input_digest: Mapped[str] = mapped_column(String(64))
    recommendation: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)


class BackgroundJob(Base):
    __tablename__ = "background_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_background_job_tenant_idempotency"),
        UniqueConstraint("tenant_id", "id", name="uq_background_jobs_tenant_id_id"),
        ForeignKeyConstraint(
            ["tenant_id", "actor_api_key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_background_jobs_tenant_actor",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["qc_cases.tenant_id", "qc_cases.id"],
            name="fk_background_jobs_tenant_case",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    actor_api_key_id: Mapped[str] = mapped_column(String(36))
    case_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    job_type: Mapped[str] = mapped_column(String(80))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    progress_message: Mapped[str] = mapped_column(String(200), default="Queued")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class JobAttempt(Base):
    __tablename__ = "job_attempts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "job_id", "attempt_number", name="uq_job_attempt_number"),
        ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["background_jobs.tenant_id", "background_jobs.id"],
            name="fk_job_attempts_tenant_job",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    attempt_number: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="running", index=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    worker_id: Mapped[str] = mapped_column(String(160))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AutomationSchedule(Base):
    __tablename__ = "automation_schedules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["qc_cases.tenant_id", "qc_cases.id"],
            name="fk_automation_schedules_tenant_case",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_api_key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_automation_schedules_tenant_actor",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    case_id: Mapped[str] = mapped_column(String(36), index=True)
    actor_api_key_id: Mapped[str] = mapped_column(String(36))
    job_type: Mapped[str] = mapped_column(String(80))
    interval_seconds: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    next_run_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "case_id"],
            ["qc_cases.tenant_id", "qc_cases.id"],
            name="fk_audit_events_tenant_case",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "actor_api_key_id"],
            ["api_keys.tenant_id", "api_keys.id"],
            name="fk_audit_events_tenant_actor",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id"), index=True)
    case_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    actor_api_key_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_role: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(100))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, index=True)

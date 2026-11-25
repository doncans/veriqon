import json
import re
from datetime import datetime
from typing import Annotated, Any, Literal

import regex
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,78}[a-z0-9]$")


class WorkflowCreate(BaseModel):
    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{1,98}[a-z0-9]$")
    name: str = Field(min_length=1, max_length=200)
    version: int = Field(ge=1)
    required_fields: list[str] = Field(default_factory=list, max_length=100)
    field_rules: list["FieldRule"] = Field(default_factory=list, max_length=100)
    require_evidence: bool = False
    required_evidence_types: list[str] = Field(default_factory=list, max_length=50)
    agent_profile: str = Field(default="default", pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$")

    @field_validator("required_fields")
    @classmethod
    def validate_required_fields(cls, fields: list[str]) -> list[str]:
        if len(set(fields)) != len(fields):
            raise ValueError("required_fields must not contain duplicates")
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}", field) for field in fields):
            raise ValueError("required_fields must contain valid field paths")
        return fields

    @field_validator("required_evidence_types")
    @classmethod
    def validate_evidence_types(cls, values: list[str]) -> list[str]:
        if any(not value or len(value) > 100 for value in values):
            raise ValueError("required evidence type names must contain 1 to 100 characters")
        if len(set(values)) != len(values):
            raise ValueError("required_evidence_types must not contain duplicates")
        return values

    @model_validator(mode="after")
    def require_qc_criteria(self):
        if not (self.required_fields or self.field_rules or self.require_evidence or self.required_evidence_types):
            raise ValueError("A workflow must define at least one QC criterion")
        rule_keys = [rule.key for rule in self.field_rules]
        if len(set(rule_keys)) != len(rule_keys):
            raise ValueError("field rule keys must be unique within a workflow")
        return self


class FieldRule(BaseModel):
    key: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
    field: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
    operator: Literal[
        "required",
        "regex",
        "one_of",
        "min_length",
        "max_length",
        "min",
        "max",
        "equals_field",
    ]
    value: str | int | float | list[str] | None = None
    severity: Literal["error", "warning"] = "error"
    message: str = Field(min_length=1, max_length=300)

    @field_validator("value")
    @classmethod
    def validate_rule_value(cls, value, info):
        operator = info.data.get("operator")
        if operator == "required" and value is not None:
            raise ValueError("required rule must not specify a value")
        if operator == "equals_field" and (
            not isinstance(value, str)
            or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}", value)
        ):
            raise ValueError("equals_field rule requires a valid field path as its value")
        if operator == "regex" and (not isinstance(value, str) or len(value) > 256):
            raise ValueError("regex rule requires a pattern of at most 256 characters")
        if operator == "regex" and isinstance(value, str):
            try:
                regex.compile(value)
            except regex.error as error:
                raise ValueError("regex rule contains an invalid pattern") from error
        if operator == "one_of" and (
            not isinstance(value, list) or not value or len(value) > 100
        ):
            raise ValueError("one_of rule requires a list of 1 to 100 strings")
        if operator == "one_of" and isinstance(value, list) and any(
            not isinstance(item, str) for item in value
        ):
            raise ValueError("one_of rule values must all be strings")
        if operator in {"min_length", "max_length", "min", "max"} and not isinstance(value, (int, float)):
            raise ValueError(f"{operator} rule requires a numeric value")
        return value


class WorkflowRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    slug: str
    name: str
    version: int
    required_fields: list[str]
    field_rules: list[dict[str, Any]]
    require_evidence: bool
    required_evidence_types: list[str]
    agent_profile: str
    active: bool
    created_at: datetime


class WorkflowSOPCreate(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=20000)
    confirm_no_phi_or_secrets: Literal[True]

    @field_validator("title", "content")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Text must not be blank")
        return value.strip()


class WorkflowSOPRead(BaseModel):
    id: str
    workflow_id: str
    version: int
    title: str
    content: str
    created_at: datetime


class RuleSuggestionRequest(BaseModel):
    sop_id: str
    candidate_fields: list[str] = Field(min_length=1, max_length=100)
    confirm_no_phi_or_secrets: Literal[True]

    @field_validator("candidate_fields")
    @classmethod
    def validate_candidate_fields(cls, fields: list[str]) -> list[str]:
        if len(set(fields)) != len(fields):
            raise ValueError("candidate_fields must not contain duplicates")
        if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}", field) for field in fields):
            raise ValueError("candidate_fields must contain valid field paths")
        return fields


class SuggestedRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule: "FieldRule"
    rationale: str = Field(min_length=1, max_length=500)
    sop_excerpt: str = Field(min_length=1, max_length=500)


class RuleSuggestionResult(BaseModel):
    summary: str = Field(min_length=1, max_length=1000)
    suggestions: list[SuggestedRule] = Field(max_length=25)


class ApproveRuleSuggestions(BaseModel):
    rule_keys: list[str] = Field(min_length=1, max_length=25)

    @field_validator("rule_keys")
    @classmethod
    def validate_rule_keys(cls, keys: list[str]) -> list[str]:
        if len(set(keys)) != len(keys):
            raise ValueError("rule_keys must not contain duplicates")
        return keys


class ModelProfileRead(BaseModel):
    profile_id: str
    provider: str
    model: str


class CaseCreate(BaseModel):
    workflow_slug: str = Field(min_length=1, max_length=100)
    external_reference: str | None = Field(default=None, max_length=160)
    source_system: str = Field(min_length=1, max_length=120)
    data: dict[str, Any]

    @field_validator("data")
    @classmethod
    def validate_data_size_and_json(cls, data: dict[str, Any]) -> dict[str, Any]:
        try:
            encoded = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError) as error:
            raise ValueError("data must contain valid JSON values") from error
        if len(encoded) > 262_144:
            raise ValueError("data must not exceed 256 KiB")
        return data


class IntakeSource(BaseModel):
    source_id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_.-]{0,99}$")
    filename: str = Field(min_length=1, max_length=255)
    format: Literal["json", "fhir_json", "pdf", "xlsx"]
    field_map: dict[str, str] = Field(min_length=1, max_length=100)
    evidence_type: str = Field(default="source-document", min_length=1, max_length=100)
    worksheet: str | None = Field(default=None, max_length=100)
    row_index: int = Field(default=0, ge=0, le=10000)

    @field_validator("filename")
    @classmethod
    def filename_must_be_a_basename(cls, value: str) -> str:
        if "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("filename must be a basename")
        return value

    @field_validator("field_map")
    @classmethod
    def validate_field_map(cls, value: dict[str, str]) -> dict[str, str]:
        for target, locator in value.items():
            if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.-]{0,99}", target):
                raise ValueError("field_map targets must be valid field paths")
            if not locator or len(locator) > 512:
                raise ValueError("field_map locators must contain 1 to 512 characters")
        return value

    @model_validator(mode="after")
    def validate_format_options(self):
        if self.format in {"json", "fhir_json"} and any(
            not locator.startswith("/") for locator in self.field_map.values()
        ):
            raise ValueError("JSON and FHIR locators must be JSON Pointers")
        if self.format == "pdf" and self.worksheet is not None:
            raise ValueError("worksheet is only valid for XLSX sources")
        return self


class CaseIntakeCreate(BaseModel):
    workflow_slug: str = Field(min_length=1, max_length=100)
    source_system: str = Field(min_length=1, max_length=120)
    external_reference: str | None = Field(default=None, max_length=160)
    confirm_synthetic_data: Literal[True]
    sources: list[IntakeSource] = Field(min_length=2, max_length=20)

    @model_validator(mode="after")
    def validate_sources(self):
        filenames = [source.filename for source in self.sources]
        source_ids = [source.source_id for source in self.sources]
        if len(set(filenames)) != len(filenames):
            raise ValueError("source filenames must be unique")
        if len(set(source_ids)) != len(source_ids):
            raise ValueError("source IDs must be unique")
        targets = [
            target
            for source in self.sources
            for target in source.field_map
        ]
        if len(set(targets)) != len(targets):
            raise ValueError("field_map targets must be unique across sources")
        return self


class EvidenceCreate(BaseModel):
    evidence_type: str = Field(min_length=1, max_length=100)
    source_system: str = Field(min_length=1, max_length=120)
    reference: str = Field(min_length=1, max_length=2048)
    sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")
    attributes: dict[str, Any] = Field(default_factory=dict)


class ReviewCreate(BaseModel):
    decision: Literal["approve", "reject", "request_information"]
    note: str = Field(min_length=1, max_length=2000)
    confirm_qc_reviewed: bool = False
    confirm_evidence_reviewed: bool = False


class APIKeyCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    role: Literal["tenant_admin", "operator", "reviewer"]


class APIKeyRead(BaseModel):
    id: str
    name: str
    role: str
    token: str
    expires_at: datetime


class CheckRead(BaseModel):
    id: str
    run_id: str
    rule_key: str
    status: str
    result: dict[str, Any]
    checked_at: datetime


class EvidenceRead(BaseModel):
    id: str
    evidence_type: str
    source_system: str
    reference: str
    sha256: str | None
    attributes: dict[str, Any]
    captured_at: datetime
    created_at: datetime


class CaseRead(BaseModel):
    id: str
    workflow_id: str
    workflow_slug: str
    workflow_version: int
    external_reference: str | None
    source_system: str
    data: dict[str, Any]
    status: str
    created_at: datetime
    updated_at: datetime
    checks: list[CheckRead]
    evidence: list[EvidenceRead]


class AuditRead(BaseModel):
    id: str
    actor_api_key_id: str | None
    actor_role: str
    action: str
    details: dict[str, Any]
    occurred_at: datetime


class AgentFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_key: str
    severity: Literal["info", "low", "medium", "high"]
    explanation: str = Field(max_length=500)
    recommended_action: str = Field(max_length=500)


class AgentRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(max_length=1000)
    findings: list[AgentFinding] = Field(max_length=100)
    next_steps: list[Annotated[str, Field(max_length=500)]] = Field(max_length=20)
    confidence: float = Field(ge=0, le=1)
    human_review_required: Literal[True] = True


class AgentRunRead(BaseModel):
    id: str
    case_id: str
    provider: str
    profile_id: str
    model: str
    status: str
    input_digest: str
    recommendation: AgentRecommendation
    created_at: datetime


class BackgroundJobRead(BaseModel):
    id: str
    job_type: str
    case_id: str | None
    status: str
    progress: int
    progress_message: str
    attempts: int
    max_attempts: int
    result: dict[str, Any] | None
    error_code: str | None
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None


class JobAttemptRead(BaseModel):
    id: str
    job_id: str
    attempt_number: int
    status: str
    error_code: str | None
    worker_id: str
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


class MonitoringSummary(BaseModel):
    window_days: int
    cases_by_status: dict[str, int]
    jobs_by_status: dict[str, int]
    attempts_by_status: dict[str, int]
    daily_cases: list[dict[str, Any]]
    daily_jobs: list[dict[str, Any]]
    error_count: int


class AutomationJobCreate(BaseModel):
    case_id: str
    job_type: Literal["qc_revalidate", "agent_review"]
    max_attempts: int = Field(default=3, ge=1, le=10)


class AutomationScheduleCreate(BaseModel):
    case_id: str
    job_type: Literal["qc_revalidate", "agent_review"]
    interval_seconds: int = Field(ge=60, le=31_536_000)


class AutomationScheduleUpdate(BaseModel):
    enabled: bool


class AutomationScheduleRead(BaseModel):
    id: str
    case_id: str
    job_type: str
    interval_seconds: int
    enabled: bool
    next_run_at: datetime
    last_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


WorkflowCreate.model_rebuild()
SuggestedRule.model_rebuild()

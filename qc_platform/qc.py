import math
import regex
from uuid import uuid4
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from qc_platform.models import AuditEvent, Evidence, QCCase, QCCheck, Workflow
from qc_platform.security import Principal


def field_value(data: dict, path: str):
    value = data
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def run_checks(
    session: Session,
    case: QCCase,
    workflow: Workflow,
    principal: Principal,
) -> list[QCCheck]:
    run_id = str(uuid4())
    checks: list[QCCheck] = []
    for field in workflow.required_fields:
        present = field_value(case.data, field) not in (None, "")
        checks.append(
            QCCheck(
                tenant_id=case.tenant_id,
                case_id=case.id,
                run_id=run_id,
                rule_key=f"required_field:{field}",
                status="pass" if present else "fail",
                result={
                    "field": field,
                    "present": present,
                    "message": "" if present else f"Required field '{field}' is missing.",
                },
            )
        )

    for rule in workflow.field_rules:
        value = field_value(case.data, rule["field"])
        passed = evaluate_rule(value, rule, case.data)
        severity = rule.get("severity", "error")
        checks.append(
            QCCheck(
                tenant_id=case.tenant_id,
                case_id=case.id,
                run_id=run_id,
                rule_key=f"field_rule:{rule['key']}",
                status="pass" if passed else ("warning" if severity == "warning" else "fail"),
                result={
                    "field": rule["field"],
                    "operator": rule["operator"],
                    **(
                        {"compared_field": rule["value"]}
                        if rule["operator"] == "equals_field"
                        else {}
                    ),
                    "valid": passed,
                    "message": rule["message"],
                },
            )
        )

    evidence_types = session.scalars(
        select(Evidence.evidence_type).where(
            Evidence.tenant_id == case.tenant_id,
            Evidence.case_id == case.id,
        )
    ).all()
    if workflow.require_evidence and not workflow.required_evidence_types:
        evidence_exists = bool(evidence_types)
        checks.append(
            QCCheck(
                tenant_id=case.tenant_id,
                case_id=case.id,
                run_id=run_id,
                rule_key="evidence:at_least_one",
                status="pass" if evidence_exists else "fail",
                result={
                    "required": True,
                    "present": evidence_exists,
                    "message": "" if evidence_exists else "At least one evidence item is required.",
                },
            )
        )
    for evidence_type in workflow.required_evidence_types:
        evidence_exists = evidence_type in evidence_types
        checks.append(
            QCCheck(
                tenant_id=case.tenant_id,
                case_id=case.id,
                run_id=run_id,
                rule_key=f"evidence_type:{evidence_type}",
                status="pass" if evidence_exists else "fail",
                result={
                    "evidence_type": evidence_type,
                    "present": evidence_exists,
                    "message": (
                        "" if evidence_exists
                        else f"Required evidence type '{evidence_type}' is missing."
                    ),
                },
            )
        )

    session.add_all(checks)
    session.flush()
    has_failure = any(check.status == "fail" for check in checks)
    case.status = "needs_information" if has_failure else "in_review"
    session.add(
        AuditEvent(
            tenant_id=case.tenant_id,
            case_id=case.id,
            actor_api_key_id=principal.api_key_id,
            actor_role=principal.role,
            action="qc_checks_run",
            details={
                "run_id": run_id,
                "check_count": len(checks),
                "failed_count": sum(check.status == "fail" for check in checks),
            },
        )
    )
    return checks


def evaluate_rule(value, rule: dict, data: dict | None = None) -> bool:
    operator = rule["operator"]
    expected = rule.get("value")
    if operator == "required":
        return value not in (None, "")
    if operator == "equals_field":
        other_value = field_value(data or {}, expected)
        if value is None or other_value is None:
            return False
        if isinstance(value, (dict, list)) or isinstance(other_value, (dict, list)):
            return False
        return type(value) is type(other_value) and value == other_value
    if value is None:
        return False
    if operator == "regex":
        if not isinstance(value, str):
            return False
        try:
            return regex.fullmatch(expected, value, timeout=0.05) is not None
        except TimeoutError:
            return False
    if operator == "one_of":
        return isinstance(value, str) and value in expected
    if operator == "min_length":
        try:
            return len(value) >= expected
        except TypeError:
            return False
    if operator == "max_length":
        try:
            return len(value) <= expected
        except TypeError:
            return False
    if operator in {"min", "max"}:
        if isinstance(value, bool):
            return False
        try:
            numeric_value = float(value)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(numeric_value):
            return False
        return numeric_value >= expected if operator == "min" else numeric_value <= expected
    return False


def latest_checks(session: Session, case_id: str, tenant_id: str) -> list[QCCheck]:
    rows = session.scalars(
        select(QCCheck)
        .where(QCCheck.case_id == case_id, QCCheck.tenant_id == tenant_id)
        .order_by(QCCheck.checked_at.desc(), QCCheck.id.desc())
    ).all()
    latest: dict[str, QCCheck] = {}
    for row in rows:
        latest.setdefault(row.rule_key, row)
    return list(latest.values())

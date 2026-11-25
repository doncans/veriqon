import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from qc_platform.database import Base, make_engine, make_session_factory
from qc_platform.main import create_app
from qc_platform.models import APIKey, Tenant
from qc_platform.security import hash_api_key


@pytest.fixture
def platform(tmp_path):
    database_path = tmp_path / "test.db"
    engine = make_engine(f"sqlite:///{database_path}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    keys = {"tenant_admin": "admin-secret", "operator": "operator-secret", "reviewer": "reviewer-secret"}
    with factory() as session:
        for slug in ("tenant-one", "tenant-two"):
            tenant = Tenant(name=slug, slug=slug)
            session.add(tenant)
            session.flush()
            for role, token in keys.items():
                session.add(
                    APIKey(
                        tenant_id=tenant.id,
                        name=role,
                        role=role,
                        key_hash=hash_api_key(f"{slug}-{token}"),
                    )
                )
        session.commit()
    app = create_app(f"sqlite:///{database_path}")
    with TestClient(app) as client:
        yield client, keys
    app.state.engine.dispose()
    engine.dispose()


def auth(keys, role="tenant_admin", tenant="tenant-one"):
    return {"Authorization": f"Bearer {tenant}-{keys[role]}"}


def create_workflow(client, keys, tenant="tenant-one"):
    response = client.post(
        "/workflows",
        headers=auth(keys, tenant=tenant),
        json={
            "slug": "credentialing",
            "name": "Credentialing",
            "version": 1,
            "required_fields": ["provider.npi", "license_number"],
            "require_evidence": True,
            "agent_profile": "rules",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_case_requires_fields_and_evidence_before_review_approval(platform):
    client, keys = platform
    create_workflow(client, keys)
    response = client.post(
        "/cases",
        headers=auth(keys, "operator"),
        json={
            "workflow_slug": "credentialing",
            "source_system": "roster-upload",
            "data": {"provider": {"npi": "1234567890"}},
        },
    )
    assert response.status_code == 201
    case = response.json()
    assert case["status"] == "needs_information"
    assert {check["rule_key"]: check["status"] for check in case["checks"]} == {
        "required_field:provider.npi": "pass",
        "required_field:license_number": "fail",
        "evidence:at_least_one": "fail",
    }
    approval = client.post(
        f"/cases/{case['id']}/review",
        headers=auth(keys, "reviewer"),
        json={"decision": "approve", "note": "Reviewed."},
    )
    assert approval.status_code == 409

    evidence = client.post(
        f"/cases/{case['id']}/evidence",
        headers=auth(keys, "operator"),
        json={
            "evidence_type": "license",
            "source_system": "state-board",
            "reference": "evidence://license/1",
        },
    )
    assert evidence.status_code == 201
    assert evidence.json()["status"] == "needs_information"

    completed = client.post(
        f"/cases/{case['id']}/revalidate",
        headers=auth(keys, "operator"),
    )
    assert completed.status_code == 200
    reviewed = client.post(
        f"/cases/{case['id']}/review",
        headers=auth(keys, "reviewer"),
        json={"decision": "request_information", "note": "Update license number."},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "needs_information"


def test_case_list_can_filter_by_status(platform):
    client, keys = platform
    create_workflow(client, keys)
    case_ids = []
    for data in (
        {"provider": {"npi": "1234567890"}},
        {"provider": {"npi": "1234567890"}, "license_number": "CA-1"},
    ):
        response = client.post(
            "/cases",
            headers=auth(keys, "operator"),
            json={
                "workflow_slug": "credentialing",
                "source_system": "roster",
                "data": data,
            },
        )
        assert response.status_code == 201
        case_ids.append(response.json()["id"])
    evidence = client.post(
        f"/cases/{case_ids[1]}/evidence",
        headers=auth(keys, "operator"),
        json={
            "evidence_type": "license",
            "source_system": "state-board",
            "reference": "evidence://license/1",
        },
    )
    assert evidence.status_code == 201
    waiting = client.get("/cases?status=needs_information", headers=auth(keys, "reviewer"))
    in_review = client.get("/cases?status=in_review", headers=auth(keys, "reviewer"))
    invalid = client.get("/cases?status=unrecognized", headers=auth(keys, "reviewer"))
    assert [case["status"] for case in waiting.json()] == ["needs_information"]
    assert [case["status"] for case in in_review.json()] == ["in_review"]
    assert invalid.status_code == 422


def test_tenant_isolation_and_role_enforcement(platform):
    client, keys = platform
    create_workflow(client, keys)
    created = client.post(
        "/cases",
        headers=auth(keys, "operator"),
        json={
            "workflow_slug": "credentialing",
            "source_system": "roster",
            "data": {"provider": {"npi": "1234567890"}, "license_number": "CA-1"},
        },
    )
    assert created.status_code == 201
    case_id = created.json()["id"]

    cross_tenant = client.get(f"/cases/{case_id}", headers=auth(keys, tenant="tenant-two"))
    assert cross_tenant.status_code == 404

    forbidden_workflow = client.post(
        "/workflows",
        headers=auth(keys, "operator"),
        json={
            "slug": "not-allowed",
            "name": "Not allowed",
            "version": 1,
            "required_fields": ["field"],
        },
    )
    assert forbidden_workflow.status_code == 403


def test_approval_and_audit_trail(platform):
    client, keys = platform
    create_workflow(client, keys)
    created = client.post(
        "/cases",
        headers=auth(keys, "operator"),
        json={
            "workflow_slug": "credentialing",
            "source_system": "roster",
            "data": {"provider": {"npi": "1234567890"}, "license_number": "CA-1"},
        },
    )
    case_id = created.json()["id"]
    added = client.post(
        f"/cases/{case_id}/evidence",
        headers=auth(keys, "operator"),
        json={
            "evidence_type": "license",
            "source_system": "state-board",
            "reference": "evidence://license/1",
        },
    )
    assert added.json()["status"] == "in_review"
    queued_qc = client.post(
        "/automation/jobs",
        headers=auth(keys, "operator"),
        json={"case_id": case_id, "job_type": "qc_revalidate"},
    )
    assert queued_qc.status_code == 202
    approved = client.post(
        f"/cases/{case_id}/review",
        headers=auth(keys, "reviewer"),
        json={
            "decision": "approve",
            "note": "Evidence and checks reviewed.",
            "confirm_qc_reviewed": True,
            "confirm_evidence_reviewed": True,
        },
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "approved"
    add_after_approval = client.post(
        f"/cases/{case_id}/evidence",
        headers=auth(keys, "operator"),
        json={
            "evidence_type": "updated-license",
            "source_system": "state-board",
            "reference": "evidence://license/updated",
        },
    )
    revalidate_after_approval = client.post(
        f"/cases/{case_id}/revalidate",
        headers=auth(keys, "operator"),
    )
    assert add_after_approval.status_code == 409
    assert revalidate_after_approval.status_code == 409
    still_approved = client.get(f"/cases/{case_id}", headers=auth(keys, "reviewer"))
    assert still_approved.json()["status"] == "approved"
    audit = client.get(f"/cases/{case_id}/audit", headers=auth(keys, "reviewer"))
    assert audit.status_code == 200
    decision = next(event for event in audit.json() if event["action"] == "human_review")
    assert decision["details"]["confirm_qc_reviewed"] is True
    assert decision["details"]["confirm_evidence_reviewed"] is True
    assert decision["details"]["qc_check_ids"]
    assert decision["details"]["evidence_ids"]
    from qc_platform.worker import process_one_job

    assert process_one_job(client.app.state.session_factory, "post-approval-worker")
    post_approval_job = client.get(
        f"/agent-jobs/{queued_qc.json()['id']}",
        headers=auth(keys, "reviewer"),
    )
    assert post_approval_job.json()["status"] == "failed"
    assert post_approval_job.json()["error_code"] == "case_finalized"
    final_case = client.get(f"/cases/{case_id}", headers=auth(keys, "reviewer"))
    assert final_case.json()["status"] == "approved"


def test_approval_requires_human_qc_and_evidence_attestations(platform):
    client, keys = platform
    create_workflow(client, keys)
    created = client.post(
        "/cases",
        headers=auth(keys, "operator"),
        json={
            "workflow_slug": "credentialing",
            "source_system": "roster",
            "data": {"provider": {"npi": "1234567890"}, "license_number": "CA-1"},
        },
    )
    case_id = created.json()["id"]
    evidence = client.post(
        f"/cases/{case_id}/evidence",
        headers=auth(keys, "operator"),
        json={
            "evidence_type": "license",
            "source_system": "state-board",
            "reference": "evidence://license/1",
        },
    )
    assert evidence.status_code == 201
    response = client.post(
        f"/cases/{case_id}/review",
        headers=auth(keys, "reviewer"),
        json={"decision": "approve", "note": "Reviewed."},
    )
    assert response.status_code == 422
    unchanged = client.get(f"/cases/{case_id}", headers=auth(keys, "reviewer"))
    assert unchanged.json()["status"] == "in_review"


def test_authentication_required(platform):
    client, _ = platform
    assert client.get("/cases").status_code == 401


def test_agent_review_is_tenant_scoped_audited_and_never_approves(platform):
    client, keys = platform
    available = client.get("/model-profiles", headers=auth(keys))
    assert available.status_code == 200
    assert any(profile["profile_id"] == "rules" for profile in available.json())
    create_workflow(client, keys)
    created = client.post(
        "/cases",
        headers=auth(keys, "operator"),
        json={
            "workflow_slug": "credentialing",
            "source_system": "roster",
            "data": {"provider": {"npi": "1234567890"}},
        },
    )
    case_id = created.json()["id"]

    recommendation = client.post(
        f"/cases/{case_id}/agent-review",
        headers={
            "Authorization": f"Bearer tenant-one-{keys['operator']}",
            "Idempotency-Key": "agent-job-1",
        },
    )
    assert recommendation.status_code == 202
    job = recommendation.json()
    assert job["status"] == "queued"
    assert job["progress"] == 0

    from qc_platform.worker import process_one_job

    assert process_one_job(client.app.state.session_factory, "test-worker")
    tracked = client.get(
        f"/agent-jobs/{job['id']}",
        headers=auth(keys, "reviewer"),
    )
    assert tracked.status_code == 200
    assert tracked.json()["status"] == "succeeded"
    assert tracked.json()["progress"] == 100

    runs = client.get(
        f"/cases/{case_id}/agent-runs",
        headers=auth(keys, "reviewer"),
    )
    assert runs.status_code == 200
    assert len(runs.json()) == 1
    result = runs.json()[0]
    assert result["provider"] == "rules"
    assert result["recommendation"]["human_review_required"] is True
    assert result["recommendation"]["findings"]
    assert "approval" not in result["recommendation"]
    assert result["recommendation"]["next_steps"]

    case = client.get(f"/cases/{case_id}", headers=auth(keys, "reviewer"))
    assert case.json()["status"] == "needs_information"
    other_tenant = client.post(
        f"/cases/{case_id}/agent-review",
        headers=auth(keys, "operator", "tenant-two"),
    )
    assert other_tenant.status_code == 404

    audit = client.get(f"/cases/{case_id}/audit", headers=auth(keys, "reviewer"))
    assert any(event["action"] == "agent_review_completed" for event in audit.json())
    history = client.get(f"/cases/{case_id}/agent-runs", headers=auth(keys, "reviewer"))
    assert history.status_code == 200
    assert history.json()[0]["id"] == result["id"]

    repeated = client.post(
        f"/cases/{case_id}/agent-review",
        headers={
            "Authorization": f"Bearer tenant-one-{keys['operator']}",
            "Idempotency-Key": "agent-job-1",
        },
    )
    assert repeated.status_code == 202
    assert repeated.json()["id"] == job["id"]


def test_tenant_specific_model_profiles_are_allowlisted(monkeypatch):
    import json

    from qc_platform.config import available_model_profiles, get_model_profile

    profiles = {
        "tenant-a-model": {
            "provider": "openai_compatible",
            "model": "client-open-model",
            "base_url": "https://models.client.example/v1",
            "allowed_tenants": ["tenant-one"],
            "api_key_env": "CLIENT_MODEL_KEY",
        }
    }
    monkeypatch.setattr("qc_platform.config.QC_MODEL_PROFILES", json.dumps(profiles))
    available = available_model_profiles("tenant-one")
    assert [profile_id for profile_id, _ in available] == ["default", "rules", "tenant-a-model"]
    with pytest.raises(PermissionError):
        get_model_profile("tenant-a-model", "tenant-two")


def test_workflow_field_rules_are_tenant_configurable(platform):
    client, keys = platform
    response = client.post(
        "/workflows",
        headers=auth(keys),
        json={
            "slug": "application-qc",
            "name": "Generic Application QC",
            "version": 1,
            "required_fields": ["applicant.id"],
            "field_rules": [
                {
                    "key": "email-format",
                    "field": "applicant.email",
                    "operator": "regex",
                    "value": r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
                    "severity": "error",
                    "message": "Provide a valid email address.",
                },
                {
                    "key": "priority-warning",
                    "field": "priority",
                    "operator": "one_of",
                    "value": ["normal", "urgent"],
                    "severity": "warning",
                    "message": "Check the submitted priority.",
                },
            ],
        },
    )
    assert response.status_code == 201, response.text

    created = client.post(
        "/cases",
        headers=auth(keys, "operator"),
        json={
            "workflow_slug": "application-qc",
            "source_system": "client-application",
            "data": {"applicant": {"id": "A1", "email": "invalid"}, "priority": "low"},
        },
    )
    assert created.status_code == 201
    check_results = {check["rule_key"]: check["status"] for check in created.json()["checks"]}
    assert check_results == {
        "required_field:applicant.id": "pass",
        "field_rule:email-format": "fail",
        "field_rule:priority-warning": "warning",
    }


def test_workflow_can_compare_client_and_payer_fields(platform):
    client, keys = platform
    workflow = client.post(
        "/workflows",
        headers=auth(keys),
        json={
            "slug": "cross-platform-credentialing",
            "name": "Cross-platform credentialing",
            "version": 1,
            "field_rules": [
                {
                    "key": "payer-npi-matches-client",
                    "field": "client.provider.npi",
                    "operator": "equals_field",
                    "value": "payer.provider.npi",
                    "severity": "error",
                    "message": "Client and payer provider NPIs must match.",
                }
            ],
        },
    )
    assert workflow.status_code == 201, workflow.text

    matching = client.post(
        "/cases",
        headers=auth(keys, "operator"),
        json={
            "workflow_slug": "cross-platform-credentialing",
            "source_system": "cross-platform-intake",
            "data": {
                "client": {"provider": {"npi": "1234567890"}},
                "payer": {"provider": {"npi": "1234567890"}},
            },
        },
    )
    mismatching = client.post(
        "/cases",
        headers=auth(keys, "operator"),
        json={
            "workflow_slug": "cross-platform-credentialing",
            "source_system": "cross-platform-intake",
            "data": {
                "client": {"provider": {"npi": "1234567890"}},
                "payer": {"provider": {"npi": "9999999999"}},
            },
        },
    )
    check = matching.json()["checks"][0]
    assert check["status"] == "pass"
    assert check["result"]["compared_field"] == "payer.provider.npi"
    failed_check = mismatching.json()["checks"][0]
    assert failed_check["status"] == "fail"
    assert failed_check["result"]["message"] == "Client and payer provider NPIs must match."
    assert "1234567890" not in str(failed_check["result"])
    assert mismatching.json()["status"] == "needs_information"


def make_text_pdf(text):
    content = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(content)} >>\nstream\n".encode() + content + b"\nendstream",
    ]
    document = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for index, value in enumerate(objects, start=1):
        offsets.append(len(document))
        document.extend(f"{index} 0 obj\n".encode() + value + b"\nendobj\n")
    xref_offset = len(document)
    document.extend(f"xref\n0 {len(offsets)}\n".encode())
    document.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        document.extend(f"{offset:010} 00000 n \n".encode())
    document.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode()
    )
    return bytes(document)


def test_document_parsers_support_json_fhir_text_pdf_and_xlsx():
    from openpyxl import Workbook

    from qc_platform.document_intake import parse_source
    from qc_platform.schemas import IntakeSource

    json_source = IntakeSource(
        source_id="client",
        filename="client.json",
        format="json",
        field_map={"client.provider.npi": "/provider/identifier/0/value"},
    )
    parsed_json = parse_source(
        json_source,
        b'{"provider":{"identifier":[{"value":"1234567890"}]}}',
    )
    assert parsed_json.values == {"client.provider.npi": "1234567890"}

    fhir_source = IntakeSource(
        source_id="fhir",
        filename="practitioner.json",
        format="fhir_json",
        field_map={"practitioner.npi": "/entry/0/resource/identifier/0/value"},
    )
    parsed_fhir = parse_source(
        fhir_source,
        b'{"resourceType":"Bundle","entry":[{"resource":{"resourceType":"Practitioner",'
        b'"identifier":[{"value":"1234567890"}]}}]}',
    )
    assert parsed_fhir.values == {"practitioner.npi": "1234567890"}

    pdf_source = IntakeSource(
        source_id="form",
        filename="form.pdf",
        format="pdf",
        field_map={"submission.npi": r"NPI:\s*([0-9]+)"},
    )
    parsed_pdf = parse_source(pdf_source, make_text_pdf("NPI: 1234567890"))
    assert parsed_pdf.values == {"submission.npi": "1234567890"}
    assert parsed_pdf.extracted_text_characters > 0

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Provider NPI", "Provider name"])
    sheet.append(["1234567890", "Northstar Clinic"])
    workbook_file = io.BytesIO()
    workbook.save(workbook_file)
    xlsx_source = IntakeSource(
        source_id="payer",
        filename="payer.xlsx",
        format="xlsx",
        field_map={
            "payer.provider.npi": "Provider NPI",
            "payer.provider.name": "Provider name",
        },
    )
    parsed_xlsx = parse_source(xlsx_source, workbook_file.getvalue())
    assert parsed_xlsx.values == {
        "payer.provider.npi": "1234567890",
        "payer.provider.name": "Northstar Clinic",
    }


def test_document_parser_rejects_scanned_pdf_without_ocr():
    from pypdf import PdfWriter

    from qc_platform.document_intake import parse_source
    from qc_platform.schemas import IntakeSource

    writer = PdfWriter()
    writer.add_blank_page(width=300, height=100)
    pdf_file = io.BytesIO()
    writer.write(pdf_file)
    source = IntakeSource(
        source_id="scan",
        filename="scan.pdf",
        format="pdf",
        field_map={"document.identifier": r"Identifier:\s*([A-Za-z0-9-]+)"},
    )
    with pytest.raises(ValueError, match="scanned PDFs require OCR"):
        parse_source(source, pdf_file.getvalue())


def test_cross_platform_case_intake_uploads_client_and_payer_sources(platform):
    client, keys = platform
    workflow = client.post(
        "/workflows",
        headers=auth(keys),
        json={
            "slug": "cross-platform-intake",
            "name": "Cross-platform intake",
            "version": 1,
            "required_fields": ["client.provider.npi", "payer.provider.npi"],
            "field_rules": [
                {
                    "key": "payer-npi-matches-client",
                    "field": "client.provider.npi",
                    "operator": "equals_field",
                    "value": "payer.provider.npi",
                    "severity": "error",
                    "message": "Client and payer provider NPIs must match.",
                }
            ],
        },
    )
    assert workflow.status_code == 201, workflow.text
    manifest = {
        "workflow_slug": "cross-platform-intake",
        "source_system": "cross-platform-intake",
        "external_reference": "synthetic-case-01",
        "confirm_synthetic_data": True,
        "sources": [
            {
                "source_id": "client_platform",
                "filename": "client.json",
                "format": "json",
                "field_map": {"client.provider.npi": "/provider/npi"},
            },
            {
                "source_id": "payer_roster",
                "filename": "payer.xlsx",
                "format": "xlsx",
                "field_map": {"payer.provider.npi": "Provider NPI"},
            },
        ],
    }
    from openpyxl import Workbook

    workbook = Workbook()
    workbook.active.append(["Provider NPI"])
    workbook.active.append(["1234567890"])
    xlsx_file = io.BytesIO()
    workbook.save(xlsx_file)

    response = client.post(
        "/cases/intake",
        headers=auth(keys, "operator"),
        data={"manifest": json.dumps(manifest)},
        files=[
            ("files", ("client.json", b'{"provider":{"npi":"1234567890"}}', "application/json")),
            ("files", ("payer.xlsx", xlsx_file.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")),
        ],
    )
    assert response.status_code == 201, response.text
    case = response.json()
    assert case["status"] == "in_review"
    assert case["data"] == {
        "client": {"provider": {"npi": "1234567890"}},
        "payer": {"provider": {"npi": "1234567890"}},
    }
    assert len(case["evidence"]) == 2
    assert {check["rule_key"]: check["status"] for check in case["checks"]} == {
        "required_field:client.provider.npi": "pass",
        "required_field:payer.provider.npi": "pass",
        "field_rule:payer-npi-matches-client": "pass",
    }


def test_sop_agent_suggestions_are_tenant_scoped_and_require_human_approval(platform, monkeypatch):
    from qc_platform.models import BackgroundJob
    from qc_platform.schemas import RuleSuggestionResult, SuggestedRule
    from qc_platform.worker import process_one_job

    client, keys = platform
    created_workflow = client.post(
        "/workflows",
        headers=auth(keys),
        json={
            "slug": "openemr-provider-intake",
            "name": "OpenEMR FHIR Provider Intake Demo",
            "version": 1,
            "required_fields": ["practitioner.identifier"],
            "agent_profile": "default",
        },
    )
    assert created_workflow.status_code == 201, created_workflow.text
    workflow_id = created_workflow.json()["id"]
    sop_text = "The practitioner's identifier must be present."
    sop_response = client.post(
        f"/workflows/{workflow_id}/sops",
        headers=auth(keys),
        json={
            "title": "Provider intake sample SOP",
            "content": sop_text,
            "confirm_no_phi_or_secrets": True,
        },
    )
    assert sop_response.status_code == 201, sop_response.text
    assert sop_response.json()["version"] == 1

    suggestion = client.post(
        f"/workflows/{workflow_id}/rule-suggestions",
        headers=auth(keys),
        json={
            "sop_id": sop_response.json()["id"],
            "candidate_fields": ["practitioner.identifier"],
            "confirm_no_phi_or_secrets": True,
        },
    )
    assert suggestion.status_code == 202, suggestion.text
    job_id = suggestion.json()["job_id"]
    premature_approval = client.post(
        f"/workflows/{workflow_id}/rule-suggestions/{job_id}/approve",
        headers=auth(keys),
        json={"rule_keys": ["identifier-required"]},
    )
    assert premature_approval.status_code == 409
    cross_tenant = client.get(
        f"/workflows/{workflow_id}/sops",
        headers=auth(keys, "tenant_admin", "tenant-two"),
    )
    assert cross_tenant.status_code == 404
    reviewer_read = client.get(
        f"/workflows/{workflow_id}/sops",
        headers=auth(keys, "reviewer"),
    )
    assert reviewer_read.status_code == 403

    received_payload = {}

    def fake_suggestions(payload, profile):
        received_payload.update(payload)
        return RuleSuggestionResult(
            summary="The SOP supports one draft rule.",
            suggestions=[
                SuggestedRule(
                    rule={
                        "key": "identifier-required",
                        "field": "practitioner.identifier",
                        "operator": "required",
                        "value": None,
                        "severity": "error",
                        "message": "A practitioner identifier must be supplied.",
                    },
                    rationale="The SOP says the identifier must be present.",
                    sop_excerpt="The practitioner's identifier must be present.",
                )
            ],
        )

    monkeypatch.setattr("qc_platform.worker.make_rule_suggestions", fake_suggestions)
    assert process_one_job(client.app.state.session_factory, "suggestion-worker")
    tracked = client.get(f"/agent-jobs/{job_id}", headers=auth(keys, "tenant_admin"))
    assert tracked.status_code == 200
    assert tracked.json()["status"] == "succeeded"
    assert received_payload == {
        "sop_text": sop_text,
        "candidate_fields": ["practitioner.identifier"],
        "existing_rule_keys": [],
    }

    operator_approval = client.post(
        f"/workflows/{workflow_id}/rule-suggestions/{job_id}/approve",
        headers=auth(keys, "operator"),
        json={"rule_keys": ["identifier-required"]},
    )
    assert operator_approval.status_code == 403
    approved = client.post(
        f"/workflows/{workflow_id}/rule-suggestions/{job_id}/approve",
        headers=auth(keys),
        json={"rule_keys": ["identifier-required"]},
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["version"] == 2
    assert approved.json()["field_rules"][0]["key"] == "identifier-required"
    workflow_v2_sops = client.get(
        f"/workflows/{approved.json()['id']}/sops",
        headers=auth(keys),
    )
    assert workflow_v2_sops.status_code == 200
    assert [item["id"] for item in workflow_v2_sops.json()] == [sop_response.json()["id"]]
    sop_v2 = client.post(
        f"/workflows/{approved.json()['id']}/sops",
        headers=auth(keys),
        json={
            "title": "Provider intake sample SOP revision",
            "content": "The practitioner's identifier must remain present.",
            "confirm_no_phi_or_secrets": True,
        },
    )
    assert sop_v2.status_code == 201
    assert sop_v2.json()["version"] == 2
    stale_approval = client.post(
        f"/workflows/{workflow_id}/rule-suggestions/{job_id}/approve",
        headers=auth(keys),
        json={"rule_keys": ["identifier-required"]},
    )
    assert stale_approval.status_code == 409
    with client.app.state.session_factory() as session:
        job = session.get(BackgroundJob, job_id)
        assert job is not None
        assert job.case_id is None


def test_model_recommendations_only_reference_failed_checks():
    from qc_platform.agent import make_recommendation
    from qc_platform.config import ModelProfile

    profile = ModelProfile(
        provider="rules",
        model="qc-rules-v1",
        base_url="",
        allowed_tenants=frozenset({"tenant-one"}),
    )
    recommendation = make_recommendation(
        {
            "prompt_version": "test",
            "workflow_version": 1,
            "checks": [
                {"rule_key": "safe-rule", "status": "pass"},
                {"rule_key": "failed-rule", "status": "fail"},
            ],
            "evidence_types": [],
        },
        profile,
    )
    assert [finding.rule_key for finding in recommendation.findings] == ["failed-rule"]
    assert recommendation.human_review_required is True


def test_automation_qc_jobs_tracking_and_tenant_monitoring(platform):
    from datetime import timedelta

    from sqlalchemy import select

    from qc_platform.models import APIKey, AutomationSchedule, BackgroundJob, Tenant, utc_now
    from qc_platform.worker import enqueue_due_schedules, process_one_job

    client, keys = platform
    create_workflow(client, keys)
    created = client.post(
        "/cases",
        headers=auth(keys, "operator"),
        json={
            "workflow_slug": "credentialing",
            "source_system": "roster",
            "data": {"provider": {"npi": "1234567890"}},
        },
    )
    case_id = created.json()["id"]

    job_response = client.post(
        "/automation/jobs",
        headers={
            "Authorization": f"Bearer tenant-one-{keys['operator']}",
            "Idempotency-Key": "qc-run-1",
        },
        json={"case_id": case_id, "job_type": "qc_revalidate"},
    )
    assert job_response.status_code == 202
    job_id = job_response.json()["id"]
    assert process_one_job(client.app.state.session_factory, "qc-worker")
    finished = client.get(f"/agent-jobs/{job_id}", headers=auth(keys, "reviewer"))
    assert finished.status_code == 200
    assert finished.json()["status"] == "succeeded"
    assert finished.json()["result"]["failed_count"] == 2
    attempts = client.get(f"/agent-jobs/{job_id}/attempts", headers=auth(keys, "reviewer"))
    assert len(attempts.json()) == 1
    assert attempts.json()[0]["status"] == "succeeded"
    summary = client.get("/monitoring/api/summary", headers=auth(keys, "reviewer"))
    assert summary.status_code == 200
    assert summary.json()["jobs_by_status"]["succeeded"] == 1

    schedule_response = client.post(
        "/automation/schedules",
        headers=auth(keys),
        json={"case_id": case_id, "job_type": "qc_revalidate", "interval_seconds": 60},
    )
    assert schedule_response.status_code == 201
    schedule_id = schedule_response.json()["id"]
    with client.app.state.session_factory() as session:
        schedule = session.scalar(
            select(AutomationSchedule).where(AutomationSchedule.id == schedule_id)
        )
        schedule.next_run_at = utc_now() - timedelta(seconds=1)
        session.commit()
    assert enqueue_due_schedules(client.app.state.session_factory) == 1
    with client.app.state.session_factory() as session:
        scheduled_job = session.scalar(
            select(BackgroundJob).where(
                BackgroundJob.tenant_id
                == session.scalar(
                    select(AutomationSchedule.tenant_id).where(
                        AutomationSchedule.id == schedule_id
                    )
                ),
                BackgroundJob.idempotency_key.like(f"schedule:{schedule_id}:%"),
            )
        )
        assert scheduled_job is not None
        scheduled_job_id = scheduled_job.id
    assert process_one_job(client.app.state.session_factory, "qc-worker")
    scheduled_status = client.get(
        f"/agent-jobs/{scheduled_job_id}",
        headers=auth(keys, "reviewer"),
    )
    assert scheduled_status.json()["status"] == "succeeded"

    cross_tenant_summary = client.get(
        "/monitoring/api/summary",
        headers=auth(keys, "reviewer", "tenant-two"),
    )
    assert cross_tenant_summary.status_code == 200
    assert cross_tenant_summary.json()["jobs_by_status"] == {}

    with client.app.state.session_factory() as session:
        tenant_id = session.scalar(select(Tenant.id).where(Tenant.slug == "tenant-one"))
        actor_id = session.scalar(
            select(APIKey.id).where(
                APIKey.tenant_id == tenant_id,
                APIKey.role == "operator",
            )
        )
        failed_job = BackgroundJob(
            tenant_id=tenant_id,
            actor_api_key_id=actor_id,
            job_type="unsupported-internal-job",
            payload={},
        )
        session.add(failed_job)
        session.commit()
        failed_job_id = failed_job.id
    assert process_one_job(client.app.state.session_factory, "qc-worker")
    failed = client.get(f"/agent-jobs/{failed_job_id}", headers=auth(keys, "reviewer"))
    assert failed.json()["status"] == "failed"
    assert failed.json()["error_code"] == "unsupported_job"
    error_log = client.get("/monitoring/api/errors", headers=auth(keys, "reviewer"))
    assert error_log.status_code == 200
    assert error_log.json()[0]["error_code"] == "unsupported_job"


def test_ui_is_a_separate_react_service(platform):
    client, _ = platform
    assert client.get("/monitoring").status_code == 404
    assert client.get("/assets/monitoring.js").status_code == 404
    workspace = Path(__file__).resolve().parents[1] / "ui"
    package = json.loads((workspace / "package.json").read_text())
    assert "react" in package["dependencies"]
    assert "react-dom" in package["dependencies"]
    assert package["scripts"]["dev"].startswith("vite ")
    app_source = (workspace / "src" / "App.jsx").read_text()
    assert "function WorkflowPanel" in app_source
    assert "function CasePanel" in app_source
    assert "function MonitoringPanel" in app_source


def test_api_allows_configured_ui_origin(platform):
    client, _ = platform
    response = client.options(
        "/workflows",
        headers={
            "Origin": "http://127.0.0.1:8001",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        },
    )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:8001"
    assert "authorization" in response.headers["access-control-allow-headers"].lower()

    untrusted = client.get("/health", headers={"Origin": "https://untrusted.example"})
    assert "access-control-allow-origin" not in untrusted.headers


def test_mcp_server_exposes_only_tenant_api_tools():
    import asyncio

    from qc_platform.mcp_server import create_mcp_server

    server = create_mcp_server("http://127.0.0.1:8000", "local-test-key")
    tools = asyncio.run(server.list_tools())
    names = {tool.name for tool in tools}
    assert names == {
        "list_workflows",
        "get_case",
        "submit_case",
        "enqueue_qc_job",
        "get_job_status",
    }


def test_mcp_client_rejects_non_loopback_http_and_invalid_ids():
    from qc_platform.mcp_server import QCPlatformClient, _validated_id

    with pytest.raises(ValueError):
        QCPlatformClient("http://model.example", "test-key")
    with pytest.raises(ValueError):
        _validated_id("../../automation/jobs", "job_id")


def test_mcp_request_preserves_bearer_auth_when_adding_headers(monkeypatch):
    import httpx

    from qc_platform.mcp_server import QCPlatformClient

    http_client = httpx.Client
    captured = {}

    def handle(request):
        captured.update(request.headers)
        return httpx.Response(202, json={"status": "queued"})

    monkeypatch.setattr(
        "qc_platform.mcp_server.httpx.Client",
        lambda **kwargs: http_client(
            transport=httpx.MockTransport(handle),
            **kwargs,
        ),
    )
    client = QCPlatformClient("http://127.0.0.1:8000", "tenant-scoped-secret")
    response = client.request(
        "POST",
        "/automation/jobs",
        headers={"Idempotency-Key": "job-1"},
        json={"case_id": "case-id", "job_type": "qc_revalidate"},
    )
    assert response == {"status": "queued"}
    assert captured["authorization"] == "Bearer tenant-scoped-secret"
    assert captured["idempotency-key"] == "job-1"


def test_agent_input_excludes_case_data_and_evidence_references(platform):
    from qc_platform.agent import build_agent_input
    from qc_platform.models import Evidence, QCCheck, QCCase, Workflow

    _, _ = platform
    case = QCCase(
        id="case",
        tenant_id="tenant",
        workflow_id="workflow",
        source_system="upload",
        data={"secret": "sensitive-provider-data"},
        status="needs_information",
    )
    workflow = Workflow(
        id="workflow",
        tenant_id="tenant",
        slug="credentialing",
        name="Credentialing",
        version=1,
        required_fields=["secret"],
        require_evidence=True,
    )
    evidence = Evidence(
        id="evidence",
        tenant_id="tenant",
        case_id="case",
        evidence_type="license",
        source_system="state-board",
        reference="https://internal.example/secret-token",
    )
    check = QCCheck(
        id="check",
        tenant_id="tenant",
        case_id="case",
        run_id="run",
        rule_key="required_field:secret",
        status="fail",
        result={"field": "secret", "present": False},
    )

    payload = build_agent_input(case, workflow, [check], [evidence])
    encoded = str(payload)
    assert "sensitive-provider-data" not in encoded
    assert "internal.example" not in encoded
    assert payload["checks"][0]["status"] == "fail"


def test_request_body_size_is_limited(platform):
    client, _ = platform
    oversized = client.post("/health", content=b"x" * (1_048_577))
    assert oversized.status_code == 413

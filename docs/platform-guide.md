# How VERIQON Works

VERIQON is an AI-enabled quality operations platform by AXIOVARA. It is a
tenant-scoped service for structured
application processes such as provider credentialing, vendor onboarding,
licensing, and payer enrollment. Each tenant defines its own versioned rules,
submits cases, tracks evidence and automated checks, and records human review
decisions.

The platform provides decision support and an auditable workflow. It does not
make final approval decisions or submit information to client portals.

## The services

```text
React UI ───────────────┐
Client systems ─────────┼──> FastAPI ──> tenant-scoped database
MCP client ──> MCP API ─┘                 ├─ QC checks and audit events
                                          └─ SQL-backed job queue
                                                   │
                                                   v
                                            worker process
                                            └─ agent/model calls
```

| Service | What it does |
|---|---|
| React UI (`ui/`) | Runs separately from the API. The client workspace configures workflows and supports case review; the monitoring page shows cases, jobs, retries, and errors. The API key is kept in page memory. |
| FastAPI (`qc_platform.main`) | Authenticates API keys, enforces tenant and role boundaries, validates requests, executes synchronous QC, and writes tenant data and audit events. |
| Worker (`qc_platform.worker`) | Claims queued jobs, runs background QC or model tasks, records progress and attempts, retries eligible failures, and fires due schedules. |
| MCP adapter (`qc_platform.mcp_server`) | Gives an MCP host a small set of tools backed by the same authenticated REST API. It does not grant extra privileges. |

The API and worker share a database and SQL-backed queue. They are separate
processes, but this is not yet a broker-backed, independently scalable
microservice architecture. The React UI can be deployed separately from the
API. In local development, the UI runs on port `8001` and the API on `8000`;
`VITE_API_BASE_URL` selects the API, and `QC_UI_ORIGINS` on the API controls
which browser origins may call it.

## Tenant access and roles

Every REST call uses a tenant API key in the `Authorization: Bearer ...`
header. The API stores a hash of the key, checks expiry and revocation, and
scopes data queries to the key's tenant. A key from another tenant cannot read
or change this tenant's records.

| Role | Typical permissions |
|---|---|
| `tenant_admin` | Configure workflow versions, manage API keys, save SOPs, approve selected rule suggestions, create schedules, submit cases, and review cases. |
| `operator` | Submit cases, attach evidence, and enqueue supported automation jobs. Does not configure workflows or make review decisions. |
| `reviewer` | Read cases and monitoring information, request agent review, and record approve/reject/request-information decisions. Does not configure workflows. |

The bootstrap command creates the first tenant administrator key. An
administrator can create shorter-lived operator and reviewer keys with
`POST /api-keys` and revoke them with `DELETE /api-keys/{key_id}`. Treat the
initial key as a secret and do not put real keys in source control or shared
examples.

## Workflow, case, and review lifecycle

1. **Configure a workflow.** A tenant administrator creates a version with a
   slug, required field paths, field rules, evidence requirements, and an
   enabled model profile. Dot-separated paths address nested JSON values.
   Examples of supported rule operators are `required`, `regex`, `one_of`,
   `min_length`, `max_length`, `min`, `max`, and `equals_field` for comparing
   two mapped case fields.
2. **Submit a case.** An administrator or operator submits JSON against the
   active workflow for a slug. The API runs deterministic QC immediately and
   records the case, check results, and audit events.
3. **Resolve evidence gaps.** Evidence is attached as metadata: type, source,
   reference, optional SHA-256, and attributes. The platform does not upload,
   store, or download the referenced file. Adding evidence runs QC again.
4. **Review the case.** A reviewer inspects the data, latest checks, evidence,
   agent recommendations, and audit history. Approval requires that no latest
   check is failed and that the reviewer attests to reviewing QC and evidence.
   A review note is required for every decision.
5. **Keep the history.** Checks and review actions are recorded rather than
   silently replacing the audit trail. A higher workflow version applies to
   subsequently submitted cases; it does not rewrite a case's historical
   workflow.

Case statuses used by the API include:

- `needs_information`: one or more latest deterministic checks failed, or a
  reviewer requested more information.
- `in_review`: no deterministic check is failed. Warning-level checks can
  remain, but should be considered by the reviewer.
- `approved` and `rejected`: final human decisions. Finalized cases cannot be
  changed or revalidated. Submit a new case to correct finalized application
  data.

There is no endpoint to edit submitted case data in place. Evidence can be
attached to a non-final case and QC can be re-run; if the submitted data itself
needs correction, create a replacement case with the corrected data.

### What a QC failure explanation contains

Each check has a status and structured result. For a configured field rule,
the result includes its field path, operator, whether it passed, and the
administrator-authored `message`. Use that message to say what failed and
what the submitter should correct, for example: “The NPI in the client record
must contain exactly 10 digits; correct the source record and resubmit.”
Required-field checks instead report the field path and `present: false`.
Evidence-type checks report the required type and `present: false`; they do
not have a separate configurable explanation. Reviewers can add more context
in their decision note.

The UI shows the check result on the case, and the REST API returns it in
`checks[].result`. Messages should describe the rule, not echo sensitive
submitted values.

### Example: client record, customer form, and attached documents

Suppose a customer submits a provider application. Their client platform has
an existing provider record, the customer fills out an intake form, and the
customer attaches a license document. The integration first maps the platform
record and form fields to a JSON case. It can separately attach metadata
pointing to the license document in the customer's storage.

Synthetic case data could look like this:

```json
{
  "client_record": {
    "provider": {
      "npi": "12345",
      "legal_name": "Northstar Clinic"
    }
  },
  "submission_form": {
    "provider": {
      "npi": "1234567890",
      "legal_name": "North Star Clinic"
    }
  }
}
```

A workflow can check the presence or format of each configured field. For
example, a `required` rule on `submission_form.provider.npi` can have the
message “Enter the provider NPI on the customer submission form.” A regex rule
on `client_record.provider.npi` can have the message “The NPI in the client
record must contain exactly 10 digits; correct the source record and
resubmit.” If a rule fails, the check includes that message and the failed
field path. The example client-record NPI above would fail such a 10-digit
format rule.

If the workflow also requires evidence type `license-document`, but the
integration has not attached metadata for that evidence type, its check
reports `evidence_type: "license-document"` and `present: false`. Once that
metadata is attached, the check confirms only that the expected evidence type
is present.

The platform can also compare two normalized case fields with `equals_field`.
For example, configure `field: "client_record.provider.npi"` and
`value: "submission_form.provider.npi"` with a message explaining what to
correct. The QC check reports the field paths and configured explanation; it
does not echo the submitted values.

The React intake screen can parse mapped values from uploaded JSON, FHIR JSON
Bundles, text-based PDFs, and XLSX files, then create a case and run its chosen
workflow. This is not a connection to a customer's source platform and does
not read browser forms. PDF patterns need a capture group; XLSX mappings use
exact column headers and one selected data row. FHIR input must be a Bundle,
but full FHIR schema, profile, and terminology validation are not implemented.
Scanned-PDF OCR, DOCX, and HL7 v2 parsing are not supported.

Uploaded file contents are parsed in memory and not retained. The case stores
the mapped values and evidence metadata including the filename and content
hash, not a retrievable copy of the document. Intake is limited to synthetic
test data: each file is limited to 768 KiB, the UI limits selected files to
850 KB combined, and the API request body limit is 1 MiB. Do not upload PHI or
real customer documents. No signature, expiration date, identity, or
clinical-content validation is performed unless a workflow rule explicitly
checks a mapped value.

### Guided cross-platform intake

An operator or tenant administrator can use the React **Document intake** tab
to:

1. Select an existing QC workflow and optionally enter a case reference.
2. Add at least two source cards, choose each source's format, and select one
   file per source.
3. Map each desired canonical case field to a JSON Pointer, a PDF text pattern
   with a capture group, or an exact XLSX header. For XLSX, optionally choose a
   worksheet and data row (the header is row 1 and the first data row is row 2).
4. Confirm the synthetic-data-only acknowledgement and create the case.

The screen reports QC pass/fail results and the evidence hashes for uploaded
sources. A failed mapping or QC check does not alter the original document;
correct the mapping or source and submit a new case.

## Background jobs and agent assistance

Background jobs are durable records in the SQL-backed queue. The worker claims
them with a lease, reports progress, and records each attempt. A stale lease
can be recovered after a worker stops unexpectedly. Eligible failures are
retried up to the configured attempt limit with a delay.

Supported work includes:

- `qc_revalidate`: run the workflow's deterministic checks again.
- `agent_review`: request a structured, advisory recommendation for a case.
- `rule_suggestion`: analyze an administrator-provided SOP and selected field
  names to propose draft workflow rules.

An API request to queue work returns a job ID. Clients can poll
`GET /agent-jobs/{job_id}` or list jobs. Jobs may be submitted with an
`Idempotency-Key` so a retried client request does not create duplicate work.
The monitoring API and UI show job status, progress, retries, and safe error
codes.

Model profiles are configured and allowlisted for tenants. The agent output is
validated against a structured schema and remains advisory; it does not alter
the case decision. SOP suggestions require a model-backed profile, explicit
confirmation that the SOP and field names contain no PHI, personal data,
credentials, or secrets, and a tenant administrator's selection and approval.
The model receives the SOP, candidate field names, and existing rule keys—not
case values or evidence references. Approval creates a new workflow version;
suggestions are never activated automatically.

## Example scenarios

### 1. Provider intake waiting on a license document

An administrator configures a `provider-credentialing` workflow with required
fields `provider.npi` and `license_number`, plus required evidence type
`license-document`. An operator submits a case with both fields present but
without evidence.

The field checks pass, the evidence check fails, and the case enters
`needs_information`. The operator attaches a reference to the document in the
client's secure document store. The API records the evidence metadata and
re-runs QC; once the evidence check passes, the case moves to `in_review`.
A reviewer then inspects the actual evidence through the client's authorized
process and submits an approval with both review attestations and a note.

Example evidence metadata request:

```bash
curl -X POST "$API_URL/cases/$CASE_ID/evidence" \
  -H "Authorization: Bearer $OPERATOR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "evidence_type": "license-document",
    "source_system": "client-document-store",
    "reference": "evidence://provider-intake/license/CASE-1042",
    "attributes": {"document_kind": "state-license"}
  }'
```

The reference is metadata only; this request does not transfer the document.

### 2. Vendor onboarding with a non-blocking warning

A `vendor-onboarding` workflow requires an applicant ID and email. It has an
email-format rule with `severity: "error"` and a priority allowlist rule with
`severity: "warning"`.

If the email is malformed, the corresponding check fails and the case is
`needs_information`. If the email is valid but priority is outside the
allowlist, that check is a warning; with no failed checks, the case can be
`in_review`. A reviewer can consider the warning and make the human decision.
Warnings do not block approval by themselves; failed checks do.

### 3. Agent recommendation without automated approval

For a case that needs additional context, an authorized user queues
`agent_review`. The worker sends the configured, bounded case-review input to
the tenant's allowed model profile, validates the response, and stores the
recommendation and input digest. The user polls the job and can inspect the
result in the case workspace.

The recommendation is not a review decision: it cannot approve, reject, or
submit the case. A reviewer still checks current QC and evidence and records a
decision through the review API.

### 4. Propose a new rule from a sanitized SOP

A tenant administrator selects a workflow, saves an approved SOP version with
the required no-sensitive-data confirmation, and lists only schema field
paths such as `practitioner.identifier`. The administrator requests rule
suggestions and waits for the worker's `rule_suggestion` job to finish.

The administrator reviews the cited SOP excerpts and suggested rules, selects
the rules to keep, and explicitly approves them. The platform validates the
selected rule definitions and creates the next workflow version. If the
workflow changed while the suggestion job was running, approval is rejected
and suggestions must be requested again. No historical case is rewritten.

### 5. Recurring QC revalidation

A tenant administrator creates a schedule for a case, such as a daily
`qc_revalidate`. The worker finds due schedules, queues tracked jobs, and
advances the next-run time. An administrator can pause or resume the schedule.
This periodically rechecks configured deterministic rules; it does not
automatically approve cases or submit them to an external system.

## Interfaces and example requests

- **React UI:** start the API separately, then run `npm ci` and
  `VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev` from `ui/`. Open
  `http://127.0.0.1:8001/client-portal` or
  `http://127.0.0.1:8001/monitoring`.
- **REST API:** in development, `/docs` lists operations. Typical endpoints
  include `POST /workflows`, `POST /cases`, `POST /cases/{id}/evidence`,
  `POST /automation/jobs`, `POST /cases/{id}/review`, and
  `GET /monitoring/api/summary`.
- **MCP:** run `uv run python -m qc_platform.mcp_server` in the configured
  MCP host with `QC_API_URL` and that tenant's `QC_API_KEY`. Available tools
  are limited to listing workflows, reading/submitting cases, enqueuing QC or
  agent jobs, and reading job status. MCP does not provide review approval,
  arbitrary URL fetching, or portal submission.

## Boundaries and operational notes

- The platform is not a browser-automation agent and does not log in to or
  operate client portals. Its connector registry is an integration boundary,
  not a general-purpose URL fetcher.
- Evidence references point to external storage; the platform stores metadata,
  not file contents.
- A model recommendation is not a deterministic QC result or a human decision.
- The API and worker share the SQL database and queue. Before independently
  scaling them across services, introduce an appropriate broker/event boundary
  and data-ownership design.
- In production, use HTTPS for non-loopback API origins, configure tenant
  model access and UI CORS explicitly, run database migrations, and protect
  API keys and evidence stores using the deployment's secret-management
  controls.

# VERIQON

**AXIOVARA** is the company behind VERIQON, an AI-enabled,
tenant-configurable quality operations platform.

Tenant-configurable quality control for **any client application process**:
credentialing, payer enrollment, licensing, onboarding, document review, or
other structured workflows. Clients configure versioned field/evidence rules,
choose an approved model profile, submit cases, and track automated QC and
human review. Integrations are registered by the platform; arbitrary URLs and
portal credentials are not accepted as connector configuration.

For the service architecture, case lifecycle, and example workflows, see the
[platform guide](docs/platform-guide.md).

## What is implemented

- Tenant-scoped API keys, workflows, cases, evidence metadata, QC checks, agent
  recommendations, audit events, automation jobs, job attempts, and schedules.
- Generic validation rules: required fields, regex, allowlists, length/range
  checks, warning or blocking severity, and required evidence types.
- Tenant-allowlisted local/open or client-hosted model profiles; uses LangGraph
  for bounded structured QC recommendations.
- Durable SQL job queue for QC revalidation and agent-review jobs, with
  idempotency, lease recovery, retries, progress, attempt/error history, and
  recurring per-case schedules.
- Tenant-scoped monitoring APIs and a graphical dashboard at `/monitoring`:
  volume charts, job states, progress, retries, and redacted error events.
- Client workspace at `/client-portal` for versioned QC setup and human case
  review; tenant admins can store SOP versions, request background agent rule
  suggestions, and selectively approve suggestions into a new workflow version.
- Guided cross-platform intake at `/intake`: operators map synthetic JSON,
  FHIR JSON Bundle, text-PDF, or XLSX source files into case fields and run a
  selected QC workflow, including cross-field comparison rules. Source files
  are parsed in memory and not retained; do not use this MVP with PHI or real
  customer data.
- A standard MCP stdio server that exposes narrowly scoped, tenant-authorized
  operations through the same REST API.
- Tenant-consistent database foreign keys, expiring/revocable API keys, request
  body limits, and production security defaults.
- `uv` project/lock management, Alembic migrations, SQLite local development,
  PostgreSQL deployment path.

## Agentic and service architecture

The deployment uses independently runnable API, React UI, and worker
processes in a single codebase. The UI is a separate Vite service; the API and
worker still share data ownership and the SQL-backed job queue.

```text
Browser ─> React UI service ───HTTP/CORS──> API routers ─> tenant-scoped database
REST/MCP clients ─────────────────────────> API routers    ├─ QC and validation
                                                           └─ durable job queue
                                                                    │
                                                                    v
                                                             worker process
                                                             └─ LangGraph agent
```

| Capability | Current boundary | Responsibility |
|---|---|---|
| UI | `ui/` (React + Vite) | Independently run React client workspace and monitoring dashboard |
| API/control plane | `qc_platform.api.routers` | Authenticated tenant operations; routers grouped by cases, workflows, automation, monitoring, and API keys |
| QC domain | `qc_platform.qc` | Deterministic field/evidence rules and QC outcomes |
| Agent orchestration | `qc_platform.agent` | Bounded LangGraph recommendations using tenant-allowlisted model profiles; no approval or portal submission |
| Long-running execution | `qc_platform.worker` | Durable job claiming, retries, lease recovery, schedules, and progress |
| Platform integration | `qc_platform.connectors` | Explicitly registered connector boundary; no user-supplied arbitrary URLs |
| Human and operational oversight | Monitoring and case-review API routes | Review decisions, job tracking, and tenant-scoped audit/monitoring |
| Tool access | `qc_platform.mcp_server` | MCP stdio adapter calling the same authenticated REST API |

The API, React UI, worker, and MCP adapter are independently runnable:

```bash
uv run uvicorn qc_platform.main:app --host 127.0.0.1 --port 8000
uv run python -m qc_platform.worker
uv run python -m qc_platform.mcp_server
```

Start those commands in separate terminals. Start the UI in its own terminal:

```bash
cd ui
npm install
VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

The UI browser requests target `VITE_API_BASE_URL` (default
`http://127.0.0.1:8000`). Configure `QC_UI_ORIGINS` on the API as a
comma-separated allowlist of UI origins when hosting the UI elsewhere; the
local default allows `http://127.0.0.1:8001` and `http://localhost:8001`.
The React app requires Node.js 18 or newer and npm.
The UI and API can be deployed independently. The API and worker still share
a database and the SQL-backed queue, so they are not yet a safe independently
scalable multi-service queue. Before splitting domain services across
deployments, introduce versioned service contracts, an outbox/event boundary,
and a production queue (for example, a managed broker or workflow engine),
then separate data ownership and verify tenant propagation at every boundary.
Keep the agent's decision support separate from deterministic QC and human
decisions. Use idempotent jobs, explicit review checkpoints, evidence
provenance, policy-bounded tools, and tenant-specific model access as the
workflow grows.

## Run locally

Requires Python 3.11+ and `uv`. Local model-assisted review uses Ollama; this
workspace has `gemma4:e2b` configured by default.

```bash
uv sync
uv run alembic upgrade head
uv run python -m qc_platform.bootstrap \
  --name "Demo Health" \
  --slug demo-health \
  --key-file .local-admin-key
uv run uvicorn qc_platform.main:app --host 127.0.0.1 --port 8000
```

In another terminal, start the React UI service:

```bash
cd ui
npm install
VITE_API_BASE_URL=http://127.0.0.1:8000 npm run dev
```

In another terminal, start the durable worker:

```bash
uv run python -m qc_platform.worker
```

Ollama must be running locally for the default `agent_profile: "default"`:

```bash
ollama serve
ollama pull gemma4:e2b
```

## Run with Docker Compose

Docker Compose starts PostgreSQL, the API, the background worker, and the
production-built UI. Docker Compose v2 is required.

```bash
docker compose up --build -d
docker compose exec api /app/.venv/bin/python -m qc_platform.bootstrap \
  --name "Demo Health" \
  --slug demo-health \
  --key-file /tmp/demo-health-admin-key
docker compose exec api cat /tmp/demo-health-admin-key
```

The API is available at `http://localhost:8000` and the UI at
`http://localhost:8001`. The bootstrap key is created inside the API container;
copy it to a secure location and remove the temporary file after saving it.
Set `POSTGRES_PASSWORD` before starting Compose to replace the local default,
and use a secret manager for deployed environments. `VITE_API_BASE_URL` is
baked into the UI at build time; set it to the browser-reachable API origin
before running `docker compose up --build` when hosting elsewhere. Set
`QC_UI_ORIGINS` to the deployed UI origin(s) for the API CORS allowlist.

Stop the services with `docker compose down`. Add `-v` only when you intend to
delete the persisted PostgreSQL data volume.

The starter database defaults to `./qc_platform.db`. The bootstrap command
creates `.local-admin-key` with owner-only permissions and refuses to overwrite
an existing key file. Do not share that key. The API is at
`http://127.0.0.1:8000`, readiness at `/ready`, and docs at `/docs`. The
separate React UI service is at `http://127.0.0.1:8001`; its monitoring
dashboard is at `/monitoring` and client workspace at `/client-portal`. The UI
keeps the API key in memory only. For a production build, run `npm run build`
in `ui/` and serve `ui/dist/` with a static web server; set
`VITE_API_BASE_URL` at build time and allow the deployed UI origin in
`QC_UI_ORIGINS` on the API. Configure the static server with the equivalent
security headers, especially a Content Security Policy whose `connect-src`
allows only the UI origin and the configured API origin.

## Access options: UI, REST API, and MCP

The UI, REST API, and MCP adapter are different entry points to the same
tenant-scoped workflows, rules, cases, jobs, and audit history:

- **UI:** on the separate UI service, `/client-portal` provides tenant workflow
  configuration and case review; `/monitoring` provides the graphical
  operations view.
- **REST API:** `/docs` in development lists the API. Use it for client
  application integrations, roster ingestion, and scheduled automation.
- **MCP:** run a local stdio server per user/AI client, configured with that
  tenant's own least-privilege API key. MCP calls the same REST API; API
  authentication and tenant authorization remain authoritative.

### Use the client workspace

1. Open `http://127.0.0.1:8001/client-portal` and connect with the tenant's
   administrator API key. The key stays in page memory and is cleared when
   disconnecting or leaving the page.
2. Create a versioned QC workflow: choose a unique slug for each process
   (for example `credentialing`, `payer-enrollment`, or `license-renewal`),
   specify required fields, evidence, field rules, and an enabled model profile.
   Repeat to configure any number of separate processes. To revise one, submit
   a higher version number with the same slug.
3. Use the **Case review** tab to filter by status, open a case, inspect its
   submitted data, evidence, QC findings, agent recommendations, and audit
   history, then submit an approve/reject/request-information decision with a
   note. Approval additionally requires explicit attestations that the
   reviewer checked the latest QC findings and case evidence. The API records
   the reviewer, decision, note, QC check IDs, evidence IDs, and attestations;
   it blocks approval when any latest blocking QC check has failed.
4. Client systems submit cases through the REST API using the configured
   workflow slug. Tenant admins configure workflows; reviewers can review
   cases. A tenant admin can do both.
5. In **SOP-guided agent rule suggestions**, choose a workflow, paste approved
   SOP text without PHI, personal data, credentials, or secrets, and list only
   schema field paths. The selected tenant model receives the SOP and those
   field names—not case values, provider records, evidence references, or
   credentials. Suggestions must cite exact SOP excerpts and only use the
   supplied field paths. Review the suggestions and explicitly select rules;
   approval creates a new version and never changes historical cases.

The **Load OpenEMR FHIR demo** button fills a synthetic provider-intake workflow
and sample SOP. This demonstrates using normalized FHIR R4 Practitioner-like
field paths for a sample QC flow. It is not an OpenEMR connector, official
OpenEMR policy, or production-ready clinical rule set; replace it with an
approved client SOP and test against that client's mapping. See the upstream
[OpenEMR FHIR documentation](https://github.com/openemr/openemr/blob/master/FHIR_README.md)
and [API documentation](https://github.com/openemr/openemr/blob/master/API_README.md).

Final case decisions are terminal in this starter: API evidence updates and
QC revalidation are rejected after approval or rejection, and queued QC jobs
will not overwrite a final reviewer decision. To correct a finalized case,
submit a new case rather than editing its historical review record.

### Browser UI automation status

The current project does **not** execute browser automation against OpenEMR or
other portals. Its registered connector interface and SQL worker are not a
browser-control engine. A production browser executor needs a separate,
isolated Playwright service with deployment-registered origins and reviewed
deterministic recipes, tenant-scoped secret-manager injection, per-job browser
contexts, audit/evidence capture with sensitive-data redaction, and explicit
human confirmation before any portal submit, attestation, or other
irreversible action. An LLM must not invent selectors/actions or navigate to
arbitrary URLs. The OpenEMR demo above only configures QC/SOP suggestions and
does not log in to or modify OpenEMR.

Example VS Code MCP configuration (do not commit real API keys):

```json
{
  "servers": {
    "veriqon": {
      "type": "stdio",
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/VERIQON", "run", "python", "-m", "qc_platform.mcp_server"],
      "env": {
        "QC_API_URL": "http://127.0.0.1:8000",
        "QC_API_KEY": "${input:veriqonApiKey}"
      }
    }
  },
  "inputs": [
    {
      "id": "veriqonApiKey",
      "type": "promptString",
      "description": "VERIQON tenant API key",
      "password": true
    }
  ]
}
```

Set the absolute workspace path, then start `uv run python -m
qc_platform.mcp_server` through the MCP host. Its tools list workflows, read or
submit a case, enqueue QC/agent jobs, and read job progress. It does not expose
review approval, external portal submission, arbitrary URL fetching, or
credential management. A future hosted MCP option should use Streamable HTTP
with per-user OIDC/authentication; do not share one tenant API key across
customers or users.

### Configure multiple client QC flows

Create a workflow as tenant admin. The same schema supports diverse client
application processes; each tenant defines its own fields, checks, evidence
requirements, and model profile.

```bash
curl -X POST http://127.0.0.1:8000/workflows \
  -H "Authorization: Bearer $QC_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "slug":"vendor-onboarding",
    "name":"Vendor Application Review",
    "version":1,
    "required_fields":["applicant.id","applicant.email"],
    "field_rules":[
      {
        "key":"email-format",
        "field":"applicant.email",
        "operator":"regex",
        "value":"^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$",
        "severity":"error",
        "message":"Provide a valid email address."
      },
      {
        "key":"priority-level",
        "field":"priority",
        "operator":"one_of",
        "value":["normal","urgent"],
        "severity":"warning",
        "message":"Review the selected priority."
      }
    ],
    "required_evidence_types":["application-form"],
    "agent_profile":"rules"
  }'
```

Use the tenant admin to create short-lived operator and reviewer API keys via
`POST /api-keys`. Submit a case using the operator key:

```bash
curl -X POST http://127.0.0.1:8000/cases \
  -H "Authorization: Bearer $QC_OPERATOR_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "workflow_slug":"vendor-onboarding",
    "external_reference":"demo-app-001",
    "source_system":"client-application",
    "data":{
      "applicant":{"id":"DEMO-1","email":"person@example.test"},
      "priority":"normal"
    }
  }'
```

Attach evidence as a secure object reference. This starter stores metadata; it
does not upload or download files. Enqueue QC or agent work asynchronously:

```bash
curl -X POST http://127.0.0.1:8000/automation/jobs \
  -H "Authorization: Bearer $QC_OPERATOR_KEY" \
  -H "Idempotency-Key: demo-app-001-qc" \
  -H "Content-Type: application/json" \
  -d '{"case_id":"<CASE_ID>","job_type":"qc_revalidate"}'

curl -X POST http://127.0.0.1:8000/automation/jobs \
  -H "Authorization: Bearer $QC_OPERATOR_KEY" \
  -H "Idempotency-Key: demo-app-001-agent" \
  -H "Content-Type: application/json" \
  -d '{"case_id":"<CASE_ID>","job_type":"agent_review"}'
```

The API responds with a job ID; poll `GET /agent-jobs/{job_id}` or browse
`GET /automation/jobs`. For recurring monitoring, a tenant administrator can
create a schedule:

```bash
curl -X POST http://127.0.0.1:8000/automation/schedules \
  -H "Authorization: Bearer $QC_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"case_id":"<CASE_ID>","job_type":"qc_revalidate","interval_seconds":86400}'
```

Pause/resume a schedule with `PATCH /automation/schedules/{schedule_id}` and
`{"enabled":false}` / `{"enabled":true}`. The worker discovers due schedules
and enqueues tracked QC or agent jobs. Human reviewers still make case
decisions; passing automated checks or model output does not approve or submit
an application.

## Monitoring and error logs

The graphical dashboard fetches data only from the same API origin and does
not persist the API key. It includes:

- Daily case activity and case/job status counts
- Job progress, retry count, completion status, and recent jobs
- Per-attempt duration and redacted error codes
- A 7/14/30/90-day selector and periodic refresh

Monitoring endpoints require a tenant admin or reviewer key:

- `GET /monitoring/api/summary?window_days=7`
- `GET /monitoring/api/errors?limit=50`
- `GET /operations/jobs/summary`
- `GET /automation/jobs?status=failed`
- `GET /agent-jobs/{job_id}/attempts`

Worker logs contain job IDs and error class/context, not submitted application
payloads. Database attempt rows store safe error codes, worker ID, timestamps,
and duration, not stack traces, credentials, or case data. Route application
logs to a protected log service and alert on persistent job failures or worker
health outside this starter.

## Tenant model profiles and supported AI frameworks

Workflows select a profile by ID; clients do not supply a model URL or secret in
an API request. Deployment administrators configure model endpoints and secrets
out of band. LangGraph is the orchestration framework; the current graph has a
bounded structured-recommendation node and no external action tools.

Example:

```bash
export QC_DEFAULT_MODEL_TENANTS='demo-health'
export QC_MODEL_PROFILES='{
  "client-open-model":{
    "provider":"openai_compatible",
    "model":"client-approved-model",
    "base_url":"https://model-gateway.example/v1",
    "allowed_tenants":["demo-health"],
    "api_key_env":"CLIENT_MODEL_API_KEY"
  },
  "local-open-model":{
    "provider":"ollama",
    "model":"gemma4:e2b",
    "base_url":"http://127.0.0.1:11434",
    "allowed_tenants":["demo-health"]
  }
}'
export CLIENT_MODEL_API_KEY='inject-from-a-secret-manager'
```

`GET /model-profiles` lists only profile IDs allowed for the authenticated
tenant. OpenAI-compatible endpoints require HTTPS, redirects are disabled,
environment HTTP proxies are not trusted, responses are size-limited, and
credentials are read from server environment/secret injection—not the database
or model prompt. Ollama must use loopback. `rules` is for testing/local
deterministic recommendations and is disabled in production.

The model sees only rule identifiers, pass/fail states, evidence type names,
and workflow version—not raw case fields, evidence URLs, or tenant IDs. Model
responses are schema-validated and findings must reference failed checks.

## API outline

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health`, `/ready` | Liveness and database readiness |
| `GET` | `/monitoring` | Graphical tenant operations dashboard |
| `GET` | `/model-profiles` | List tenant-allowed model options |
| `GET`, `POST` | `/workflows` | List/create tenant-versioned QC flows |
| `GET`, `POST` | `/cases` | List/create application cases |
| `POST` | `/cases/{id}/evidence` | Attach evidence metadata and rerun checks |
| `POST` | `/automation/jobs` | Queue QC revalidation or agent review |
| `GET` | `/automation/jobs`, `/agent-jobs/{id}` | Query job progress and outcomes |
| `GET`, `POST`, `PATCH` | `/automation/schedules` | Manage recurring case automation |
| `GET` | `/monitoring/api/summary`, `/monitoring/api/errors` | Tenant operational metrics and error log |
| `POST` | `/cases/{id}/review` | Record human review; failures block approval |
| `GET` | `/cases/{id}/audit` | Read tenant-scoped decision history |

## Configuration and deployment

| Variable | Description |
|---|---|
| `QC_ENV` | `development`, `test`, or `production` |
| `DATABASE_URL` | SQLAlchemy URL; SQLite locally, PostgreSQL in production |
| `OLLAMA_BASE_URL`, `OLLAMA_MODEL` | Default local model service and model |
| `QC_DEFAULT_MODEL_TENANTS` | Comma-separated tenants allowed to use `default` profile |
| `QC_MODEL_PROFILES` | JSON mapping of deployment-approved model profiles |
| `API_KEY_TTL_DAYS` | API key lifetime, 1–365 days |
| `MAX_REQUEST_BYTES` | HTTP request body size ceiling |

Production mode requires PostgreSQL, disables development docs and automatic
schema creation, and expects migrations to run before application startup:

```bash
uv run alembic upgrade head
QC_ENV=production DATABASE_URL='postgresql+psycopg://...' \
  uv run uvicorn qc_platform.main:app --host 0.0.0.0 --port 8000 --workers 4
QC_ENV=production DATABASE_URL='postgresql+psycopg://...' \
  uv run python -m qc_platform.worker
```

Use a secret manager, TLS ingress, restricted database/model network access,
ingress rate limits, monitored backups, and independent restore tests. The
starter is not a compliance certification or production healthcare approval;
complete customer-specific security/privacy reviews before sending regulated
data.

## Development

```bash
uv sync
uv run alembic upgrade head
uv run pytest -q
uv run alembic check
```

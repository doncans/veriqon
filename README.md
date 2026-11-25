# VERIQON

VERIQON is an AI-enabled, tenant-configurable quality operations platform built
by AXIOVARA. It supports case intake, versioned workflow/QC rules, evidence
metadata, human review, monitoring, durable background jobs, and tenant-scoped
MCP access.

It is a configurable quality-control starter, not a production clinical,
compliance, or browser-automation system. Do not use it with PHI or real
customer credentials.

## Architecture

| Component | Responsibility |
|---|---|
| FastAPI API | Authentication, tenant authorization, workflows, cases, reviews, and jobs |
| React/Vite UI | Client workflow setup, case review, intake, and monitoring |
| SQL worker | Durable job leases, retries, schedules, and QC/agent execution |
| LangGraph agent | Bounded, structured decision support; never makes final case decisions |
| MCP stdio server | Narrow tenant API operations for local MCP clients |

The API and UI are independently runnable. API, worker, and UI deployments
must share the same API/database configuration described below.

## Requirements

- Python 3.11+
- `uv`
- Node.js 18+ and npm
- PostgreSQL for production

## Run locally

From the repository root:

```bash
uv sync
uv run alembic upgrade head
uv run python -m qc_platform.bootstrap \
  --name "Demo Health" \
  --slug demo-health \
  --key-file .local-admin-key
```

Start the API:

```bash
uv run uvicorn qc_platform.main:app --host 127.0.0.1 --port 8000
```

Start the worker in another terminal:

```bash
uv run python -m qc_platform.worker
```

Start the UI in another terminal:

```bash
cd ui
npm install
npm run dev
```

Open:

- API docs: `http://127.0.0.1:8000/docs`
- Client workspace: `http://127.0.0.1:8001/client-portal`
- Monitoring: `http://127.0.0.1:8001/monitoring`
- Cross-platform intake: `http://127.0.0.1:8001/intake`

The UI uses `http://127.0.0.1:8000` by default. Override it at build/dev time:

```bash
VITE_API_BASE_URL=https://api.example.com npm run dev
```

Configure the API CORS allowlist with:

```bash
export QC_UI_ORIGINS='https://qc.example.com'
```

## Docker Compose

```bash
docker compose up --build -d
docker compose exec api /app/.venv/bin/python -m qc_platform.bootstrap \
  --name "Demo Health" \
  --slug demo-health \
  --key-file /tmp/demo-health-admin-key
docker compose exec api cat /tmp/demo-health-admin-key
```

Set `POSTGRES_PASSWORD` before starting Compose. Copy the bootstrap key to a
secure location and remove the container temp file. Do not commit API keys.

- API: `http://localhost:8000`
- UI: `http://localhost:8001`

## Core workflow

1. A tenant administrator creates a versioned workflow.
2. An operator submits a case or cross-platform document intake.
3. Deterministic QC checks produce pass, fail, or warning results.
4. An optional agent creates decision-support findings and never approves.
5. A reviewer records approve, reject, or request-information.
6. Approved/rejected cases are finalized and reject later evidence/revalidation.

Example workflow:

```bash
curl -X POST http://127.0.0.1:8000/workflows \
  -H "Authorization: Bearer $QC_ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "slug":"vendor-onboarding",
    "name":"Vendor Onboarding",
    "version":1,
    "required_fields":["applicant.id","applicant.email"],
    "required_evidence_types":["application-form"],
    "agent_profile":"rules"
  }'
```

Queue an idempotent QC job:

```bash
curl -X POST http://127.0.0.1:8000/automation/jobs \
  -H "Authorization: Bearer $QC_OPERATOR_KEY" \
  -H "Idempotency-Key: vendor-001-qc" \
  -H "Content-Type: application/json" \
  -d '{"case_id":"<CASE_ID>","job_type":"qc_revalidate"}'
```

Poll `GET /agent-jobs/{job_id}` or use the monitoring endpoints.

## API overview

| Area | Endpoints |
|---|---|
| Health | `GET /health`, `GET /ready` |
| Workflows | `GET/POST /workflows` |
| Cases | `GET/POST /cases`, `GET /cases/{id}` |
| Evidence/review | `POST /cases/{id}/evidence`, `POST /cases/{id}/revalidate`, `POST /cases/{id}/review` |
| Jobs | `POST /automation/jobs`, `GET /automation/jobs`, `GET /agent-jobs/{id}` |
| Schedules | `GET/POST/PATCH /automation/schedules` |
| Monitoring | `GET /monitoring/api/summary`, `GET /monitoring/api/errors` |
| Audit | `GET /cases/{id}/audit`, `GET /cases/{id}/agent-runs` |

## MCP

Run a local stdio server:

```bash
uv run python -m qc_platform.mcp_server
```

Configure `QC_API_URL` and a tenant-scoped `QC_API_KEY` in the MCP host.
The server exposes only tenant API operations for listing workflows, reading or
submitting cases, enqueueing QC jobs, and reading job status. It does not expose
review approval or arbitrary URL/credential configuration.

## Model profiles

Profiles are deployment-configured; API clients cannot submit model URLs or
secrets. For local Ollama:

```bash
export QC_DEFAULT_MODEL_TENANTS='demo-health'
export OLLAMA_BASE_URL='http://127.0.0.1:11434'
export OLLAMA_MODEL='gemma4:e2b'
ollama serve
ollama pull gemma4:e2b
```

External profiles can be configured through `QC_MODEL_PROFILES`:

```bash
export QC_MODEL_PROFILES='{
  "client-model":{
    "provider":"openai_compatible",
    "model":"approved-model",
    "base_url":"https://model-gateway.example/v1",
    "allowed_tenants":["demo-health"],
    "api_key_env":"CLIENT_MODEL_API_KEY"
  }
}'
```

Store `CLIENT_MODEL_API_KEY` in a secret manager, not source control or the
database. OpenAI-compatible endpoints should use HTTPS. The deterministic
`rules` profile is disabled in production.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `QC_ENV` | `development` | `development`, `test`, or `production` |
| `DATABASE_URL` | `sqlite:///./qc_platform.db` | SQLAlchemy database URL |
| `QC_UI_ORIGINS` | Local Vite origin | Comma-separated explicit UI origins |
| `QC_DOCUMENT_INGESTION_ENABLED` | Enabled outside production | Enables document intake |
| `MAX_REQUEST_BYTES` | `1048576` | Maximum HTTP request body |
| `API_KEY_TTL_DAYS` | `90` | API key lifetime, 1–365 days |
| `QC_DEFAULT_MODEL_TENANTS` | Empty | Tenants allowed to use the default profile |
| `QC_MODEL_PROFILES` | `{}` | Deployment-approved model profile JSON |

## Production

```bash
uv run alembic upgrade head

QC_ENV=production \
DATABASE_URL='postgresql+psycopg://...' \
QC_UI_ORIGINS='https://qc.example.com' \
uv run uvicorn qc_platform.main:app --host 0.0.0.0 --port 8000 --workers 4

QC_ENV=production \
DATABASE_URL='postgresql+psycopg://...' \
uv run python -m qc_platform.worker
```

Production must use PostgreSQL, TLS ingress, an explicit UI origin, secret
manager credentials, restricted network access, rate limits, backups, and
monitoring. Run migrations before startup and keep the API and worker on
compatible application versions.

## Development and validation

```bash
uv sync
uv run alembic upgrade head
uv run pytest -q
uv run alembic check

cd ui
npm install
npm run build
```

## Security boundaries

- API keys are hashed and tenant-scoped.
- Workflow, case, evidence, audit, and job operations are tenant-scoped.
- Tenant admins manage workflows and keys; operators create cases; reviewers decide cases.
- Request bodies have a configurable size limit.
- Evidence and case data are not sent to the agent as raw values.
- Approval requires explicit QC/evidence review attestations.
- Final case decisions cannot be changed through API mutation.
- Browser automation, arbitrary portal URLs, credential collection, and irreversible external submission are not implemented.

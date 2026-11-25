import os
from uuid import UUID
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP


class QCAPIError(RuntimeError):
    pass


class QCPlatformClient:
    def __init__(self, base_url: str, api_key: str):
        parsed = urlparse(base_url)
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"})
        ):
            raise ValueError("QC_API_URL must be HTTPS, or HTTP on loopback for local development.")
        if not api_key or len(api_key) > 256:
            raise ValueError("QC_API_KEY is missing or invalid.")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key

    def request(self, method: str, path: str, **kwargs):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Accept": "application/json",
        }
        headers.update(kwargs.pop("headers", {}))
        try:
            with httpx.Client(
                timeout=httpx.Timeout(30.0, connect=3.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    **kwargs,
                )
        except httpx.HTTPError as error:
            raise QCAPIError(f"QC API request failed ({type(error).__name__}).") from error
        if not response.is_success:
            detail = "request rejected"
            try:
                body = response.json()
                if isinstance(body, dict) and isinstance(body.get("detail"), str):
                    detail = body["detail"][:240]
            except ValueError:
                pass
            raise QCAPIError(f"QC API returned {response.status_code}: {detail}")
        return response.json()


def _validated_id(value: str, name: str) -> str:
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as error:
        raise ValueError(f"{name} must be a UUID.") from error


def create_mcp_server(api_url: str | None = None, api_key: str | None = None) -> FastMCP:
    base_url = api_url if api_url is not None else os.getenv("QC_API_URL", "http://127.0.0.1:8000")
    bearer = api_key if api_key is not None else os.getenv("QC_API_KEY", "")
    client = QCPlatformClient(base_url, bearer)
    mcp = FastMCP("VERIQON", instructions=(
        "AI-enabled, tenant-scoped quality operations by AXIOVARA. "
        "Use tools only for the authenticated tenant. "
        "QC results and AI recommendations are advisory. Human reviewers must make "
        "approval decisions in VERIQON."
    ))

    @mcp.tool()
    def list_workflows() -> list[dict]:
        """List workflows visible to the authenticated tenant."""
        return client.request("GET", "/workflows")

    @mcp.tool()
    def get_case(case_id: str) -> dict:
        """Read a QC case and its checks/evidence for the authenticated tenant."""
        return client.request("GET", f"/cases/{_validated_id(case_id, 'case_id')}")

    @mcp.tool()
    def submit_case(
        workflow_slug: str,
        source_system: str,
        data: dict,
        external_reference: str | None = None,
    ) -> dict:
        """Submit application data for deterministic QC; use only authorized, minimized data."""
        return client.request(
            "POST",
            "/cases",
            json={
                "workflow_slug": workflow_slug,
                "source_system": source_system,
                "external_reference": external_reference,
                "data": data,
            },
        )

    @mcp.tool()
    def enqueue_qc_job(case_id: str, job_type: str, idempotency_key: str) -> dict:
        """Queue a QC revalidation or advisory agent review for a case."""
        if job_type not in {"qc_revalidate", "agent_review"}:
            raise ValueError("job_type must be qc_revalidate or agent_review")
        if not idempotency_key or len(idempotency_key) > 128:
            raise ValueError("idempotency_key must contain 1 to 128 characters")
        case_id = _validated_id(case_id, "case_id")
        return client.request(
            "POST",
            "/automation/jobs",
            headers={"Idempotency-Key": idempotency_key},
            json={"case_id": case_id, "job_type": job_type},
        )

    @mcp.tool()
    def get_job_status(job_id: str) -> dict:
        """Read progress, outcome, and safe error code for one tenant job."""
        return client.request("GET", f"/agent-jobs/{_validated_id(job_id, 'job_id')}")

    return mcp


def main() -> None:
    server = create_mcp_server()
    server.run(transport="stdio")


if __name__ == "__main__":
    main()

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY alembic.ini ./
COPY migrations ./migrations
COPY qc_platform ./qc_platform

RUN uv sync --frozen --no-dev

EXPOSE 8000
CMD ["/app/.venv/bin/uvicorn", "qc_platform.main:app", "--host", "0.0.0.0", "--port", "8000"]

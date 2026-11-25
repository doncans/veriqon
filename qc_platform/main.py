from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from qc_platform.api.routers import (
    auth_router,
    automation_router,
    cases_router,
    monitoring_router,
    system_router,
    workflows_router,
)
from qc_platform.config import APP_ENV, DATABASE_URL, MAX_REQUEST_BYTES, UI_ORIGINS
from qc_platform.connectors import ConnectorRegistry
from qc_platform.database import Base, make_engine, make_session_factory
from qc_platform.middleware import MaxRequestBodyMiddleware


def create_app(database_url: str | None = None) -> FastAPI:
    resolved_database_url = database_url or DATABASE_URL
    if APP_ENV == "production" and resolved_database_url.startswith("sqlite"):
        raise ValueError("Production mode requires PostgreSQL; SQLite is for local development only")
    engine = make_engine(resolved_database_url)
    session_factory = make_session_factory(engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if APP_ENV != "production":
            Base.metadata.create_all(engine)
        else:
            with engine.connect() as connection:
                connection.exec_driver_sql("SELECT 1")
        yield
        engine.dispose()

    app = FastAPI(
        title="VERIQON",
        version="0.1.0",
        description="AI-enabled, tenant-scoped quality operations API by AXIOVARA.",
        docs_url="/docs" if APP_ENV != "production" else None,
        redoc_url=None,
        openapi_url="/openapi.json" if APP_ENV != "production" else None,
        lifespan=lifespan,
    )
    app.state.session_factory = session_factory
    app.state.engine = engine
    app.state.connector_registry = ConnectorRegistry()
    app.add_middleware(MaxRequestBodyMiddleware, max_bytes=MAX_REQUEST_BYTES)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=UI_ORIGINS,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Accept", "Authorization", "Content-Type", "Idempotency-Key"],
        max_age=600,
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if APP_ENV == "production":
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
            )
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response

    for router in (
        system_router,
        auth_router,
        workflows_router,
        cases_router,
        automation_router,
        monitoring_router,
    ):
        app.include_router(router)
    return app


app = create_app()

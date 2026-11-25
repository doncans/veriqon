from qc_platform.api.routers.auth import router as auth_router
from qc_platform.api.routers.automation import router as automation_router
from qc_platform.api.routers.cases import router as cases_router
from qc_platform.api.routers.monitoring import router as monitoring_router
from qc_platform.api.routers.system import router as system_router
from qc_platform.api.routers.workflows import router as workflows_router

__all__ = [
    "auth_router",
    "automation_router",
    "cases_router",
    "monitoring_router",
    "system_router",
    "workflows_router",
]

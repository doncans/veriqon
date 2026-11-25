import json
import os
from dataclasses import dataclass
from urllib.parse import urlsplit

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./qc_platform.db")
APP_ENV = os.getenv("QC_ENV", "development").lower()
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "gemma4:e2b")
API_KEY_TTL_DAYS = int(os.getenv("API_KEY_TTL_DAYS", "90"))
MAX_REQUEST_BYTES = int(os.getenv("MAX_REQUEST_BYTES", str(1_048_576)))
QC_MODEL_PROFILES = os.getenv("QC_MODEL_PROFILES", "{}")
_DOCUMENT_INGESTION_SETTING = os.getenv(
    "QC_DOCUMENT_INGESTION_ENABLED",
    "false" if APP_ENV == "production" else "true",
).lower()
if _DOCUMENT_INGESTION_SETTING not in {"true", "false", "1", "0"}:
    raise ValueError("QC_DOCUMENT_INGESTION_ENABLED must be true or false")
DOCUMENT_INGESTION_ENABLED = _DOCUMENT_INGESTION_SETTING in {"true", "1"}


def _configured_ui_origins() -> list[str]:
    configured = os.getenv(
        "QC_UI_ORIGINS",
        "http://127.0.0.1:8001,http://localhost:8001",
    )
    origins = []
    for value in configured.split(","):
        origin = value.strip()
        if not origin:
            continue
        parsed = urlsplit(origin)
        if (
            "*" in origin
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("QC_UI_ORIGINS must contain only explicit HTTP(S) origins")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("QC_UI_ORIGINS contains an invalid port") from error
        origins.append(f"{parsed.scheme}://{parsed.netloc}")
    return origins


UI_ORIGINS = _configured_ui_origins()

if APP_ENV not in {"development", "test", "production"}:
    raise ValueError("QC_ENV must be development, test, or production")
if not 1 <= API_KEY_TTL_DAYS <= 365:
    raise ValueError("API_KEY_TTL_DAYS must be between 1 and 365")
if MAX_REQUEST_BYTES < 1024:
    raise ValueError("MAX_REQUEST_BYTES must be at least 1024")


@dataclass(frozen=True)
class ModelProfile:
    provider: str
    model: str
    base_url: str
    allowed_tenants: frozenset[str]
    api_key_env: str | None = None


def get_model_profile(profile_id: str, tenant_slug: str) -> ModelProfile:
    if profile_id == "rules":
        if APP_ENV == "production":
            raise PermissionError("The deterministic test profile is disabled in production")
        return ModelProfile(
            provider="rules",
            model="qc-rules-v1",
            base_url="",
            allowed_tenants=frozenset({tenant_slug}),
        )
    default_allowed = frozenset(
        tenant.strip()
        for tenant in os.getenv("QC_DEFAULT_MODEL_TENANTS", "").split(",")
        if tenant.strip()
    )
    profiles: dict[str, ModelProfile] = {
        "default": ModelProfile(
            provider="ollama",
            model=OLLAMA_MODEL,
            base_url=OLLAMA_BASE_URL,
            allowed_tenants=default_allowed,
        )
    }
    try:
        configured = json.loads(QC_MODEL_PROFILES)
    except json.JSONDecodeError as error:
        raise ValueError("QC_MODEL_PROFILES must be valid JSON") from error
    if not isinstance(configured, dict):
        raise ValueError("QC_MODEL_PROFILES must be a JSON object")
    for name, value in configured.items():
        if (
            not isinstance(name, str)
            or not name
            or not isinstance(value, dict)
            or value.get("provider") not in {"ollama", "openai_compatible", "rules"}
            or not isinstance(value.get("model"), str)
            or not isinstance(value.get("base_url", ""), str)
            or not isinstance(value.get("allowed_tenants"), list)
            or any(not isinstance(tenant, str) or not tenant for tenant in value["allowed_tenants"])
        ):
            raise ValueError(f"Invalid model profile configuration: {name!r}")
        if name == "default":
            raise ValueError("QC_MODEL_PROFILES cannot override the reserved default profile")
        api_key_env = value.get("api_key_env")
        if api_key_env is not None and (
            not isinstance(api_key_env, str)
            or not api_key_env.replace("_", "A").isalnum()
            or not api_key_env[0].isalpha()
        ):
            raise ValueError(f"Invalid api_key_env in model profile: {name!r}")
        profiles[name] = ModelProfile(
            provider=value["provider"],
            model=value["model"],
            base_url=value.get("base_url", "").rstrip("/"),
            allowed_tenants=frozenset(value["allowed_tenants"]),
            api_key_env=api_key_env,
        )
    profile = profiles.get(profile_id)
    if profile is None:
        raise ValueError("Unknown model profile")
    if tenant_slug not in profile.allowed_tenants and not (
        APP_ENV != "production" and profile_id == "default" and not profile.allowed_tenants
    ):
        raise PermissionError("Model profile is not enabled for this tenant")
    return profile


def available_model_profiles(tenant_slug: str) -> list[tuple[str, ModelProfile]]:
    try:
        configured = json.loads(QC_MODEL_PROFILES)
    except json.JSONDecodeError as error:
        raise ValueError("QC_MODEL_PROFILES must be valid JSON") from error
    if not isinstance(configured, dict):
        raise ValueError("QC_MODEL_PROFILES must be a JSON object")
    profile_ids = {"default", *configured.keys(), "rules"}
    available = []
    for profile_id in sorted(profile_ids):
        try:
            profile = get_model_profile(profile_id, tenant_slug)
        except PermissionError:
            continue
        available.append((profile_id, profile))
    return available

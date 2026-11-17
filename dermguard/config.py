"""Centralized runtime configuration and Google Cloud Secret Manager loader.

Ensures zero hardcoded API keys, project identifiers, or credentials exist in
source code. Secrets are loaded at runtime via Google Cloud Secret Manager
(when `GOOGLE_CLOUD_PROJECT` and `GOOGLE_API_KEY_SECRET_ID` are set) or via
standard environment variables.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

load_dotenv()

PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = PACKAGE_ROOT.parent


def fetch_secret_from_secret_manager(project_id: str, secret_id: str, version: str = "latest") -> str | None:
    """Retrieves a secret payload from Google Cloud Secret Manager if available.

    Args:
        project_id: Target Google Cloud project identifier.
        secret_id: Secret Manager secret name.
        version: Secret version identifier (defaults to 'latest').

    Returns:
        Decoded secret string if accessible, or None when the client library or
        secret is unavailable.
    """
    if not project_id or not secret_id:
        return None
    try:
        from google.cloud import secretmanager  # type: ignore[import-untyped]

        client = secretmanager.SecretManagerServiceClient()
        resource_name = f"projects/{project_id}/secrets/{secret_id}/versions/{version}"
        response = client.access_secret_version(request={"name": resource_name})
        return response.payload.data.decode("utf-8").strip()
    except Exception:
        return None


class DermGuardSettings(BaseModel):
    """Validated runtime settings for DermGuard."""

    app_name: str = Field(default="dermguard", description="ADK application container name.")
    primary_model: str = Field(
        default_factory=lambda: os.getenv("DERMGUARD_PRIMARY_MODEL", "gemini-2.5-flash"),
        description="Primary low-latency Gemini model for conversational orchestration.",
    )
    planner_model: str = Field(
        default_factory=lambda: os.getenv("DERMGUARD_PLANNER_MODEL", "gemini-2.5-pro"),
        description="Reasoning Gemini model routed for multi-active AM/PM schedule synthesis.",
    )
    summarizer_model: str = Field(
        default_factory=lambda: os.getenv("DERMGUARD_SUMMARIZER_MODEL", "gemini-2.5-flash-lite"),
        description="Lightweight Gemini model for asynchronous sliding-window event compaction.",
    )
    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    compaction_interval: int = Field(default=10, gt=0)
    compaction_overlap: int = Field(default=2, ge=0)
    session_db_url: str = Field(
        default_factory=lambda: os.getenv(
            "DERMGUARD_SESSION_DB_URL",
            f"sqlite+aiosqlite:///{PROJECT_ROOT / 'data' / 'dermguard_sessions.db'}",
        )
    )
    sensitivity_json_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "DERMGUARD_SENSITIVITY_FILE",
                str(PROJECT_ROOT / "data" / "user_sensitivities.json"),
            )
        )
    )
    sensitivity_sqlite_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "DERMGUARD_SENSITIVITY_SQLITE",
                str(PROJECT_ROOT / "data" / "user_memory.db"),
            )
        )
    )
    ingredient_rules_path: Path = Field(
        default_factory=lambda: PACKAGE_ROOT / "data" / "ingredient_rules.json"
    )
    gcp_project_id: str = Field(default_factory=lambda: os.getenv("GOOGLE_CLOUD_PROJECT", ""))
    enable_cloud_dlp: bool = Field(
        default_factory=lambda: os.getenv("ENABLE_CLOUD_DLP", "false").lower() == "true"
    )


def bootstrap_runtime_secrets() -> DermGuardSettings:
    """Loads settings and injects secrets from Secret Manager when configured.

    Returns:
        Populated `DermGuardSettings` instance.
    """
    settings = DermGuardSettings()
    secret_id = os.getenv("GOOGLE_API_KEY_SECRET_ID", "").strip()
    if settings.gcp_project_id and secret_id and not os.getenv("GOOGLE_API_KEY"):
        secret_value = fetch_secret_from_secret_manager(settings.gcp_project_id, secret_id)
        if secret_value:
            os.environ["GOOGLE_API_KEY"] = secret_value
    return settings


SETTINGS = bootstrap_runtime_secrets()

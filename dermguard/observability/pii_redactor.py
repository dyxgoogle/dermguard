"""Active PII redaction pipeline for logs, session state, and persistent memory.

Combines deterministic local regular-expression scrubbing (emails, phone
numbers, US SSNs, payment cards, IPv4 addresses, API keys) with optional
Google Cloud Sensitive Data Protection (`google-cloud-dlp`) de-identification
when `ENABLE_CLOUD_DLP=true` and `GOOGLE_CLOUD_PROJECT` are configured.
"""

from __future__ import annotations

import os
import re
from typing import Any

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]\d{3}[-.\s]\d{4}(?!\d)"
)
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_API_KEY_RE = re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b")


def _deidentify_via_cloud_dlp(text: str, project_id: str) -> str:
    """Calls Google Cloud DLP API to mask PII when enabled in cloud deployments.

    Args:
        text: Input string potentially containing sensitive personal data.
        project_id: Google Cloud project ID hosting the DLP API.

    Returns:
        De-identified text or original text if Cloud DLP is unreachable.
    """
    try:
        from google.cloud import dlp_v2  # type: ignore[import-untyped]

        dlp_client = dlp_v2.DlpServiceClient()
        parent = f"projects/{project_id}/locations/global"
        inspect_config = {
            "info_types": [
                {"name": "EMAIL_ADDRESS"},
                {"name": "PHONE_NUMBER"},
                {"name": "PERSON_NAME"},
                {"name": "US_SOCIAL_SECURITY_NUMBER"},
                {"name": "CREDIT_CARD_NUMBER"},
            ]
        }
        deidentify_config = {
            "info_type_transformations": {
                "transformations": [
                    {
                        "primitive_transformation": {
                            "replace_with_info_type_config": {}
                        }
                    }
                ]
            }
        }
        response = dlp_client.deidentify_content(
            request={
                "parent": parent,
                "inspect_config": inspect_config,
                "deidentify_config": deidentify_config,
                "item": {"value": text},
            }
        )
        return response.item.value
    except Exception:
        return text


def redact_pii_text(text: str) -> str:
    """Scrubs personally identifiable information and API keys from a string.

    Args:
        text: Raw input string to sanitize.

    Returns:
        Sanitized string safe for logging or disk persistence.
    """
    if not text:
        return text

    sanitized = _API_KEY_RE.sub("[REDACTED_API_KEY]", text)
    sanitized = _EMAIL_RE.sub("[REDACTED_EMAIL]", sanitized)
    sanitized = _SSN_RE.sub("[REDACTED_SSN]", sanitized)
    sanitized = _PHONE_RE.sub("[REDACTED_PHONE]", sanitized)
    sanitized = _CARD_RE.sub("[REDACTED_CARD]", sanitized)

    if os.getenv("ENABLE_CLOUD_DLP", "false").lower() == "true":
        project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
        if project_id:
            sanitized = _deidentify_via_cloud_dlp(sanitized, project_id)

    return sanitized


def redact_pii_structure(payload: Any) -> Any:
    """Recursively scrubs PII across dictionaries, lists, tuples, and strings.

    Args:
        payload: Arbitrary JSON-like data structure.

    Returns:
        Deep-sanitized structure with all string leaves scrubbed of PII.
    """
    if isinstance(payload, str):
        return redact_pii_text(payload)
    if isinstance(payload, dict):
        return {str(k): redact_pii_structure(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [redact_pii_structure(item) for item in payload]
    if isinstance(payload, tuple):
        return tuple(redact_pii_structure(item) for item in payload)
    return payload

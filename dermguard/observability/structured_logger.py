"""Structured JSON logging with explicit INTENT vs. OUTCOME telemetry capture.

Emits Google Cloud Logging-compatible JSON records enriched with OpenTelemetry
`trace_id` and `span_id` fields and automatic PII scrubbing.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any, Literal

from opentelemetry import trace

from dermguard.observability.pii_redactor import redact_pii_structure, redact_pii_text

EventPhase = Literal["INTENT", "OUTCOME", "GUARDRAIL", "SYSTEM", "ERROR"]


class JsonLogFormatter(logging.Formatter):
    """Formats Python `LogRecord` objects into single-line structured JSON."""

    def format(self, record: logging.LogRecord) -> str:
        """Serializes a log record into Cloud Logging JSON format.

        Args:
            record: Standard Python logging record.

        Returns:
            Single-line JSON string with scrubbed message and metadata.
        """
        span_ctx = trace.get_current_span().get_span_context()
        trace_id = f"{span_ctx.trace_id:032x}" if span_ctx and span_ctx.is_valid else None
        span_id = f"{span_ctx.span_id:016x}" if span_ctx and span_ctx.is_valid else None

        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "severity": record.levelname,
            "logger": record.name,
            "message": redact_pii_text(record.getMessage()),
            "event_phase": getattr(record, "event_phase", "SYSTEM"),
            "action_name": getattr(record, "action_name", record.funcName),
            "details": redact_pii_structure(getattr(record, "details", {})),
        }
        if trace_id:
            payload["trace_id"] = trace_id
            project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
            if project_id:
                payload["logging.googleapis.com/trace"] = f"projects/{project_id}/traces/{trace_id}"
        if span_id:
            payload["span_id"] = span_id
            payload["logging.googleapis.com/spanId"] = span_id
        return json.dumps(payload, default=str)


def get_structured_logger(name: str = "dermguard") -> logging.Logger:
    """Returns a configured structured JSON logger writing to stderr.

    Args:
        name: Logger namespace.

    Returns:
        Configured `logging.Logger` instance without duplicate handlers.
    """
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(JsonLogFormatter())
        logger.addHandler(handler)
        level_name = os.getenv("DERMGUARD_LOG_LEVEL", "INFO").upper()
        logger.setLevel(getattr(logging, level_name, logging.INFO))
        logger.propagate = False
    return logger


def log_agent_event(
    *,
    event_phase: EventPhase,
    action_name: str,
    details: dict[str, Any],
    message: str | None = None,
    level: int = logging.INFO,
    logger_instance: logging.Logger | None = None,
) -> None:
    """Records an explicit INTENT, OUTCOME, GUARDRAIL, or ERROR structured event.

    Args:
        event_phase: Lifecycle stage (`INTENT` before execution, `OUTCOME` after).
        action_name: Specific tool, model, or guardrail identifier.
        details: Metadata dictionary (automatically scrubbed for PII).
        message: Optional human-readable summary string.
        level: Python logging severity level.
        logger_instance: Optional custom logger for isolated testing.
    """
    active_logger = logger_instance or get_structured_logger()
    summary = message or f"[{event_phase}] {action_name}"
    active_logger.log(
        level,
        summary,
        extra={
            "event_phase": event_phase,
            "action_name": action_name,
            "details": details,
        },
    )

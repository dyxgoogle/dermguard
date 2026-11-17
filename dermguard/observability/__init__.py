"""Observability package providing PII redaction, JSON logging, and OTel tracing."""

from dermguard.observability.pii_redactor import redact_pii_structure, redact_pii_text
from dermguard.observability.structured_logger import (
    JsonLogFormatter,
    get_structured_logger,
    log_agent_event,
)
from dermguard.observability.tracing import configure_tracing, get_in_memory_spans

__all__ = [
    "JsonLogFormatter",
    "configure_tracing",
    "get_in_memory_spans",
    "get_structured_logger",
    "log_agent_event",
    "redact_pii_structure",
    "redact_pii_text",
]

"""OpenTelemetry distributed tracing setup for DermGuard.

Links incoming HTTP `/chat` requests, ADK agent invocations, LLM calls, and
local FastMCP safety checks into unified parent-child trace spans. Supports
in-memory span capture for local verification and Google Cloud Trace export in
production deployments (`OTEL_TRACES_EXPORTER=google_cloud_trace`).
"""

from __future__ import annotations

import os

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

_IN_MEMORY_EXPORTER = InMemorySpanExporter()
_INITIALIZED = False


def configure_tracing(service_name: str | None = None) -> trace.Tracer:
    """Initializes the global OpenTelemetry `TracerProvider` once per process.

    Args:
        service_name: Logical service name attached to emitted spans.

    Returns:
        Configured OpenTelemetry `Tracer` for DermGuard components.
    """
    global _INITIALIZED
    resolved_service = service_name or os.getenv("OTEL_SERVICE_NAME", "dermguard-agent")

    if not _INITIALIZED and os.getenv("OTEL_SDK_DISABLED", "false").lower() != "true":
        resource = Resource.create({"service.name": resolved_service, "service.version": "1.0.0"})
        provider = TracerProvider(resource=resource)
        exporter_mode = os.getenv("OTEL_TRACES_EXPORTER", "memory").lower()

        if exporter_mode == "google_cloud_trace":
            try:
                from opentelemetry.exporter.cloud_trace import (  # type: ignore[import-untyped]
                    CloudTraceSpanExporter,
                )

                provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter()))
            except Exception:
                provider.add_span_processor(SimpleSpanProcessor(_IN_MEMORY_EXPORTER))
        else:
            provider.add_span_processor(SimpleSpanProcessor(_IN_MEMORY_EXPORTER))

        trace.set_tracer_provider(provider)
        _INITIALIZED = True

    return trace.get_tracer(resolved_service)


def get_in_memory_spans() -> list:
    """Returns finished spans captured by the in-memory exporter during local runs."""
    return list(_IN_MEMORY_EXPORTER.get_finished_spans())

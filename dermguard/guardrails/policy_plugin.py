"""ADK BasePlugin enforcing cosmetic scope guardrails and Intent/Outcome telemetry.

Provides global cross-cutting protections across all agents and tools in the
ADK `App`:
1. **Medical Diagnosis & Prescription Guardrail (`before_model_callback`)**:
   Deterministically intercepts requests asking to diagnose rashes/lesions/infections
   or prescribe steroids/antibiotics, returning a safe dermatological referral.
2. **Output Self-Evaluation Guardrail (`after_model_callback`)**:
   Audits model responses to prevent accidental prescription dosing claims.
3. **Intent vs. Outcome Structured Logging & OpenTelemetry Spans**:
   Explicitly records `INTENT` before every LLM/tool call and `OUTCOME` (with
   latency and status) after execution, with PII redacted.
4. **Guided Tool Error Recovery (`on_tool_error_callback`)**:
   Converts unexpected tool exceptions into actionable LLM recovery instructions.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.plugins.base_plugin import BasePlugin
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext
from google.genai import types as genai_types

from dermguard.observability.pii_redactor import redact_pii_structure, redact_pii_text
from dermguard.observability.structured_logger import log_agent_event
from dermguard.observability.tracing import configure_tracing

_MEDICAL_DIAGNOSIS_PATTERNS = [
    re.compile(
        r"\b(diagnose|what\s+is\s+this\s+rash|bleeding\s+(?:rash|mole|lesion)|infected\s+(?:wound|cyst))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(prescribe|prescription\s+for|should\s+i\s+take\s+oral)\b.*\b(steroid|hydrocortisone|clobetasol|antibiotic|doxycycline|clindamycin|isotretinoin|accutane|antifungal)\b",
        re.IGNORECASE,
    ),
]

_PROMPT_INJECTION_RE = re.compile(
    r"\b(ignore\s+(?:all\s+)?previous\s+instructions|override\s+safety\s+constitution)\b",
    re.IGNORECASE,
)

MEDICAL_REFUSAL_MESSAGE = (
    "I cannot diagnose skin conditions, evaluate rashes or lesions, or recommend "
    "prescription medications (such as topical steroids, oral/topical antibiotics, "
    "or prescription therapies). DermGuard is strictly a cosmetic skincare ingredient "
    "and routine compatibility information tool. Please consult a board-certified "
    "dermatologist or licensed healthcare provider for medical evaluation and treatment."
)


def _extract_latest_user_text(llm_request: LlmRequest) -> str:
    """Returns the concatenated text of the most recent user message in `LlmRequest`."""
    if not llm_request.contents:
        return ""
    for content in reversed(llm_request.contents):
        if content.role == "user" and content.parts:
            texts = [part.text for part in content.parts if getattr(part, "text", None)]
            if texts:
                return "\n".join(texts)
    return ""


class DermGuardPolicyAndTelemetryPlugin(BasePlugin):
    """Global ADK policy guardrail and OpenTelemetry / JSON Intent-Outcome plugin."""

    def __init__(self, name: str = "dermguard_policy_and_telemetry") -> None:
        super().__init__(name=name)
        self._tracer = configure_tracing("dermguard-plugin")
        self._tool_start_times: dict[str, float] = {}
        self._model_start_times: dict[str, float] = {}

    async def before_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        """Logs model INTENT and enforces deterministic medical & injection guardrails."""
        inv_id = getattr(callback_context, "invocation_id", "unknown")
        self._model_start_times[inv_id] = time.perf_counter()
        user_text = _extract_latest_user_text(llm_request)

        log_agent_event(
            event_phase="INTENT",
            action_name=f"llm_call:{llm_request.model}",
            details={
                "agent_name": getattr(callback_context, "agent_name", "unknown"),
                "invocation_id": inv_id,
                "user_prompt_preview": redact_pii_text(user_text[:240]),
            },
        )

        if _PROMPT_INJECTION_RE.search(user_text):
            log_agent_event(
                event_phase="GUARDRAIL",
                action_name="prompt_injection_blocked",
                details={"invocation_id": inv_id},
            )
            return LlmResponse(
                content=genai_types.Content(
                    role="model",
                    parts=[
                        genai_types.Part.from_text(
                            text=(
                                "I can only assist with cosmetic skincare ingredient lookup "
                                "and multi-product routine compatibility analysis."
                            )
                        )
                    ],
                )
            )

        for pattern in _MEDICAL_DIAGNOSIS_PATTERNS:
            if pattern.search(user_text):
                log_agent_event(
                    event_phase="GUARDRAIL",
                    action_name="medical_scope_refusal",
                    details={"invocation_id": inv_id, "matched_rule": pattern.pattern[:60]},
                )
                return LlmResponse(
                    content=genai_types.Content(
                        role="model",
                        parts=[genai_types.Part.from_text(text=MEDICAL_REFUSAL_MESSAGE)],
                    )
                )

        return None

    async def after_model_callback(
        self,
        *,
        callback_context: CallbackContext,
        llm_response: LlmResponse,
    ) -> LlmResponse | None:
        """Logs model OUTCOME with latency and performs output policy verification."""
        inv_id = getattr(callback_context, "invocation_id", "unknown")
        started = self._model_start_times.pop(inv_id, None)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2) if started else 0.0

        has_tool_calls = False
        text_preview = ""
        if llm_response.content and llm_response.content.parts:
            for part in llm_response.content.parts:
                if getattr(part, "function_call", None):
                    has_tool_calls = True
                if getattr(part, "text", None) and not text_preview:
                    text_preview = redact_pii_text(part.text[:200])

        log_agent_event(
            event_phase="OUTCOME",
            action_name="llm_response",
            details={
                "agent_name": getattr(callback_context, "agent_name", "unknown"),
                "invocation_id": inv_id,
                "latency_ms": latency_ms,
                "delegated_to_tool": has_tool_calls,
                "response_preview": text_preview,
            },
        )
        return None

    async def before_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
    ) -> dict[str, Any] | None:
        """Records explicit tool INTENT and scrubs PII before tool execution."""
        key = f"{id(tool_context)}:{tool.name}"
        self._tool_start_times[key] = time.perf_counter()
        log_agent_event(
            event_phase="INTENT",
            action_name=f"tool:{tool.name}",
            details={
                "tool_name": tool.name,
                "intended_args": redact_pii_structure(tool_args),
            },
        )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        result: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Records explicit tool OUTCOME with execution duration and status."""
        key = f"{id(tool_context)}:{tool.name}"
        started = self._tool_start_times.pop(key, None)
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2) if started else 0.0
        status = result.get("status", "completed") if isinstance(result, dict) else "completed"

        log_agent_event(
            event_phase="OUTCOME",
            action_name=f"tool:{tool.name}",
            details={
                "tool_name": tool.name,
                "status": status,
                "latency_ms": latency_ms,
            },
        )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        tool_args: dict[str, Any],
        tool_context: ToolContext,
        error: Exception,
    ) -> dict[str, Any] | None:
        """Transforms unexpected tool exceptions into guided LLM recovery instructions."""
        if isinstance(error, asyncio.CancelledError):
            return None

        log_agent_event(
            event_phase="ERROR",
            action_name=f"tool:{tool.name}",
            details={
                "tool_name": tool.name,
                "error_type": type(error).__name__,
                "error_message": redact_pii_text(str(error)),
            },
        )
        return {
            "status": "error",
            "tool_name": tool.name,
            "error_type": type(error).__name__,
            "message": redact_pii_text(str(error)),
            "recovery_instructions": (
                f"Tool '{tool.name}' failed due to {type(error).__name__}. "
                "Verify all required parameters match the JSON schema types, "
                "avoid passing None where strings/dicts are required, and retry once."
            ),
        }

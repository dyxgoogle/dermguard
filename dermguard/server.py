"""FastAPI HTTP server exposing DermGuard with persistent SQLite session storage.

Provides:
- `GET /health`: Readiness probe verifying toxicology rules and session backend.
- `POST /chat`: Conversational endpoint executing `dermguard.agent.app` with
  persistent `DatabaseSessionService` (`sqlite+aiosqlite`), OpenTelemetry
  distributed tracing spans, PII scrubbing, and structured JSON telemetry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from google.adk.errors.already_exists_error import AlreadyExistsError
from google.adk.memory import InMemoryMemoryService
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService, InMemorySessionService
from google.genai import types as genai_types

from dermguard.agent import app as dermguard_adk_app
from dermguard.config import SETTINGS
from dermguard.data.seed_ingredients import DEFAULT_RULES_PATH, generate_ingredient_rules
from dermguard.observability.pii_redactor import redact_pii_text
from dermguard.observability.structured_logger import log_agent_event
from dermguard.observability.tracing import configure_tracing
from dermguard.tools.routine_tools import ROUTINE_STATE_KEY
from dermguard.tools.schemas import ChatRequest, ChatResponse

_TRACER = configure_tracing("dermguard-fastapi")


def _build_session_service():
    """Creates a persistent SQLite `DatabaseSessionService` (with in-memory fallback)."""
    db_url = SETTINGS.session_db_url
    if db_url.startswith("sqlite"):
        raw_path = db_url.split("///", maxsplit=1)[-1]
        if raw_path and raw_path != ":memory:":
            Path(raw_path).parent.mkdir(parents=True, exist_ok=True)
        return DatabaseSessionService(db_url=db_url)
    if db_url == "memory":
        return InMemorySessionService()
    return DatabaseSessionService(db_url=db_url)


_SESSION_SERVICE = _build_session_service()
_MEMORY_SERVICE = InMemoryMemoryService()
_RUNNER = Runner(
    app=dermguard_adk_app,
    session_service=_SESSION_SERVICE,
    memory_service=_MEMORY_SERVICE,
)


@asynccontextmanager
async def _lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Ensures the 30-active ingredient rules database exists at server startup."""
    if not DEFAULT_RULES_PATH.exists():
        generate_ingredient_rules(output_path=DEFAULT_RULES_PATH)
    log_agent_event(
        event_phase="SYSTEM",
        action_name="fastapi_startup",
        details={"app_name": SETTINGS.app_name, "primary_model": SETTINGS.primary_model},
    )
    yield
    await _RUNNER.close()


fastapi_app = FastAPI(
    title="DermGuard Skincare Ingredient & Conflict Analyst API",
    version="1.0.0",
    description="Conversational cosmetic ingredient lookup and conflict analyzer powered by Google ADK.",
    lifespan=_lifespan,
)


@fastapi_app.get("/health")
async def health_check() -> dict[str, str | int | bool]:
    """Health check endpoint for local verification and Cloud Run probes."""
    return {
        "status": "ok",
        "app_name": SETTINGS.app_name,
        "compaction_interval": SETTINGS.compaction_interval,
        "rules_seeded": DEFAULT_RULES_PATH.exists(),
    }


@fastapi_app.post("/chat", response_model=ChatResponse)
async def chat_endpoint(payload: ChatRequest) -> ChatResponse:
    """Executes a conversational skincare safety turn within a persistent session."""
    with _TRACER.start_as_current_span("http.post.chat") as span:
        clean_user_id = redact_pii_text(payload.user_id.strip())
        clean_session_id = redact_pii_text(payload.session_id.strip())
        clean_message = redact_pii_text(payload.message.strip())

        span.set_attribute("dermguard.user_id", clean_user_id)
        span.set_attribute("dermguard.session_id", clean_session_id)

        log_agent_event(
            event_phase="INTENT",
            action_name="http_chat_request",
            details={"user_id": clean_user_id, "session_id": clean_session_id},
        )

        existing = await _SESSION_SERVICE.get_session(
            app_name=SETTINGS.app_name,
            user_id=clean_user_id,
            session_id=clean_session_id,
        )
        if existing is None:
            try:
                await _SESSION_SERVICE.create_session(
                    app_name=SETTINGS.app_name,
                    user_id=clean_user_id,
                    session_id=clean_session_id,
                    state={ROUTINE_STATE_KEY: {}},
                )
            except AlreadyExistsError:
                pass

        final_chunks: list[str] = []
        try:
            async for event in _RUNNER.run_async(
                user_id=clean_user_id,
                session_id=clean_session_id,
                new_message=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part.from_text(text=clean_message)],
                ),
            ):
                if event.is_final_response() and event.content and event.content.parts:
                    for part in event.content.parts:
                        if getattr(part, "text", None):
                            final_chunks.append(part.text)
        except Exception as exc:
            log_agent_event(
                event_phase="ERROR",
                action_name="http_chat_request",
                details={"error_type": type(exc).__name__, "message": str(exc)},
            )
            raise HTTPException(
                status_code=500,
                detail="DermGuard encountered an execution error. Verify GOOGLE_API_KEY or Vertex AI credentials.",
            ) from exc

        updated_session = await _SESSION_SERVICE.get_session(
            app_name=SETTINGS.app_name,
            user_id=clean_user_id,
            session_id=clean_session_id,
        )
        routine_map = (
            dict(updated_session.state.get(ROUTINE_STATE_KEY, {}))
            if updated_session and updated_session.state
            else {}
        )
        span_ctx = span.get_span_context()
        trace_hex = f"{span_ctx.trace_id:032x}" if span_ctx and span_ctx.is_valid else None

        reply_text = "\n".join(final_chunks).strip() or "No text response generated."
        log_agent_event(
            event_phase="OUTCOME",
            action_name="http_chat_request",
            details={
                "user_id": clean_user_id,
                "session_id": clean_session_id,
                "routine_products": len(routine_map),
            },
        )
        return ChatResponse(
            user_id=clean_user_id,
            session_id=clean_session_id,
            reply=reply_text,
            active_routine_products=routine_map,
            trace_id=trace_hex,
        )


app = fastapi_app

"""Deterministic unit and contract test suite for DermGuard.

Covers:
1. Seeded 30-ingredient cosmetic toxicology dataset & MCP safety rules.
2. Incremental product addition in session state triggering pairwise conflict
   detection (Glycolic Acid + Retinol).
3. Cumulative active stacking warnings across multiple Salicylic Acid products.
4. Persistent user sensitivity memory across sessions with PII redaction.
5. ADK App EventsCompactionConfig (compaction_interval=10, overlap_size=2),
   GoogleSearchTool(bypass_multi_tools_limit=True), and multi-agent routing.
6. Deterministic medical diagnosis / prescription guardrail refusal.
7. Guided LLM error handling, explicit JSON schemas, HITL confirmation hooks,
   structured JSON Intent/Outcome logging, and OpenTelemetry tracing.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.genai import types as genai_types


class DummyToolContext:
    """Minimal deterministic stand-in for ADK ToolContext during unit tests."""

    def __init__(self, initial_state: dict | None = None) -> None:
        self.state: dict = initial_state if initial_state is not None else {}
        self.actions = SimpleNamespace(escalate=False)


def test_seed_ingredients_generates_30_actives(tmp_path: Path) -> None:
    """Verifies deterministic generation of 30 skincare actives and conflict rules."""
    from dermguard.data.seed_ingredients import generate_ingredient_rules

    out_file = tmp_path / "ingredient_rules.json"
    dataset = generate_ingredient_rules(output_path=out_file)

    assert out_file.exists()
    persisted = json.loads(out_file.read_text(encoding="utf-8"))
    assert len(dataset["ingredients"]) == 30
    assert len(persisted["ingredients"]) == 30
    assert "glycolic_acid" in persisted["ingredients"]
    assert "retinol" in persisted["ingredients"]
    assert "salicylic_acid" in persisted["ingredients"]
    assert len(persisted["pairwise_conflicts"]) >= 12


def test_incremental_product_addition_triggers_conflict() -> None:
    """Verifies Turn 1 + Turn 2 routine accumulation triggers Glycolic + Retinol conflict."""
    from dermguard.mcp_server.ingredient_safety_mcp import analyze_routine_safety
    from dermguard.tools.routine_tools import manage_routine_state

    ctx = DummyToolContext()

    # Turn 1: Add The Ordinary Glycolic Acid 7% Toning Solution
    turn1_state = manage_routine_state(
        action="add",
        product_name="The Ordinary Glycolic Acid 7% Toning Solution",
        ingredients=["Glycolic Acid 7%", "Rosa Damascena Flower Water", "Glycerin"],
        tool_context=ctx,
    )
    assert turn1_state["status"] == "success"
    assert turn1_state["total_products"] == 1

    turn1_safety = analyze_routine_safety(
        routine_products=turn1_state["active_routine_products"],
        user_sensitivities=["sensitive_skin"],
    )
    assert turn1_safety["status"] == "success"
    assert turn1_safety["pairwise_conflicts"] == []
    assert any(
        flag["caution_type"] in {"HIGH_DOSE_CAUTION", "SENSITIVITY_CONTRAINDICATION"}
        for flag in turn1_safety["caution_flags"]
    )

    # Turn 2: Incrementally add CeraVe Resurfacing Retinol Serum
    turn2_state = manage_routine_state(
        action="add",
        product_name="CeraVe Resurfacing Retinol Serum",
        ingredients=["Retinol", "Niacinamide", "Ceramides"],
        tool_context=ctx,
    )
    assert turn2_state["status"] == "success"
    assert turn2_state["total_products"] == 2
    assert "The Ordinary Glycolic Acid 7% Toning Solution" in turn2_state["active_routine_products"]
    assert "CeraVe Resurfacing Retinol Serum" in turn2_state["active_routine_products"]

    turn2_safety = analyze_routine_safety(
        routine_products=turn2_state["active_routine_products"],
        user_sensitivities=["sensitive_skin"],
    )
    assert turn2_safety["status"] == "success"
    conflict_pairs = {
        frozenset((c["ingredient_a"], c["ingredient_b"]))
        for c in turn2_safety["pairwise_conflicts"]
    }
    assert frozenset(("glycolic_acid", "retinol")) in conflict_pairs
    assert turn2_safety["overall_risk_level"] in {"HIGH", "CRITICAL"}


def test_cumulative_active_stacking_warning() -> None:
    """Verifies stacking multiple Salicylic Acid products raises CUMULATIVE_STACKING_CAUTION."""
    from dermguard.mcp_server.ingredient_safety_mcp import analyze_routine_safety

    routine = {
        "CeraVe Renewing SA Cleanser": ["Salicylic Acid", "Niacinamide", "Ceramides"],
        "Paula's Choice 2% BHA Liquid Exfoliant": [
            "Salicylic Acid 2%",
            "Methylpropanediol",
            "Camellia Oleifera Leaf Extract",
        ],
    }

    report = analyze_routine_safety(
        routine_products=routine,
        user_sensitivities=["sensitive_skin"],
    )
    assert report["status"] == "success"
    assert len(report["cumulative_stacking_warnings"]) >= 1
    sa_stack = next(
        w for w in report["cumulative_stacking_warnings"] if w["ingredient"] == "salicylic_acid"
    )
    assert sa_stack["product_count"] == 2
    assert sa_stack["warning_type"] == "CUMULATIVE_STACKING_CAUTION"


@pytest.mark.asyncio
async def test_persistent_sensitivity_memory_across_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies user sensitivities persist across distinct sessions and scrub PII."""
    from dermguard.tools.memory_tools import manage_user_sensitivities

    mem_file = tmp_path / "user_sensitivities.json"
    db_file = tmp_path / "sensitivities.db"
    monkeypatch.setenv("DERMGUARD_SENSITIVITY_FILE", str(mem_file))
    monkeypatch.setenv("DERMGUARD_SENSITIVITY_SQLITE", str(db_file))

    session_1_ctx = DummyToolContext()
    res_add_1 = await manage_user_sensitivities(
        action="add",
        user_id="user_42",
        sensitivity="rosacea",
        tool_context=session_1_ctx,
    )
    assert res_add_1["status"] == "success"
    assert "rosacea" in res_add_1["sensitivities"]

    # Include accidental PII in sensitivity string to verify scrubbing
    res_add_2 = await manage_user_sensitivities(
        action="add",
        user_id="user_42",
        sensitivity="pregnant contact jane.doe@example.org",
        tool_context=session_1_ctx,
    )
    assert res_add_2["status"] == "success"

    # Fresh session context simulates a brand-new conversation turn days later
    session_2_ctx = DummyToolContext()
    res_get = await manage_user_sensitivities(
        action="get",
        user_id="user_42",
        sensitivity="",
        tool_context=session_2_ctx,
    )
    assert res_get["status"] == "success"
    assert "rosacea" in res_get["sensitivities"]
    assert any("pregnant" in item for item in res_get["sensitivities"])
    raw_disk = mem_file.read_text(encoding="utf-8")
    assert "jane.doe@example.org" not in raw_disk


def test_compaction_config_set_to_10_queries() -> None:
    """Verifies ADK App compaction interval=10, overlap=2, and GoogleSearchTool setup."""
    from dermguard.agent import app, root_agent

    assert app.name == "dermguard"
    assert app.root_agent is root_agent
    assert root_agent.name == "dermguard_ingredient_agent"
    assert root_agent.model == "gemini-2.5-flash"

    assert app.events_compaction_config is not None
    assert app.events_compaction_config.compaction_interval == 10
    assert app.events_compaction_config.overlap_size == 2

    search_tools = [t for t in root_agent.tools if isinstance(t, GoogleSearchTool)]
    assert len(search_tools) == 1
    assert search_tools[0].bypass_multi_tools_limit is True


@pytest.mark.asyncio
async def test_medical_claim_refusal() -> None:
    """Verifies medical diagnosis & prescription requests are deterministically refused."""
    from google.adk.models.llm_request import LlmRequest

    from dermguard.guardrails.policy_plugin import DermGuardPolicyAndTelemetryPlugin

    plugin = DermGuardPolicyAndTelemetryPlugin()
    callback_ctx = SimpleNamespace(
        agent_name="dermguard_ingredient_agent",
        invocation_id="inv_test_1",
        state={},
    )
    medical_request = LlmRequest(
        model="gemini-2.5-flash",
        contents=[
            genai_types.Content(
                role="user",
                parts=[
                    genai_types.Part.from_text(
                        text="Can you diagnose this bleeding skin rash and prescribe topical hydrocortisone or an antibiotic?"
                    )
                ],
            )
        ],
    )

    intercepted = await plugin.before_model_callback(
        callback_context=callback_ctx,
        llm_request=medical_request,
    )
    assert intercepted is not None
    response_text = intercepted.content.parts[0].text.lower()
    assert "cosmetic" in response_text
    assert "dermatologist" in response_text


def test_guided_error_handling_and_hitl_hooks() -> None:
    """Verifies guided LLM recovery messages on invalid tool calls and HITL hooks."""
    from dermguard.mcp_server.ingredient_safety_mcp import get_ingredient_caution_profile
    from dermguard.tools.memory_tools import reset_user_sensitivity_memory_tool
    from dermguard.tools.routine_tools import (
        clear_entire_skincare_routine_tool,
        manage_routine_state,
    )

    ctx = DummyToolContext()
    bad_action = manage_routine_state(
        action="invalid_op",
        product_name="",
        ingredients=[],
        tool_context=ctx,
    )
    assert bad_action["status"] == "error"
    assert "recovery_instructions" in bad_action

    missing_ing = get_ingredient_caution_profile(ingredient_name="nonexistent_xyz_molecule")
    assert missing_ing["status"] == "not_found"
    assert "recovery_instructions" in missing_ing

    assert clear_entire_skincare_routine_tool._require_confirmation is True
    assert reset_user_sensitivity_memory_tool._require_confirmation is True


def test_structured_json_intent_outcome_logging_and_pii_redaction() -> None:
    """Verifies structured JSON logs capture INTENT and OUTCOME with PII scrubbed."""
    from dermguard.observability.pii_redactor import redact_pii_text
    from dermguard.observability.structured_logger import JsonLogFormatter, log_agent_event

    scrubbed = redact_pii_text("Reach me at alice@domain.org or 415-555-0199 regarding retinol.")
    assert "alice@domain.org" not in scrubbed
    assert "415-555-0199" not in scrubbed
    assert "[REDACTED_EMAIL]" in scrubbed
    assert "[REDACTED_PHONE]" in scrubbed

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    test_logger = logging.getLogger("dermguard.test_audit")
    test_logger.handlers = [handler]
    test_logger.setLevel(logging.INFO)
    test_logger.propagate = False

    log_agent_event(
        event_phase="INTENT",
        action_name="manage_routine_state",
        details={"user_note": "email bob@domain.org"},
        logger_instance=test_logger,
    )
    log_agent_event(
        event_phase="OUTCOME",
        action_name="manage_routine_state",
        details={"status": "success", "latency_ms": 4.2},
        logger_instance=test_logger,
    )

    lines = [json.loads(line) for line in stream.getvalue().strip().splitlines()]
    assert len(lines) == 2
    assert lines[0]["event_phase"] == "INTENT"
    assert "bob@domain.org" not in json.dumps(lines[0])
    assert lines[1]["event_phase"] == "OUTCOME"
    assert lines[1]["details"]["status"] == "success"


@pytest.mark.asyncio
async def test_mcp_stdio_toolset_and_fastapi_health() -> None:
    """Verifies live stdio MCP tool discovery and FastAPI /health readiness."""
    from httpx import ASGITransport, AsyncClient

    from dermguard.agent import create_ingredient_safety_mcp_toolset
    from dermguard.server import fastapi_app

    toolset = create_ingredient_safety_mcp_toolset()
    try:
        discovered_tools = await toolset.get_tools()
        tool_names = {t.name for t in discovered_tools}
        assert "analyze_routine_safety" in tool_names
        assert "get_ingredient_caution_profile" in tool_names
    finally:
        await toolset.close()

    transport = ASGITransport(app=fastapi_app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["compaction_interval"] == 10
        assert body["rules_seeded"] is True


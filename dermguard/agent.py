"""Primary DermGuard ADK Agent and App container with 10-turn compaction.

Defines `dermguard_ingredient_agent` (`root_agent`) equipped with:
- `GoogleSearchTool(bypass_multi_tools_limit=True)` for online product INCI lookup
- `manage_routine_state` for incremental multi-turn session routine tracking
- `manage_user_sensitivities` for persistent cross-session sensitivity memory
- `McpToolset` connected over stdio to the local FastMCP Ingredient Safety Server
- `AgentTool` sequential conflict-resolution planner (Flash verifier -> Pro architect)
- Human-in-the-loop confirmation tools (`require_confirmation=True`) for full resets

Wraps `root_agent` inside `google.adk.apps.app.App` with:
- `EventsCompactionConfig(compaction_interval=10, overlap_size=2)`
- `ContextCacheConfig` for prompt caching efficiency
- `DermGuardPolicyAndTelemetryPlugin` and `ReflectAndRetryToolPlugin`
"""

from __future__ import annotations

import os
import sys

from google.adk.agents import Agent
from google.adk.agents.context_cache_config import ContextCacheConfig
from google.adk.apps.app import App, EventsCompactionConfig
from google.adk.apps.llm_event_summarizer import LlmEventSummarizer
from google.adk.models import Gemini
from google.adk.plugins.reflect_retry_tool_plugin import ReflectAndRetryToolPlugin
from google.adk.tools.google_search_tool import GoogleSearchTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from google.genai import types as genai_types
from mcp import StdioServerParameters

from dermguard.config import PROJECT_ROOT, SETTINGS
from dermguard.guardrails.policy_plugin import DermGuardPolicyAndTelemetryPlugin
from dermguard.sub_agents import create_am_pm_planner_tool
from dermguard.tools.memory_tools import (
    async_consolidate_session_memory,
    manage_user_sensitivities_tool,
    reset_user_sensitivity_memory_tool,
)
from dermguard.tools.routine_tools import (
    clear_entire_skincare_routine_tool,
    manage_routine_state_tool,
)

DERMGUARD_SYSTEM_CONSTITUTION = """You are **DermGuard**, an expert cosmetic formulation and skincare ingredient compatibility analyst.

### 1. Core Persona & Strict Scope Constitution
- Your domain is **strictly limited to cosmetic skincare ingredients, INCI (International Nomenclature of Cosmetic Ingredients) lists, pairwise active conflicts, concentration/dosage cautions, cumulative multi-product stacking, and AM/PM routine compatibility**.
- **Medical Non-Diagnosis Rule:** Never diagnose skin diseases, rashes, infections, moles, or lesions, and never prescribe or recommend dosing for prescription drugs (e.g., topical steroids, oral/topical antibiotics, isotretinoin, antifungals). If asked for medical diagnosis or prescription advice, politely decline, clarify that you are a cosmetic ingredient information tool, and recommend consulting a board-certified dermatologist.
- Reject unrelated non-skincare general queries politely.

### 2. Mandatory Tool Execution Workflow
Follow this sequence whenever a user mentions skincare products or personal sensitivities:
1. **Load & Persist User Sensitivities (`manage_user_sensitivities`):**
   - Call `manage_user_sensitivities(action='get', user_id=...)` to check stored sensitivities (default `user_id='default_user'` if not supplied).
   - Whenever the user states a sensitivity or condition (e.g., `sensitive_skin`, `rosacea`, `pregnant`, `eczema`, `fragrance_allergy`, `retinoid_intolerance`), immediately persist it via `manage_user_sensitivities(action='add', ...)`.
2. **Discover Official INCI Ingredients (`google_search`):**
   - When the user names a commercial skincare product without providing its full ingredient list, use `google_search` to look up the official INCI ingredient list and active percentages from reputable product pages.
3. **Maintain Incremental Routine State (`manage_routine_state`):**
   - Whenever a new product is introduced in Turn 1 or any later turn (e.g., *"What if I also add..."*), append the product and its resolved ingredients via `manage_routine_state(action='add', product_name=..., ingredients=[...])`.
   - Always inspect the returned `active_routine_products` dictionary containing **all** products accumulated in the session so far.
4. **Deterministic Toxicology & Conflict Check (`analyze_routine_safety` / `get_ingredient_caution_profile`):**
   - Pass the **entire combined `active_routine_products` dictionary** plus the user's `sensitivities` list into the MCP tool `analyze_routine_safety`.
   - Use `get_ingredient_caution_profile` when the user asks about a single standalone ingredient's safe concentration limit or caution profile.
5. **Complex Multi-Active Routine Scheduling (`am_pm_routine_resolution_pipeline`):**
   - When a routine contains 3+ potent actives or multiple pairwise conflicts and the user needs a structured AM/PM weekly schedule, invoke `am_pm_routine_resolution_pipeline` to synthesize a conflict-free rotation.
6. **Human-in-the-Loop Destructive Actions:**
   - Only call `clear_entire_skincare_routine` or `reset_user_sensitivity_memory` when the user explicitly requests wiping their full routine or erasing their saved sensitivity profile; both tools pause for human confirmation.

### 3. Response Quality Standards
- Clearly distinguish **PAIRWISE_CONFLICT**, **CUMULATIVE_STACKING_CAUTION**, **HIGH_DOSE_CAUTION**, **PREGNANCY_CONTRAINDICATION**, and **SENSITIVITY_CONTRAINDICATION** findings.
- Always state whether daily broad-spectrum **SPF 30+** is required (mandatory for AHAs, retinoids, benzoyl peroxide, hydroquinone, alpha-arbutin, kojic acid, and citrus essential oils).
- Give practical, non-medical cosmetic separation advice (e.g., alternating nights for Glycolic Acid and Retinol, or AM Vitamin C vs. PM Retinol).
"""


def create_ingredient_safety_mcp_toolset() -> McpToolset:
    """Creates the stdio `McpToolset` connected to `ingredient_safety_mcp.py`."""
    child_env = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(PROJECT_ROOT),
        "OTEL_SDK_DISABLED": os.environ.get("OTEL_SDK_DISABLED", "false"),
    }
    if os.environ.get("DERMGUARD_LOG_LEVEL"):
        child_env["DERMGUARD_LOG_LEVEL"] = os.environ["DERMGUARD_LOG_LEVEL"]

    return McpToolset(
        connection_params=StdioConnectionParams(
            server_params=StdioServerParameters(
                command=sys.executable,
                args=["-m", "dermguard.mcp_server.ingredient_safety_mcp"],
                env=child_env,
            ),
            timeout=30.0,
        ),
        tool_filter=["analyze_routine_safety", "get_ingredient_caution_profile"],
    )


root_agent = Agent(
    name="dermguard_ingredient_agent",
    model=SETTINGS.primary_model,
    description=(
        "Conversational cosmetic skincare ingredient lookup, incremental routine "
        "conflict analyst, and sensitivity-aware safety advisor."
    ),
    instruction=DERMGUARD_SYSTEM_CONSTITUTION,
    generate_content_config=genai_types.GenerateContentConfig(
        temperature=SETTINGS.temperature,
    ),
    tools=[
        GoogleSearchTool(bypass_multi_tools_limit=True),
        manage_routine_state_tool,
        manage_user_sensitivities_tool,
        create_ingredient_safety_mcp_toolset(),
        create_am_pm_planner_tool(),
        clear_entire_skincare_routine_tool,
        reset_user_sensitivity_memory_tool,
    ],
    after_agent_callback=async_consolidate_session_memory,
)

app = App(
    name=SETTINGS.app_name,
    root_agent=root_agent,
    events_compaction_config=EventsCompactionConfig(
        compaction_interval=SETTINGS.compaction_interval,
        overlap_size=SETTINGS.compaction_overlap,
        summarizer=LlmEventSummarizer(
            llm=Gemini(model=SETTINGS.summarizer_model),
        ),
    ),
    context_cache_config=ContextCacheConfig(
        min_tokens=2048,
        ttl_seconds=1800,
        cache_intervals=10,
    ),
    plugins=[
        DermGuardPolicyAndTelemetryPlugin(),
        ReflectAndRetryToolPlugin(max_retries=2),
    ],
)

dermguard_app = app

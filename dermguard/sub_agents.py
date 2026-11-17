"""Specialist multi-agent conflict resolution pipeline with strategic model routing.

Implements a deterministic two-stage `SequentialAgent` exposed via `AgentTool`:
1. `biochemical_conflict_verifier_agent` (`gemini-2.5-flash`): Rapidly extracts
   conflicting chemical pairs, pH incompatibilities, and photosensitivity constraints.
2. `am_pm_schedule_architect_agent` (`gemini-2.5-pro`): Strategically routes
   multi-constraint routine synthesis (AM vs. PM split, alternating retinoid /
   acid nights, barrier recovery nights) to Gemini Pro for higher reasoning accuracy.
"""

from __future__ import annotations

from google.adk.agents import Agent, SequentialAgent
from google.adk.tools.agent_tool import AgentTool
from google.genai import types as genai_types

from dermguard.config import SETTINGS


def create_biochemical_verifier_agent() -> Agent:
    """Builds the fast Gemini Flash conflict-summarization stage."""
    return Agent(
        name="biochemical_conflict_verifier_agent",
        model=SETTINGS.primary_model,
        description="Extracts pH, redox, and keratolytic constraints from routine safety data.",
        instruction=(
            "You are a cosmetic formulation chemist. Given a list of skincare products, "
            "resolved active ingredients, pairwise conflicts, and user sensitivities, "
            "produce a concise bulleted summary of: (1) AM-preferred antioxidants/SPF, "
            "(2) PM-only photosensitizing actives, (3) mutually incompatible pairs that "
            "must never be layered in the same session, and (4) barrier-supportive buffers."
        ),
        generate_content_config=genai_types.GenerateContentConfig(temperature=0.1),
        output_key="verified_biochemical_constraints",
    )


def create_am_pm_architect_agent() -> Agent:
    """Builds the Gemini Pro reasoning stage for conflict-free AM/PM scheduling."""
    return Agent(
        name="am_pm_schedule_architect_agent",
        model=SETTINGS.planner_model,
        description="Synthesizes a conflict-free weekly AM/PM cosmetic skincare schedule.",
        instruction=(
            "You are a senior cosmetic routine architect. Using the verified constraints:\n"
            "{verified_biochemical_constraints}\n\n"
            "Design a clear, conflict-free AM and PM weekly rotation (e.g., skin cycling "
            "or alternating nights) that eliminates every pairwise chemical conflict and "
            "prevents cumulative over-exfoliation. Always include daily morning broad-spectrum "
            "SPF 30+ and remind the user that recommendations are cosmetic only, not medical advice."
        ),
        generate_content_config=genai_types.GenerateContentConfig(temperature=0.2),
        output_key="am_pm_conflict_free_plan",
    )


def create_am_pm_routine_resolution_pipeline() -> SequentialAgent:
    """Factory creating a fresh `SequentialAgent` for multi-active schedule planning."""
    return SequentialAgent(
        name="am_pm_routine_resolution_pipeline",
        description=(
            "Delegates complex multi-product routines (3+ actives or multiple conflicts) "
            "to a sequential Flash-verifier + Pro-architect pipeline to generate a "
            "conflict-free AM/PM weekly rotation."
        ),
        sub_agents=[
            create_biochemical_verifier_agent(),
            create_am_pm_architect_agent(),
        ],
    )


def create_am_pm_planner_tool() -> AgentTool:
    """Wraps the sequential AM/PM conflict-resolution pipeline as an `AgentTool`."""
    return AgentTool(agent=create_am_pm_routine_resolution_pipeline())

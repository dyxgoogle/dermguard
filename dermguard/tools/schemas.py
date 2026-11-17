"""Explicit Pydantic JSON schemas for DermGuard tool inputs and outputs.

Enforces strict type, enum, and structural validation across ADK FunctionTools,
FastMCP safety endpoints, and FastAPI HTTP payloads, while providing structured
guided error payloads when arguments fail validation.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RoutineStateInput(BaseModel):
    """Strict input schema for `manage_routine_state`."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["add", "remove", "list", "clear"] = Field(
        description="Operation to perform on the session's active skincare routine."
    )
    product_name: str | None = Field(
        default=None,
        description="Full brand and product name (required when action is 'add' or 'remove').",
    )
    ingredients: list[str] | None = Field(
        default=None,
        description="Normalized INCI or active ingredient strings resolved for the product.",
    )


class RoutineStateOutput(BaseModel):
    """Structured output schema returned by `manage_routine_state`."""

    status: Literal["success", "error"]
    action: str
    total_products: int = Field(ge=0)
    active_routine_products: dict[str, list[str]]
    message: str
    recovery_instructions: str | None = None


class UserSensitivityInput(BaseModel):
    """Strict input schema for `manage_user_sensitivities`."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["get", "add", "remove"] = Field(
        description="Whether to read ('get'), append ('add'), or delete ('remove') a sensitivity."
    )
    user_id: str = Field(
        min_length=1,
        max_length=128,
        description="Stable identifier for the user across sessions.",
    )
    sensitivity: str | None = Field(
        default=None,
        description="Normalized sensitivity tag such as 'sensitive_skin', 'rosacea', 'pregnant', 'eczema', or 'fragrance_allergy'.",
    )


class UserSensitivityOutput(BaseModel):
    """Structured output schema returned by `manage_user_sensitivities`."""

    status: Literal["success", "error"]
    user_id: str
    sensitivities: list[str]
    updated_at: str
    message: str
    recovery_instructions: str | None = None


class PairwiseConflictFinding(BaseModel):
    """Represents a deterministic chemical or barrier conflict between two actives."""

    conflict_type: Literal["PAIRWISE_CONFLICT"] = "PAIRWISE_CONFLICT"
    ingredient_a: str
    ingredient_b: str
    products_involved: list[str]
    severity: Literal["MODERATE", "HIGH", "CRITICAL"]
    mechanism: str
    resolution: str


class CumulativeStackingWarning(BaseModel):
    """Represents multi-product stacking of the same active ingredient."""

    warning_type: Literal["CUMULATIVE_STACKING_CAUTION"] = "CUMULATIVE_STACKING_CAUTION"
    ingredient: str
    display_name: str
    product_count: int = Field(ge=2)
    products: list[str]
    guidance: str


class CautionFlag(BaseModel):
    """Represents high-dose, photosensitivity, pregnancy, or sensitivity cautions."""

    caution_type: Literal[
        "HIGH_DOSE_CAUTION",
        "PHOTOSENSITIVITY_SPF_REQUIRED",
        "PREGNANCY_CONTRAINDICATION",
        "SENSITIVITY_CONTRAINDICATION",
    ]
    ingredient: str
    display_name: str
    product_name: str
    detected_concentration_pct: float | None = None
    safe_max_concentration_pct: float
    details: str
    recommended_action: str


class RoutineSafetyAnalysisOutput(BaseModel):
    """Comprehensive deterministic output schema for `analyze_routine_safety`."""

    status: Literal["success", "error"]
    overall_risk_level: Literal["LOW", "MODERATE", "HIGH", "CRITICAL"]
    evaluated_product_count: int
    resolved_actives_by_product: dict[str, list[str]]
    pairwise_conflicts: list[PairwiseConflictFinding]
    cumulative_stacking_warnings: list[CumulativeStackingWarning]
    caution_flags: list[CautionFlag]
    requires_daily_spf: bool
    summary: str
    recovery_instructions: str | None = None


class ChatRequest(BaseModel):
    """Strict HTTP request payload for `/chat`."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1, max_length=128, description="Unique user identifier.")
    session_id: str = Field(min_length=1, max_length=128, description="Conversational session ID.")
    message: str = Field(min_length=1, max_length=8000, description="User skincare query.")


class ChatResponse(BaseModel):
    """Strict HTTP response payload for `/chat`."""

    user_id: str
    session_id: str
    reply: str
    active_routine_products: dict[str, list[str]]
    trace_id: str | None = None

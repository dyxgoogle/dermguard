"""Session-scoped skincare routine state management and HITL confirmation tools.

Maintains `tool_context.state["active_routine_products"]` so that whenever a
user incrementally introduces or removes a product mid-conversation, the agent
retains and re-evaluates the complete combined routine.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from dermguard.observability.pii_redactor import redact_pii_text
from dermguard.tools.schemas import RoutineStateInput, RoutineStateOutput

ROUTINE_STATE_KEY = "active_routine_products"


def manage_routine_state(
    action: str,
    product_name: str | None = None,
    ingredients: list[str] | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Updates or inspects the user's active multi-product skincare routine in session state.

    Use this tool whenever the user mentions a product they use or asks about
    adding/removing a product so all products can be cross-evaluated together.

    Args:
        action: Operation to execute; must be one of `'add'`, `'remove'`,
            `'list'`, or `'clear'`.
        product_name: Exact brand and product title (required for `'add'` and
            `'remove'`; leave None or empty for `'list'` or `'clear'`).
        ingredients: List of INCI or key active ingredient strings resolved for
            `product_name` when `action` is `'add'`.

    Returns:
        Dictionary conforming to `RoutineStateOutput` with `status`,
        `active_routine_products`, `total_products`, `message`, and optional
        `recovery_instructions` if arguments are invalid.
    """
    normalized_action = (action or "").strip().lower()
    clean_product = redact_pii_text((product_name or "").strip()) or None
    clean_ingredients = (
        [redact_pii_text(item.strip()) for item in ingredients if isinstance(item, str) and item.strip()]
        if ingredients is not None
        else None
    )

    try:
        validated = RoutineStateInput(
            action=normalized_action,  # type: ignore[arg-type]
            product_name=clean_product,
            ingredients=clean_ingredients,
        )
    except ValidationError:
        return RoutineStateOutput(
            status="error",
            action=normalized_action or "unknown",
            total_products=0,
            active_routine_products={},
            message=f"Invalid routine state action '{action}'.",
            recovery_instructions=(
                "Call `manage_routine_state` with `action` set to strictly one of: "
                "'add', 'remove', 'list', or 'clear'. Include `product_name` and "
                "`ingredients` when `action='add'`."
            ),
        ).model_dump()

    state_store = tool_context.state if tool_context is not None else {}
    current_routine: dict[str, list[str]] = dict(state_store.get(ROUTINE_STATE_KEY, {}))

    if validated.action == "list":
        return RoutineStateOutput(
            status="success",
            action="list",
            total_products=len(current_routine),
            active_routine_products=current_routine,
            message=f"Retrieved {len(current_routine)} active product(s) from session state.",
        ).model_dump()

    if validated.action == "clear":
        current_routine = {}
        state_store[ROUTINE_STATE_KEY] = current_routine
        return RoutineStateOutput(
            status="success",
            action="clear",
            total_products=0,
            active_routine_products={},
            message="Cleared all products from the current session routine.",
        ).model_dump()

    if not validated.product_name:
        return RoutineStateOutput(
            status="error",
            action=validated.action,
            total_products=len(current_routine),
            active_routine_products=current_routine,
            message=f"`product_name` is required when action is '{validated.action}'.",
            recovery_instructions=(
                "Retry `manage_routine_state` and pass a non-empty `product_name` string "
                "(e.g., 'CeraVe Resurfacing Retinol Serum')."
            ),
        ).model_dump()

    if validated.action == "add":
        if not validated.ingredients:
            return RoutineStateOutput(
                status="error",
                action="add",
                total_products=len(current_routine),
                active_routine_products=current_routine,
                message=f"Cannot add '{validated.product_name}' without an `ingredients` list.",
                recovery_instructions=(
                    "First search for the product's official INCI ingredient list using "
                    "`google_search`, then call `manage_routine_state(action='add', "
                    "product_name=..., ingredients=[...])`."
                ),
            ).model_dump()

        current_routine[validated.product_name] = validated.ingredients
        state_store[ROUTINE_STATE_KEY] = current_routine
        return RoutineStateOutput(
            status="success",
            action="add",
            total_products=len(current_routine),
            active_routine_products=current_routine,
            message=(
                f"Added '{validated.product_name}' ({len(validated.ingredients)} ingredients). "
                f"Active routine now has {len(current_routine)} product(s); run `analyze_routine_safety` "
                "on the full `active_routine_products` dictionary."
            ),
        ).model_dump()

    # action == "remove"
    removed = current_routine.pop(validated.product_name, None)
    state_store[ROUTINE_STATE_KEY] = current_routine
    if removed is None:
        return RoutineStateOutput(
            status="error",
            action="remove",
            total_products=len(current_routine),
            active_routine_products=current_routine,
            message=f"Product '{validated.product_name}' was not found in the active routine.",
            recovery_instructions=(
                "Call `manage_routine_state(action='list')` to view exact stored product names "
                "before attempting removal."
            ),
        ).model_dump()

    return RoutineStateOutput(
        status="success",
        action="remove",
        total_products=len(current_routine),
        active_routine_products=current_routine,
        message=f"Removed '{validated.product_name}' from the active routine.",
    ).model_dump()


def clear_entire_skincare_routine(
    reason: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Permanently removes every product from the user's active skincare routine after human confirmation.

    Invoke this tool only when the user explicitly requests wiping their entire
    multi-product skincare routine to start fresh.

    Args:
        reason: Brief explanation of why the entire skincare routine is being reset.

    Returns:
        Dictionary confirming routine reset with `status` and empty routine map.
    """
    state_store = tool_context.state if tool_context is not None else {}
    previous_count = len(state_store.get(ROUTINE_STATE_KEY, {}))
    state_store[ROUTINE_STATE_KEY] = {}
    return {
        "status": "success",
        "cleared_product_count": previous_count,
        "reason": redact_pii_text(reason),
        "active_routine_products": {},
        "message": f"Confirmed human-in-the-loop reset of {previous_count} routine product(s).",
    }


manage_routine_state_tool = FunctionTool(func=manage_routine_state)
clear_entire_skincare_routine_tool = FunctionTool(
    func=clear_entire_skincare_routine,
    require_confirmation=True,
)

"""Persistent cross-session user sensitivity memory with async consolidation.

Stores user-specific skin sensitivities (e.g., `sensitive_skin`, `rosacea`,
`pregnant`, `eczema`, `fragrance_allergy`, `retinoid_intolerance`) across
conversational sessions in both JSON (`data/user_sensitivities.json`) and a
indexed SQLite table (`data/user_memory.db`), while synchronizing with ADK's
`user:sensitivities` state prefix and scrubbing PII before persistence.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from google.adk.agents.callback_context import CallbackContext
from google.adk.tools.function_tool import FunctionTool
from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from dermguard.config import PROJECT_ROOT
from dermguard.observability.pii_redactor import redact_pii_text
from dermguard.observability.structured_logger import log_agent_event
from dermguard.tools.schemas import UserSensitivityInput, UserSensitivityOutput

_WRITE_LOCK = asyncio.Lock()
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _resolve_json_path() -> Path:
    return Path(
        os.getenv(
            "DERMGUARD_SENSITIVITY_FILE",
            str(PROJECT_ROOT / "data" / "user_sensitivities.json"),
        )
    )


def _resolve_sqlite_path() -> Path:
    return Path(
        os.getenv(
            "DERMGUARD_SENSITIVITY_SQLITE",
            str(PROJECT_ROOT / "data" / "user_memory.db"),
        )
    )


def _sync_sqlite_upsert(sqlite_path: Path, user_id: str, sensitivities: list[str], updated_at: str) -> None:
    """Writes user sensitivity profile into persistent SQLite storage."""
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(sqlite_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_sensitivities (
                user_id TEXT PRIMARY KEY,
                sensitivities_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO user_sensitivities (user_id, sensitivities_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                sensitivities_json = excluded.sensitivities_json,
                updated_at = excluded.updated_at
            """,
            (user_id, json.dumps(sensitivities), updated_at),
        )
        conn.commit()


def _read_json_store(json_path: Path) -> dict[str, dict[str, Any]]:
    if not json_path.exists():
        return {}
    try:
        return json.loads(json_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_json_store(json_path: Path, store: dict[str, dict[str, Any]]) -> None:
    json_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = json_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(store, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp_path.replace(json_path)


def _schedule_async_sqlite_mirror(
    sqlite_path: Path, user_id: str, sensitivities: list[str], updated_at: str
) -> None:
    """Schedules non-blocking background SQLite consolidation without blocking the turn."""

    async def _worker() -> None:
        await asyncio.to_thread(_sync_sqlite_upsert, sqlite_path, user_id, sensitivities, updated_at)

    task = asyncio.create_task(_worker())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


async def manage_user_sensitivities(
    action: str,
    user_id: str,
    sensitivity: str | None = None,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Reads, adds, or removes persistent skin sensitivities and contraindications for a user across sessions.

    Always call with `action='get'` at the beginning of a safety evaluation to
    load existing sensitivities (e.g., `'sensitive_skin'`, `'rosacea'`,
    `'pregnant'`, `'eczema'`, `'fragrance_allergy'`, `'retinoid_intolerance'`),
    and call with `action='add'` whenever the user shares a new skin condition
    or sensitivity.

    Args:
        action: Memory operation to perform; must be `'get'`, `'add'`, or `'remove'`.
        user_id: Unique identifier of the user (use `'default_user'` if unspecified).
        sensitivity: Normalized sensitivity label when `action` is `'add'` or
            `'remove'` (omit or pass empty string for `'get'`).

    Returns:
        Dictionary conforming to `UserSensitivityOutput` containing `status`,
        `user_id`, `sensitivities`, `updated_at`, and optional
        `recovery_instructions` if validation fails.
    """
    clean_action = (action or "").strip().lower()
    clean_user = redact_pii_text((user_id or "").strip())
    raw_sens = redact_pii_text((sensitivity or "").strip()).lower()
    clean_sens = raw_sens.replace(" ", "_") if raw_sens else None

    try:
        validated = UserSensitivityInput(
            action=clean_action,  # type: ignore[arg-type]
            user_id=clean_user,
            sensitivity=clean_sens,
        )
    except ValidationError:
        return UserSensitivityOutput(
            status="error",
            user_id=clean_user or "unknown",
            sensitivities=[],
            updated_at=datetime.now(UTC).isoformat(),
            message=f"Invalid sensitivity parameters (action='{action}', user_id='{clean_user}').",
            recovery_instructions=(
                "Call `manage_user_sensitivities` with `action` in ['get', 'add', 'remove'] "
                "and a non-empty `user_id` string."
            ),
        ).model_dump()

    json_path = _resolve_json_path()
    sqlite_path = _resolve_sqlite_path()

    async with _WRITE_LOCK:
        store = await asyncio.to_thread(_read_json_store, json_path)
        record = store.get(
            validated.user_id,
            {"user_id": validated.user_id, "sensitivities": [], "updated_at": datetime.now(UTC).isoformat()},
        )
        sens_list: list[str] = list(record.get("sensitivities", []))

        if validated.action == "get":
            if tool_context is not None:
                tool_context.state["user:sensitivities"] = sens_list
            return UserSensitivityOutput(
                status="success",
                user_id=validated.user_id,
                sensitivities=sens_list,
                updated_at=record.get("updated_at", datetime.now(UTC).isoformat()),
                message=f"Loaded {len(sens_list)} persistent sensitivity tag(s) for '{validated.user_id}'.",
            ).model_dump()

        if not validated.sensitivity:
            return UserSensitivityOutput(
                status="error",
                user_id=validated.user_id,
                sensitivities=sens_list,
                updated_at=record.get("updated_at", datetime.now(UTC).isoformat()),
                message=f"`sensitivity` cannot be empty when action is '{validated.action}'.",
                recovery_instructions=(
                    "Pass a descriptive `sensitivity` string such as 'sensitive_skin', "
                    "'rosacea', 'pregnant', 'eczema', or 'fragrance_allergy'."
                ),
            ).model_dump()

        now_iso = datetime.now(UTC).isoformat()
        if validated.action == "add":
            if validated.sensitivity not in sens_list:
                sens_list.append(validated.sensitivity)
            msg = f"Saved sensitivity '{validated.sensitivity}' to persistent user memory."
        else:
            if validated.sensitivity in sens_list:
                sens_list.remove(validated.sensitivity)
                msg = f"Removed sensitivity '{validated.sensitivity}' from persistent user memory."
            else:
                return UserSensitivityOutput(
                    status="error",
                    user_id=validated.user_id,
                    sensitivities=sens_list,
                    updated_at=record.get("updated_at", now_iso),
                    message=f"Sensitivity '{validated.sensitivity}' was not present in profile.",
                    recovery_instructions="Call `manage_user_sensitivities(action='get', ...)` to inspect stored tags first.",
                ).model_dump()

        updated_record = {
            "user_id": validated.user_id,
            "sensitivities": sens_list,
            "updated_at": now_iso,
        }
        store[validated.user_id] = updated_record
        await asyncio.to_thread(_write_json_store, json_path, store)
        _schedule_async_sqlite_mirror(sqlite_path, validated.user_id, sens_list, now_iso)

        if tool_context is not None:
            tool_context.state["user:sensitivities"] = sens_list

        return UserSensitivityOutput(
            status="success",
            user_id=validated.user_id,
            sensitivities=sens_list,
            updated_at=now_iso,
            message=msg,
        ).model_dump()


async def reset_user_sensitivity_memory(
    user_id: str,
    tool_context: ToolContext | None = None,
) -> dict[str, Any]:
    """Permanently deletes a user's saved cross-session sensitivity profile after human confirmation.

    Invoke only when the user explicitly asks to erase all stored skin
    sensitivities and allergy records from persistent memory.

    Args:
        user_id: Unique user identifier whose persistent sensitivity profile will be erased.

    Returns:
        Confirmation dictionary reporting profile deletion status.
    """
    clean_user = redact_pii_text((user_id or "").strip())
    if not clean_user:
        return {
            "status": "error",
            "message": "`user_id` is required to reset persistent sensitivity memory.",
            "recovery_instructions": "Provide the target `user_id` string and retry.",
        }

    json_path = _resolve_json_path()
    sqlite_path = _resolve_sqlite_path()
    now_iso = datetime.now(UTC).isoformat()

    async with _WRITE_LOCK:
        store = await asyncio.to_thread(_read_json_store, json_path)
        store.pop(clean_user, None)
        await asyncio.to_thread(_write_json_store, json_path, store)
        _schedule_async_sqlite_mirror(sqlite_path, clean_user, [], now_iso)

    if tool_context is not None:
        tool_context.state["user:sensitivities"] = []

    return {
        "status": "success",
        "user_id": clean_user,
        "sensitivities": [],
        "message": f"Confirmed human-in-the-loop deletion of persistent sensitivities for '{clean_user}'.",
    }


async def async_consolidate_session_memory(callback_context: CallbackContext) -> None:
    """Schedules non-blocking background ADK MemoryService consolidation after an agent turn.

    Args:
        callback_context: ADK `CallbackContext` for the completed turn.

    Returns:
        Always `None` so normal agent response delivery is never blocked.
    """

    async def _consolidate() -> None:
        try:
            add_fn = getattr(callback_context, "add_session_to_memory", None)
            if callable(add_fn):
                await add_fn()
            log_agent_event(
                event_phase="OUTCOME",
                action_name="async_memory_consolidation",
                details={"agent_name": callback_context.agent_name, "status": "consolidated"},
            )
        except Exception as exc:
            log_agent_event(
                event_phase="SYSTEM",
                action_name="async_memory_consolidation",
                details={"status": "skipped", "reason": str(exc)},
            )

    task = asyncio.create_task(_consolidate())
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
    return None


manage_user_sensitivities_tool = FunctionTool(func=manage_user_sensitivities)
reset_user_sensitivity_memory_tool = FunctionTool(
    func=reset_user_sensitivity_memory,
    require_confirmation=True,
)

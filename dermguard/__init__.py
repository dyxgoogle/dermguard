"""DermGuard ADK package entry point for `adk web`, `adk run`, and FastAPI."""

from dermguard import agent
from dermguard.agent import app, dermguard_app, root_agent

__all__ = ["agent", "app", "dermguard_app", "root_agent"]

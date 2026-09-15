"""
Agent tool registry primitives.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.services.agent_context import ToolUseContext


@dataclass(frozen=True)
class AgentTool:
    name: str
    title: str
    permission: str
    runner: Callable[[ToolUseContext], Any]
    description: str = ""
    toolset: str = "read"
    default_enabled: bool = True

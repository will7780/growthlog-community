"""
Agent permission checks.
"""
from __future__ import annotations

from app.services.agent_tool_registry import AgentTool


class PermissionGate:
    """Minimal permission gate. Current Agent mode is read-only."""

    allowed_permissions = {"read"}

    @classmethod
    def assert_allowed(cls, tool: AgentTool) -> None:
        if tool.permission not in cls.allowed_permissions:
            raise PermissionError(f"工具 {tool.name} 需要 {tool.permission} 权限，当前 Agent 未授权")

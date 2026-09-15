"""
Central constants and helpers for AI session personas and statuses.
"""
from __future__ import annotations

from typing import FrozenSet

PERSONA_RETRIEVER = "retriever"
PERSONA_EXPLAINER = "explainer"
PERSONA_ORGANIZER = "organizer"

VALID_PERSONAS: FrozenSet[str] = frozenset(
    {PERSONA_RETRIEVER, PERSONA_EXPLAINER, PERSONA_ORGANIZER}
)

SESSION_STATUS_ACTIVE = "active"
SESSION_STATUS_ARCHIVED = "archived"

VALID_SESSION_STATUSES: FrozenSet[str] = frozenset(
    {SESSION_STATUS_ACTIVE, SESSION_STATUS_ARCHIVED}
)

# Map chat route mode strings to persona values for compatibility checks.
MODE_TO_PERSONA = {
    "retrieval": PERSONA_RETRIEVER,
    "retriever": PERSONA_RETRIEVER,
    "explainer": PERSONA_EXPLAINER,
    "organize": PERSONA_ORGANIZER,
    "organizer": PERSONA_ORGANIZER,
    "query": PERSONA_RETRIEVER,
    "hermes": PERSONA_RETRIEVER,
}


class SessionPersonaError(Exception):
    """Stable business error for session persona contracts."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def normalize_persona(raw: str) -> str:
    value = (raw or "").strip().lower()
    if value not in VALID_PERSONAS:
        raise SessionPersonaError(
            "SESSION_PERSONA_INVALID",
            "会话角色无效，请选择检索员、讲解员或整理师。",
        )
    return value


def persona_for_mode(mode: str) -> str | None:
    return MODE_TO_PERSONA.get((mode or "").strip().lower())


def assert_mode_matches_persona(mode: str, persona: str) -> None:
    expected = persona_for_mode(mode)
    if expected is None or expected != persona:
        raise SessionPersonaError(
            "SESSION_PERSONA_MISMATCH",
            "当前请求与会话角色不匹配，请新建对应角色的对话。",
        )

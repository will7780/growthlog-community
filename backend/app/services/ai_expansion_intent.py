"""Validation helpers for explicit explainer source expansion."""
from __future__ import annotations

from typing import Optional

# ACTION_ANSWER remains as a compatibility value for historical test helpers.
# Runtime explainer chat no longer classifies messages into expansion actions.
ACTION_ANSWER = "answer_with_current_sources"
ACTION_EXPAND = "propose_source_expansion"

EXPANSION_TOPIC_MIN = 2
EXPANSION_TOPIC_MAX = 200


class ExpansionIntentError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def normalize_expansion_topic(raw: Optional[str]) -> str:
    return (raw or "").strip()


def validate_expansion_topic(raw: Optional[str]) -> str:
    topic = normalize_expansion_topic(raw)
    if len(topic) < EXPANSION_TOPIC_MIN or len(topic) > EXPANSION_TOPIC_MAX:
        raise ExpansionIntentError(
            "SOURCE_EXPANSION_QUERY_REQUIRED",
            "请先输入要补充的主题（2～200 字）。",
        )
    return topic


def derive_expansion_topic_from_user_content(
    user_content: str,
    *,
    phase: Optional[str] = None,
) -> str:
    """Recover the exact topic bound to an explicit pending expansion."""
    if phase != "awaiting_expansion_confirm":
        return ""
    topic = normalize_expansion_topic(user_content)
    if EXPANSION_TOPIC_MIN <= len(topic) <= EXPANSION_TOPIC_MAX:
        return topic
    return ""

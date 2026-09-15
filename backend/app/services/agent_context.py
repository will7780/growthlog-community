"""
Agent context structures.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class ToolUseContext:
    db: Session
    user_id: int
    session_id: str
    query: str
    conversation_history: List[Dict]
    conversation_summary: Optional[Dict]
    memories: List[Dict]

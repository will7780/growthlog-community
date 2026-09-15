"""
Agent context builder.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from app.services.agent_compaction import load_conversation_summary
from app.services.agent_context import ToolUseContext
from app.services.agent_memory_embeddings import (
    merge_vector_and_recent_memories,
    search_agent_memories_vector,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


def _get_agent_memory_model():
    from app.models import AgentMemory

    return AgentMemory


def _recent_agent_memories(db: "Session", user_id: int, limit: int = 12) -> List[Dict]:
    AgentMemory = _get_agent_memory_model()
    rows = (
        db.query(AgentMemory)
        .filter(AgentMemory.user_id == user_id, AgentMemory.is_active.is_(True))
        .order_by(AgentMemory.updated_at.desc(), AgentMemory.created_at.desc())
        .limit(limit)
        .all()
    )
    return [
        {
            "id": int(row.id),
            "type": row.memory_type,
            "content": row.content,
            "source": row.source,
            "confidence": float(row.confidence or 0.0),
            "retrieval_method": "memory_recent",
        }
        for row in rows
    ]


def load_agent_memories(
    db: "Session",
    user_id: int,
    limit: int = 12,
    *,
    query: Optional[str] = None,
    query_vector: Optional[Sequence[float]] = None,
) -> List[Dict]:
    """
    Load active memories for Agent context.

    Prefer vector recall when query / reused query_vector is provided and
    embeddings are available; otherwise fall back to recent memories.
    Never raises for missing table/model. Never regenerates query embedding
    when query_vector is supplied.
    """
    query_text = (query or "").strip()
    if query_text or query_vector is not None:
        vector_hits = search_agent_memories_vector(
            db,
            user_id,
            query_text or "",
            top_k=limit,
            query_vector=query_vector,
        )
        if vector_hits is not None:
            if len(vector_hits) >= limit:
                return vector_hits[:limit]
            recent = _recent_agent_memories(db, user_id, limit=limit * 2)
            return merge_vector_and_recent_memories(vector_hits, recent, limit=limit)

    return _recent_agent_memories(db, user_id, limit=limit)


def build_tool_use_context(
    db: "Session",
    *,
    user_id: int,
    session_id: str,
    query: str,
    conversation_history: Optional[List[Dict]] = None,
) -> ToolUseContext:
    return ToolUseContext(
        db=db,
        user_id=user_id,
        session_id=session_id,
        query=query,
        conversation_history=conversation_history or [],
        conversation_summary=load_conversation_summary(db, user_id, session_id),
        memories=load_agent_memories(db, user_id, query=query),
    )

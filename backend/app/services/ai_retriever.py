"""
Retriever persona helpers (R11.2-Fix).

Each user question is an independent retrieval turn via shared retrieval service.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.ai_retrieval_service import run_shared_retrieval_turn


async def retrieve_for_retriever_turn(
    db: Session,
    *,
    user_id: int,
    query: str,
    intent_hint: Optional[str] = None,
    label_code: Optional[str] = None,
    top_k: int = 30,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Run shared retrieval using only the current-turn raw query."""
    return await run_shared_retrieval_turn(
        db,
        user_id=user_id,
        query=query,
        intent_hint=intent_hint,
        label_code=label_code,
        top_k=top_k,
        model_key=model_key,
        fallback_candidates=fallback_candidates,
        generate_answer=True,
        answer_style="retriever",
    )


def retriever_conversation_history_for_generation(
    _history: Optional[List[Dict]] = None,
) -> List[Dict]:
    """Retriever answers must not inherit prior-turn semantics."""
    return []

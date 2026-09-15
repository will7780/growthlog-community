"""
AI session history embedding index and vector search.

Vectors are stored in ai_conversation_embeddings only; they must not be written
to entries/embeddings/knowledge_embeddings/attachment_embeddings.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 8
DEFAULT_MIN_SCORE = 0.25
# Keep in sync with app.services.embedding.MODEL_NAME
MODEL_NAME = "intfloat/multilingual-e5-base"


def hash_conversation_content(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def _get_conversation_models():
    from app.models import AIConversation, AIConversationEmbedding

    return AIConversation, AIConversationEmbedding


def generate_embedding(text: str) -> List[float]:
    from app.services.embedding import generate_embedding as _generate_embedding

    return _generate_embedding(text)


def cosine_similarity(vec1: Sequence[float], vec2: Sequence[float]) -> float:
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot = 0.0
    n1 = 0.0
    n2 = 0.0
    for a, b in zip(vec1, vec2):
        fa = float(a)
        fb = float(b)
        dot += fa * fb
        n1 += fa * fa
        n2 += fb * fb
    if n1 == 0.0 or n2 == 0.0:
        return 0.0
    return float(dot / (math.sqrt(n1) * math.sqrt(n2)))


def has_ai_conversation_embeddings_table(db: "Session") -> bool:
    try:
        from sqlalchemy import inspect

        bind = db.get_bind()
        if bind is None:
            return False
        return bool(inspect(bind).has_table("ai_conversation_embeddings"))
    except Exception as exc:
        logger.debug("ai_conversation_embeddings table check failed: %s", exc)
        return False


def _parse_vector(raw: object) -> Optional[List[float]]:
    if raw is None:
        return None
    try:
        if isinstance(raw, list):
            vector = [float(item) for item in raw]
        elif isinstance(raw, str):
            vector = [float(item) for item in json.loads(raw)]
        else:
            return None
        if not vector:
            return None
        return vector
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def sync_ai_conversation_embedding(db: "Session", conversation: Any) -> str:
    """
    Generate or update embedding for one conversation message.

    Returns: indexed | skipped | failed
    """
    if not has_ai_conversation_embeddings_table(db):
        return "skipped"

    content = (conversation.content or "").strip()
    if not content:
        return "skipped"

    try:
        _, AIConversationEmbedding = _get_conversation_models()

        content_hash = hash_conversation_content(content)
        existing = (
            db.query(AIConversationEmbedding)
            .filter(
                AIConversationEmbedding.conversation_id == conversation.id,
                AIConversationEmbedding.embedding_model == MODEL_NAME,
            )
            .first()
        )
        if existing and existing.content_hash == content_hash:
            return "skipped"

        vector = generate_embedding(f"passage: {content}")
        if existing:
            existing.user_id = conversation.user_id
            existing.session_id = conversation.session_id
            existing.role = conversation.role
            existing.content_hash = content_hash
            existing.vector = vector
        else:
            db.add(AIConversationEmbedding(
                conversation_id=int(conversation.id),
                user_id=int(conversation.user_id),
                session_id=conversation.session_id,
                role=conversation.role,
                content_hash=content_hash,
                embedding_model=MODEL_NAME,
                vector=vector,
            ))
        db.commit()
        return "indexed"
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            logger.debug(
                "rollback after sync_ai_conversation_embedding failure skipped conversation_id=%s",
                conversation.id,
                exc_info=True,
            )
        logger.warning(
            "sync_ai_conversation_embedding failed conversation_id=%s error=%s",
            conversation.id,
            exc,
        )
        return "failed"


def backfill_ai_conversation_embeddings(
    db: "Session",
    *,
    user_id: Optional[int] = None,
    limit: int = 200,
) -> Dict[str, int]:
    stats = {"processed": 0, "indexed": 0, "skipped": 0, "failed": 0}
    if not has_ai_conversation_embeddings_table(db):
        logger.warning("ai_conversation_embeddings table missing; backfill skipped")
        stats["skipped"] = 1
        return stats

    try:
        AIConversation, AIConversationEmbedding = _get_conversation_models()
    except Exception as exc:
        logger.warning("ai conversation models unavailable: %s", exc)
        stats["failed"] = 1
        return stats

    query = db.query(AIConversation).order_by(AIConversation.created_at.desc())
    if user_id is not None:
        query = query.filter(AIConversation.user_id == user_id)

    rows = query.limit(max(limit * 3, limit)).all()
    for conversation in rows:
        if stats["processed"] >= limit:
            break

        processed_this_item = False
        try:
            content_hash = hash_conversation_content(conversation.content or "")
            existing = (
                db.query(AIConversationEmbedding)
                .filter(
                    AIConversationEmbedding.conversation_id == conversation.id,
                    AIConversationEmbedding.embedding_model == MODEL_NAME,
                )
                .first()
            )
            if existing and existing.content_hash == content_hash:
                continue

            stats["processed"] += 1
            processed_this_item = True
            outcome = sync_ai_conversation_embedding(db, conversation)
            stats[outcome] = stats.get(outcome, 0) + 1
        except Exception as exc:
            if not processed_this_item:
                stats["processed"] += 1
            stats["failed"] += 1
            try:
                db.rollback()
            except Exception:
                logger.debug(
                    "rollback after backfill conversation failure skipped conversation_id=%s",
                    conversation.id,
                    exc_info=True,
                )
            logger.warning(
                "backfill_ai_conversation_embeddings failed conversation_id=%s error=%s",
                conversation.id,
                exc,
            )
            continue

    return stats


def _search_ai_conversations_numpy(
    db: "Session",
    user_id: int,
    session_id: str,
    query_vector: List[float],
    *,
    top_k: int,
    min_score: float,
) -> Optional[List[Dict]]:
    try:
        AIConversation, AIConversationEmbedding = _get_conversation_models()

        rows = (
            db.query(AIConversation, AIConversationEmbedding)
            .join(
                AIConversationEmbedding,
                AIConversationEmbedding.conversation_id == AIConversation.id,
            )
            .filter(
                AIConversation.user_id == user_id,
                AIConversation.session_id != session_id,
                AIConversationEmbedding.embedding_model == MODEL_NAME,
            )
            .all()
        )
    except Exception as exc:
        logger.warning(
            "ai session vector search query failed user_id=%s error=%s",
            user_id,
            exc,
        )
        try:
            db.rollback()
        except Exception:
            logger.debug("rollback after vector search query failure skipped", exc_info=True)
        return None

    if not rows:
        return []

    scored: List[Tuple[float, Any]] = []
    for conversation, embedding_row in rows:
        vector = _parse_vector(embedding_row.vector)
        if vector is None:
            logger.warning(
                "invalid ai session embedding vector conversation_id=%s",
                conversation.id,
            )
            continue
        score = cosine_similarity(query_vector, vector)
        if score >= min_score:
            scored.append((score, conversation))

    if not scored:
        return []

    scored.sort(key=lambda item: (item[0], item[1].created_at or 0), reverse=True)
    return [
        {
            "message_id": int(conversation.id),
            "session_id": conversation.session_id,
            "role": conversation.role,
            "snippet": (conversation.content or "")[:360],
            "created_at": conversation.created_at.isoformat() if conversation.created_at else None,
            "retrieval_method": "vector_numpy",
            "relevance_score": float(score),
        }
        for score, conversation in scored[:top_k]
    ]


def search_ai_conversations_vector(
    db: "Session",
    user_id: int,
    session_id: str,
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> Optional[List[Dict]]:
    """
    Vector search over indexed AI conversation messages for the current user.

    Returns None on infrastructure failure (table missing, model/vector errors).
    Returns [] when no indexed rows or no matches above min_score.
    """
    query_text = (query or "").strip()
    if not query_text:
        return []

    if not has_ai_conversation_embeddings_table(db):
        logger.debug("ai_conversation_embeddings table missing; vector search skipped")
        return None

    try:
        query_vector = generate_embedding(f"query: {query_text}")
    except Exception as exc:
        logger.warning(
            "ai session vector query embedding failed user_id=%s error=%s",
            user_id,
            exc,
        )
        return None

    from app.services.ai_session_vector_index import search_ai_conversations_faiss

    faiss_hits = search_ai_conversations_faiss(
        db,
        user_id,
        session_id,
        query_vector,
        top_k=top_k,
        min_score=min_score,
    )
    if faiss_hits:
        return faiss_hits

    return _search_ai_conversations_numpy(
        db,
        user_id,
        session_id,
        query_vector,
        top_k=top_k,
        min_score=min_score,
    )

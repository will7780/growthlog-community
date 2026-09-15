"""
AI conversation summary embedding search.

Summary vectors are stored in ai_conversation_summary_embeddings only. They
augment ai_session_search before message-level vector/FULLTEXT/LIKE fallback.
Top-level imports stay stdlib-only for lightweight tests.
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
MODEL_NAME = "intfloat/multilingual-e5-base"


def _get_summary_models():
    from app.models import AIConversationSummary, AIConversationSummaryEmbedding

    return AIConversationSummary, AIConversationSummaryEmbedding


def generate_embedding(text: str) -> List[float]:
    from app.services.embedding import generate_embedding as _generate_embedding

    return _generate_embedding(text)


def hash_summary_content(summary: Any) -> str:
    text = build_summary_embedding_text(summary)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_summary_embedding_text(summary: Any) -> str:
    parts: List[str] = []
    plain = str(getattr(summary, "summary", "") or "").strip()
    if plain:
        parts.append(plain)
    structured = getattr(summary, "structured_json", None)
    if isinstance(structured, str):
        try:
            structured = json.loads(structured)
        except json.JSONDecodeError:
            structured = None
    if isinstance(structured, dict):
        for key in ("summary", "open_questions", "decisions", "user_preferences", "pending_actions"):
            value = structured.get(key)
            if isinstance(value, list):
                parts.extend(str(item).strip() for item in value if str(item).strip())
            elif isinstance(value, str) and value.strip():
                parts.append(value.strip())
    return "\n".join(parts).strip()


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


def has_ai_conversation_summary_embeddings_table(db: "Session") -> bool:
    try:
        from sqlalchemy import inspect

        bind = db.get_bind()
        if bind is None:
            return False
        return bool(inspect(bind).has_table("ai_conversation_summary_embeddings"))
    except Exception as exc:
        logger.debug("ai_conversation_summary_embeddings table check failed: %s", exc)
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
        return vector or None
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


def sync_ai_conversation_summary_embedding(db: "Session", summary: Any) -> str:
    """Generate or update embedding for one conversation summary."""
    if not has_ai_conversation_summary_embeddings_table(db):
        return "skipped"

    text = build_summary_embedding_text(summary)
    if not text:
        return "skipped"

    try:
        _, AIConversationSummaryEmbedding = _get_summary_models()
        summary_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        existing = (
            db.query(AIConversationSummaryEmbedding)
            .filter(
                AIConversationSummaryEmbedding.summary_id == summary.id,
                AIConversationSummaryEmbedding.embedding_model == MODEL_NAME,
            )
            .first()
        )
        if existing and existing.summary_hash == summary_hash:
            return "skipped"

        vector = generate_embedding(f"passage: {text}")
        if existing:
            existing.user_id = int(summary.user_id)
            existing.session_id = summary.session_id
            existing.summary_hash = summary_hash
            existing.vector = vector
        else:
            db.add(AIConversationSummaryEmbedding(
                summary_id=int(summary.id),
                user_id=int(summary.user_id),
                session_id=summary.session_id,
                summary_hash=summary_hash,
                embedding_model=MODEL_NAME,
                vector=vector,
            ))
        db.commit()
        return "indexed"
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            logger.debug("rollback after summary embedding failure skipped", exc_info=True)
        logger.warning(
            "sync_ai_conversation_summary_embedding failed summary_id=%s error=%s",
            getattr(summary, "id", None),
            exc,
        )
        return "failed"


def backfill_ai_conversation_summary_embeddings(
    db: "Session",
    *,
    user_id: Optional[int] = None,
    limit: int = 200,
) -> Dict[str, int]:
    stats = {"processed": 0, "indexed": 0, "skipped": 0, "failed": 0}
    if not has_ai_conversation_summary_embeddings_table(db):
        stats["skipped"] = 1
        return stats

    try:
        AIConversationSummary, AIConversationSummaryEmbedding = _get_summary_models()
    except Exception as exc:
        logger.warning("ai conversation summary models unavailable: %s", exc)
        stats["failed"] = 1
        return stats

    query = db.query(AIConversationSummary).order_by(AIConversationSummary.updated_at.desc())
    if user_id is not None:
        query = query.filter(AIConversationSummary.user_id == user_id)

    rows = query.limit(max(limit * 3, limit)).all()
    for summary in rows:
        if stats["processed"] >= limit:
            break
        try:
            text = build_summary_embedding_text(summary)
            if not text:
                continue
            summary_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
            existing = (
                db.query(AIConversationSummaryEmbedding)
                .filter(
                    AIConversationSummaryEmbedding.summary_id == summary.id,
                    AIConversationSummaryEmbedding.embedding_model == MODEL_NAME,
                )
                .first()
            )
            if existing and existing.summary_hash == summary_hash:
                continue

            stats["processed"] += 1
            outcome = sync_ai_conversation_summary_embedding(db, summary)
            stats[outcome] = stats.get(outcome, 0) + 1
        except Exception as exc:
            stats["processed"] += 1
            stats["failed"] += 1
            try:
                db.rollback()
            except Exception:
                logger.debug("rollback after summary backfill failure skipped", exc_info=True)
            logger.warning(
                "backfill_ai_conversation_summary_embeddings failed summary_id=%s error=%s",
                getattr(summary, "id", None),
                exc,
            )
    return stats


def search_ai_conversation_summaries_vector(
    db: "Session",
    user_id: int,
    session_id: str,
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
) -> Optional[List[Dict]]:
    """Search compressed conversation summaries for the current user."""
    query_text = (query or "").strip()
    if not query_text:
        return []
    if not has_ai_conversation_summary_embeddings_table(db):
        return None

    try:
        query_vector = generate_embedding(f"query: {query_text}")
    except Exception as exc:
        logger.warning("summary vector query embedding failed user_id=%s error=%s", user_id, exc)
        return None

    try:
        AIConversationSummary, AIConversationSummaryEmbedding = _get_summary_models()
        rows = (
            db.query(AIConversationSummary, AIConversationSummaryEmbedding)
            .join(
                AIConversationSummaryEmbedding,
                AIConversationSummaryEmbedding.summary_id == AIConversationSummary.id,
            )
            .filter(
                AIConversationSummary.user_id == user_id,
                AIConversationSummary.session_id != session_id,
                AIConversationSummaryEmbedding.embedding_model == MODEL_NAME,
            )
            .all()
        )
    except Exception as exc:
        logger.warning("summary vector search query failed user_id=%s error=%s", user_id, exc)
        try:
            db.rollback()
        except Exception:
            logger.debug("rollback after summary vector query failure skipped", exc_info=True)
        return None

    scored: List[Tuple[float, Any]] = []
    for summary, embedding_row in rows:
        vector = _parse_vector(getattr(embedding_row, "vector", None))
        if vector is None:
            continue
        score = cosine_similarity(query_vector, vector)
        if score >= min_score:
            scored.append((score, summary))
    def _sort_key(item: Tuple[float, Any]) -> Tuple[float, str]:
        candidate = getattr(item[1], "updated_at", None) or getattr(item[1], "created_at", None)
        if candidate is not None and hasattr(candidate, "isoformat"):
            candidate_text = candidate.isoformat()
        else:
            candidate_text = str(candidate or "")
        return item[0], candidate_text

    scored.sort(key=_sort_key, reverse=True)

    hits: List[Dict] = []
    for score, summary in scored[:top_k]:
        updated_at = getattr(summary, "updated_at", None) or getattr(summary, "created_at", None)
        hits.append({
            "summary_id": int(summary.id),
            "session_id": summary.session_id,
            "role": "summary",
            "snippet": build_summary_embedding_text(summary)[:360],
            "created_at": updated_at.isoformat() if updated_at is not None and hasattr(updated_at, "isoformat") else None,
            "retrieval_method": "summary_vector",
            "relevance_score": float(score),
            "source_type": "conversation_summary",
        })
    return hits

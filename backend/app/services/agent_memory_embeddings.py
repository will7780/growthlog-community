"""
Agent long-term memory embedding index and vector search.

Vectors are stored in agent_memory_embeddings only; they must not be written
to entries/embeddings/knowledge_embeddings/attachment_embeddings/
ai_conversation_embeddings.

Top-level imports stay stdlib-only so lightweight tests can run without
SQLAlchemy/numpy/FastAPI. DB and embedding model are lazy-imported at runtime.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 12
DEFAULT_MIN_SCORE = 0.25
# Keep in sync with app.services.embedding.MODEL_NAME
MODEL_NAME = "intfloat/multilingual-e5-base"


def hash_memory_content(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def parse_vector(raw: object) -> Optional[List[float]]:
    """Parse stored vector JSON/list into a float list. No numpy required."""
    if raw is None:
        return None
    try:
        if isinstance(raw, list):
            vector = [float(v) for v in raw]
        elif isinstance(raw, str):
            vector = [float(v) for v in json.loads(raw)]
        else:
            return None
        if not vector:
            return None
        return vector
    except (TypeError, ValueError, json.JSONDecodeError):
        return None


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


def embedding_matches_memory_content(memory: Any, embedding_row: Any) -> bool:
    """True only when stored embedding hash matches current memory content."""
    content = (getattr(memory, "content", None) or "").strip()
    if not content:
        return False
    stored = getattr(embedding_row, "content_hash", None) or ""
    if not stored:
        return False
    return stored == hash_memory_content(content)


def filter_consistent_memory_embedding_rows(
    rows: Iterable[Tuple[Any, Any]],
) -> List[Tuple[Any, Any]]:
    """Drop inactive/stale pairs so old vectors never participate in retrieval."""
    kept: List[Tuple[Any, Any]] = []
    for memory, embedding_row in rows:
        if not bool(getattr(memory, "is_active", False)):
            continue
        if not embedding_matches_memory_content(memory, embedding_row):
            logger.info(
                "skip stale agent memory embedding memory_id=%s",
                getattr(memory, "id", None),
            )
            continue
        kept.append((memory, embedding_row))
    return kept


def score_memory_embedding_rows(
    query_vector: Sequence[float],
    rows: Iterable[Tuple[Any, Any]],
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    retrieval_method: str = "memory_vector_numpy",
) -> List[Dict]:
    """
    Score (memory, embedding_row) pairs with cosine similarity.

    Pure function: rows are duck-typed objects with memory.id/type/content/...
    and embedding_row.vector. Stale content_hash pairs are skipped.
    """
    scored: List[Tuple[float, Any]] = []
    for memory, embedding_row in filter_consistent_memory_embedding_rows(rows):
        vector = parse_vector(getattr(embedding_row, "vector", None))
        if vector is None:
            logger.warning(
                "invalid agent memory embedding vector memory_id=%s",
                getattr(memory, "id", None),
            )
            continue
        score = cosine_similarity(query_vector, vector)
        if score >= min_score:
            scored.append((score, memory))

    if not scored:
        return []

    scored.sort(
        key=lambda item: (
            item[0],
            getattr(item[1], "updated_at", None) or getattr(item[1], "created_at", None) or 0,
        ),
        reverse=True,
    )
    return [
        {
            "id": int(memory.id),
            "type": memory.memory_type,
            "content": memory.content,
            "source": memory.source,
            "confidence": float(getattr(memory, "confidence", 0.0) or 0.0),
            "relevance_score": float(score),
            "retrieval_method": retrieval_method,
        }
        for score, memory in scored[:top_k]
    ]


def merge_vector_and_recent_memories(
    vector_hits: List[Dict],
    recent_rows: List[Dict],
    *,
    limit: int,
) -> List[Dict]:
    """
    Merge vector hits with recent memories, de-duplicating by id.
    Recent fillers are tagged memory_recent_fallback.
    """
    if len(vector_hits) >= limit:
        return vector_hits[:limit]

    seen_ids = {int(item["id"]) for item in vector_hits}
    padded = list(vector_hits)
    for recent in recent_rows:
        memory_id = int(recent["id"])
        if memory_id in seen_ids:
            continue
        item = dict(recent)
        item["retrieval_method"] = "memory_recent_fallback"
        padded.append(item)
        seen_ids.add(memory_id)
        if len(padded) >= limit:
            break
    return padded[:limit]


def generate_embedding(text: str) -> List[float]:
    """Lazy wrapper so tests can monkeypatch this symbol."""
    from app.services.embedding import generate_embedding as _generate_embedding

    return _generate_embedding(text)


def _get_memory_models():
    """Lazy model import so tests can monkeypatch without SQLAlchemy."""
    from app.models import AgentMemory, AgentMemoryEmbedding

    return AgentMemory, AgentMemoryEmbedding


def has_agent_memory_embeddings_table(db: "Session") -> bool:
    try:
        from sqlalchemy import inspect

        bind = db.get_bind()
        if bind is None:
            return False
        return bool(inspect(bind).has_table("agent_memory_embeddings"))
    except Exception as exc:
        logger.debug("agent_memory_embeddings table check failed: %s", exc)
        return False


def sync_agent_memory_embedding(db: "Session", memory: Any) -> str:
    """
    Generate or update embedding for one agent memory.

    Returns: indexed | skipped | failed | deactivated
    Inactive memories do not keep a searchable embedding row.
    """
    if not has_agent_memory_embeddings_table(db):
        return "skipped"

    try:
        _, AgentMemoryEmbedding = _get_memory_models()
    except Exception as exc:  # noqa: BLE001
        logger.warning("agent memory models unavailable for sync: %s", type(exc).__name__)
        return "failed"

    try:
        existing = (
            db.query(AgentMemoryEmbedding)
            .filter(
                AgentMemoryEmbedding.memory_id == memory.id,
                AgentMemoryEmbedding.embedding_model == MODEL_NAME,
            )
            .first()
        )
        if not bool(getattr(memory, "is_active", True)):
            if existing is not None:
                db.delete(existing)
                db.commit()
            return "deactivated"

        content = (getattr(memory, "content", None) or "").strip()
        if not content:
            if existing is not None:
                db.delete(existing)
                db.commit()
            return "skipped"

        content_hash = hash_memory_content(content)
        if existing and existing.content_hash == content_hash:
            # Keep type/user metadata aligned even when vector reuse is valid.
            existing.user_id = int(memory.user_id)
            existing.memory_type = memory.memory_type
            db.commit()
            return "skipped"

        vector = generate_embedding(f"passage: {content}")
        if existing:
            existing.user_id = int(memory.user_id)
            existing.memory_type = memory.memory_type
            existing.content_hash = content_hash
            existing.vector = vector
        else:
            db.add(AgentMemoryEmbedding(
                memory_id=int(memory.id),
                user_id=int(memory.user_id),
                memory_type=memory.memory_type,
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
                "rollback after sync_agent_memory_embedding failure skipped memory_id=%s",
                getattr(memory, "id", None),
                exc_info=True,
            )
        logger.warning(
            "sync_agent_memory_embedding failed memory_id=%s error=%s",
            getattr(memory, "id", None),
            type(exc).__name__,
        )
        return "failed"


def after_agent_memory_mutation(
    db: "Session",
    *,
    user_id: int,
    memory: Any = None,
    memories: Optional[Iterable[Any]] = None,
) -> None:
    """
    Sync embedding(s) and invalidate the user-level memory vector cache.

    Embedding provider failures are logged; caller may still serve recent-memory
    fallback. Stale vectors must not remain searchable.
    """
    targets: List[Any] = []
    if memory is not None:
        targets.append(memory)
    if memories is not None:
        targets.extend(list(memories))
    for item in targets:
        try:
            sync_agent_memory_embedding(db, item)
        except Exception:  # noqa: BLE001
            logger.warning(
                "after_agent_memory_mutation sync failed memory_id=%s",
                getattr(item, "id", None),
            )
    try:
        from app.services.agent_memory_vector_index import reset_agent_memory_vector_index_cache

        reset_agent_memory_vector_index_cache(user_id=int(user_id))
    except Exception:  # noqa: BLE001
        logger.warning(
            "reset_agent_memory_vector_index_cache failed user_id=%s",
            user_id,
        )


def count_active_agent_memories(
    db: "Session",
    *,
    user_id: Optional[int] = None,
) -> int:
    """Count active personalization rows (safe scalar; no content)."""
    try:
        AgentMemory, _ = _get_memory_models()
    except Exception:
        return 0
    query = db.query(AgentMemory).filter(AgentMemory.is_active.is_(True))
    if user_id is not None:
        query = query.filter(AgentMemory.user_id == user_id)
    return int(query.count() or 0)


def verify_agent_memory_embeddings(
    db: "Session",
    *,
    user_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Consistency check for personalization embeddings.

    Returns counts/codes only — never content, URLs, paths, or vectors.
    ok=True only when every active memory has exactly one current-model
    embedding whose content_hash matches the live body, and inactive rows
    keep no current-model embedding.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "table_present": False,
        "model": MODEL_NAME,
        "active_total": 0,
        "valid_current_model_embeddings": 0,
        "missing": 0,
        "stale": 0,
        "duplicate": 0,
        "inactive_embeddings": 0,
        "failed": 0,
        "error_code": None,
    }
    if not has_agent_memory_embeddings_table(db):
        result["error_code"] = "table_missing"
        result["failed"] = 1
        return result
    result["table_present"] = True
    try:
        AgentMemory, AgentMemoryEmbedding = _get_memory_models()
    except Exception:
        result["error_code"] = "models_unavailable"
        result["failed"] = 1
        return result

    try:
        active_q = db.query(AgentMemory).filter(AgentMemory.is_active.is_(True))
        if user_id is not None:
            active_q = active_q.filter(AgentMemory.user_id == user_id)
        active_rows = active_q.all()
        result["active_total"] = len(active_rows)

        emb_q = db.query(AgentMemoryEmbedding).filter(
            AgentMemoryEmbedding.embedding_model == MODEL_NAME
        )
        if user_id is not None:
            emb_q = emb_q.filter(AgentMemoryEmbedding.user_id == user_id)
        emb_rows = emb_q.all()

        by_memory: Dict[int, List[Any]] = {}
        for row in emb_rows:
            mid = int(row.memory_id)
            by_memory.setdefault(mid, []).append(row)

        active_ids = {int(m.id) for m in active_rows}
        for memory in active_rows:
            mid = int(memory.id)
            rows = by_memory.get(mid) or []
            if len(rows) == 0:
                result["missing"] += 1
                continue
            if len(rows) > 1:
                result["duplicate"] += 1
                continue
            if not embedding_matches_memory_content(memory, rows[0]):
                result["stale"] += 1
                continue
            result["valid_current_model_embeddings"] += 1

        for mid, rows in by_memory.items():
            if mid not in active_ids:
                result["inactive_embeddings"] += len(rows)
            elif len(rows) > 1:
                # already counted as duplicate above for active; keep for inactive too
                pass

        result["ok"] = (
            result["active_total"] == result["valid_current_model_embeddings"]
            and result["missing"] == 0
            and result["stale"] == 0
            and result["duplicate"] == 0
            and result["inactive_embeddings"] == 0
            and result["failed"] == 0
        )
        if not result["ok"] and result["error_code"] is None:
            result["error_code"] = "consistency_failed"
        return result
    except Exception:
        logger.warning("verify_agent_memory_embeddings failed", exc_info=True)
        result["failed"] = 1
        result["error_code"] = "verify_exception"
        return result


def backfill_agent_memory_embeddings(
    db: "Session",
    *,
    user_id: Optional[int] = None,
    limit: int = 200,
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "processed": 0,
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
        "deactivated": 0,
        "error_code": None,
    }
    if not has_agent_memory_embeddings_table(db):
        logger.warning("agent_memory_embeddings table missing; backfill skipped")
        stats["failed"] = 1
        stats["error_code"] = "table_missing"
        return stats

    try:
        AgentMemory, AgentMemoryEmbedding = _get_memory_models()
    except Exception as exc:
        logger.warning("agent memory models unavailable: %s", type(exc).__name__)
        stats["failed"] = 1
        stats["error_code"] = "models_unavailable"
        return stats

    query = (
        db.query(AgentMemory)
        .filter(AgentMemory.is_active.is_(True))
        .order_by(AgentMemory.updated_at.desc(), AgentMemory.created_at.desc())
    )
    if user_id is not None:
        query = query.filter(AgentMemory.user_id == user_id)

    rows = query.limit(max(limit * 3, limit)).all()
    for memory in rows:
        if stats["processed"] >= limit:
            break

        processed_this_item = False
        try:
            content_hash = hash_memory_content(memory.content or "")
            existing = (
                db.query(AgentMemoryEmbedding)
                .filter(
                    AgentMemoryEmbedding.memory_id == memory.id,
                    AgentMemoryEmbedding.embedding_model == MODEL_NAME,
                )
                .first()
            )
            if existing and existing.content_hash == content_hash:
                continue

            stats["processed"] += 1
            processed_this_item = True
            outcome = sync_agent_memory_embedding(db, memory)
            stats[outcome] = int(stats.get(outcome, 0) or 0) + 1
        except Exception as exc:
            if not processed_this_item:
                stats["processed"] += 1
            stats["failed"] += 1
            try:
                db.rollback()
            except Exception:
                logger.debug(
                    "rollback after backfill memory failure skipped memory_id=%s",
                    getattr(memory, "id", None),
                    exc_info=True,
                )
            logger.warning(
                "backfill_agent_memory_embeddings failed memory_id=%s error=%s",
                getattr(memory, "id", None),
                type(exc).__name__,
            )
            continue

    # Drop current-model embeddings that no longer belong to an active memory.
    try:
        emb_q = db.query(AgentMemoryEmbedding).filter(
            AgentMemoryEmbedding.embedding_model == MODEL_NAME
        )
        if user_id is not None:
            emb_q = emb_q.filter(AgentMemoryEmbedding.user_id == user_id)
        for emb in emb_q.all():
            mem = db.query(AgentMemory).filter(AgentMemory.id == emb.memory_id).first()
            if mem is None or not bool(getattr(mem, "is_active", False)):
                db.delete(emb)
                stats["deactivated"] = int(stats.get("deactivated") or 0) + 1
                stats["processed"] = int(stats.get("processed") or 0) + 1
        if int(stats.get("deactivated") or 0) > 0:
            db.commit()
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        stats["failed"] = int(stats.get("failed") or 0) + 1
        stats["error_code"] = "inactive_cleanup_failed"
        logger.warning(
            "backfill inactive embedding cleanup failed error=%s",
            type(exc).__name__,
        )

    return stats


def search_agent_memories_vector(
    db: "Session",
    user_id: int,
    query: str,
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    memory_type: Optional[str] = None,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    query_vector: Optional[Sequence[float]] = None,
) -> Optional[List[Dict]]:
    """
    Vector search over indexed active agent memories for the current user.

    Returns None on infrastructure failure (table missing, model/vector errors).
    Returns [] when no indexed rows or no matches above min_score.
    When query_vector is provided, it is reused and embed_fn is not called.
    """
    query_text = (query or "").strip()
    reused = list(query_vector) if query_vector is not None else None
    if reused is None and not query_text:
        return []

    if not has_agent_memory_embeddings_table(db):
        logger.debug("agent_memory_embeddings table missing; vector search skipped")
        return None

    if reused is None:
        embed = embed_fn or generate_embedding
        try:
            reused = embed(f"query: {query_text}")
        except Exception as exc:
            logger.warning(
                "agent memory vector query embedding failed user_id=%s error=%s",
                user_id,
                exc,
            )
            return None
    query_vector = reused

    # Prefer dedicated in-memory index cache (FAISS / NumPy / Python).
    try:
        from app.services.agent_memory_vector_index import search_agent_memories_index

        index_hits = search_agent_memories_index(
            db,
            user_id,
            query_vector,
            top_k=top_k,
            min_score=min_score,
            memory_type=memory_type,
        )
        if index_hits is not None:
            return index_hits
    except Exception as exc:
        logger.warning(
            "agent memory index search wrapper failed user_id=%s error=%s",
            user_id,
            exc,
        )

    # Fallback: DB row scan + pure cosine scoring.
    try:
        AgentMemory, AgentMemoryEmbedding = _get_memory_models()

        q = (
            db.query(AgentMemory, AgentMemoryEmbedding)
            .join(
                AgentMemoryEmbedding,
                AgentMemoryEmbedding.memory_id == AgentMemory.id,
            )
            .filter(
                AgentMemory.user_id == user_id,
                AgentMemory.is_active.is_(True),
                AgentMemoryEmbedding.embedding_model == MODEL_NAME,
            )
        )
        if memory_type:
            q = q.filter(AgentMemory.memory_type == memory_type)
        rows = q.all()
    except Exception as exc:
        logger.warning(
            "agent memory vector search query failed user_id=%s error=%s",
            user_id,
            exc,
        )
        try:
            db.rollback()
        except Exception:
            logger.debug("rollback after memory vector search query failure skipped", exc_info=True)
        return None

    if not rows:
        return []

    return score_memory_embedding_rows(
        query_vector,
        rows,
        top_k=top_k,
        min_score=min_score,
        retrieval_method="memory_vector_numpy",
    )

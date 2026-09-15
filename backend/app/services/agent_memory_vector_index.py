"""
In-memory vector index cache for agent long-term memories.

Data source: agent_memory_embeddings only.
Separate from ai_session_vector_index / visual_vector_index / entries VectorIndexManager.

Top-level imports stay stdlib-only so lightweight tests can run without
SQLAlchemy/numpy/faiss. Search order: FAISS -> NumPy -> pure Python.
"""
from __future__ import annotations

import hashlib
import logging
import math
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# Keep in sync with agent_memory_embeddings.MODEL_NAME
MODEL_NAME = "intfloat/multilingual-e5-base"
DEFAULT_TOP_K = 12
DEFAULT_MIN_SCORE = 0.25

_cache_lock = threading.Lock()
_CACHE: Dict[Tuple[int, str], "_AgentMemoryVectorCache"] = {}


@dataclass
class AgentMemoryVectorRecord:
    memory_id: int
    memory_type: str
    content: str
    source: str
    confidence: float
    vector: List[float]


@dataclass
class _AgentMemoryVectorCache:
    user_id: int
    memory_type_key: str
    vector_count: int
    signature: str
    max_updated_at: Optional[str]
    records: List[AgentMemoryVectorRecord] = field(default_factory=list)
    numpy_matrix: Any = None
    faiss_index: Any = None


def reset_agent_memory_vector_index_cache(user_id: Optional[int] = None) -> None:
    with _cache_lock:
        if user_id is None:
            _CACHE.clear()
            return
        uid = int(user_id)
        for key in list(_CACHE):
            if key[0] == uid:
                _CACHE.pop(key, None)


def get_agent_memory_vector_index_cache_stats(user_id: Optional[int] = None) -> Dict[str, Any]:
    with _cache_lock:
        items = []
        for key, cache in _CACHE.items():
            if user_id is not None and key[0] != int(user_id):
                continue
            index_type = "faiss" if cache.faiss_index is not None else (
                "numpy" if cache.numpy_matrix is not None else "python"
            )
            items.append({
                "user_id": cache.user_id,
                "memory_type": cache.memory_type_key,
                "vector_count": cache.vector_count,
                "signature": cache.signature,
                "max_updated_at": cache.max_updated_at,
                "index_type": index_type,
            })
        return {"cache_count": len(items), "users": items}


def _normalize(vector: Sequence[float]) -> List[float]:
    values = [float(v) for v in vector]
    norm = math.sqrt(sum(v * v for v in values)) or 1.0
    return [v / norm for v in values]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right)))


def _parse_vector(raw: object) -> Optional[List[float]]:
    from app.services.agent_memory_embeddings import parse_vector

    return parse_vector(raw)


def _compute_signature(rows: Sequence[Any]) -> Tuple[int, str, Optional[str]]:
    if not rows:
        return 0, "empty", None

    parts: List[str] = []
    max_updated = None
    for memory, emb in rows:
        memory_id = getattr(memory, "id", None)
        content_hash = getattr(emb, "content_hash", "") or ""
        updated = getattr(emb, "updated_at", None) or getattr(emb, "created_at", None)
        updated_iso = updated.isoformat() if updated is not None and hasattr(updated, "isoformat") else str(updated or "")
        parts.append(f"{memory_id}:{content_hash}:{updated_iso}")
        if updated is not None and (max_updated is None or updated > max_updated):
            max_updated = updated

    parts.sort()
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    max_updated_iso = max_updated.isoformat() if max_updated is not None and hasattr(max_updated, "isoformat") else (
        str(max_updated) if max_updated is not None else None
    )
    return len(parts), digest, max_updated_iso


def _get_memory_models():
    from app.models import AgentMemory, AgentMemoryEmbedding

    return AgentMemory, AgentMemoryEmbedding


def _load_embedding_rows(
    db: "Session",
    user_id: int,
    *,
    memory_type: Optional[str] = None,
) -> List[Tuple[Any, Any]]:
    from app.services.agent_memory_embeddings import filter_consistent_memory_embedding_rows

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
            AgentMemoryEmbedding.user_id == user_id,
            AgentMemoryEmbedding.embedding_model == MODEL_NAME,
        )
    )
    if memory_type:
        q = q.filter(AgentMemory.memory_type == memory_type)
    # Never index stale content_hash / inactive pairs into the user cache.
    return filter_consistent_memory_embedding_rows(q.all())


def _try_build_numpy_matrix(vectors: List[List[float]]):
    try:
        import numpy as np  # type: ignore

        return np.asarray(vectors, dtype="float32")
    except Exception:
        return None


def _try_build_faiss_index(matrix):
    if matrix is None:
        return None
    try:
        import faiss  # type: ignore

        index = faiss.IndexFlatIP(int(matrix.shape[1]))
        index.add(matrix)
        return index
    except Exception as exc:
        logger.debug("agent memory FAISS index unavailable: %s", exc)
        return None


def _build_cache(
    db: "Session",
    user_id: int,
    *,
    memory_type: Optional[str] = None,
) -> _AgentMemoryVectorCache:
    type_key = memory_type or "*"
    rows = _load_embedding_rows(db, user_id, memory_type=memory_type)
    vector_count, signature, max_updated_at = _compute_signature(rows)

    records: List[AgentMemoryVectorRecord] = []
    vectors: List[List[float]] = []
    for memory, emb in rows:
        vector = _parse_vector(getattr(emb, "vector", None))
        if vector is None:
            logger.warning(
                "skip invalid agent memory vector during index build memory_id=%s",
                getattr(memory, "id", None),
            )
            continue
        normalized = _normalize(vector)
        records.append(AgentMemoryVectorRecord(
            memory_id=int(memory.id),
            memory_type=str(memory.memory_type),
            content=str(memory.content or ""),
            source=str(getattr(memory, "source", "") or ""),
            confidence=float(getattr(memory, "confidence", 0.0) or 0.0),
            vector=normalized,
        ))
        vectors.append(normalized)

    matrix = _try_build_numpy_matrix(vectors) if vectors else None
    return _AgentMemoryVectorCache(
        user_id=int(user_id),
        memory_type_key=type_key,
        vector_count=len(records),
        signature=signature if records else "empty",
        max_updated_at=max_updated_at,
        records=records,
        numpy_matrix=matrix,
        faiss_index=_try_build_faiss_index(matrix),
    )


def _get_or_build_cache(
    db: "Session",
    user_id: int,
    *,
    memory_type: Optional[str] = None,
) -> Optional[_AgentMemoryVectorCache]:
    type_key = memory_type or "*"
    key = (int(user_id), type_key)
    try:
        rows = _load_embedding_rows(db, user_id, memory_type=memory_type)
        _, signature, _ = _compute_signature(rows)
    except Exception as exc:
        logger.warning(
            "agent memory index load rows failed user_id=%s error=%s",
            user_id,
            exc,
        )
        return None

    with _cache_lock:
        cached = _CACHE.get(key)
        if cached is not None and cached.signature == signature:
            return cached

    try:
        built = _build_cache(db, user_id, memory_type=memory_type)
    except Exception as exc:
        logger.warning(
            "agent memory index build failed user_id=%s error=%s",
            user_id,
            exc,
        )
        return None

    with _cache_lock:
        _CACHE[key] = built
    return built


def _hit_dict(record: AgentMemoryVectorRecord, score: float, method: str) -> Dict[str, Any]:
    return {
        "id": record.memory_id,
        "type": record.memory_type,
        "content": record.content,
        "source": record.source,
        "confidence": record.confidence,
        "relevance_score": float(score),
        "retrieval_method": method,
    }


def search_cached_memory_vectors(
    query_vector: Sequence[float],
    records: Sequence[AgentMemoryVectorRecord],
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    numpy_matrix: Any = None,
    faiss_index: Any = None,
    memory_type: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """
    Pure-ish search over cached records. Prefer FAISS, then NumPy, then Python.
    """
    if top_k <= 0 or not records:
        return []

    filtered_indices = [
        idx for idx, record in enumerate(records)
        if memory_type is None or record.memory_type == memory_type
    ]
    if not filtered_indices:
        return []

    normalized_query = _normalize(query_vector)

    # FAISS path (full cache; filter after)
    if faiss_index is not None and numpy_matrix is not None and memory_type is None:
        try:
            import numpy as np  # type: ignore

            query_matrix = np.asarray([normalized_query], dtype="float32")
            search_k = min(max(top_k * 3, top_k), len(records))
            scores, indices = faiss_index.search(query_matrix, search_k)
            hits: List[Dict[str, Any]] = []
            for score, idx in zip(scores[0], indices[0]):
                if int(idx) < 0:
                    continue
                if float(score) < min_score:
                    continue
                hits.append(_hit_dict(records[int(idx)], float(score), "memory_vector_faiss"))
                if len(hits) >= top_k:
                    break
            return hits
        except Exception as exc:
            logger.debug("agent memory FAISS search failed: %s", exc)

    # NumPy path
    if numpy_matrix is not None:
        try:
            import numpy as np  # type: ignore

            query_matrix = np.asarray(normalized_query, dtype="float32")
            if memory_type is None:
                scores = numpy_matrix @ query_matrix
                order = scores.argsort()[::-1]
                hits = []
                for idx in order:
                    score = float(scores[int(idx)])
                    if score < min_score:
                        continue
                    hits.append(_hit_dict(records[int(idx)], score, "memory_vector_numpy"))
                    if len(hits) >= top_k:
                        break
                return hits

            # Filtered subset via python scores on selected rows, still mark numpy when matrix exists
            # Prefer matrix rows for selected indices
            hits = []
            scored = []
            for idx in filtered_indices:
                score = float(numpy_matrix[idx] @ query_matrix)
                if score >= min_score:
                    scored.append((score, idx))
            scored.sort(key=lambda item: item[0], reverse=True)
            for score, idx in scored[:top_k]:
                hits.append(_hit_dict(records[idx], score, "memory_vector_numpy"))
            return hits
        except Exception as exc:
            logger.debug("agent memory NumPy search failed: %s", exc)

    # Pure Python fallback
    scored_py: List[Tuple[float, AgentMemoryVectorRecord]] = []
    for idx in filtered_indices:
        record = records[idx]
        score = _dot(normalized_query, record.vector)
        if score >= min_score:
            scored_py.append((score, record))
    scored_py.sort(key=lambda item: item[0], reverse=True)
    return [
        _hit_dict(record, score, "memory_vector_python")
        for score, record in scored_py[:top_k]
    ]


def search_agent_memories_index(
    db: "Session",
    user_id: int,
    query_vector: Sequence[float],
    *,
    top_k: int = DEFAULT_TOP_K,
    min_score: float = DEFAULT_MIN_SCORE,
    memory_type: Optional[str] = None,
) -> Optional[List[Dict[str, Any]]]:
    """
    Search agent memories via dedicated in-memory index cache.

    Returns None when cache build/load fails (caller should fallback to DB scan).
    Returns [] when cache is empty or no hits above min_score.
    """
    if not query_vector:
        return []

    try:
        # Prefer full-user cache and filter by memory_type in search.
        cache = _get_or_build_cache(db, user_id, memory_type=None)
    except Exception as exc:
        logger.warning(
            "agent memory index get/build failed user_id=%s error=%s",
            user_id,
            exc,
        )
        return None

    if cache is None:
        return None
    if cache.vector_count == 0 or not cache.records:
        return []

    try:
        return search_cached_memory_vectors(
            query_vector,
            cache.records,
            top_k=top_k,
            min_score=min_score,
            numpy_matrix=cache.numpy_matrix,
            faiss_index=cache.faiss_index,
            memory_type=memory_type,
        )
    except Exception as exc:
        logger.warning(
            "agent memory index search failed user_id=%s error=%s",
            user_id,
            exc,
        )
        return None


def warm_agent_memory_vector_index_cache(
    db: "Session",
    user_id: Optional[int] = None,
) -> Dict[str, int]:
    """
    Pre-build memory vector caches. Failures are counted, never raised.
    """
    stats = {"warmed": 0, "skipped": 0, "failed": 0}
    try:
        from app.services.agent_memory_embeddings import has_agent_memory_embeddings_table

        if not has_agent_memory_embeddings_table(db):
            stats["skipped"] += 1
            return stats
    except Exception:
        stats["skipped"] += 1
        return stats

    user_ids: List[int]
    if user_id is not None:
        user_ids = [int(user_id)]
    else:
        try:
            _, AgentMemoryEmbedding = _get_memory_models()
            user_ids = [
                int(row[0])
                for row in (
                    db.query(AgentMemoryEmbedding.user_id)
                    .filter(AgentMemoryEmbedding.embedding_model == MODEL_NAME)
                    .distinct()
                    .all()
                )
            ]
        except Exception as exc:
            logger.warning("agent memory index warm user discovery failed: %s", exc)
            stats["failed"] += 1
            return stats

    for uid in user_ids:
        try:
            cache = _get_or_build_cache(db, uid, memory_type=None)
            if cache is None:
                stats["failed"] += 1
            elif cache.vector_count == 0:
                stats["skipped"] += 1
            else:
                stats["warmed"] += 1
        except Exception as exc:
            logger.warning("agent memory index warm failed user_id=%s error=%s", uid, exc)
            stats["failed"] += 1

    return stats

"""
Dedicated AI session conversation vector cache.

Data source: ai_conversation_embeddings only. Separate from entries
VectorIndexManager / visual indexes / agent memory indexes.

Top-level imports stay stdlib-only so lightweight tests can run without
SQLAlchemy/numpy/faiss. Optional disk snapshots default to disabled.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

MODEL_NAME = "intfloat/multilingual-e5-base"
EMBEDDING_DIM = 768
SNAPSHOT_VERSION = 1
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")

_faiss_available: Optional[bool] = None
_cache_lock = threading.Lock()
_user_caches: Dict[int, "AISessionUserFaissCache"] = {}


@dataclass
class AISessionVectorRecord:
    conversation_id: int
    session_id: str
    role: str
    snippet: str
    created_at_iso: Optional[str]
    embedding_dim: int = EMBEDDING_DIM
    updated_at_iso: str = ""


@dataclass
class AISessionUserFaissCache:
    user_id: int
    vector_count: int
    signature: str
    max_updated_at: Optional[str]
    records: List[AISessionVectorRecord] = field(default_factory=list)
    vectors: List[List[float]] = field(default_factory=list)
    index: object | None = None
    loaded_from_disk: bool = False


def _get_conversation_models():
    from app.models import AIConversation, AIConversationEmbedding

    return AIConversation, AIConversationEmbedding


def _persistence_settings() -> Tuple[bool, str]:
    try:
        from app.config import settings

        enabled = bool(getattr(settings, "ai_session_index_persistence_enabled", False))
        cache_dir = str(
            getattr(settings, "ai_session_index_cache_dir", None)
            or "vector_indexes/ai_sessions"
        )
        return enabled, cache_dir
    except Exception:
        return False, "vector_indexes/ai_sessions"


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


def parse_vector(raw: object) -> Optional[List[float]]:
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


def _normalize(vector: Sequence[float]) -> List[float]:
    values = [float(item) for item in vector]
    norm = math.sqrt(sum(item * item for item in values)) or 1.0
    return [item / norm for item in values]


def _dot(left: Sequence[float], right: Sequence[float]) -> float:
    return float(sum(a * b for a, b in zip(left, right)))


def is_faiss_available() -> bool:
    global _faiss_available
    if _faiss_available is None:
        try:
            import faiss  # noqa: F401

            _faiss_available = True
        except Exception as exc:
            _faiss_available = False
            logger.warning("faiss unavailable for ai session vector index: %s", exc)
    return bool(_faiss_available)


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
        logger.debug("ai session FAISS index build unavailable: %s", exc)
        return None


def _compute_signature(rows: Sequence[Any]) -> Tuple[int, str, Optional[str]]:
    if not rows:
        return 0, "empty", None

    max_updated: Optional[datetime] = None
    max_updated_iso: Optional[str] = None
    parts: List[str] = []
    for row in rows:
        conversation_id = getattr(row, "conversation_id", None)
        content_hash = getattr(row, "content_hash", "") or ""
        candidate = getattr(row, "updated_at", None) or getattr(row, "created_at", None)
        if candidate is not None and hasattr(candidate, "isoformat"):
            candidate_iso = candidate.isoformat()
            if max_updated is None or candidate > max_updated:
                max_updated = candidate
                max_updated_iso = candidate_iso
        else:
            candidate_iso = str(candidate or "")
            if candidate_iso and (max_updated_iso is None or candidate_iso > max_updated_iso):
                max_updated_iso = candidate_iso
        parts.append(f"{conversation_id}:{content_hash}:{candidate_iso}")

    parts.sort()
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return len(parts), digest, max_updated_iso


def _load_embedding_rows(db: "Session", user_id: int) -> List[Tuple[Any, Any]]:
    AIConversation, AIConversationEmbedding = _get_conversation_models()
    return (
        db.query(AIConversation, AIConversationEmbedding)
        .join(
            AIConversationEmbedding,
            AIConversationEmbedding.conversation_id == AIConversation.id,
        )
        .filter(
            AIConversation.user_id == user_id,
            AIConversationEmbedding.user_id == user_id,
            AIConversationEmbedding.embedding_model == MODEL_NAME,
        )
        .all()
    )


def build_ai_session_cache_from_records(
    *,
    user_id: int,
    signature: str,
    max_updated_at: Optional[str],
    records: Sequence[AISessionVectorRecord],
    vectors: Sequence[Sequence[float]],
    loaded_from_disk: bool = False,
) -> AISessionUserFaissCache:
    normalized = [_normalize(vector) for vector in vectors]
    matrix = _try_build_numpy_matrix(normalized) if normalized else None
    index = _try_build_faiss_index(matrix) if is_faiss_available() else None
    return AISessionUserFaissCache(
        user_id=int(user_id),
        vector_count=len(records),
        signature=signature,
        max_updated_at=max_updated_at,
        records=list(records),
        vectors=normalized,
        index=index,
        loaded_from_disk=loaded_from_disk,
    )


def _record_from_row(conversation: Any, embedding_row: Any) -> AISessionVectorRecord:
    created_at = getattr(conversation, "created_at", None)
    updated_at = getattr(embedding_row, "updated_at", None) or getattr(embedding_row, "created_at", None)
    return AISessionVectorRecord(
        conversation_id=int(conversation.id),
        session_id=str(conversation.session_id),
        role=str(conversation.role),
        snippet=(getattr(conversation, "content", None) or "")[:360],
        created_at_iso=created_at.isoformat() if created_at is not None and hasattr(created_at, "isoformat") else (
            str(created_at) if created_at is not None else None
        ),
        embedding_dim=int(getattr(embedding_row, "embedding_dim", EMBEDDING_DIM) or EMBEDDING_DIM),
        updated_at_iso=updated_at.isoformat() if updated_at is not None and hasattr(updated_at, "isoformat") else str(updated_at or ""),
    )


def _build_user_cache(db: "Session", user_id: int) -> Optional[AISessionUserFaissCache]:
    if not has_ai_conversation_embeddings_table(db):
        return None

    rows = _load_embedding_rows(db, user_id)
    embedding_rows = [emb for _, emb in rows]
    _, signature, max_updated_at = _compute_signature(embedding_rows)

    records: List[AISessionVectorRecord] = []
    vectors: List[List[float]] = []
    for conversation, embedding_row in rows:
        vector = parse_vector(getattr(embedding_row, "vector", None))
        if vector is None:
            logger.warning(
                "skip invalid ai session vector during cache build conversation_id=%s",
                getattr(conversation, "id", None),
            )
            continue
        if len(vector) != EMBEDDING_DIM:
            logger.warning(
                "skip ai session vector with unexpected dim conversation_id=%s",
                getattr(conversation, "id", None),
            )
            continue
        records.append(_record_from_row(conversation, embedding_row))
        vectors.append(vector)

    return build_ai_session_cache_from_records(
        user_id=user_id,
        signature=signature,
        max_updated_at=max_updated_at,
        records=records,
        vectors=vectors,
        loaded_from_disk=False,
    )


def sanitize_ai_session_index_token(value: str, *, max_len: int = 64) -> str:
    text = (value or "").strip() or "unknown"
    text = _SAFE_TOKEN_RE.sub("_", text).strip("._-") or "unknown"
    if len(text) > max_len:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        text = f"{text[: max_len - 11]}_{digest}"
    return text


def ai_session_index_snapshot_path(cache_dir: str, user_id: int) -> Path:
    return Path(cache_dir) / f"u{int(user_id)}_{sanitize_ai_session_index_token(MODEL_NAME)}.json"


def serialize_ai_session_index_snapshot(cache: AISessionUserFaissCache) -> Dict[str, Any]:
    return {
        "version": SNAPSHOT_VERSION,
        "user_id": int(cache.user_id),
        "embedding_model": MODEL_NAME,
        "embedding_dim": EMBEDDING_DIM,
        "signature": cache.signature,
        "max_updated_at": cache.max_updated_at,
        "records": [
            {
                "conversation_id": record.conversation_id,
                "session_id": record.session_id,
                "role": record.role,
                "snippet": record.snippet,
                "created_at_iso": record.created_at_iso,
                "embedding_dim": record.embedding_dim,
                "updated_at_iso": record.updated_at_iso,
                "vector": [float(v) for v in vector],
            }
            for record, vector in zip(cache.records, cache.vectors)
        ],
    }


def load_ai_session_index_snapshot(
    path: Path | str,
    *,
    expected_user_id: Optional[int] = None,
    expected_signature: Optional[str] = None,
) -> Optional[AISessionUserFaissCache]:
    try:
        snapshot_path = Path(path)
        if not snapshot_path.is_file():
            return None
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version") or 0) != SNAPSHOT_VERSION:
            return None
        if payload.get("embedding_model") != MODEL_NAME:
            return None
        if int(payload.get("embedding_dim") or 0) != EMBEDDING_DIM:
            return None
        user_id = int(payload.get("user_id"))
        signature = str(payload.get("signature") or "")
        if expected_user_id is not None and user_id != int(expected_user_id):
            return None
        if expected_signature is not None and signature != expected_signature:
            return None
        raw_records = payload.get("records")
        if not isinstance(raw_records, list):
            return None

        records: List[AISessionVectorRecord] = []
        vectors: List[List[float]] = []
        for item in raw_records:
            if not isinstance(item, dict):
                return None
            vector = parse_vector(item.get("vector"))
            if vector is None or len(vector) != EMBEDDING_DIM:
                return None
            records.append(AISessionVectorRecord(
                conversation_id=int(item["conversation_id"]),
                session_id=str(item["session_id"]),
                role=str(item.get("role") or ""),
                snippet=str(item.get("snippet") or "")[:360],
                created_at_iso=item.get("created_at_iso"),
                embedding_dim=int(item.get("embedding_dim") or EMBEDDING_DIM),
                updated_at_iso=str(item.get("updated_at_iso") or ""),
            ))
            vectors.append(vector)

        return build_ai_session_cache_from_records(
            user_id=user_id,
            signature=signature,
            max_updated_at=payload.get("max_updated_at"),
            records=records,
            vectors=vectors,
            loaded_from_disk=True,
        )
    except Exception as exc:
        logger.debug("ai session index snapshot load failed path=%s error=%s", path, exc)
        return None


def save_ai_session_index_snapshot(path: Path | str, cache: AISessionUserFaissCache) -> bool:
    try:
        snapshot_path = Path(path)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        data = json.dumps(serialize_ai_session_index_snapshot(cache), ensure_ascii=False, separators=(",", ":"))
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(snapshot_path.parent),
            prefix=f".{snapshot_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp.write(data)
            tmp_name = tmp.name
        Path(tmp_name).replace(snapshot_path)
        return True
    except Exception as exc:
        logger.warning("ai session index snapshot save failed path=%s error=%s", path, exc)
        return False


def _get_or_build_cache(db: "Session", user_id: int) -> Optional[AISessionUserFaissCache]:
    if not has_ai_conversation_embeddings_table(db):
        return None

    rows = _load_embedding_rows(db, user_id)
    embedding_rows = [emb for _, emb in rows]
    vector_count, signature, _ = _compute_signature(embedding_rows)

    with _cache_lock:
        cached = _user_caches.get(user_id)
        if cached is not None and cached.vector_count == vector_count and cached.signature == signature:
            return cached

    persistence_enabled, cache_dir = _persistence_settings()
    if persistence_enabled:
        restored = load_ai_session_index_snapshot(
            ai_session_index_snapshot_path(cache_dir, user_id),
            expected_user_id=user_id,
            expected_signature=signature,
        )
        if restored is not None:
            with _cache_lock:
                _user_caches[user_id] = restored
            return restored

    try:
        built = _build_user_cache(db, user_id)
    except Exception as exc:
        logger.warning("ai session cache build failed user_id=%s error=%s", user_id, exc)
        return None

    if built is None:
        return None

    with _cache_lock:
        _user_caches[user_id] = built

    if persistence_enabled:
        save_ai_session_index_snapshot(ai_session_index_snapshot_path(cache_dir, user_id), built)

    return built


def reset_ai_session_faiss_cache(user_id: Optional[int] = None) -> None:
    """Clear in-memory AI session cache (all users or one user)."""
    with _cache_lock:
        if user_id is None:
            _user_caches.clear()
        else:
            _user_caches.pop(int(user_id), None)


def get_ai_session_faiss_cache_stats(user_id: Optional[int] = None) -> Dict:
    """Return cache metadata for troubleshooting."""
    persistence_enabled, cache_dir = _persistence_settings()
    with _cache_lock:
        caches = (
            [cached]
            if user_id is not None and (cached := _user_caches.get(int(user_id))) is not None
            else list(_user_caches.values()) if user_id is None else []
        )
        return {
            "faiss_available": is_faiss_available(),
            "persistence_enabled": persistence_enabled,
            "persistence_cache_dir": cache_dir,
            "users": [
                {
                    "user_id": cached.user_id,
                    "vector_count": cached.vector_count,
                    "signature": cached.signature,
                    "max_updated_at": cached.max_updated_at,
                    "loaded_from_disk": cached.loaded_from_disk,
                }
                for cached in caches
            ],
        }


def warm_ai_session_faiss_cache(db: Any = None, user_id: Optional[int] = None) -> Dict[str, Any]:
    """
    Pre-build cache for one user or all users with embeddings.
    Failures are counted but do not raise.
    """
    stats: Dict[str, Any] = {"warmed": 0, "skipped": 0, "failed": 0, "error": None}
    if db is None:
        stats["skipped"] += 1
        stats["error"] = "db_missing"
        return stats
    if not has_ai_conversation_embeddings_table(db):
        stats["skipped"] += 1
        return stats

    try:
        _, AIConversationEmbedding = _get_conversation_models()
    except Exception as exc:
        stats["failed"] += 1
        stats["error"] = f"model_import_failed:{type(exc).__name__}"
        return stats

    user_ids: List[int]
    if user_id is not None:
        user_ids = [int(user_id)]
    else:
        try:
            user_ids = [
                int(row[0])
                for row in (
                    db.query(AIConversationEmbedding.user_id)
                    .filter(AIConversationEmbedding.embedding_model == MODEL_NAME)
                    .distinct()
                    .all()
                )
            ]
        except Exception as exc:
            logger.warning("ai session cache warm user discovery failed: %s", exc)
            stats["failed"] += 1
            stats["error"] = type(exc).__name__
            return stats

    for uid in user_ids:
        try:
            cache = _get_or_build_cache(db, uid)
            if cache is None:
                stats["failed"] += 1
            elif cache.vector_count == 0:
                stats["skipped"] += 1
            else:
                stats["warmed"] += 1
        except Exception as exc:
            logger.warning("ai session cache warm failed user_id=%s error=%s", uid, exc)
            stats["failed"] += 1

    return stats


def _search_cache_with_faiss(
    cache: AISessionUserFaissCache,
    query_vector: Sequence[float],
    *,
    top_k: int,
) -> Optional[List[Tuple[float, int]]]:
    if cache.index is None:
        return None
    try:
        import numpy as np  # type: ignore

        query_matrix = np.asarray([_normalize(query_vector)], dtype="float32")
        search_k = min(max(top_k * 4, top_k), cache.vector_count)
        distances, indices = cache.index.search(query_matrix, search_k)
        return [
            (float(dist), int(idx))
            for dist, idx in zip(distances[0], indices[0])
            if int(idx) >= 0
        ]
    except Exception as exc:
        logger.warning("ai session FAISS search failed: %s", exc)
        return None


def search_ai_conversations_faiss(
    db: "Session",
    user_id: int,
    session_id: str,
    query_vector: List[float],
    *,
    top_k: int = 8,
    min_score: float = 0.25,
) -> Optional[List[Dict]]:
    """
    Search AI session embeddings via dedicated FAISS cache.

    Returns None when FAISS unavailable or cache build/search fails.
    Returns [] when cache is empty or no hits above min_score.
    """
    if not is_faiss_available():
        return None

    try:
        cache = _get_or_build_cache(db, int(user_id))
    except Exception as exc:
        logger.warning("ai session cache get/build failed user_id=%s error=%s", user_id, exc)
        return None

    if cache is None or cache.index is None:
        return None
    if cache.vector_count == 0 or not cache.records:
        return []

    scored_indices = _search_cache_with_faiss(cache, query_vector, top_k=top_k)
    if scored_indices is None:
        return None

    hits: List[Dict] = []
    for score, idx in scored_indices:
        record = cache.records[int(idx)]
        if record.session_id == session_id:
            continue
        if score < min_score:
            continue
        hits.append({
            "message_id": record.conversation_id,
            "session_id": record.session_id,
            "role": record.role,
            "snippet": record.snippet,
            "created_at": record.created_at_iso,
            "retrieval_method": "vector_faiss",
            "relevance_score": float(score),
        })
        if len(hits) >= top_k:
            break

    hits.sort(key=lambda item: item["relevance_score"], reverse=True)
    return hits[:top_k]

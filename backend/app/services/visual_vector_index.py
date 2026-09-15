"""
In-memory visual vector index cache with optional disk snapshot (Phase 5F).

Visual vectors stay separate from E5 text vectors. FAISS/NumPy are optional.
Top-level imports are stdlib-only so lightweight tests can import this module
without SQLAlchemy/FastAPI/numpy/faiss.

Persistence (VISUAL_INDEX_PERSISTENCE_ENABLED) defaults to false.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence, Tuple

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

SNAPSHOT_VERSION = 1
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._-]+")


@dataclass
class VisualVectorRowRecord:
    """Duck-typed row used by cache / search / disk snapshot (not an ORM object)."""

    id: int
    attachment_id: int
    entry_id: int
    image_ref: Optional[str] = None
    page_no: Optional[int] = None
    metadata_json: Optional[Dict[str, Any]] = None
    embedding_dim: int = 0
    updated_at_iso: str = ""


@dataclass
class VisualVectorHit:
    row: Any
    score: float
    retrieval_method: str


@dataclass
class _VisualVectorCache:
    signature: str
    rows: List[Any]
    vectors: List[List[float]]
    faiss_index: Any = None
    numpy_matrix: Any = None
    loaded_from_disk: bool = False
    embedding_dim: int = 0
    user_id: int = 0
    provider: str = ""
    model: str = ""


_CACHE: Dict[Tuple[int, str, str], _VisualVectorCache] = {}


def reset_visual_vector_index_cache(user_id: Optional[int] = None) -> None:
    if user_id is None:
        _CACHE.clear()
        return
    for key in list(_CACHE):
        if key[0] == int(user_id):
            _CACHE.pop(key, None)


def _persistence_settings() -> Tuple[bool, str]:
    """Return (enabled, cache_dir). Never raises; defaults to disabled."""
    try:
        from app.config import settings

        enabled = bool(getattr(settings, "visual_index_persistence_enabled", False))
        cache_dir = str(
            getattr(settings, "visual_index_cache_dir", None) or "vector_indexes/visual"
        )
        return enabled, cache_dir
    except Exception:
        return False, "vector_indexes/visual"


def get_visual_vector_index_cache_stats() -> Dict[str, Any]:
    enabled, cache_dir = _persistence_settings()
    return {
        "cache_count": len(_CACHE),
        "persistence_enabled": enabled,
        "persistence_cache_dir": cache_dir,
        "keys": [
            {
                "user_id": key[0],
                "provider": key[1],
                "model": key[2],
                "vector_count": len(cache.vectors),
                "signature": cache.signature,
                "index_type": "faiss" if cache.faiss_index is not None else (
                    "numpy" if cache.numpy_matrix is not None else "python"
                ),
                "loaded_from_disk": bool(getattr(cache, "loaded_from_disk", False)),
            }
            for key, cache in _CACHE.items()
        ],
    }


def sanitize_visual_index_token(value: str, *, max_len: int = 64) -> str:
    """Make provider/model safe for filenames."""
    text = (value or "").strip() or "unknown"
    text = _SAFE_TOKEN_RE.sub("_", text)
    text = text.strip("._-") or "unknown"
    if len(text) > max_len:
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        text = f"{text[: max_len - 11]}_{digest}"
    return text


def visual_index_snapshot_filename(
    user_id: int,
    provider: str,
    model: str,
) -> str:
    safe_provider = sanitize_visual_index_token(provider)
    safe_model = sanitize_visual_index_token(model)
    return f"u{int(user_id)}_{safe_provider}_{safe_model}.json"


def visual_index_snapshot_path(
    cache_dir: str,
    user_id: int,
    provider: str,
    model: str,
) -> Path:
    return Path(cache_dir) / visual_index_snapshot_filename(user_id, provider, model)


def _parse_vector_json(raw: object, expected_dim: int) -> Optional[List[float]]:
    try:
        if isinstance(raw, list):
            vector = [float(item) for item in raw]
        elif isinstance(raw, str):
            vector = [float(item) for item in json.loads(raw)]
        else:
            return None
        if expected_dim > 0 and len(vector) != expected_dim:
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


def _row_to_record(row: Any) -> VisualVectorRowRecord:
    updated = getattr(row, "updated_at", None)
    if updated is not None and hasattr(updated, "isoformat"):
        updated_iso = updated.isoformat()
    else:
        updated_iso = str(updated or "")
    meta = getattr(row, "metadata_json", None)
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    if not isinstance(meta, dict):
        meta = meta if meta is None else {}
    return VisualVectorRowRecord(
        id=int(getattr(row, "id", 0) or 0),
        attachment_id=int(row.attachment_id),
        entry_id=int(row.entry_id),
        image_ref=getattr(row, "image_ref", None),
        page_no=getattr(row, "page_no", None),
        metadata_json=meta if isinstance(meta, dict) else {},
        embedding_dim=int(getattr(row, "embedding_dim", 0) or 0),
        updated_at_iso=updated_iso,
    )


def _get_attachment_visual_embedding_model():
    from app.models import AttachmentVisualEmbedding

    return AttachmentVisualEmbedding


def _load_rows(
    db: "Session",
    *,
    user_id: int,
    provider: str,
    model: str,
) -> List[Any]:
    AttachmentVisualEmbedding = _get_attachment_visual_embedding_model()
    return (
        db.query(AttachmentVisualEmbedding)
        .filter(
            AttachmentVisualEmbedding.user_id == user_id,
            AttachmentVisualEmbedding.provider == provider,
            AttachmentVisualEmbedding.model == model,
        )
        .all()
    )


def _signature(rows: Sequence[Any]) -> str:
    parts = []
    for row in rows:
        if isinstance(row, VisualVectorRowRecord):
            updated = row.updated_at_iso
            row_id = row.id
            dim = row.embedding_dim
        else:
            updated_at = getattr(row, "updated_at", None)
            updated = updated_at.isoformat() if updated_at is not None and hasattr(updated_at, "isoformat") else str(updated_at or "")
            row_id = int(getattr(row, "id", 0) or 0)
            dim = int(getattr(row, "embedding_dim", 0) or 0)
        parts.append(f"{row_id}:{dim}:{updated}")
    return "|".join(parts)


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
        logger.debug("visual FAISS index unavailable: %s", exc)
        return None


def build_cache_from_records(
    *,
    user_id: int,
    provider: str,
    model: str,
    embedding_dim: int,
    signature: str,
    records: Sequence[VisualVectorRowRecord],
    vectors: Sequence[Sequence[float]],
    loaded_from_disk: bool = False,
) -> _VisualVectorCache:
    """Pure helper: build cache object from duck-typed records + vectors."""
    normalized = [_normalize(vector) for vector in vectors]
    matrix = _try_build_numpy_matrix(normalized) if normalized else None
    return _VisualVectorCache(
        signature=signature,
        rows=list(records),
        vectors=normalized,
        numpy_matrix=matrix,
        faiss_index=_try_build_faiss_index(matrix),
        loaded_from_disk=loaded_from_disk,
        embedding_dim=int(embedding_dim),
        user_id=int(user_id),
        provider=str(provider),
        model=str(model),
    )


def serialize_visual_index_snapshot(cache: _VisualVectorCache) -> Dict[str, Any]:
    records_payload = []
    for row, vector in zip(cache.rows, cache.vectors):
        record = row if isinstance(row, VisualVectorRowRecord) else _row_to_record(row)
        records_payload.append({
            "id": int(record.id),
            "attachment_id": int(record.attachment_id),
            "entry_id": int(record.entry_id),
            "image_ref": record.image_ref,
            "page_no": record.page_no,
            "metadata_json": record.metadata_json if isinstance(record.metadata_json, dict) else {},
            "embedding_dim": int(record.embedding_dim or cache.embedding_dim or 0),
            "updated_at_iso": record.updated_at_iso,
            "vector": [float(v) for v in vector],
        })
    return {
        "version": SNAPSHOT_VERSION,
        "user_id": int(cache.user_id),
        "provider": cache.provider,
        "model": cache.model,
        "signature": cache.signature,
        "embedding_dim": int(cache.embedding_dim),
        "records": records_payload,
    }


def load_visual_index_snapshot(
    path: Path | str,
    *,
    expected_signature: Optional[str] = None,
    expected_user_id: Optional[int] = None,
    expected_provider: Optional[str] = None,
    expected_model: Optional[str] = None,
    expected_embedding_dim: Optional[int] = None,
) -> Optional[_VisualVectorCache]:
    """
    Load snapshot from disk. Returns None on mismatch/corruption (never raises).
    """
    try:
        snapshot_path = Path(path)
        if not snapshot_path.is_file():
            return None
        raw = snapshot_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return None
        if int(payload.get("version") or 0) != SNAPSHOT_VERSION:
            return None
        signature = str(payload.get("signature") or "")
        if expected_signature is not None and signature != expected_signature:
            return None
        user_id = int(payload.get("user_id"))
        provider = str(payload.get("provider") or "")
        model = str(payload.get("model") or "")
        embedding_dim = int(payload.get("embedding_dim") or 0)
        if expected_user_id is not None and user_id != int(expected_user_id):
            return None
        if expected_provider is not None and provider != expected_provider:
            return None
        if expected_model is not None and model != expected_model:
            return None
        if expected_embedding_dim is not None and embedding_dim != int(expected_embedding_dim):
            return None

        records_raw = payload.get("records")
        if not isinstance(records_raw, list):
            return None

        records: List[VisualVectorRowRecord] = []
        vectors: List[List[float]] = []
        for item in records_raw:
            if not isinstance(item, dict):
                return None
            vector = item.get("vector")
            if not isinstance(vector, list) or not vector:
                return None
            parsed = [float(v) for v in vector]
            if embedding_dim > 0 and len(parsed) != embedding_dim:
                return None
            records.append(VisualVectorRowRecord(
                id=int(item.get("id") or 0),
                attachment_id=int(item["attachment_id"]),
                entry_id=int(item["entry_id"]),
                image_ref=item.get("image_ref"),
                page_no=item.get("page_no"),
                metadata_json=item.get("metadata_json") if isinstance(item.get("metadata_json"), dict) else {},
                embedding_dim=int(item.get("embedding_dim") or embedding_dim or 0),
                updated_at_iso=str(item.get("updated_at_iso") or ""),
            ))
            vectors.append(_normalize(parsed))

        return build_cache_from_records(
            user_id=user_id,
            provider=provider,
            model=model,
            embedding_dim=embedding_dim,
            signature=signature,
            records=records,
            vectors=vectors,
            loaded_from_disk=True,
        )
    except Exception as exc:
        logger.debug("visual index snapshot load failed path=%s error=%s", path, exc)
        return None


def save_visual_index_snapshot(
    path: Path | str,
    cache: _VisualVectorCache,
) -> bool:
    """Atomically write snapshot. Failures return False and never raise to callers."""
    try:
        snapshot_path = Path(path)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        payload = serialize_visual_index_snapshot(cache)
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
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
        logger.warning("visual index snapshot save failed path=%s error=%s", path, exc)
        return False


def search_cached_visual_vectors(
    query_vector: Sequence[float],
    rows: Sequence[Any],
    vectors: Sequence[Sequence[float]],
    *,
    top_k: int = 5,
    numpy_matrix: Any = None,
    faiss_index: Any = None,
) -> List[VisualVectorHit]:
    """Pure-ish search over cached rows/vectors. Prefer FAISS -> NumPy -> Python."""
    if top_k <= 0 or not rows or not vectors:
        return []

    normalized_query = _normalize(query_vector)

    if faiss_index is not None and numpy_matrix is not None:
        try:
            import numpy as np  # type: ignore

            query_matrix = np.asarray([normalized_query], dtype="float32")
            scores, indices = faiss_index.search(query_matrix, min(top_k, len(rows)))
            hits = []
            for score, idx in zip(scores[0], indices[0]):
                if int(idx) < 0:
                    continue
                hits.append(VisualVectorHit(
                    row=rows[int(idx)],
                    score=float(score),
                    retrieval_method="visual_faiss",
                ))
            if hits:
                return hits
        except Exception as exc:
            logger.debug("visual FAISS search failed: %s", exc)

    if numpy_matrix is not None:
        try:
            import numpy as np  # type: ignore

            query_matrix = np.asarray(normalized_query, dtype="float32")
            scores = numpy_matrix @ query_matrix
            order = scores.argsort()[::-1][:top_k]
            return [
                VisualVectorHit(
                    row=rows[int(idx)],
                    score=float(scores[int(idx)]),
                    retrieval_method="visual_numpy",
                )
                for idx in order
            ]
        except Exception as exc:
            logger.debug("visual NumPy search failed: %s", exc)

    scored = [
        VisualVectorHit(row=row, score=_dot(normalized_query, vector), retrieval_method="visual_python")
        for row, vector in zip(rows, vectors)
    ]
    scored.sort(key=lambda item: item.score, reverse=True)
    return scored[:top_k]


def _build_cache(
    db: "Session",
    *,
    user_id: int,
    provider: str,
    model: str,
    embedding_dim: int,
) -> _VisualVectorCache:
    raw_rows = _load_rows(db, user_id=user_id, provider=provider, model=model)
    records: List[VisualVectorRowRecord] = []
    vectors: List[List[float]] = []
    for row in raw_rows:
        vector = _parse_vector_json(getattr(row, "vector_json", None), embedding_dim)
        if vector is None:
            logger.warning(
                "skip invalid visual embedding attachment_id=%s",
                getattr(row, "attachment_id", None),
            )
            continue
        records.append(_row_to_record(row))
        vectors.append(_normalize(vector))

    return build_cache_from_records(
        user_id=user_id,
        provider=provider,
        model=model,
        embedding_dim=embedding_dim,
        signature=_signature(raw_rows),
        records=records,
        vectors=vectors,
        loaded_from_disk=False,
    )


def _get_or_build_cache(
    db: "Session",
    *,
    user_id: int,
    provider: str,
    model: str,
    embedding_dim: int,
) -> _VisualVectorCache:
    key = (int(user_id), provider, model)
    raw_rows = _load_rows(db, user_id=user_id, provider=provider, model=model)
    current_signature = _signature(raw_rows)
    cached = _CACHE.get(key)
    if cached and cached.signature == current_signature:
        return cached

    persistence_enabled, cache_dir = _persistence_settings()
    if persistence_enabled:
        snapshot_path = visual_index_snapshot_path(cache_dir, user_id, provider, model)
        restored = load_visual_index_snapshot(
            snapshot_path,
            expected_signature=current_signature,
            expected_user_id=user_id,
            expected_provider=provider,
            expected_model=model,
            expected_embedding_dim=embedding_dim,
        )
        if restored is not None:
            _CACHE[key] = restored
            return restored

    cache = _build_cache(
        db,
        user_id=user_id,
        provider=provider,
        model=model,
        embedding_dim=embedding_dim,
    )
    cache.signature = current_signature
    _CACHE[key] = cache

    if persistence_enabled:
        try:
            snapshot_path = visual_index_snapshot_path(cache_dir, user_id, provider, model)
            ok = save_visual_index_snapshot(snapshot_path, cache)
            if not ok:
                logger.debug(
                    "visual index snapshot write skipped user_id=%s provider=%s",
                    user_id,
                    provider,
                )
        except Exception as exc:
            logger.warning(
                "visual index snapshot write wrapper failed user_id=%s error=%s",
                user_id,
                exc,
            )

    return cache


def warm_visual_vector_index_cache(
    db: Any = None,
    *,
    user_id: Optional[int] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Pre-build visual vector caches for discovered (user, provider, model, dim) groups.

    Failures are counted in stats and never raised. Persistence writes only occur
    when VISUAL_INDEX_PERSISTENCE_ENABLED is true (via _get_or_build_cache).
    """
    stats: Dict[str, Any] = {
        "warmed": 0,
        "skipped": 0,
        "failed": 0,
        "groups": 0,
        "error": None,
    }
    if db is None:
        stats["skipped"] += 1
        stats["error"] = "db_missing"
        return stats

    try:
        AttachmentVisualEmbedding = _get_attachment_visual_embedding_model()
    except Exception as exc:
        stats["failed"] += 1
        stats["error"] = f"model_import_failed:{type(exc).__name__}"
        return stats

    try:
        q = db.query(
            AttachmentVisualEmbedding.user_id,
            AttachmentVisualEmbedding.provider,
            AttachmentVisualEmbedding.model,
            AttachmentVisualEmbedding.embedding_dim,
        )
        if user_id is not None:
            q = q.filter(AttachmentVisualEmbedding.user_id == int(user_id))
        if provider is not None:
            q = q.filter(AttachmentVisualEmbedding.provider == provider)
        if model is not None:
            q = q.filter(AttachmentVisualEmbedding.model == model)
        groups = q.distinct().all()
    except Exception as exc:
        logger.warning("visual index warm group discovery failed: %s", exc)
        stats["failed"] += 1
        stats["error"] = type(exc).__name__
        try:
            db.rollback()
        except Exception:
            logger.debug("rollback after visual warm discovery failure skipped", exc_info=True)
        return stats

    if not groups:
        stats["skipped"] += 1
        return stats

    stats["groups"] = len(groups)
    for group in groups:
        try:
            uid = int(group[0])
            prov = str(group[1])
            mdl = str(group[2])
            dim = int(group[3] or 0)
            if dim <= 0 or not prov or not mdl:
                stats["skipped"] += 1
                continue
            cache = _get_or_build_cache(
                db,
                user_id=uid,
                provider=prov,
                model=mdl,
                embedding_dim=dim,
            )
            if cache is None:
                stats["failed"] += 1
            elif not cache.vectors:
                stats["skipped"] += 1
            else:
                stats["warmed"] += 1
        except Exception as exc:
            logger.warning("visual index warm failed group=%s error=%s", group, exc)
            stats["failed"] += 1

    return stats


def search_visual_embeddings_index(
    db: "Session",
    *,
    user_id: int,
    provider: str,
    model: str,
    embedding_dim: int,
    query_vector: List[float],
    top_k: int = 5,
) -> List[VisualVectorHit]:
    if top_k <= 0 or embedding_dim <= 0 or len(query_vector) != embedding_dim:
        return []

    try:
        cache = _get_or_build_cache(
            db,
            user_id=user_id,
            provider=provider,
            model=model,
            embedding_dim=embedding_dim,
        )
    except Exception as exc:
        logger.warning("visual vector index build failed user_id=%s error=%s", user_id, exc)
        try:
            db.rollback()
        except Exception:
            logger.debug("rollback after visual index build failure skipped", exc_info=True)
        return []

    if not cache.rows:
        return []

    return search_cached_visual_vectors(
        query_vector,
        cache.rows,
        cache.vectors,
        top_k=top_k,
        numpy_matrix=cache.numpy_matrix,
        faiss_index=cache.faiss_index,
    )

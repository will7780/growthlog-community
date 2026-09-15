"""
Conservative visual late fusion for Hybrid RAG (Phase 5E).

Stdlib-only at module top so lightweight tests can run without
FastAPI/SQLAlchemy/numpy/faiss. Visual vectors stay in attachment_visual_embeddings;
this module only appends normalized visual hits after text fusion.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

DEFAULT_VISUAL_TOP_K = 3
DEFAULT_VISUAL_MIN_SCORE = 0.25


def is_visual_late_fusion_enabled(settings_obj: Any = None) -> bool:
    """Read RAG_ENABLE_VISUAL_LATE_FUSION; default False on any failure."""
    try:
        cfg = settings_obj
        if cfg is None:
            from app.config import settings as cfg
        return bool(getattr(cfg, "rag_enable_visual_late_fusion", False))
    except Exception:
        return False


def get_visual_late_fusion_params(settings_obj: Any = None) -> Dict[str, Any]:
    """Return enabled / top_k / min_score with safe defaults."""
    enabled = False
    top_k = DEFAULT_VISUAL_TOP_K
    min_score = DEFAULT_VISUAL_MIN_SCORE
    try:
        cfg = settings_obj
        if cfg is None:
            from app.config import settings as cfg
        enabled = bool(getattr(cfg, "rag_enable_visual_late_fusion", False))
        raw_top_k = getattr(cfg, "rag_visual_top_k", DEFAULT_VISUAL_TOP_K)
        raw_min = getattr(cfg, "rag_visual_min_score", DEFAULT_VISUAL_MIN_SCORE)
        top_k = max(int(raw_top_k), 0)
        min_score = float(raw_min)
    except Exception:
        return {
            "enabled": False,
            "top_k": DEFAULT_VISUAL_TOP_K,
            "min_score": DEFAULT_VISUAL_MIN_SCORE,
        }
    return {"enabled": enabled, "top_k": top_k, "min_score": min_score}


def normalize_visual_hit_to_reference(hit: Any) -> Optional[Dict[str, Any]]:
    """
    Convert a VisualSearchResult-like object/dict into a RAG reference dict.

    Required-compatible fields: source_type, source_id/attachment_id,
    chunk_id (nullable), page_no (nullable), metadata with retrieval_method,
    provider, model, modality=visual.
    """
    if hit is None:
        return None

    if isinstance(hit, dict):
        attachment_id = hit.get("attachment_id") or hit.get("source_id")
        entry_id = hit.get("entry_id")
        page_no = hit.get("page_no")
        score = float(hit.get("score") or hit.get("relevance_score") or 0.0)
        image_ref = hit.get("image_ref")
        raw_meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    else:
        attachment_id = getattr(hit, "attachment_id", None)
        entry_id = getattr(hit, "entry_id", None)
        page_no = getattr(hit, "page_no", None)
        score = float(getattr(hit, "score", 0.0) or 0.0)
        image_ref = getattr(hit, "image_ref", None)
        raw_meta = getattr(hit, "metadata", None)
        raw_meta = raw_meta if isinstance(raw_meta, dict) else {}

    if attachment_id is None:
        return None

    try:
        attachment_id_int = int(attachment_id)
    except (TypeError, ValueError):
        return None

    provider = str(raw_meta.get("provider") or "unknown")
    model = str(raw_meta.get("model") or "unknown")
    retrieval_method = str(raw_meta.get("retrieval_method") or "visual")

    title = f"视觉附件 #{attachment_id_int}"
    snippet = str(image_ref or raw_meta.get("original_filename") or title)

    metadata = {
        **raw_meta,
        "retrieval_method": retrieval_method,
        "provider": provider,
        "model": model,
        "modality": "visual",
        "visual": True,
    }
    if image_ref is not None:
        metadata.setdefault("image_ref", image_ref)

    return {
        "source_type": "attachment_visual",
        "source_id": attachment_id_int,
        "attachment_id": attachment_id_int,
        "entry_id": int(entry_id) if entry_id is not None else None,
        "chunk_id": None,
        "page_no": page_no,
        "title": title,
        "label_name": "",
        "created_at": None,
        "snippet": snippet[:260],
        "content": snippet,
        "relevance_score": score,
        "retrieval_method": retrieval_method,
        "source_reason": "visual_late_fusion",
        "metadata": metadata,
    }


def merge_visual_late_fusion(
    text_references: Sequence[Dict[str, Any]],
    visual_references: Sequence[Dict[str, Any]],
    *,
    max_visual: int = DEFAULT_VISUAL_TOP_K,
) -> List[Dict[str, Any]]:
    """
    Append visual refs after text refs without displacing high-scoring text hits.

    Dedupes by attachment_id / source_id already present in text results.
    """
    out: List[Dict[str, Any]] = [dict(item) for item in text_references]
    if max_visual <= 0 or not visual_references:
        return out

    seen_attachment_ids = set()
    for item in out:
        aid = item.get("attachment_id")
        if aid is None and item.get("source_type") in ("attachment_chunk", "attachment", "attachment_visual"):
            aid = item.get("source_id")
        if aid is not None:
            try:
                seen_attachment_ids.add(int(aid))
            except (TypeError, ValueError):
                pass

    added = 0
    for visual in visual_references:
        if added >= max_visual:
            break
        aid = visual.get("attachment_id", visual.get("source_id"))
        try:
            aid_int = int(aid) if aid is not None else None
        except (TypeError, ValueError):
            continue
        if aid_int is None or aid_int in seen_attachment_ids:
            continue
        seen_attachment_ids.add(aid_int)
        out.append(dict(visual))
        added += 1

    return out


def maybe_append_visual_late_fusion(
    text_references: Sequence[Dict[str, Any]],
    *,
    enabled: bool,
    db: Any = None,
    user_id: Optional[int] = None,
    query: str = "",
    visual_top_k: int = DEFAULT_VISUAL_TOP_K,
    visual_min_score: float = DEFAULT_VISUAL_MIN_SCORE,
    search_fn: Optional[Callable[..., Any]] = None,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Optionally run visual search and late-fuse into text references.

    When disabled or on any visual failure, returns text_references unchanged.
    search_fn is injectable for tests (defaults to visual_rag.search_visual).
    """
    debug: Dict[str, Any] = {
        "visual_late_fusion_enabled": bool(enabled),
        "visual_count": 0,
        "visual_appended": 0,
        "visual_error": None,
    }
    text_list = [dict(item) for item in text_references]

    if not enabled:
        return text_list, debug

    if visual_top_k <= 0 or not (query or "").strip() or user_id is None:
        return text_list, debug

    try:
        fn = search_fn
        if fn is None:
            from app.services.visual_rag import search_visual as fn

        hits = fn(
            db,
            user_id=int(user_id),
            query=query,
            top_k=int(visual_top_k),
            min_score=float(visual_min_score),
        )
    except TypeError:
        # Older search_visual without min_score
        try:
            hits = fn(  # type: ignore[misc]
                db,
                user_id=int(user_id),
                query=query,
                top_k=int(visual_top_k),
            )
        except Exception as exc:
            logger.warning("visual late fusion search failed user_id=%s error=%s", user_id, exc)
            debug["visual_error"] = type(exc).__name__
            return text_list, debug
    except Exception as exc:
        logger.warning("visual late fusion search failed user_id=%s error=%s", user_id, exc)
        debug["visual_error"] = type(exc).__name__
        return text_list, debug

    visual_refs: List[Dict[str, Any]] = []
    for hit in hits or []:
        ref = normalize_visual_hit_to_reference(hit)
        if ref is None:
            continue
        if float(ref.get("relevance_score") or 0.0) < float(visual_min_score):
            continue
        visual_refs.append(ref)

    debug["visual_count"] = len(visual_refs)
    merged = merge_visual_late_fusion(text_list, visual_refs, max_visual=visual_top_k)
    debug["visual_appended"] = max(0, len(merged) - len(text_list))
    return merged, debug

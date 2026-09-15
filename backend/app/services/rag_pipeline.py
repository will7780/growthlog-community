"""
Hybrid RAG retrieval pipeline.

Phase 1 keeps the public API response compatible while moving retrieval behind
a source-agnostic pipeline: dense entries + keyword entries + attachment chunks
are normalized, fused with RRF and returned as reference dictionaries.

Phase 5E optionally appends visual late-fusion hits after text RRF/rerank
when RAG_ENABLE_VISUAL_LATE_FUSION=true (default false).
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.services.attachment_search import search_similar_attachment_chunks
from app.services.embedding import generate_embedding
from app.services.knowledge_rag import search_knowledge_chunks
from app.services.keyword_search import search_keyword_entries
from app.services.query_normalize import normalize_query, retrieval_query_text
from app.services.todo_knowledge import search_keyword_todos
from app.services.notion_knowledge import search_keyword_notion_pages
from app.services.rank_fusion import reciprocal_rank_fusion, result_key
from app.services.reference_identity import dedupe_by_evidence_origin
from app.services.reranker import rerank_references
from app.services.vector_search import initialize_user_index, search_similar_entries
from app.services.visual_late_fusion import (
    get_visual_late_fusion_params,
    maybe_append_visual_late_fusion,
)

logger = logging.getLogger(__name__)


@dataclass
class RagRetrievalDebug:
    dense_count: int = 0
    entry_dense_count: int = 0
    knowledge_dense_count: int = 0
    keyword_count: int = 0
    attachment_count: int = 0
    visual_count: int = 0
    fused_count: int = 0
    mode: str = "hybrid_rrf"
    visual_late_fusion_enabled: bool = False
    query_normalize: Dict = field(default_factory=dict)
    channel_status: Dict = field(default_factory=dict)
    degraded: bool = False
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "mode": self.mode,
            "dense_count": self.dense_count,
            "entry_dense_count": self.entry_dense_count,
            "knowledge_dense_count": self.knowledge_dense_count,
            "keyword_count": self.keyword_count,
            "attachment_count": self.attachment_count,
            "visual_count": self.visual_count,
            "fused_count": self.fused_count,
            "visual_late_fusion_enabled": self.visual_late_fusion_enabled,
            "query_normalize": self.query_normalize,
            "channel_status": self.channel_status,
            "degraded": self.degraded,
            "errors": self.errors,
        }


def _title_and_snippet(content: str, max_len: int = 260) -> tuple[str, str]:
    title = (content or "").split("\n")[0].strip()
    body = (content or "").replace(title, "", 1).strip() if title else (content or "").strip()
    return title[:120], body[:max_len]


def _normalize_dense_entry_from_hit(result: Dict) -> Optional[Dict]:
    """
    Normalize an already-hydrated dense hit (no extra DB round-trip).

    R5.2.3: search_similar_entries bulk-hydrates; never call get_entry_with_label here.
    """
    if result.get("entry_id") is None:
        return None
    if result.get("content") is None and result.get("label_code") is None:
        # Missing hydrate payload — skip rather than N+1 query.
        return None
    content = result.get("content") or ""
    title, snippet = _title_and_snippet(content)
    eid = int(result["entry_id"])
    return {
        "source_type": "entry",
        "source_id": eid,
        "entry_id": eid,
        "chunk_id": None,
        "title": title or f"记录 #{eid}",
        "label_name": result.get("label_name") or result.get("label_code") or "",
        "created_at": result.get("created_at"),
        "snippet": snippet,
        "content": content,
        "relevance_score": float(result.get("relevance_score", 0.0)),
        "retrieval_method": "dense",
        "source_reason": "semantic_match",
        "channel_rank": result.get("channel_rank"),
        "dense_rank": result.get("dense_rank"),
        "metadata": {
            "label_code": result.get("label_code"),
        },
    }


def _normalize_dense_entry(db: Session, user_id: int, result: Dict) -> Optional[Dict]:
    """Compat shim: must not issue per-row DB lookups (R5.2.3)."""
    _ = (db, user_id)
    return _normalize_dense_entry_from_hit(result)


def _normalize_attachment(item: Dict) -> Dict:
    normalized = dict(item)
    normalized["source_id"] = item.get("attachment_id")
    normalized["retrieval_method"] = item.get("retrieval_method", "attachment_dense")
    normalized["source_reason"] = item.get("source_reason", "attachment_semantic_match")
    metadata = dict(normalized.get("metadata") or {})
    if not metadata.get("filename"):
        title = str(normalized.get("title") or "")
        # title may be "file.pdf（第 1 页）" — keep bare filename for UI.
        bare = title.split("（", 1)[0].strip()
        if bare:
            metadata["filename"] = bare
    normalized["metadata"] = metadata
    return normalized


# Production RRF constants (keep eval hybrid aligned).
RRF_K = 60
RRF_WEIGHTS = [1.0, 0.9, 0.85]


def fetch_dense_channel_refs(
    db: Session,
    *,
    user_id: int,
    query_vector: List[float],
    top_k: int,
    label_code: Optional[str] = None,
) -> Tuple[List[Dict], List[Dict], Dict[str, str], Dict[str, float]]:
    """
    Independently run knowledge dense and entry dense.
    Never skip entry dense because knowledge returned hits.
    Channel failures are marked degraded/infra — not silent zero.
    R5.2.3: entry dense uses bulk hydrate (no per-hit N+1).
    """
    knowledge_refs: List[Dict] = []
    entry_refs: List[Dict] = []
    status: Dict[str, str] = {
        "knowledge_dense": "ok",
        "entry_dense": "ok",
    }
    timings: Dict[str, float] = {"knowledge_dense": 0.0, "entry_dense": 0.0}

    def _knowledge() -> List[Dict]:
        return list(
            search_knowledge_chunks(
                db=db,
                user_id=user_id,
                query_vector=query_vector,
                top_k=top_k,
                label_code=label_code,
            )
            or []
        )

    def _entry() -> List[Dict]:
        dense_results = search_similar_entries(
            db=db,
            query_vector=query_vector,
            user_id=user_id,
            top_k=top_k,
            label_code=label_code,
        )
        out: List[Dict] = []
        for rank, result in enumerate(dense_results or [], start=1):
            item = dict(result)
            item["channel_rank"] = rank
            item["dense_rank"] = rank
            ref = _normalize_dense_entry_from_hit(item)
            if ref:
                out.append(ref)
        return out

    # Independent sequential execution (shared Session is not thread-safe).
    t0 = time.perf_counter()
    try:
        knowledge_refs = _knowledge()
    except Exception as exc:
        status["knowledge_dense"] = f"degraded:{type(exc).__name__}"
        logger.warning("knowledge_dense failed for user=%s: %s", user_id, exc)
    timings["knowledge_dense"] = round((time.perf_counter() - t0) * 1000.0, 2)

    t0 = time.perf_counter()
    try:
        entry_refs = _entry()
    except Exception as exc:
        status["entry_dense"] = f"degraded:{type(exc).__name__}"
        logger.warning("entry_dense failed for user=%s: %s", user_id, exc)
    timings["entry_dense"] = round((time.perf_counter() - t0) * 1000.0, 2)

    return knowledge_refs, entry_refs, status, timings


def fetch_dense_refs(
    db: Session,
    *,
    user_id: int,
    query_vector: List[float],
    top_k: int,
    label_code: Optional[str] = None,
) -> List[Dict]:
    """
    Production dense path: knowledge + entry dense in parallel, then origin dedupe.
    Shared by retrieve_rag_context and live eval.
    """
    knowledge_refs, entry_refs, _status, _timings = fetch_dense_channel_refs(
        db,
        user_id=user_id,
        query_vector=query_vector,
        top_k=top_k,
        label_code=label_code,
    )
    merged = list(knowledge_refs) + list(entry_refs)
    kept, _dropped = dedupe_by_evidence_origin(merged)
    return kept


def retrieve_rag_context(
    db: Session,
    *,
    user_id: int,
    query: str,
    top_k: int = 15,
    label_code: Optional[str] = None,
    dense_top_k: int = 30,
    keyword_top_k: int = 30,
    attachment_top_k: int = 10,
    include_attachments: bool = True,
    query_vector: Optional[List[float]] = None,
    generate_query_embedding: bool = True,
) -> Dict:
    debug = RagRetrievalDebug()
    nq = normalize_query(query)
    retrieval_query = retrieval_query_text(nq)
    debug.query_normalize = dict(nq.diagnostics)
    debug.query_normalize["synonym_hit_count"] = len(nq.synonym_hits)

    dense_refs: List[Dict] = []
    knowledge_refs: List[Dict] = []
    entry_dense_refs: List[Dict] = []
    keyword_refs: List[Dict] = []
    attachment_refs: List[Dict] = []
    channel_status: Dict[str, str] = {}
    reused_query_vector = query_vector is not None

    try:
        initialize_user_index(user_id, db)
    except Exception as exc:
        logger.warning("index init failed for user=%s: %s", user_id, exc)
        debug.errors.append(f"index_init:{type(exc).__name__}")
        channel_status["index_init"] = f"degraded:{type(exc).__name__}"
        debug.degraded = True

    if query_vector is None and generate_query_embedding:
        try:
            query_vector = generate_embedding(f"query: {retrieval_query}")
        except Exception as exc:
            logger.warning("query embedding failed for user=%s: %s", user_id, exc)
            debug.errors.append(f"embedding:{type(exc).__name__}")
            channel_status["embedding"] = f"degraded:{type(exc).__name__}"
            debug.degraded = True
            query_vector = None
    elif query_vector is None and not generate_query_embedding:
        channel_status["embedding"] = "skipped:caller_degraded"
        debug.degraded = True

    try:
        if query_vector is None:
            raise RuntimeError("no_query_vector")
        knowledge_refs, entry_dense_refs, dense_status, _dense_timings = fetch_dense_channel_refs(
            db,
            user_id=user_id,
            query_vector=query_vector,
            top_k=dense_top_k,
            label_code=label_code,
        )
        channel_status.update(dense_status)
        merged_dense = list(knowledge_refs) + list(entry_dense_refs)
        dense_refs, _ = dedupe_by_evidence_origin(merged_dense)
        debug.knowledge_dense_count = len(knowledge_refs)
        debug.entry_dense_count = len(entry_dense_refs)
        debug.dense_count = len(dense_refs)
    except Exception as exc:
        logger.warning("dense retrieval failed for user=%s: %s", user_id, exc)
        debug.errors.append(f"dense:{type(exc).__name__}")
        channel_status["dense"] = f"degraded:{type(exc).__name__}"
        debug.degraded = True

    try:
        keyword_refs = search_keyword_entries(
            db=db,
            user_id=user_id,
            query=retrieval_query,
            top_k=keyword_top_k,
            label_code=label_code,
        )
        keyword_refs.extend(
            search_keyword_todos(
                db,
                user_id=user_id,
                terms=list(nq.expanded_terms),
                top_k=keyword_top_k,
            )
        )
        keyword_refs.extend(
            search_keyword_notion_pages(
                db,
                user_id=user_id,
                terms=list(nq.expanded_terms),
                top_k=keyword_top_k,
            )
        )
        debug.keyword_count = len(keyword_refs)
        channel_status["keyword"] = "ok"
    except Exception as exc:
        logger.warning("keyword retrieval failed for user=%s: %s", user_id, exc)
        debug.errors.append(f"keyword:{type(exc).__name__}")
        channel_status["keyword"] = f"degraded:{type(exc).__name__}"
        debug.degraded = True

    if include_attachments and query_vector is not None:
        try:
            attachment_refs = [
                _normalize_attachment(item)
                for item in search_similar_attachment_chunks(
                    db=db,
                    query_vector=query_vector,
                    user_id=user_id,
                    top_k=attachment_top_k,
                )
            ]
            attachment_refs, _ = dedupe_by_evidence_origin(attachment_refs)
            debug.attachment_count = len(attachment_refs)
            channel_status["attachment_dense"] = "ok"
        except Exception as exc:
            logger.warning("attachment retrieval failed for user=%s: %s", user_id, exc)
            debug.errors.append(f"attachment:{type(exc).__name__}")
            channel_status["attachment_dense"] = f"degraded:{type(exc).__name__}"
            debug.degraded = True
    elif include_attachments and query_vector is None:
        channel_status["attachment_dense"] = "skipped:no_query_vector"
        debug.degraded = True

    if any(str(v).startswith("degraded:") for v in channel_status.values()):
        debug.degraded = True
    debug.channel_status = channel_status
    debug.query_normalize["query_vector_reused"] = reused_query_vector

    fused = reciprocal_rank_fusion(
        [dense_refs, keyword_refs, attachment_refs],
        k=RRF_K,
        weights=list(RRF_WEIGHTS),
        top_k=top_k,
    )
    fused, _ = dedupe_by_evidence_origin(fused)
    fused = rerank_references(retrieval_query, fused, top_k)

    visual_params = get_visual_late_fusion_params()
    debug.visual_late_fusion_enabled = bool(visual_params.get("enabled"))
    visual_refs: List[Dict] = []
    if debug.visual_late_fusion_enabled:
        try:
            fused, visual_debug = maybe_append_visual_late_fusion(
                fused,
                enabled=True,
                db=db,
                user_id=user_id,
                query=query,
                visual_top_k=int(visual_params.get("top_k") or 0),
                visual_min_score=float(visual_params.get("min_score") or 0.0),
            )
            debug.visual_count = int(visual_debug.get("visual_appended") or 0)
            if visual_debug.get("visual_error"):
                debug.errors.append(f"visual:{visual_debug['visual_error']}")
                logger.debug(
                    "visual late fusion skipped user_id=%s error=%s",
                    user_id,
                    visual_debug.get("visual_error"),
                )
            # Expose appended visual refs for callers/tests without mixing into dense lists.
            text_len = max(0, len(fused) - debug.visual_count)
            visual_refs = list(fused[text_len:])
        except Exception as exc:
            logger.warning("visual late fusion wrapper failed user_id=%s: %s", user_id, exc)
            debug.errors.append(f"visual:{type(exc).__name__}")

    debug.fused_count = len(fused)

    # Option B: child entry / child-attachment hits carry parent/root for cite + LLM.
    from app.services.entry_family import enrich_reference_lists

    fused, dense_refs, keyword_refs, attachment_refs, visual_refs = enrich_reference_lists(
        db,
        user_id,
        fused,
        dense_refs,
        keyword_refs,
        attachment_refs,
        visual_refs,
        inject_parent_into_content=True,
    )

    return {
        "references": fused,
        "dense_results": dense_refs,
        "keyword_results": keyword_refs,
        "attachment_results": attachment_refs,
        "visual_results": visual_refs,
        "retrieval_debug": debug.to_dict(),
    }


# Generation judge pool quotas (does not change production RRF k/weights).
# Dense-first per D.4 entry conclusion; fused/keyword/attachment still present.
GENERATION_POOL_QUOTAS = {
    "dense": 16,
    "fused": 12,
    "keyword": 6,
    "attachment": 6,
}


def expand_references_for_generation(
    rag_context: Dict,
    *,
    max_refs: int = 40,
    quotas: Optional[Dict[str, int]] = None,
) -> List[Dict]:
    """
    Build a source-quota generation judge pool without changing RRF weights/k.

    Priority order: entry-dense → fused → keyword → attachment.
    Quotas prevent fused[:30] from starving dense evidence.
    Dedupes by reference_key then by evidence_origin_key (attachment preferred).
    """
    from app.services.reference_identity import dedupe_by_evidence_origin

    q = dict(GENERATION_POOL_QUOTAS)
    if quotas:
        q.update({k: int(v) for k, v in quotas.items()})
    buckets = {
        "dense": list(rag_context.get("dense_results") or []),
        "fused": list(rag_context.get("references") or []),
        "keyword": list(rag_context.get("keyword_results") or []),
        "attachment": list(rag_context.get("attachment_results") or []),
    }
    order = ("dense", "fused", "keyword", "attachment")
    seen_keys: set = set()
    selected: List[Dict] = []
    leftovers: List[Dict] = []

    def _take(bucket_name: str, limit: int) -> None:
        nonlocal selected
        taken = 0
        for ref in buckets.get(bucket_name) or []:
            if not isinstance(ref, dict):
                continue
            key = result_key(ref)
            if key in seen_keys:
                continue
            item = dict(ref)
            item["source_reason"] = item.get("source_reason") or f"{bucket_name}_quota"
            item["pool_bucket"] = bucket_name
            if taken < limit and len(selected) < max_refs:
                seen_keys.add(key)
                selected.append(item)
                taken += 1
            else:
                leftovers.append(item)

    for name in order:
        _take(name, int(q.get(name) or 0))

    for ref in leftovers:
        if len(selected) >= max_refs:
            break
        key = result_key(ref)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        item = dict(ref)
        item["source_reason"] = item.get("source_reason") or "quota_fill"
        selected.append(item)

    kept, _dropped = dedupe_by_evidence_origin(selected[:max_refs])
    return kept[:max_refs]

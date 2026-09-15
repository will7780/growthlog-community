"""
Live read-only RAG retrieval for Phase D.3 baseline eval.

Reuses production search / RRF helpers. Does not rebuild embeddings or indexes.
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from app.eval.rag_metrics import dedupe_ranked_keys
from app.services.embedding import (
    LocalModelMissingError,
    generate_embedding,
    local_model_available,
)
from app.services.keyword_search import search_keyword_entries
from app.services.rag_pipeline import (
    RRF_K,
    RRF_WEIGHTS,
    fetch_dense_refs,
    retrieve_rag_context,
)
from app.services.rank_fusion import result_key
from app.services.vector_search import initialize_user_index

E_LOCAL_MODEL_MISSING = "E_LOCAL_MODEL_MISSING"
E_EMBEDDING_FAILED = "E_EMBEDDING_FAILED"
E_INDEX_INIT_FAILED = "E_INDEX_INIT_FAILED"
E_KEYWORD_FAILED = "E_KEYWORD_FAILED"
E_DENSE_FAILED = "E_DENSE_FAILED"
E_HYBRID_FAILED = "E_HYBRID_FAILED"
E_HYBRID_DEGRADED = "E_HYBRID_DEGRADED"
E_DB_UNAVAILABLE = "E_DB_UNAVAILABLE"

BASELINE_TOP_K = 20
BASELINE_DENSE_POOL = 30
BASELINE_KEYWORD_POOL = 30
BASELINE_ATTACHMENT_POOL = 10


def enforce_baseline_env() -> Dict[str, Any]:
    """
    Harden process env for D.3 baseline.
    Caller must invoke before embedding/model load.
    """
    os.environ["RAG_EMBEDDING_ALLOW_DOWNLOAD"] = "0"
    rerank = (os.getenv("RAG_RERANK_MODEL") or "").strip()
    visual = (os.getenv("RAG_ENABLE_VISUAL_LATE_FUSION") or "").strip().lower()
    return {
        "rag_embedding_allow_download": "0",
        "rag_rerank_model_empty": rerank == "",
        "rag_rerank_model": rerank or None,
        "visual_late_fusion_env": visual or "",
        "visual_late_fusion_disabled": visual in {"", "0", "false", "no", "off"},
        "local_model_available": local_model_available(),
        "rrf_k": RRF_K,
        "rrf_weights": list(RRF_WEIGHTS),
        "top_k": BASELINE_TOP_K,
        "dense_pool": BASELINE_DENSE_POOL,
        "keyword_pool": BASELINE_KEYWORD_POOL,
    }


def refs_to_ranked_keys(refs: Sequence[Dict[str, Any]], *, top_k: int) -> List[str]:
    keys: List[str] = []
    for ref in refs:
        if not isinstance(ref, dict):
            continue
        keys.append(result_key(ref))
    return dedupe_ranked_keys(keys)[:top_k]


def warmup_retrieval(db: Session, *, user_id: int) -> Dict[str, Any]:
    """Index + embedding warmup; must not be billed to per-query latency."""
    t0 = time.perf_counter()
    try:
        initialize_user_index(user_id, db)
    except Exception as exc:
        return {
            "ok": False,
            "error_code": E_INDEX_INIT_FAILED,
            "error_type": type(exc).__name__,
            "warmup_ms": (time.perf_counter() - t0) * 1000.0,
        }
    try:
        _ = generate_embedding("query: warmup")
    except LocalModelMissingError:
        return {
            "ok": False,
            "error_code": E_LOCAL_MODEL_MISSING,
            "warmup_ms": (time.perf_counter() - t0) * 1000.0,
        }
    except Exception as exc:
        return {
            "ok": False,
            "error_code": E_EMBEDDING_FAILED,
            "error_type": type(exc).__name__,
            "warmup_ms": (time.perf_counter() - t0) * 1000.0,
        }
    return {
        "ok": True,
        "error_code": None,
        "warmup_ms": (time.perf_counter() - t0) * 1000.0,
    }


def retrieve_keyword_ranking(
    db: Session,
    *,
    user_id: int,
    query: str,
    top_k: int = BASELINE_TOP_K,
) -> Tuple[List[str], float, Optional[str]]:
    t0 = time.perf_counter()
    try:
        refs = search_keyword_entries(
            db=db,
            user_id=user_id,
            query=query,
            top_k=top_k,
        )
        keys = refs_to_ranked_keys(refs, top_k=top_k)
        return keys, (time.perf_counter() - t0) * 1000.0, None
    except Exception as exc:
        return [], (time.perf_counter() - t0) * 1000.0, f"{E_KEYWORD_FAILED}:{type(exc).__name__}"


def retrieve_vector_ranking(
    db: Session,
    *,
    user_id: int,
    query: str,
    top_k: int = BASELINE_TOP_K,
) -> Tuple[List[str], float, Optional[str]]:
    t0 = time.perf_counter()
    try:
        query_vector = generate_embedding(f"query: {query}")
        refs = fetch_dense_refs(
            db,
            user_id=user_id,
            query_vector=query_vector,
            top_k=top_k,
        )
        keys = refs_to_ranked_keys(refs, top_k=top_k)
        return keys, (time.perf_counter() - t0) * 1000.0, None
    except LocalModelMissingError:
        return [], (time.perf_counter() - t0) * 1000.0, E_LOCAL_MODEL_MISSING
    except Exception as exc:
        return [], (time.perf_counter() - t0) * 1000.0, f"{E_DENSE_FAILED}:{type(exc).__name__}"


def retrieve_hybrid_ranking(
    db: Session,
    *,
    user_id: int,
    query: str,
    top_k: int = BASELINE_TOP_K,
    rrf_k: int = RRF_K,
    rrf_weights: Optional[List[float]] = None,
    use_production_pipeline: bool = True,
) -> Tuple[List[str], float, Optional[str], Dict[str, Any]]:
    """
    Hybrid retrieval.

    Default: production retrieve_rag_context (same pools/RRF/rerank hooks).
    Optional custom rrf_k/weights via channel-wise fuse (eval tuning only;
    does not change retrieve_rag_context defaults).

    Any channel failure in debug.errors => E_HYBRID_DEGRADED. Diagnostic
    rankings may remain, but caller must exclude the case from quality means.
    """
    from app.services.attachment_search import search_similar_attachment_chunks
    from app.services.embedding import generate_embedding
    from app.services.keyword_search import search_keyword_entries
    from app.services.rank_fusion import reciprocal_rank_fusion
    from app.services.rag_pipeline import _normalize_attachment, fetch_dense_refs
    from app.services.reranker import rerank_references
    from app.services.vector_search import initialize_user_index

    weights = list(rrf_weights) if rrf_weights is not None else list(RRF_WEIGHTS)
    t0 = time.perf_counter()
    try:
        if use_production_pipeline and rrf_k == RRF_K and weights == list(RRF_WEIGHTS):
            payload = retrieve_rag_context(
                db,
                user_id=user_id,
                query=query,
                top_k=top_k,
                dense_top_k=BASELINE_DENSE_POOL,
                keyword_top_k=BASELINE_KEYWORD_POOL,
                attachment_top_k=BASELINE_ATTACHMENT_POOL,
                include_attachments=True,
            )
            debug = payload.get("retrieval_debug") or {}
            pipe_errors = list(debug.get("errors") or [])
            keys = refs_to_ranked_keys(payload.get("references") or [], top_k=top_k)
            if pipe_errors:
                code = f"{E_HYBRID_DEGRADED}:{pipe_errors[0]}"
                return keys, (time.perf_counter() - t0) * 1000.0, code, debug
            return keys, (time.perf_counter() - t0) * 1000.0, None, debug

        # Custom RRF eval path (explicit tuning); still records channel errors.
        debug: Dict[str, Any] = {
            "mode": "hybrid_rrf_custom",
            "rrf_k": rrf_k,
            "rrf_weights": weights,
            "errors": [],
            "visual_late_fusion_enabled": False,
        }
        initialize_user_index(user_id, db)
        query_vector = generate_embedding(f"query: {query}")
        dense_refs: List[Dict] = []
        keyword_refs: List[Dict] = []
        attachment_refs: List[Dict] = []
        try:
            dense_refs = fetch_dense_refs(
                db,
                user_id=user_id,
                query_vector=query_vector,
                top_k=BASELINE_DENSE_POOL,
            )
        except Exception as exc:
            debug["errors"].append(f"dense:{type(exc).__name__}")
        try:
            keyword_refs = search_keyword_entries(
                db=db,
                user_id=user_id,
                query=query,
                top_k=BASELINE_KEYWORD_POOL,
            )
        except Exception as exc:
            debug["errors"].append(f"keyword:{type(exc).__name__}")
        try:
            attachment_refs = [
                _normalize_attachment(item)
                for item in search_similar_attachment_chunks(
                    db=db,
                    query_vector=query_vector,
                    user_id=user_id,
                    top_k=BASELINE_ATTACHMENT_POOL,
                )
            ]
        except Exception as exc:
            debug["errors"].append(f"attachment:{type(exc).__name__}")
        debug["dense_count"] = len(dense_refs)
        debug["keyword_count"] = len(keyword_refs)
        debug["attachment_count"] = len(attachment_refs)
        fused = reciprocal_rank_fusion(
            [dense_refs, keyword_refs, attachment_refs],
            k=int(rrf_k),
            weights=weights,
            top_k=top_k,
        )
        fused = rerank_references(query, fused, top_k)
        keys = refs_to_ranked_keys(fused, top_k=top_k)
        if debug["errors"]:
            code = f"{E_HYBRID_DEGRADED}:{debug['errors'][0]}"
            return keys, (time.perf_counter() - t0) * 1000.0, code, debug
        return keys, (time.perf_counter() - t0) * 1000.0, None, debug
    except LocalModelMissingError:
        return [], (time.perf_counter() - t0) * 1000.0, E_LOCAL_MODEL_MISSING, {}
    except Exception as exc:
        return [], (time.perf_counter() - t0) * 1000.0, f"{E_HYBRID_FAILED}:{type(exc).__name__}", {}


def retrieve_three_strategies(
    db: Session,
    *,
    user_id: int,
    query: str,
    top_k: int = BASELINE_TOP_K,
) -> Dict[str, Any]:
    kw_keys, kw_ms, kw_err = retrieve_keyword_ranking(
        db, user_id=user_id, query=query, top_k=top_k
    )
    vec_keys, vec_ms, vec_err = retrieve_vector_ranking(
        db, user_id=user_id, query=query, top_k=top_k
    )
    hyb_keys, hyb_ms, hyb_err, hyb_debug = retrieve_hybrid_ranking(
        db, user_id=user_id, query=query, top_k=top_k
    )
    errors = {
        "keyword-only": kw_err,
        "vector-only": vec_err,
        "hybrid": hyb_err,
    }
    return {
        "rankings": {
            "keyword-only": kw_keys,
            "vector-only": vec_keys,
            "hybrid": hyb_keys,
            "latency_ms": {
                "keyword-only": kw_ms,
                "vector-only": vec_ms,
                "hybrid": hyb_ms,
            },
            "errors": {k: v for k, v in errors.items() if v},
        },
        "hybrid_debug": {
            "mode": hyb_debug.get("mode"),
            "dense_count": hyb_debug.get("dense_count"),
            "keyword_count": hyb_debug.get("keyword_count"),
            "attachment_count": hyb_debug.get("attachment_count"),
            "visual_late_fusion_enabled": hyb_debug.get("visual_late_fusion_enabled"),
            "error_count": len(hyb_debug.get("errors") or []),
        },
        "has_infra_error": any(errors.values()),
    }

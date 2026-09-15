"""
Rank fusion helpers for hybrid retrieval.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, List, Optional, Sequence

_REFERENCE_KEY_RE = re.compile(
    r"^(?P<source_type>[a-z][a-z0-9_]{0,62}):(?P<kind>chunk|source|entry):(?P<id>\d+)$"
)


def result_key(item: Dict) -> str:
    source_type = item.get("source_type") or "entry"
    if item.get("chunk_id") is not None:
        return f"{source_type}:chunk:{item['chunk_id']}"
    if item.get("source_id") is not None:
        return f"{source_type}:source:{item['source_id']}"
    return f"{source_type}:entry:{item.get('entry_id')}"


def reciprocal_rank_fusion(
    ranked_lists: Iterable[List[Dict]],
    *,
    k: int = 60,
    weights: Optional[List[float]] = None,
    top_k: int = 15,
) -> List[Dict]:
    fused: Dict[str, Dict] = {}
    score_by_key: Dict[str, float] = {}

    for list_index, ranked in enumerate(ranked_lists):
        weight = weights[list_index] if weights and list_index < len(weights) else 1.0
        for rank, item in enumerate(ranked, start=1):
            key = result_key(item)
            score_by_key[key] = score_by_key.get(key, 0.0) + weight * (1.0 / (k + rank))
            existing = fused.get(key)
            if not existing:
                fused[key] = dict(item)
                fused[key]["retrieval_sources"] = [item.get("retrieval_method", f"ranked_{list_index}")]
                fused[key]["source_scores"] = {
                    item.get("retrieval_method", f"ranked_{list_index}"): item.get("relevance_score", 0.0)
                }
            else:
                if item.get("relevance_score", 0.0) > existing.get("relevance_score", 0.0):
                    merged = dict(item)
                    merged["retrieval_sources"] = existing.get("retrieval_sources", [])
                    merged["source_scores"] = existing.get("source_scores", {})
                    fused[key] = merged
                source = item.get("retrieval_method", f"ranked_{list_index}")
                if source not in fused[key].setdefault("retrieval_sources", []):
                    fused[key]["retrieval_sources"].append(source)
                fused[key].setdefault("source_scores", {})[source] = item.get("relevance_score", 0.0)

    ranked_items = []
    for key, item in fused.items():
        fused_score = score_by_key.get(key, 0.0)
        item["rrf_score"] = fused_score
        item["relevance_score"] = max(float(item.get("relevance_score", 0.0) or 0.0), fused_score)
        item["source_reason"] = "hybrid_rrf" if len(item.get("retrieval_sources", [])) > 1 else item.get("source_reason")
        ranked_items.append(item)

    ranked_items.sort(key=lambda item: item.get("rrf_score", 0.0), reverse=True)
    return ranked_items[:top_k]


def _ref_from_reference_key(key: str) -> Optional[Dict]:
    m = _REFERENCE_KEY_RE.fullmatch(str(key or "").strip())
    if not m:
        return None
    source_type = m.group("source_type")
    kind = m.group("kind")
    sid = int(m.group("id"))
    item: Dict = {
        "source_type": source_type,
        "relevance_score": 0.0,
        "retrieval_method": "offline_rrf",
    }
    if kind == "chunk":
        item["chunk_id"] = sid
        item["source_id"] = sid
    elif kind == "source":
        item["source_id"] = sid
        item["chunk_id"] = None
        if source_type == "entry":
            item["entry_id"] = sid
    else:  # entry
        item["entry_id"] = sid
        item["source_id"] = sid
        item["chunk_id"] = None
    return item


def reciprocal_rank_fusion_keys(
    ranked_key_lists: Sequence[Sequence[str]],
    *,
    k: int = 60,
    weights: Optional[List[float]] = None,
    top_k: int = 20,
) -> List[str]:
    """
    Offline RRF over reference_key lists (no embedding / DB).
    Used by D.4 grid search on cached D.3 rankings.
    """
    lists: List[List[Dict]] = []
    for keyed in ranked_key_lists:
        refs: List[Dict] = []
        for key in keyed:
            ref = _ref_from_reference_key(str(key))
            if ref:
                refs.append(ref)
        lists.append(refs)
    fused = reciprocal_rank_fusion(lists, k=k, weights=weights, top_k=top_k)
    return [result_key(item) for item in fused]

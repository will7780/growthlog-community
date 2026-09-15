"""
Deterministic source-aware context builder for generation.

Does NOT read qrels. Does NOT change production RRF k/weights.
Selects context_references under hard per-source caps with reservation
and unused-quota redistribution.
"""
from __future__ import annotations

from typing import Any, Dict, List, Sequence, Tuple

from app.services.rank_fusion import result_key
from app.services.reference_identity import (
    annotate_with_canonical_keys,
    dedupe_by_evidence_origin,
)

CONTEXT_BUDGET_TOTAL = 18
CONTEXT_BUDGET_DENSE = 14
CONTEXT_BUDGET_KEYWORD = 4
CONTEXT_BUDGET_ATTACHMENT = 8

_SOURCE_ORDER = ("dense", "keyword", "attachment", "other")
_PRIMARY = ("dense", "keyword", "attachment")


def _bucket_of(ref: Dict[str, Any]) -> str:
    st = str(ref.get("source_type") or "entry").strip()
    method = str(ref.get("retrieval_method") or ref.get("pool_bucket") or "").lower()
    reason = str(ref.get("source_reason") or "").lower()
    if st in {"attachment_chunk", "knowledge_source", "attachment"}:
        return "attachment"
    if "keyword" in method or "keyword" in reason or method == "keyword":
        return "keyword"
    if "dense" in method or "vector" in method or "knowledge_dense" in method or method in {
        "dense",
        "fused",
        "dense_quota",
        "fused_quota",
    }:
        if st == "entry":
            return "dense"
    if st == "entry":
        return "dense"
    return "other"


def build_source_aware_context(
    retrieved_candidates: Sequence[Dict[str, Any]],
    *,
    total: int = CONTEXT_BUDGET_TOTAL,
    dense_budget: int = CONTEXT_BUDGET_DENSE,
    keyword_budget: int = CONTEXT_BUDGET_KEYWORD,
    attachment_budget: int = CONTEXT_BUDGET_ATTACHMENT,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Hard caps + reservation + unused total redistribution within caps.

    Priority after reservation: dense → keyword → attachment → other.
    Fill never exceeds a source's remaining cap.
    """
    caps = {
        "dense": int(dense_budget),
        "keyword": int(keyword_budget),
        "attachment": int(attachment_budget),
        "other": max(0, int(total)),
    }
    annotated = annotate_with_canonical_keys(list(retrieved_candidates or []))
    buckets: Dict[str, List[Dict[str, Any]]] = {name: [] for name in _SOURCE_ORDER}
    for ref in annotated:
        buckets[_bucket_of(ref)].append(ref)

    selected: List[Dict[str, Any]] = []
    seen_keys: set = set()
    counts = {name: 0 for name in _SOURCE_ORDER}
    cursors = {name: 0 for name in _SOURCE_ORDER}

    def _take_one(name: str, tag: str) -> bool:
        if len(selected) >= total:
            return False
        if counts[name] >= caps.get(name, 0):
            return False
        bucket = buckets.get(name) or []
        while cursors[name] < len(bucket):
            ref = bucket[cursors[name]]
            cursors[name] += 1
            key = str(ref.get("reference_key") or result_key(ref))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            item = dict(ref)
            item["context_bucket"] = tag
            selected.append(item)
            counts[name] += 1
            return True
        return False

    def _take_upto(name: str, n: int, tag: str) -> int:
        taken = 0
        while taken < n:
            if not _take_one(name, tag):
                break
            taken += 1
        return taken

    present = [n for n in _PRIMARY if buckets.get(n)]
    # Phase 1: reservation — at least one slot per present primary source.
    for name in present:
        _take_one(name, f"{name}_reserve")

    # Phase 2: priority fill within remaining caps / total.
    for name in _SOURCE_ORDER:
        room = caps.get(name, 0) - counts[name]
        slots_left = total - len(selected)
        if room <= 0 or slots_left <= 0:
            continue
        _take_upto(name, min(room, slots_left), f"{name}_fill")

    # Phase 3: redistribute unused TOTAL to sources that still have cap room + items.
    for name in _SOURCE_ORDER:
        room = caps.get(name, 0) - counts[name]
        slots_left = total - len(selected)
        if room <= 0 or slots_left <= 0:
            continue
        _take_upto(name, min(room, slots_left), f"{name}_redistribute")

    kept, dropped = dedupe_by_evidence_origin(selected)

    # Post-dedupe refill under caps.
    if len(kept) < total:
        kept_keys = {str(r.get("reference_key") or result_key(r)) for r in kept}
        selected = list(kept)
        seen_keys = set(kept_keys)
        counts = {name: 0 for name in _SOURCE_ORDER}
        for ref in kept:
            name = _bucket_of(ref)
            counts[name] = counts.get(name, 0) + 1
        # Reset cursors and skip already-seen via seen_keys.
        cursors = {name: 0 for name in _SOURCE_ORDER}
        for name in _SOURCE_ORDER:
            room = caps.get(name, 0) - counts[name]
            slots_left = total - len(selected)
            if room <= 0 or slots_left <= 0:
                continue
            _take_upto(name, min(room, slots_left), f"{name}_post_dedupe")
        kept, more_dropped = dedupe_by_evidence_origin(selected)
        dropped.extend(more_dropped)

    # Final hard-cap enforce.
    out: List[Dict[str, Any]] = []
    used = {name: 0 for name in _SOURCE_ORDER}
    for ref in kept:
        name = _bucket_of(ref)
        if used.get(name, 0) >= caps.get(name, 0):
            continue
        if len(out) >= total:
            break
        out.append(ref)
        used[name] = used.get(name, 0) + 1
    kept = out

    selected_counts = {
        "dense": sum(1 for r in kept if _bucket_of(r) == "dense"),
        "keyword": sum(1 for r in kept if _bucket_of(r) == "keyword"),
        "attachment": sum(1 for r in kept if _bucket_of(r) == "attachment"),
        "other": sum(1 for r in kept if _bucket_of(r) == "other"),
    }
    meta = {
        "strategy": "deterministic_source_aware",
        "budget_total": total,
        "budget_dense": dense_budget,
        "budget_keyword": keyword_budget,
        "budget_attachment": attachment_budget,
        "selected_counts": selected_counts,
        "reservation_sources": present,
        "raw_selected_before_dedupe": len(selected),
        "context_count": len(kept),
        "mirror_dropped": len(dropped),
        "retrieved_count": len(annotated),
    }
    return kept, meta

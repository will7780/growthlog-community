"""
RAG ranking metrics with stable reference_key dedupe and graded nDCG.
"""
from __future__ import annotations

import math
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Union

DEFAULT_K_VALUES = (5, 10, 20)


def dedupe_ranked_keys(keys: Iterable[str]) -> List[str]:
    """Stable first-seen dedupe of reference keys."""
    out: List[str] = []
    seen: set[str] = set()
    for raw in keys:
        key = str(raw).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out


def _to_graded(relevant: Union[Mapping[str, int], Iterable[str]]) -> Dict[str, int]:
    if isinstance(relevant, Mapping):
        return {str(k): int(v) for k, v in relevant.items() if int(v) > 0}
    return {str(k): 1 for k in relevant}


def recall_at_k(ranked: Sequence[str], graded: Mapping[str, int], k: int) -> float:
    if not graded:
        return 0.0
    hits = sum(1 for key in ranked[:k] if key in graded)
    return hits / len(graded)


def precision_at_k(ranked: Sequence[str], graded: Mapping[str, int], k: int) -> float:
    top = ranked[:k]
    if not top:
        return 0.0
    hits = sum(1 for key in top if key in graded)
    return hits / len(top)


def hit_at_k(ranked: Sequence[str], graded: Mapping[str, int], k: int) -> float:
    return 1.0 if any(key in graded for key in ranked[:k]) else 0.0


def mrr_at_k(ranked: Sequence[str], graded: Mapping[str, int], k: int) -> float:
    for idx, key in enumerate(ranked[:k], start=1):
        if key in graded:
            return 1.0 / idx
    return 0.0


def _dcg(ranked: Sequence[str], graded: Mapping[str, int], k: int, *, graded_gain: bool) -> float:
    score = 0.0
    for idx, key in enumerate(ranked[:k], start=1):
        rel = float(graded.get(key, 0))
        if not graded_gain:
            rel = 1.0 if rel > 0 else 0.0
        if rel <= 0:
            continue
        gain = (2.0**rel - 1.0) if graded_gain else rel
        score += gain / math.log2(idx + 1)
    return score


def ndcg_at_k(
    ranked: Sequence[str],
    graded: Mapping[str, int],
    k: int,
    *,
    graded_gain: bool = True,
) -> float:
    if not graded:
        return 0.0
    actual = _dcg(ranked, graded, k, graded_gain=graded_gain)
    ideal_rels = sorted((int(v) for v in graded.values()), reverse=True)
    if not graded_gain:
        ideal_rels = [1 for _ in ideal_rels]
    ideal_keys = [f"__ideal_{i}" for i in range(len(ideal_rels))]
    ideal_map = {ideal_keys[i]: ideal_rels[i] for i in range(len(ideal_rels))}
    ideal = _dcg(ideal_keys, ideal_map, k, graded_gain=graded_gain)
    if ideal <= 0:
        return 0.0
    return actual / ideal


def binary_ndcg_at_k(ranked: Sequence[str], graded: Mapping[str, int], k: int) -> float:
    return ndcg_at_k(ranked, graded, k, graded_gain=False)


def compute_ranking_metrics(
    ranked_keys: Sequence[str],
    relevant: Union[Mapping[str, int], Iterable[str]],
    k_values: Sequence[int] = DEFAULT_K_VALUES,
) -> Dict[str, float]:
    ranked = dedupe_ranked_keys(ranked_keys)
    graded = _to_graded(relevant)
    out: Dict[str, float] = {}
    for k in k_values:
        out[f"recall@{k}"] = recall_at_k(ranked, graded, k)
        out[f"precision@{k}"] = precision_at_k(ranked, graded, k)
        out[f"hit@{k}"] = hit_at_k(ranked, graded, k)
        out[f"mrr@{k}"] = mrr_at_k(ranked, graded, k)
        out[f"ndcg@{k}"] = ndcg_at_k(ranked, graded, k, graded_gain=True)
        out[f"binary_ndcg@{k}"] = binary_ndcg_at_k(ranked, graded, k)
    return out


def latency_percentiles(latencies_ms: Sequence[float]) -> Dict[str, Optional[float]]:
    vals = sorted(float(x) for x in latencies_ms if x is not None)
    if not vals:
        return {"p50_ms": None, "p95_ms": None, "count": 0}

    def pct(p: float) -> float:
        if len(vals) == 1:
            return vals[0]
        idx = min(len(vals) - 1, max(0, int(math.ceil(p * len(vals)) - 1)))
        return vals[idx]

    return {"p50_ms": pct(0.50), "p95_ms": pct(0.95), "count": len(vals)}


# No production abstention threshold exists on keyword/vector/hybrid fusion today.
NO_ANSWER_DECISION_SUPPORTED = False
NO_ANSWER_UNSUPPORTED_REASON = (
    "retrievers return top-k candidates without an abstention threshold or "
    "no-answer decision rule; false-positive/abstention accuracy are unsupported"
)


def no_answer_metrics(
    ranked_keys: Sequence[str],
    *,
    expected_answerable: bool,
) -> Dict[str, object]:
    """
    Without a rejection rule, no-answer quality cannot be scored fairly from
    top-k presence alone.
    """
    if NO_ANSWER_DECISION_SUPPORTED:
        ranked = dedupe_ranked_keys(ranked_keys)
        returned = len(ranked) > 0
        if not expected_answerable:
            return {
                "status": "ok",
                "false_positive": 1.0 if returned else 0.0,
                "abstention_accuracy": 0.0 if returned else 1.0,
            }
        return {
            "status": "ok",
            "false_positive": None,
            "abstention_accuracy": None,
        }
    return {
        "status": "unsupported",
        "reason": NO_ANSWER_UNSUPPORTED_REASON,
        "false_positive": None,
        "abstention_accuracy": None,
        "returned_count": len(dedupe_ranked_keys(ranked_keys)),
        "expected_answerable": expected_answerable,
    }

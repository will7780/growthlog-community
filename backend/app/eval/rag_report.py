"""
Bucket aggregation, failure classification, and privacy-safe reporting.

Three metric layers (never mix unsupported zeros into comparable means):
  - per_retriever_supported_scope
  - comparable_subset (all three retrievers support needed facets)
  - hybrid_product_scope (Hybrid product coverage only; not a ranking winner)

Ranking metrics (Recall/Precision/Hit/MRR/nDCG) aggregate ONLY over
expected_answerable=true cases with non-empty qrels. no-answer cases never
enter ranking means or winner as zeros.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence

from app.eval.rag_coverage import (
    RETRIEVER_COVERAGE,
    RETRIEVER_NAMES,
    comparison_supported,
    coverage_scope_for,
    required_facets_from_source_types,
    retriever_supports,
)
from app.eval.rag_metrics import (
    compute_ranking_metrics,
    latency_percentiles,
    no_answer_metrics,
)
from app.eval.rag_schema import graded_relevance_map

FAILURE_TYPES = (
    "all_retrievers_missed",
    "keyword_only_hit",
    "vector_only_hit",
    "fusion_regression",
    "rank_regression",
    "wrong_source",
    "no_answer_false_positive",
    "unsupported_comparison",
    "retrieval_infra_error",
)

RETRIEVER_ALIASES = {
    "keyword": "keyword-only",
    "keyword-only": "keyword-only",
    "vector": "vector-only",
    "vector-only": "vector-only",
    "dense": "vector-only",
    "hybrid": "hybrid",
}


def _canon(name: str) -> str:
    return RETRIEVER_ALIASES.get(name, name)


def _key_source_type(key: str) -> str:
    text = str(key or "").strip()
    return text.split(":", 1)[0] if ":" in text else text


def classify_failure(
    *,
    expected_answerable: bool,
    metrics_by_retriever: Dict[str, Optional[Dict[str, float]]],
    comparison_ok: bool,
    graded: Optional[Dict[str, int]] = None,
    hybrid_ranked: Optional[Sequence[str]] = None,
    hit_k: int = 10,
) -> Optional[str]:
    if not comparison_ok:
        return "unsupported_comparison"

    keyword = metrics_by_retriever.get("keyword-only") or {}
    vector = metrics_by_retriever.get("vector-only") or {}
    hybrid = metrics_by_retriever.get("hybrid") or {}

    if not expected_answerable:
        return None

    h = f"hit@{hit_k}"
    m = f"mrr@{hit_k}"
    hybrid_hit = float(hybrid.get(h, 0.0)) > 0.0
    keyword_hit = float(keyword.get(h, 0.0)) > 0.0
    vector_hit = float(vector.get(h, 0.0)) > 0.0

    if not hybrid_hit and not keyword_hit and not vector_hit:
        return "all_retrievers_missed"
    if not hybrid_hit and keyword_hit and not vector_hit:
        return "keyword_only_hit"
    if not hybrid_hit and vector_hit and not keyword_hit:
        return "vector_only_hit"
    if not hybrid_hit and (keyword_hit or vector_hit):
        return "fusion_regression"
    if hybrid_hit:
        best_single = max(float(keyword.get(m, 0.0)), float(vector.get(m, 0.0)))
        if float(hybrid.get(m, 0.0)) + 1e-9 < best_single:
            return "rank_regression"
        graded_map = graded or {}
        if graded_map and hybrid_ranked is not None:
            max_rel = max(int(v) for v in graded_map.values())
            top_expected_types = {
                _key_source_type(k) for k, v in graded_map.items() if int(v) == max_rel
            }
            first_hit = next(
                (k for k in list(hybrid_ranked)[:hit_k] if k in graded_map),
                None,
            )
            if first_hit is not None and _key_source_type(first_hit) not in top_expected_types:
                return "wrong_source"
    return None


def evaluate_case_offline(
    case: Dict[str, Any],
    *,
    k_values: Sequence[int] = (5, 10, 20),
) -> Dict[str, Any]:
    graded = graded_relevance_map(case)
    needed = sorted(required_facets_from_source_types(case.get("coverage_needed") or []))
    rankings = case.get("rankings") or {}
    expected_answerable = bool(case.get("expected_answerable"))
    raw_errors = rankings.get("errors") or {}
    retrieval_errors = {
        str(k): str(v)
        for k, v in raw_errors.items()
        if v
    } if isinstance(raw_errors, dict) else {}
    has_infra_error = bool(retrieval_errors)
    # Ranking metrics require answerable cases with non-empty qrels.
    # Infra errors must not be scored as Recall=0 quality failures.
    ranking_eligible = expected_answerable and bool(graded) and not has_infra_error

    support_by: Dict[str, Dict[str, Any]] = {}
    metrics_by: Dict[str, Optional[Dict[str, float]]] = {}
    scopes: Dict[str, List[str]] = {}

    for name in RETRIEVER_NAMES:
        facets = sorted(coverage_scope_for(name))
        scopes[name] = facets
        ok, lack = retriever_supports(name, needed)
        keys = rankings.get(name) or rankings.get(name.replace("-only", "")) or []
        if not isinstance(keys, list):
            keys = []
        if not ok:
            metrics_by[name] = None
            support_by[name] = {
                "supported": False,
                "coverage_facets": facets,
                "missing_facets": lack,
                "reason": (
                    f"{name} does not cover required facets "
                    f"{needed}; missing={lack}"
                ),
            }
            continue

        support_by[name] = {
            "supported": True,
            "coverage_facets": facets,
            "missing_facets": [],
            "reason": None,
            "error": retrieval_errors.get(name),
        }
        if ranking_eligible and name not in retrieval_errors:
            metrics_by[name] = compute_ranking_metrics(keys, graded, k_values)
        else:
            # no-answer / empty qrels / infra error: never invent zero ranking scores.
            metrics_by[name] = None

    comparison_ok, gaps = comparison_supported(RETRIEVER_NAMES, needed)
    hybrid_keys = rankings.get("hybrid") or []
    if not isinstance(hybrid_keys, list):
        hybrid_keys = []

    scored_metrics = {
        name: metrics_by[name]
        for name in RETRIEVER_NAMES
        if support_by[name]["supported"] and metrics_by[name] is not None
    }
    if has_infra_error:
        failure = "retrieval_infra_error"
    else:
        failure = classify_failure(
            expected_answerable=expected_answerable,
            metrics_by_retriever=scored_metrics,  # type: ignore[arg-type]
            comparison_ok=comparison_ok,
            graded=graded,
            hybrid_ranked=hybrid_keys,
        )

    na = no_answer_metrics(
        hybrid_keys,
        expected_answerable=expected_answerable,
    )

    lat_raw = rankings.get("latency_ms") or {}
    latency = {
        name: latency_percentiles([lat_raw[name]] if name in lat_raw else [])
        for name in RETRIEVER_NAMES
    }

    return {
        "case_id": case.get("case_id"),
        "example": bool(case.get("example")),
        "split": case.get("split"),
        "query_type": case.get("query_type"),
        "tags": list(case.get("tags") or []),
        "difficulty": case.get("difficulty"),
        "expected_answerable": expected_answerable,
        "ranking_eligible": ranking_eligible,
        "coverage_needed": needed,
        "coverage_scope": scopes,
        "retriever_support": support_by,
        "comparison_supported": comparison_ok,
        "comparison_gaps": gaps,
        "metrics": metrics_by,
        "no_answer": na,
        "failure_type": failure,
        "latency": latency,
        "retrieval_errors": retrieval_errors,
    }


def _mean_metrics_ranking(
    ranking_rows: List[Dict[str, Any]],
    retriever: str,
) -> Optional[Dict[str, float]]:
    """Average ranking metrics over ranking-eligible rows only. None if empty."""
    if not ranking_rows:
        return None
    vals: Dict[str, List[float]] = defaultdict(list)
    for row in ranking_rows:
        support = (row.get("retriever_support") or {}).get(retriever) or {}
        if not support.get("supported"):
            continue
        metrics = (row.get("metrics") or {}).get(retriever)
        if not isinstance(metrics, dict):
            continue
        for key, value in metrics.items():
            if isinstance(value, (int, float)):
                vals[key].append(float(value))
    if not vals:
        return None
    return {k: (sum(v) / len(v)) for k, v in sorted(vals.items())}


def _layer_pack(
    subset: List[Dict[str, Any]],
    *,
    retrievers: Sequence[str],
    excluded_unsupported_count: int,
) -> Dict[str, Any]:
    """
    Pack one coverage layer.

    case_count = all cases in this coverage scope (answerable + no-answer).
    ranking_denominator = answerable + ranking_eligible cases used for
    Recall/MRR/nDCG means and winner. Legacy field ``denominator`` aliases
    ranking_denominator (NOT case_count).
    """
    answerable = [r for r in subset if r.get("expected_answerable")]
    no_answer = [r for r in subset if not r.get("expected_answerable")]
    ranking_rows = [
        r
        for r in subset
        if r.get("ranking_eligible") and r.get("expected_answerable")
    ]
    ranking_denominator = len(ranking_rows)

    out: Dict[str, Any] = {
        "case_count": len(subset),
        "answerable_count": len(answerable),
        "no_answer_count": len(no_answer),
        "ranking_denominator": ranking_denominator,
        # Compat alias: denominator == ranking_denominator (never case_count).
        "denominator": ranking_denominator,
        "excluded_unsupported_count": int(excluded_unsupported_count),
        "excluded_no_answer_from_ranking_count": len(no_answer),
        "metrics": {},
        "failures": [
            {
                "case_id": r.get("case_id"),
                "failure_type": r.get("failure_type"),
                "coverage_needed": r.get("coverage_needed"),
            }
            for r in subset
            if r.get("failure_type")
        ],
    }
    for name in retrievers:
        means = _mean_metrics_ranking(ranking_rows, name)
        out["metrics"][name] = means
        out[name] = means
    return out


def _comparable_winner(layer: Dict[str, Any]) -> Dict[str, Any]:
    """Winner only when ranking_denominator > 0 and metrics are present."""
    ranking_denom = int(layer.get("ranking_denominator") or layer.get("denominator") or 0)
    if ranking_denom <= 0:
        return {
            "status": "unavailable",
            "reason": (
                "ranking_denominator is 0 "
                "(no answerable qrels cases in comparable coverage scope)"
            ),
            "winner": None,
            "metric": None,
        }
    metric = "mrr@10"
    scores: Dict[str, float] = {}
    for name in RETRIEVER_NAMES:
        metrics = (layer.get("metrics") or {}).get(name)
        if not isinstance(metrics, dict) or metric not in metrics:
            return {
                "status": "unavailable",
                "reason": "ranking metrics missing; refuse to invent zeros",
                "winner": None,
                "metric": None,
            }
        scores[name] = float(metrics[metric])
    best = max(scores.values())
    leaders = [name for name, score in scores.items() if abs(score - best) < 1e-12]
    return {
        "status": "ok",
        "metric": metric,
        "scores": scores,
        "winner": leaders[0] if len(leaders) == 1 else None,
        "tied_leaders": leaders if len(leaders) != 1 else None,
        "note": (
            "Winner uses ranking_denominator on comparable_subset only. "
            "hybrid_product_scope must not be used as ranking evidence. "
            "no-answer cases are excluded from ranking means."
        ),
    }


def _build_layers(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    pool = len(rows)
    per_retriever: Dict[str, Any] = {}
    for name in RETRIEVER_NAMES:
        supported_rows = [
            r
            for r in rows
            if ((r.get("retriever_support") or {}).get(name) or {}).get("supported")
        ]
        pack = _layer_pack(
            supported_rows,
            retrievers=[name],
            excluded_unsupported_count=pool - len(supported_rows),
        )
        pack["scope"] = "per_retriever_supported_scope"
        pack["retriever"] = name
        pack["coverage_facets"] = sorted(RETRIEVER_COVERAGE[name])
        per_retriever[name] = pack

    comparable_rows = [r for r in rows if r.get("comparison_supported")]
    comparable = _layer_pack(
        comparable_rows,
        retrievers=RETRIEVER_NAMES,
        excluded_unsupported_count=pool - len(comparable_rows),
    )
    comparable["scope"] = "comparable_subset"
    comparable["winner"] = _comparable_winner(comparable)

    hybrid_rows = [
        r
        for r in rows
        if ((r.get("retriever_support") or {}).get("hybrid") or {}).get("supported")
    ]
    hybrid_product = _layer_pack(
        hybrid_rows,
        retrievers=["hybrid"],
        excluded_unsupported_count=pool - len(hybrid_rows),
    )
    hybrid_product["scope"] = "hybrid_product_scope"
    hybrid_product["note"] = (
        "Product-scope Hybrid metrics only; must not be used as "
        "keyword/vector/hybrid ranking superiority evidence. "
        "Ranking means use ranking_denominator (answerable only)."
    )
    hybrid_product["winner"] = None

    buckets: Dict[str, Dict[str, List[Dict[str, Any]]]] = {
        "query_type": defaultdict(list),
        "source_type": defaultdict(list),
        "tags": defaultdict(list),
        "difficulty": defaultdict(list),
        "split": defaultdict(list),
        "coverage_scope": defaultdict(list),
    }
    for row in rows:
        buckets["query_type"][str(row.get("query_type") or "unknown")].append(row)
        buckets["difficulty"][str(row.get("difficulty") or "unknown")].append(row)
        buckets["split"][str(row.get("split") or "unknown")].append(row)
        scope_key = ",".join(sorted(row.get("coverage_needed") or [])) or "none"
        buckets["coverage_scope"][scope_key].append(row)
        for tag in row.get("tags") or ["untagged"]:
            buckets["tags"][str(tag)].append(row)
        for st in row.get("coverage_needed") or ["none"]:
            buckets["source_type"][str(st)].append(row)

    def bucket_pack(subset: List[Dict[str, Any]]) -> Dict[str, Any]:
        comparable_sub = [r for r in subset if r.get("comparison_supported")]
        layer = _layer_pack(
            comparable_sub,
            retrievers=RETRIEVER_NAMES,
            excluded_unsupported_count=len(subset) - len(comparable_sub),
        )
        layer["winner"] = _comparable_winner(layer)
        return {
            "case_count": len(subset),
            "answerable_count": sum(1 for r in subset if r.get("expected_answerable")),
            "no_answer_count": sum(1 for r in subset if not r.get("expected_answerable")),
            "unsupported_comparison_count": sum(
                1 for r in subset if not r.get("comparison_supported")
            ),
            "comparable_subset": layer,
            "failures": [
                {
                    "case_id": r.get("case_id"),
                    "failure_type": r.get("failure_type"),
                    "coverage_needed": r.get("coverage_needed"),
                }
                for r in subset
                if r.get("failure_type")
            ],
        }

    bucket_out: Dict[str, Dict[str, Any]] = {}
    for dim, mapping in buckets.items():
        bucket_out[dim] = {name: bucket_pack(items) for name, items in sorted(mapping.items())}

    # Latency: include no-answer requests when the retriever covers the case.
    latency_by = {}
    for name in RETRIEVER_NAMES:
        samples: List[float] = []
        for row in rows:
            support = (row.get("retriever_support") or {}).get(name) or {}
            if not support.get("supported"):
                continue
            lat = ((row.get("latency") or {}).get(name) or {}).get("p50_ms")
            if isinstance(lat, (int, float)):
                samples.append(float(lat))
        latency_by[name] = latency_percentiles(samples)

    answerable = [r for r in rows if r.get("expected_answerable")]
    no_answer = [r for r in rows if not r.get("expected_answerable")]

    overall = {
        "case_count": comparable["case_count"],
        "answerable_count": comparable["answerable_count"],
        "no_answer_count": comparable["no_answer_count"],
        "ranking_denominator": comparable["ranking_denominator"],
        "denominator": comparable["ranking_denominator"],
        "excluded_unsupported_count": comparable["excluded_unsupported_count"],
        "excluded_no_answer_from_ranking_count": comparable[
            "excluded_no_answer_from_ranking_count"
        ],
        "unsupported_comparison_count": comparable["excluded_unsupported_count"],
        "keyword-only": (comparable.get("metrics") or {}).get("keyword-only"),
        "vector-only": (comparable.get("metrics") or {}).get("vector-only"),
        "hybrid": (comparable.get("metrics") or {}).get("hybrid"),
        "failures": comparable.get("failures") or [],
        "winner": comparable.get("winner"),
        "note": (
            "overall mirrors comparable_subset; denominator==ranking_denominator; "
            "no-answer excluded from ranking means"
        ),
    }

    return {
        "case_count": len(rows),
        "answerable_count": len(answerable),
        "no_answer_count": len(no_answer),
        "ranking_denominator": comparable["ranking_denominator"],
        "per_retriever_supported_scope": per_retriever,
        "comparable_subset": comparable,
        "hybrid_product_scope": hybrid_product,
        "overall": overall,
        "buckets": bucket_out,
        "latency": latency_by,
        "cases": [
            {
                "case_id": r.get("case_id"),
                "split": r.get("split"),
                "query_type": r.get("query_type"),
                "tags": r.get("tags"),
                "difficulty": r.get("difficulty"),
                "expected_answerable": r.get("expected_answerable"),
                "ranking_eligible": r.get("ranking_eligible"),
                "coverage_needed": r.get("coverage_needed"),
                "retriever_support": {
                    name: {
                        "supported": ((r.get("retriever_support") or {}).get(name) or {}).get(
                            "supported"
                        ),
                        "missing_facets": ((r.get("retriever_support") or {}).get(name) or {}).get(
                            "missing_facets"
                        ),
                        "reason": ((r.get("retriever_support") or {}).get(name) or {}).get(
                            "reason"
                        ),
                    }
                    for name in RETRIEVER_NAMES
                },
                "comparison_supported": r.get("comparison_supported"),
                "failure_type": r.get("failure_type"),
                "metrics": {
                    name: (r.get("metrics") or {}).get(name)
                    for name in RETRIEVER_NAMES
                },
                "no_answer": {
                    "status": (r.get("no_answer") or {}).get("status"),
                    "reason": (r.get("no_answer") or {}).get("reason"),
                },
            }
            for r in rows
        ],
    }


def aggregate_report(
    case_rows: List[Dict[str, Any]],
    *,
    include_examples: bool = False,
) -> Dict[str, Any]:
    """
    Formal quality aggregates NEVER include example=true rows.

    --include-examples only populates tooling_examples for metric-code checks.
    quality_claim_allowed is false when tooling examples are requested or when
    there is no ranking-eligible quality data.
    """
    quality_rows = [r for r in case_rows if not r.get("example")]
    example_rows = [r for r in case_rows if r.get("example")]

    quality = _build_layers(quality_rows)
    # Formal product quality claims stay false for D.1 tooling; algorithmic
    # winner on non-example ranking_denominator>0 is still computed unless
    # --include-examples was requested (example tooling isolation).
    quality_claim_allowed = False
    infra_error_rows = [
        r
        for r in quality_rows
        if isinstance(r.get("retrieval_errors"), dict) and r.get("retrieval_errors")
    ]
    if infra_error_rows:
        blocked = {
            "status": "unavailable",
            "reason": "retrieval infrastructure errors present; refuse winner",
            "winner": None,
            "metric": None,
            "infra_error_case_count": len(infra_error_rows),
        }
        quality["comparable_subset"]["winner"] = blocked
        quality["overall"]["winner"] = blocked
    if include_examples:
        quality["comparable_subset"]["winner"] = {
            "status": "unavailable",
            "reason": "example tooling run; formal winner not emitted",
            "winner": None,
            "metric": None,
        }
        quality["overall"]["winner"] = quality["comparable_subset"]["winner"]

    report: Dict[str, Any] = {
        "retriever_coverage": {
            name: sorted(facets) for name, facets in RETRIEVER_COVERAGE.items()
        },
        "quality_claim_allowed": quality_claim_allowed,
        "example_excluded": len(example_rows),
        "include_examples": include_examples,
        "no_answer_semantics": {
            "status": "unsupported",
            "reason": (
                "no abstention threshold/decision rule on current retrievers; "
                "no_answer false-positive/abstention accuracy are unsupported; "
                "no-answer cases are excluded from ranking metric means"
            ),
        },
        "denominator_contract": {
            "case_count": "all cases in the coverage scope (answerable + no-answer)",
            "ranking_denominator": (
                "answerable cases with non-empty qrels that enter "
                "Recall/Precision/Hit/MRR/nDCG means and winner"
            ),
            "denominator": "alias of ranking_denominator (NOT case_count)",
            "excluded_unsupported_count": "cases outside this coverage scope",
            "excluded_no_answer_from_ranking_count": (
                "no-answer cases inside scope but excluded from ranking means"
            ),
        },
        **quality,
    }

    if include_examples:
        tooling = _build_layers(example_rows)
        # Tooling may compute metrics for code verification; never a quality claim.
        report["tooling_examples"] = {
            "note": (
                "example=true rows only; for metric-code verification; "
                "not a retrieval quality baseline"
            ),
            "quality_claim_allowed": False,
            "case_count": tooling["case_count"],
            "answerable_count": tooling["answerable_count"],
            "no_answer_count": tooling["no_answer_count"],
            "ranking_denominator": tooling["ranking_denominator"],
            "comparable_subset": tooling["comparable_subset"],
            "per_retriever_supported_scope": tooling["per_retriever_supported_scope"],
            "hybrid_product_scope": tooling["hybrid_product_scope"],
            "cases": tooling["cases"],
        }
        # Formal top-level counts remain quality-only (0 for example-only files).
    else:
        report["tooling_examples"] = None

    return report


def public_case_view(case_row: Dict[str, Any], *, include_query: bool = False) -> Dict[str, Any]:
    view = {
        "case_id": case_row.get("case_id"),
        "split": case_row.get("split"),
        "query_type": case_row.get("query_type"),
        "tags": case_row.get("tags"),
        "difficulty": case_row.get("difficulty"),
        "failure_type": case_row.get("failure_type"),
        "comparison_supported": case_row.get("comparison_supported"),
        "coverage_needed": case_row.get("coverage_needed"),
    }
    if include_query:
        view["query"] = case_row.get("query")
    return view

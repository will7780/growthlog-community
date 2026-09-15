"""
Phase D.4 offline RRF grid search over cached keyword/vector rankings.

Selection uses **dev** split only. Regression must not participate in ranking
or config selection. No embedding / DB / LLM calls.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import mean
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from app.eval.rag_metrics import compute_ranking_metrics
from app.eval.rag_report import classify_failure
from app.eval.rag_schema import graded_relevance_map
from app.services.rank_fusion import reciprocal_rank_fusion_keys

D3_DEFAULT_RRF_K = 60
D3_DEFAULT_WEIGHTS = [1.0, 0.9, 0.85]


def load_default_grid_config() -> Dict[str, Any]:
    """Default grid when no config file is provided (eval-side only)."""
    return {
        "recall_slack": 0.05,
        "mrr_near_vector_slack": 0.05,
        "tie_mrr_eps": 0.01,
        "tie_ndcg_eps": 0.01,
        "rrf_k_values": [30, 40, 60, 80, 100],
        "dense_keyword_weights": [
            [1.0, 0.9],
            [1.0, 0.7],
            [1.0, 0.5],
            [1.0, 0.3],
            [0.9, 1.0],
            [0.7, 1.0],
            [1.0, 0.0],
        ],
        "include_d3_hybrid_point": True,
        "include_vector_only": True,
    }


def expand_grid_points(grid: Dict[str, Any]) -> List[Dict[str, Any]]:
    points: List[Dict[str, Any]] = []
    if grid.get("include_vector_only", True):
        points.append(
            {
                "config_id": "vector_only",
                "kind": "vector_only",
                "rrf_k": None,
                "weights": None,
                "simple_score": 100,
            }
        )
    if grid.get("include_d3_hybrid_point", True):
        points.append(
            {
                "config_id": "d3_hybrid_k60_w1.0_0.9",
                "kind": "hybrid_rrf",
                "rrf_k": D3_DEFAULT_RRF_K,
                "weights": list(D3_DEFAULT_WEIGHTS[:2]),
                "simple_score": 50,
                "note": "D.3 production dense/keyword weights (attachment channel omitted offline)",
            }
        )
    for k in grid.get("rrf_k_values") or []:
        for w in grid.get("dense_keyword_weights") or []:
            if not isinstance(w, (list, tuple)) or len(w) < 2:
                continue
            wd, wk = float(w[0]), float(w[1])
            cid = f"rrf_k{int(k)}_d{wd:g}_kw{wk:g}"
            # Degenerate: keyword weight==0 is vector-only, not a hybrid candidate.
            if wk <= 0.0:
                points.append(
                    {
                        "config_id": cid,
                        "kind": "vector_equivalent",
                        "rrf_k": int(k),
                        "weights": [wd, wk],
                        "simple_score": 90,
                        "note": "keyword_weight=0; diagnostic only; not a hybrid candidate",
                    }
                )
                continue
            # Prefer configs closer to vector (high dense, low keyword) when tying.
            simple = int(
                round(
                    wd * 40
                    + (1.0 - min(wk, 1.0)) * 20
                    + (1.0 / (1 + abs(int(k) - 60))) * 10
                )
            )
            points.append(
                {
                    "config_id": cid,
                    "kind": "hybrid_rrf",
                    "rrf_k": int(k),
                    "weights": [wd, wk],
                    "simple_score": simple,
                }
            )
    # Dedupe by config_id
    seen = set()
    unique: List[Dict[str, Any]] = []
    for p in points:
        if p["config_id"] in seen:
            continue
        seen.add(p["config_id"])
        unique.append(p)
    return unique


def _metric_at(metrics: Optional[Dict[str, float]], name: str, default: float = 0.0) -> float:
    if not metrics:
        return default
    return float(metrics.get(name, default) or default)


def _fuse_for_case(
    *,
    keyword_keys: Sequence[str],
    vector_keys: Sequence[str],
    point: Dict[str, Any],
    top_k: int,
) -> List[str]:
    if point.get("kind") == "vector_only":
        return list(vector_keys)[:top_k]
    if point.get("kind") == "keyword_only":
        return list(keyword_keys)[:top_k]
    weights = list(point.get("weights") or [1.0, 0.9])
    # Offline: dense list first, keyword second (matches production weight order).
    return reciprocal_rank_fusion_keys(
        [list(vector_keys), list(keyword_keys)],
        k=int(point.get("rrf_k") or D3_DEFAULT_RRF_K),
        weights=weights,
        top_k=top_k,
    )


def evaluate_ranking_list(
    case: Dict[str, Any],
    ranked_keys: Sequence[str],
    *,
    k_values: Sequence[int] = (5, 10, 20),
) -> Optional[Dict[str, float]]:
    graded = graded_relevance_map(case)
    if not bool(case.get("expected_answerable")) or not graded:
        return None
    return compute_ranking_metrics(list(ranked_keys), graded, k_values=k_values)


def _bucket_keys(case: Dict[str, Any]) -> List[str]:
    keys = []
    qt = str(case.get("query_type") or "").strip()
    if qt:
        keys.append(f"query_type:{qt}")
    diff = str(case.get("difficulty") or "").strip()
    if diff:
        keys.append(f"difficulty:{diff}")
    for tag in case.get("tags") or []:
        t = str(tag).strip()
        if t in {"cross_entry", "hard", "semantic", "temporal"}:
            keys.append(f"tag:{t}")
    if qt in {"cross_entry", "semantic", "temporal"}:
        keys.append(qt)
    if diff == "hard":
        keys.append("hard")
    return sorted(set(keys))


def score_config_on_cases(
    cases: Sequence[Dict[str, Any]],
    rankings_by_id: Dict[str, Dict[str, List[str]]],
    point: Dict[str, Any],
    *,
    top_k: int = 20,
) -> Dict[str, Any]:
    """Score one grid point on a case subset (dev only for selection)."""
    per_case: List[Dict[str, Any]] = []
    mrrs: List[float] = []
    ndcgs: List[float] = []
    recalls: List[float] = []
    bucket_mrr: Dict[str, List[float]] = defaultdict(list)
    rank_regression = 0
    answerable = 0

    for case in cases:
        cid = str(case["case_id"])
        ranks = rankings_by_id.get(cid) or {}
        kw = ranks.get("keyword-only") or []
        vec = ranks.get("vector-only") or []
        fused = _fuse_for_case(
            keyword_keys=kw, vector_keys=vec, point=point, top_k=top_k
        )
        vec_m = evaluate_ranking_list(case, vec)
        hyb_m = evaluate_ranking_list(case, fused)
        if not bool(case.get("expected_answerable")) or not graded_relevance_map(case):
            continue
        answerable += 1
        if hyb_m is None or vec_m is None:
            continue
        mrrs.append(_metric_at(hyb_m, "mrr@10"))
        ndcgs.append(_metric_at(hyb_m, "ndcg@10"))
        recalls.append(_metric_at(hyb_m, "recall@10"))
        for b in _bucket_keys(case):
            bucket_mrr[b].append(_metric_at(hyb_m, "mrr@10"))
        # Per-case regression vs vector
        if _metric_at(hyb_m, "mrr@10") + 1e-9 < _metric_at(vec_m, "mrr@10"):
            rank_regression += 1
        # Also use classify_failure shape for hybrid-as-fusion diagnostics
        fail = classify_failure(
            expected_answerable=True,
            metrics_by_retriever={
                "keyword-only": evaluate_ranking_list(case, kw),
                "vector-only": vec_m,
                "hybrid": hyb_m,
            },
            comparison_ok=True,
            graded=graded_relevance_map(case),
            hybrid_ranked=fused,
            hit_k=10,
        )
        per_case.append(
            {
                "case_id": cid,
                "mrr@10": _metric_at(hyb_m, "mrr@10"),
                "ndcg@10": _metric_at(hyb_m, "ndcg@10"),
                "recall@10": _metric_at(hyb_m, "recall@10"),
                "vector_mrr@10": _metric_at(vec_m, "mrr@10"),
                "rank_regression_vs_vector": _metric_at(hyb_m, "mrr@10") + 1e-9
                < _metric_at(vec_m, "mrr@10"),
                "failure_type": fail,
            }
        )

    def avg(xs: List[float]) -> Optional[float]:
        return float(mean(xs)) if xs else None

    return {
        "config": point,
        "answerable_count": answerable,
        "ranking_denominator": len(mrrs),
        "metrics": {
            "mrr@10": avg(mrrs),
            "ndcg@10": avg(ndcgs),
            "recall@10": avg(recalls),
        },
        "rank_regression_vs_vector_count": rank_regression,
        "buckets": {
            k: {"mrr@10": avg(v), "n": len(v)} for k, v in sorted(bucket_mrr.items())
        },
        "per_case": per_case,
    }


def select_candidate(
    scored: Sequence[Dict[str, Any]],
    *,
    vector_metrics: Dict[str, float],
    recall_slack: float = 0.05,
    mrr_near_vector_slack: float = 0.05,
    tie_mrr_eps: float = 0.01,
    tie_ndcg_eps: float = 0.01,
) -> Dict[str, Any]:
    """
    Apply gates and pick best hybrid config on dev.
    vector_only is never returned as hybrid candidate.
    """
    vec_recall = float(vector_metrics.get("recall@10") or 0.0)
    vec_mrr = float(vector_metrics.get("mrr@10") or 0.0)

    hybrid_scored = [
        s for s in scored if (s.get("config") or {}).get("kind") == "hybrid_rrf"
    ]
    eligible: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []
    for s in hybrid_scored:
        m = s.get("metrics") or {}
        cfg = s.get("config") or {}
        weights = list(cfg.get("weights") or [])
        mrr = m.get("mrr@10")
        recall = m.get("recall@10")
        reasons: List[str] = []
        if len(weights) >= 2 and float(weights[1]) <= 0.0:
            reasons.append("degenerate_keyword_weight_zero")
        if mrr is None or recall is None:
            reasons.append("missing_metrics")
        else:
            if float(recall) + 1e-12 < vec_recall - recall_slack:
                reasons.append("recall@10_below_vector_gate")
            if float(mrr) + 1e-12 < vec_mrr - mrr_near_vector_slack:
                reasons.append("mrr@10_below_vector_gate")
        if reasons:
            rejected.append(
                {
                    "config_id": (s.get("config") or {}).get("config_id"),
                    "metrics": m,
                    "reasons": reasons,
                }
            )
        else:
            eligible.append(s)

    if not eligible:
        return {
            "status": "no_candidate",
            "selected": None,
            "eligible_count": 0,
            "rejected_count": len(rejected),
            "rejected_sample": rejected[:15],
            "selection_rule": {
                "recall_slack": recall_slack,
                "mrr_near_vector_slack": mrr_near_vector_slack,
                "primary": ["mrr@10", "ndcg@10"],
                "tie_break": [
                    "fewer_rank_regression_vs_vector",
                    "higher_simple_score",
                    "rrf_k_closer_to_60",
                ],
            },
            "note": (
                "No hybrid RRF config passed Recall/MRR gates vs vector-only on dev. "
                "Do not force a hybrid winner."
            ),
        }

    def sort_key(s: Dict[str, Any]) -> Tuple:
        m = s.get("metrics") or {}
        cfg = s.get("config") or {}
        k = cfg.get("rrf_k")
        k_dist = abs(int(k) - 60) if k is not None else 999
        return (
            -float(m.get("mrr@10") or 0.0),
            -float(m.get("ndcg@10") or 0.0),
            int(s.get("rank_regression_vs_vector_count") or 0),
            -int(cfg.get("simple_score") or 0),
            k_dist,
            str(cfg.get("config_id") or ""),
        )

    ranked = sorted(eligible, key=sort_key)
    best = ranked[0]
    # Near-tie: prefer simpler / less vector-perturbing among eps band
    best_mrr = float((best.get("metrics") or {}).get("mrr@10") or 0.0)
    best_ndcg = float((best.get("metrics") or {}).get("ndcg@10") or 0.0)
    near = [
        s
        for s in ranked
        if abs(float((s.get("metrics") or {}).get("mrr@10") or 0.0) - best_mrr)
        <= tie_mrr_eps
        and abs(float((s.get("metrics") or {}).get("ndcg@10") or 0.0) - best_ndcg)
        <= tie_ndcg_eps
    ]
    if len(near) > 1:
        near = sorted(
            near,
            key=lambda s: (
                int(s.get("rank_regression_vs_vector_count") or 0),
                -int((s.get("config") or {}).get("simple_score") or 0),
                abs(int((s.get("config") or {}).get("rrf_k") or 60) - 60),
                str((s.get("config") or {}).get("config_id") or ""),
            ),
        )
        best = near[0]

    return {
        "status": "selected",
        "selected": {
            "config": best.get("config"),
            "dev_metrics": best.get("metrics"),
            "rank_regression_vs_vector_count": best.get(
                "rank_regression_vs_vector_count"
            ),
            "buckets": best.get("buckets"),
            "ranking_denominator": best.get("ranking_denominator"),
        },
        "eligible_count": len(eligible),
        "rejected_count": len(rejected),
        "runner_up": [
            {
                "config_id": (s.get("config") or {}).get("config_id"),
                "metrics": s.get("metrics"),
                "rank_regression_vs_vector_count": s.get(
                    "rank_regression_vs_vector_count"
                ),
            }
            for s in ranked[1:6]
        ],
        "selection_rule": {
            "recall_slack": recall_slack,
            "mrr_near_vector_slack": mrr_near_vector_slack,
            "primary": ["mrr@10", "ndcg@10"],
            "tie_break": [
                "fewer_rank_regression_vs_vector",
                "higher_simple_score",
                "rrf_k_closer_to_60",
            ],
        },
    }


def run_offline_grid(
    cases: Sequence[Dict[str, Any]],
    rankings_by_id: Dict[str, Dict[str, List[str]]],
    *,
    grid: Optional[Dict[str, Any]] = None,
    top_k: int = 20,
) -> Dict[str, Any]:
    cfg = dict(load_default_grid_config())
    if grid:
        cfg.update(grid)

    # Harden: never use regression for selection
    dev_cases = [c for c in cases if str(c.get("split") or "") == "dev"]
    regression_cases = [c for c in cases if str(c.get("split") or "") == "regression"]
    if not dev_cases:
        raise ValueError("No dev-split cases available for D.4 grid selection")

    points = expand_grid_points(cfg)
    scored: List[Dict[str, Any]] = []
    for point in points:
        result = score_config_on_cases(
            dev_cases, rankings_by_id, point, top_k=top_k
        )
        # Drop bulky per_case from grid table; keep counts
        compact = {
            "config": result["config"],
            "answerable_count": result["answerable_count"],
            "ranking_denominator": result["ranking_denominator"],
            "metrics": result["metrics"],
            "rank_regression_vs_vector_count": result[
                "rank_regression_vs_vector_count"
            ],
            "buckets": {
                k: v
                for k, v in (result.get("buckets") or {}).items()
                if k
                in {
                    "cross_entry",
                    "hard",
                    "semantic",
                    "temporal",
                    "query_type:cross_entry",
                    "query_type:semantic",
                    "query_type:temporal",
                    "difficulty:hard",
                    "tag:cross_entry",
                    "tag:hard",
                    "tag:semantic",
                    "tag:temporal",
                }
            },
        }
        scored.append(compact)
        # Retain per_case only for selected later

    vector_point = next(
        (p for p in points if p.get("kind") == "vector_only"), None
    )
    vector_score = (
        score_config_on_cases(dev_cases, rankings_by_id, vector_point, top_k=top_k)
        if vector_point
        else None
    )
    vector_metrics = (vector_score or {}).get("metrics") or {
        "mrr@10": 0.0,
        "ndcg@10": 0.0,
        "recall@10": 0.0,
    }

    selection = select_candidate(
        scored,
        vector_metrics=vector_metrics,
        recall_slack=float(cfg.get("recall_slack", 0.05)),
        mrr_near_vector_slack=float(cfg.get("mrr_near_vector_slack", 0.05)),
        tie_mrr_eps=float(cfg.get("tie_mrr_eps", 0.01)),
        tie_ndcg_eps=float(cfg.get("tie_ndcg_eps", 0.01)),
    )

    selected_per_case = None
    if selection.get("status") == "selected":
        sel_cfg = (selection.get("selected") or {}).get("config") or {}
        full = score_config_on_cases(
            dev_cases, rankings_by_id, sel_cfg, top_k=top_k
        )
        selected_per_case = {
            "rank_regression_vs_vector_count": full[
                "rank_regression_vs_vector_count"
            ],
            "cases": [
                {
                    "case_id": row["case_id"],
                    "rank_regression_vs_vector": row["rank_regression_vs_vector"],
                    "failure_type": row["failure_type"],
                    "mrr@10": row["mrr@10"],
                    "vector_mrr@10": row["vector_mrr@10"],
                }
                for row in full.get("per_case") or []
            ],
        }

    return {
        "phase": "D.4",
        "mode": "offline_rrf_grid",
        "split_used_for_selection": "dev",
        "dev_case_count": len(dev_cases),
        "regression_case_count": len(regression_cases),
        "regression_used_for_selection": False,
        "top_k": top_k,
        "grid": {
            "recall_slack": cfg.get("recall_slack"),
            "mrr_near_vector_slack": cfg.get("mrr_near_vector_slack"),
            "rrf_k_values": cfg.get("rrf_k_values"),
            "dense_keyword_weights": cfg.get("dense_keyword_weights"),
            "point_count": len(points),
        },
        "vector_only_dev": {
            "metrics": vector_metrics,
            "rank_regression_vs_vector_count": 0,
            "ranking_denominator": (vector_score or {}).get("ranking_denominator"),
            "buckets": (vector_score or {}).get("buckets"),
        },
        "configs": scored,
        "selection": selection,
        "selected_per_case_dev": selected_per_case,
        "note": (
            "Offline RRF over D.3 keyword/vector key lists; attachment channel "
            "not present in offline fuse. Regression split sealed until final live."
        ),
    }


def summarize_strategy_metrics(
    cases: Sequence[Dict[str, Any]],
    strategy_rankings: Dict[str, Dict[str, List[str]]],
    strategy_names: Iterable[str],
    *,
    k_values: Sequence[int] = (5, 10, 20),
) -> Dict[str, Any]:
    """Aggregate metrics for named strategies (privacy-safe, no query text)."""
    out: Dict[str, Any] = {}
    for name in strategy_names:
        metric_lists: Dict[str, List[float]] = defaultdict(list)
        latencies: List[float] = []
        denom = 0
        for case in cases:
            if not bool(case.get("expected_answerable")):
                continue
            if not graded_relevance_map(case):
                continue
            cid = str(case["case_id"])
            pack = strategy_rankings.get(cid) or {}
            keys = pack.get(name) or []
            m = evaluate_ranking_list(case, keys, k_values=k_values)
            if m is None:
                continue
            denom += 1
            for mk, mv in m.items():
                metric_lists[mk].append(float(mv))
            lat = (pack.get("latency_ms") or {}).get(name)
            if lat is not None:
                latencies.append(float(lat))
        out[name] = {
            "ranking_denominator": denom,
            "metrics": {
                k: (float(mean(v)) if v else None) for k, v in sorted(metric_lists.items())
            },
            "latency_ms": {
                "p50": _percentile(latencies, 50) if latencies else None,
                "p95": _percentile(latencies, 95) if latencies else None,
                "n": len(latencies),
            },
        }
    return out


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    if len(xs) == 1:
        return float(xs[0])
    rank = (p / 100.0) * (len(xs) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(xs) - 1)
    frac = rank - lo
    return float(xs[lo] * (1 - frac) + xs[hi] * frac)


def count_rank_regressions(
    cases: Sequence[Dict[str, Any]],
    rankings_by_id: Dict[str, Dict[str, List[str]]],
    *,
    hybrid_name: str,
    baseline_name: str = "vector-only",
) -> Dict[str, Any]:
    n = 0
    ids: List[str] = []
    for case in cases:
        if not bool(case.get("expected_answerable")):
            continue
        if not graded_relevance_map(case):
            continue
        cid = str(case["case_id"])
        pack = rankings_by_id.get(cid) or {}
        hyb = evaluate_ranking_list(case, pack.get(hybrid_name) or [])
        base = evaluate_ranking_list(case, pack.get(baseline_name) or [])
        if hyb is None or base is None:
            continue
        if _metric_at(hyb, "mrr@10") + 1e-9 < _metric_at(base, "mrr@10"):
            n += 1
            ids.append(cid)
    return {"count": n, "case_ids": ids}

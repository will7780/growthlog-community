"""Shared single-pass semantic retrieval for /api/ai/search and retriever chat."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.services.ai_personalization_context import (
    compose_system_with_personalization,
    load_personalization_context,
)
from app.services.embedding import generate_embedding
from app.services.entry_family import (
    expand_ranked_hits_to_family_context,
    select_answer_context_refs,
)
from app.services.lookup_all_candidates import build_lookup_candidate_pool
from app.services.todo_knowledge import expand_todo_hits_to_tree_context
from app.services.lookup_judge import detect_lookup_intent
from app.services.query_normalize import extract_retrieval_topic
from app.services.reference_identity import annotate_with_canonical_keys
from app.services.retrieval_plan import build_retrieval_plan
from app.services.source_descriptor import (
    enrich_reference_descriptor,
    filter_references_for_plan,
)
from app.services.source_discovery import (
    build_inventory_answer,
    discover_source_scope,
    discover_sources,
)
from app.services.relevance_ranker import (
    candidate_pool_fingerprint,
    presentation_mode_from_intent,
    rank_candidates_by_relevance,
    ranking_fingerprint,
)

LOOKUP_NOT_FOUND = "在您的记录中没有找到相关内容。"
LOOKUP_CANDIDATE_BUILD_FAILED = "LOOKUP_CANDIDATE_BUILD_FAILED"
# Stable codes retained for old clients. The current retriever performs selection,
# concise synthesis, and citation choice in one bounded LLM turn.
LOOKUP_ANSWER_FAILED = "LOOKUP_ANSWER_FAILED"
LOOKUP_GROUNDING_FAILED = "LOOKUP_GROUNDING_FAILED"

MAX_DISPLAY_REFERENCES = 40
_INTERNAL_DISPLAY_RE = re.compile(
    r"(?:reference_key|chunk_id|entry_id|attachment_id|source_id|"
    r"[A-Za-z]:\\|/app/|/opt/|\\uploads\\|/uploads/)",
    re.IGNORECASE,
)


class RetrievalServiceError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 503):
        self.code = code
        self.message = message
        self.http_status = http_status
        super().__init__(message)


def safe_retrieval_user_message(code: str) -> str:
    mapping = {
        LOOKUP_CANDIDATE_BUILD_FAILED: "候选构建失败，请稍后重试",
        LOOKUP_ANSWER_FAILED: "答案生成失败，请重试",
        LOOKUP_GROUNDING_FAILED: "引用校验失败，请重试",
        "LLM_PROVIDER_FAILED": "AI 服务暂时不可用，请稍后重试",
        "LLM_PROVIDER_TIMEOUT": "AI 服务响应超时，请重试",
        "LLM_PROVIDER_RATE_LIMIT": "AI 服务繁忙，请稍后重试",
    }
    return mapping.get(str(code), "检索暂时不可用，请稍后重试")


def _display_title(ref: Dict[str, Any]) -> str:
    title = re.sub(r"\s+", " ", str(ref.get("title") or "")).strip()
    if not title:
        content = str(ref.get("content") or ref.get("snippet") or "")
        title = re.sub(r"\s+", " ", content.split("\n", 1)[0]).strip()
    title = re.sub(r"记录\s*#\s*\d+", "记录", title)
    if not title or _INTERNAL_DISPLAY_RE.search(title):
        return "相关记录"
    return title[:80]


def _display_reason(ref: Dict[str, Any]) -> str:
    reason = re.sub(r"\s+", " ", str(ref.get("relevance_reason") or "")).strip()
    if not reason or _INTERNAL_DISPLAY_RE.search(reason):
        reason = "内容与检索主题相关"
    return reason[:80]


def build_retriever_result_text(
    references: List[Dict[str, Any]],
    *,
    total_count: int,
    result_scope: str,
    degraded: bool,
) -> str:
    """Build the explicit bounded Hybrid fallback without a second LLM call."""
    if not references:
        return LOOKUP_NOT_FOUND

    if degraded:
        intro = (
            "AI 归纳暂时不可用，先按综合检索相关度展示"
            f" {total_count} 条候选："
        )
    elif result_scope == "budget_filtered":
        intro = f"候选较多，已为你筛选到 {total_count} 条相关记录："
    else:
        intro = f"为你找到 {total_count} 条相关记录："

    lines = [intro]
    for index, ref in enumerate(references, start=1):
        lines.append(
            f"{index}. {_display_title(ref)}：{_display_reason(ref)}〔{index}〕"
        )
    return "\n".join(lines)


async def run_shared_retrieval_turn(
    db: Session,
    *,
    user_id: int,
    query: str,
    intent_hint: Optional[str] = None,
    label_code: Optional[str] = None,
    top_k: int = 30,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
    generate_answer: bool = True,
    answer_style: str = "default",
) -> Dict[str, Any]:
    """Run one independent retriever turn.

    generate_answer and answer_style remain accepted for API compatibility.
    Non-stream and SSE share one LLM response that performs
    semantic selection, concise synthesis, and citation choice after RAG admission.
    """
    del top_k, generate_answer, answer_style

    raw_query = (query or "").strip()
    intent = detect_lookup_intent(raw_query, hint=intent_hint)
    plan_result = await build_retrieval_plan(
        raw_query,
        model_key=model_key,
        user_id=int(user_id),
        fallback_candidates=fallback_candidates,
    )
    retrieval_plan = plan_result.plan
    presentation_mode = presentation_mode_from_intent(intent)
    if retrieval_plan.operation == "discover_sources":
        presentation_mode = (
            "list_all"
            if retrieval_plan.presentation == "grouped_inventory"
            else "find_one"
        )
    retrieval_seed = (
        retrieval_plan.topic
        or retrieval_plan.named_source
        or raw_query
    )
    topic = extract_retrieval_topic(retrieval_seed)
    topic_terms = list(topic.get("topic_terms") or [])
    retrieval_query = str(topic.get("retrieval_query") or "").strip() or retrieval_seed
    pure_catalog_inventory = bool(
        retrieval_plan.operation == "discover_sources"
        and not retrieval_plan.topic
        and not retrieval_plan.named_source
    )

    shared_query_vector = None
    if not pure_catalog_inventory:
        try:
            shared_query_vector = generate_embedding(f"query: {retrieval_query}")
        except Exception:
            shared_query_vector = None

    personalization = load_personalization_context(
        db,
        int(user_id),
        query=raw_query,
        query_vector=shared_query_vector,
    )
    personalization_policy = (
        compose_system_with_personalization("", personalization).strip() or None
    )

    pool_built: Dict[str, Any] = {
        "candidates": [],
        "result_scope": "exhaustive",
        "candidate_budget_applied": False,
        "query_vector_reused": False,
    }
    if not pure_catalog_inventory:
        try:
            pool_built = build_lookup_candidate_pool(
                db,
                user_id=int(user_id),
                query=raw_query,
                label_code=label_code,
                query_vector=shared_query_vector,
                generate_query_embedding=False,
                topic_terms=topic_terms,
                retrieval_query=retrieval_query,
            )
        except Exception as exc:
            raise RetrievalServiceError(
                LOOKUP_CANDIDATE_BUILD_FAILED,
                safe_retrieval_user_message(LOOKUP_CANDIDATE_BUILD_FAILED),
            ) from exc

    candidate_pool = [
        enrich_reference_descriptor(ref)
        for ref in annotate_with_canonical_keys(
            list(pool_built.get("candidates") or [])
        )
    ]
    discovery = None
    source_scope_count = 0
    plan_has_filters = bool(
        retrieval_plan.named_source
        or retrieval_plan.filters.providers
        or retrieval_plan.filters.object_kinds
        or retrieval_plan.filters.tags
        or retrieval_plan.filters.date_range
        or retrieval_plan.filters.breadcrumb
        or retrieval_plan.filters.todo_status
    )
    if retrieval_plan.operation == "discover_sources":
        discovery = discover_sources(
            db,
            user_id=int(user_id),
            plan=retrieval_plan,
        )
        discovered_items = list(discovery.get("items") or [])
        if retrieval_plan.topic or retrieval_plan.named_source:
            filtered_pool = filter_references_for_plan(
                candidate_pool,
                retrieval_plan,
                require_named_source=bool(retrieval_plan.named_source),
                answer_context=False,
            )
            by_key = {
                str(item.get("reference_key") or ""): item
                for item in [*filtered_pool, *discovered_items]
                if str(item.get("reference_key") or "")
            }
            candidate_pool = list(by_key.values())
        else:
            candidate_pool = discovered_items
    elif retrieval_plan.operation == "discover_then_answer":
        source_scope = discover_source_scope(
            db,
            user_id=int(user_id),
            plan=retrieval_plan,
        )
        source_scope_count = len(source_scope)
        scope_by_key = {
            str(item.get("reference_key") or ""): item
            for item in source_scope
            if str(item.get("reference_key") or "")
        }
        candidate_pool = [
            item
            for item in filter_references_for_plan(
                candidate_pool,
                retrieval_plan,
                require_named_source=bool(retrieval_plan.named_source),
                answer_context=True,
            )
            if str(item.get("reference_key") or "") in scope_by_key
        ]
        if retrieval_plan.named_source and not candidate_pool:
            candidate_pool = list(scope_by_key.values())
    elif plan_has_filters:
        candidate_pool = filter_references_for_plan(
            candidate_pool,
            retrieval_plan,
            require_named_source=bool(retrieval_plan.named_source),
            answer_context=True,
        )
        if retrieval_plan.named_source and not candidate_pool:
            discovery = discover_sources(
                db,
                user_id=int(user_id),
                plan=retrieval_plan,
            )
            candidate_pool = [
                item
                for item in list(discovery.get("items") or [])
                if bool((item.get("metadata") or {}).get("sync_status") in {"", "indexed", "partial"})
            ]

    result_scope = (
        "paginated" if discovery and discovery.get("has_more")
        else str(pool_built.get("result_scope") or "exhaustive")
    )
    pool_fingerprint = candidate_pool_fingerprint(
        [str(ref.get("reference_key") or "") for ref in candidate_pool]
    )

    base_meta: Dict[str, Any] = {
        "intent": intent,
        "presentation_mode": presentation_mode,
        "found": False,
        "candidate_count": len(candidate_pool),
        "judged_count": 0,
        "ranked_count": 0,
        "relevant_count": 0,
        "result_scope": result_scope,
        "candidate_budget_applied": bool(pool_built.get("candidate_budget_applied")),
        "lookup_error_code": None,
        "ranking_strategy": "single_llm_semantic_synthesis",
        "ranking_degraded": False,
        "selection_provider_calls": 0,
        "synthesis_provider_calls": 0,
        "selection_input_count": 0,
        "selection_payload_chars": 0,
        "selection_repaired": False,
        "synthesis_repaired": False,
        "synthesis_output_chars": 0,
        "selection_truncated": False,
        "selection_unreviewed_count": 0,
        "rag_admission_input_count": 0,
        "rag_admission_count": 0,
        "rag_admission_rejected_count": 0,
        "rag_admission_channel_input": {},
        "rag_admission_channel_admitted": {},
        "rag_admission_channel_thresholds": {},
        "fallback_count": 0,
        "candidate_pool_fingerprint": pool_fingerprint,
        "ranking_fingerprint": ranking_fingerprint([]),
        "topic_fingerprint": topic.get("canonical_topic"),
        "retrieval_topic": topic.get("canonical_topic"),
        "personalization": personalization.safe_diag(),
        "embedding_reused": bool(pool_built.get("query_vector_reused")),
        "answer_context_count": 0,
        "retrieval_plan": {
            "operation": retrieval_plan.operation,
            "presentation": retrieval_plan.presentation,
            "providers": list(retrieval_plan.filters.providers),
            "object_kinds": list(retrieval_plan.filters.object_kinds),
            "has_topic": bool(retrieval_plan.topic),
            "has_named_source": bool(retrieval_plan.named_source),
        },
        "retrieval_plan_degraded": bool(plan_result.degraded),
        "retrieval_plan_repaired": bool(plan_result.repaired),
        "retrieval_plan_error_code": plan_result.error_code,
        "source_discovery_total": int(discovery.get("total_count") or 0) if discovery else 0,
        "source_scope_count": source_scope_count,
        "source_discovery_has_more": bool(discovery and discovery.get("has_more")),
        "source_discovery_groups": dict(discovery.get("provider_groups") or {}) if discovery else {},
    }

    def _payload(
        *,
        answer: str,
        ranked: List[Dict[str, Any]],
        results: List[Dict[str, Any]],
        expanded: List[Dict[str, Any]],
        answer_context: List[Dict[str, Any]],
        used: List[Dict[str, Any]],
        found: bool,
        model_fallback: Optional[str] = None,
    ) -> Dict[str, Any]:
        return {
            "query": raw_query,
            "intent": intent,
            "presentation_mode": presentation_mode,
            "answer": answer,
            "retrieval_candidates": ranked,
            "retrieval_results": results,
            "expanded_context_refs": expanded,
            "answer_context_refs": answer_context,
            "used_references": used,
            "invalid_references": [],
            "found": found,
            "meta": base_meta,
            "model_fallback": model_fallback,
            "personalization": personalization.safe_diag(),
            "system_policy": personalization_policy,
            "query_vector": shared_query_vector,
        }

    if not candidate_pool:
        return _payload(
            answer=(
                "没有找到符合这些条件的来源。"
                if retrieval_plan.operation == "discover_sources"
                else LOOKUP_NOT_FOUND
            ),
            ranked=[],
            results=[],
            expanded=[],
            answer_context=[],
            used=[],
            found=False,
        )

    if (
        retrieval_plan.operation == "discover_sources"
        and not retrieval_plan.topic
        and not retrieval_plan.named_source
        and discovery is not None
    ):
        base_meta.update(
            {
                "found": True,
                "judged_count": 0,
                "ranked_count": len(candidate_pool),
                "relevant_count": len(candidate_pool),
                "ranking_strategy": "source_catalog_inventory",
                "ranking_fingerprint": ranking_fingerprint(
                    [
                        str(ref.get("reference_key") or "")
                        for ref in candidate_pool
                    ]
                ),
            }
        )
        return _payload(
            answer=build_inventory_answer(discovery),
            ranked=candidate_pool,
            results=candidate_pool,
            expanded=candidate_pool,
            answer_context=[],
            used=candidate_pool[:MAX_DISPLAY_REFERENCES],
            found=True,
        )

    ranking = await rank_candidates_by_relevance(
        query=retrieval_query,
        user_query=raw_query,
        presentation_mode=presentation_mode,
        candidates=candidate_pool,
        topic_terms=topic_terms,
        model_key=model_key,
        user_id=int(user_id),
        fallback_candidates=fallback_candidates,
        system_policy=personalization_policy,
    )
    ranked_all = list(ranking.get("ranked") or [])
    retrieval_results = list(ranking.get("retrieval_results") or [])
    rank_meta = dict(ranking.get("meta") or {})
    found = bool(ranking.get("found") and retrieval_results)
    degraded = bool(rank_meta.get("ranking_degraded") or rank_meta.get("degraded"))

    base_meta.update(
        {
            "found": found,
            "judged_count": int(
                rank_meta.get("selection_input_count") or len(candidate_pool)
            ),
            "ranked_count": int(rank_meta.get("ranked_count") or len(ranked_all)),
            "relevant_count": int(
                rank_meta.get("relevant_count") or len(retrieval_results)
            ),
            "ranking_strategy": str(
                rank_meta.get("strategy") or "single_llm_semantic_synthesis"
            ),
            "ranking_degraded": degraded,
            "selection_provider_calls": int(
                rank_meta.get("selection_provider_calls") or 0
            ),
            "synthesis_provider_calls": int(
                rank_meta.get("synthesis_provider_calls")
                or rank_meta.get("selection_provider_calls")
                or 0
            ),
            "selection_input_count": int(
                rank_meta.get("selection_input_count") or 0
            ),
            "selection_payload_chars": int(
                rank_meta.get("selection_payload_chars") or 0
            ),
            "selection_repaired": bool(rank_meta.get("selection_repaired")),
            "synthesis_repaired": bool(
                rank_meta.get("synthesis_repaired")
                or rank_meta.get("selection_repaired")
            ),
            "synthesis_output_chars": int(
                rank_meta.get("synthesis_output_chars") or 0
            ),
            "selection_truncated": bool(rank_meta.get("selection_truncated")),
            "selection_unreviewed_count": int(
                rank_meta.get("selection_unreviewed_count") or 0
            ),
            "rag_admission_input_count": int(
                rank_meta.get("rag_admission_input_count") or 0
            ),
            "rag_admission_count": int(
                rank_meta.get("rag_admission_count") or 0
            ),
            "rag_admission_rejected_count": int(
                rank_meta.get("rag_admission_rejected_count") or 0
            ),
            "rag_admission_channel_input": dict(
                rank_meta.get("rag_admission_channel_input") or {}
            ),
            "rag_admission_channel_admitted": dict(
                rank_meta.get("rag_admission_channel_admitted") or {}
            ),
            "rag_admission_channel_thresholds": dict(
                rank_meta.get("rag_admission_channel_thresholds") or {}
            ),
            "llm_scored_count": int(rank_meta.get("llm_scored_count") or 0),
            "fallback_count": int(rank_meta.get("fallback_count") or 0),
            "unresolved_count": int(rank_meta.get("unresolved_count") or 0),
            "scoring_origin": dict(rank_meta.get("scoring_origin") or {}),
            "candidate_pool_fingerprint": str(
                ranking.get("candidate_pool_fingerprint") or pool_fingerprint
            ),
            "ranking_fingerprint": str(
                ranking.get("ranking_fingerprint")
                or ranking_fingerprint(
                    [
                        str(ref.get("reference_key") or "")
                        for ref in retrieval_results
                    ]
                )
            ),
            "topic_fingerprint": (
                ranking.get("topic_fingerprint") or topic.get("canonical_topic")
            ),
        }
    )

    if not found:
        return _payload(
            answer=LOOKUP_NOT_FOUND,
            ranked=ranked_all,
            results=[],
            expanded=[],
            answer_context=[],
            used=[],
            found=False,
        )

    record_context_refs = expand_ranked_hits_to_family_context(
        db,
        int(user_id),
        retrieval_results,
    )
    todo_context_refs = (
        expand_todo_hits_to_tree_context(
            db,
            user_id=int(user_id),
            ranked_hits=retrieval_results,
        )
        if any(str(item.get("source_type") or "") == "todo" for item in retrieval_results)
        else []
    )
    from app.services.notion_knowledge import expand_notion_page_context

    notion_context_refs = (
        expand_notion_page_context(
            db,
            user_id=int(user_id),
            ranked_hits=retrieval_results,
        )
        if any(str(item.get("source_type") or "") == "notion_page" for item in retrieval_results)
        else []
    )
    expanded_context_refs = list(record_context_refs) + list(todo_context_refs) + list(notion_context_refs)
    answer_context_refs = select_answer_context_refs(
        expanded_context_refs,
        retrieval_results,
    )
    base_meta["answer_context_count"] = len(answer_context_refs)

    display_references = list(
        ranking.get("answer_refs") or retrieval_results
    )[:MAX_DISPLAY_REFERENCES]
    if degraded:
        answer_text = build_retriever_result_text(
            display_references,
            total_count=len(retrieval_results),
            result_scope=result_scope,
            degraded=True,
        )
    else:
        answer_text = str(ranking.get("answer") or "").strip()

    return _payload(
        answer=answer_text,
        ranked=ranked_all,
        results=retrieval_results,
        expanded=expanded_context_refs,
        answer_context=answer_context_refs,
        used=display_references,
        found=True,
        model_fallback="hybrid" if degraded else None,
    )
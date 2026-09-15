#!/usr/bin/env python3
"""R11.13 query planner, source discovery, and privacy contracts."""
from __future__ import annotations

import asyncio
import json
import sys
import traceback
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.retrieval_plan import RetrievalFilters, RetrievalPlan, RetrievalPlanResult, build_retrieval_plan
from app.services.source_descriptor import filter_references_for_plan, source_descriptor_from_reference

BLOCKERS: list[str] = []


def _notion_ref(*, status: str = "indexed", title: str = "训练计划") -> dict:
    return {
        "source_type": "notion_page",
        "source_id": 91,
        "notion_page_id": 91,
        "reference_key": "notion_page:source:91",
        "title": title,
        "label_name": "Notion",
        "created_at": "2026-09-01T10:00:00",
        "content": "跑步后的恢复方法",
        "snippet": "跑步后的恢复方法",
        "metadata": {
            "provider": "notion",
            "provider_label": "Notion",
            "object_kind": "database_row",
            "breadcrumb": "健康资料 / 训练数据库 / 训练计划",
            "sync_status": status,
        },
    }


def _todo_ref() -> dict:
    return {
        "source_type": "todo",
        "source_id": 91,
        "reference_key": "todo:source:91",
        "title": "阅读 Notion 教程",
        "content": "阅读 Notion 教程",
        "metadata": {
            "provider": "growthlog",
            "provider_label": "GrowthLog",
            "object_kind": "todo",
            "sync_status": "indexed",
            "status": "open",
        },
    }


def test_plan_schema_and_examples() -> None:
    inventory = RetrievalPlan.model_validate({
        "operation": "discover_sources",
        "topic": None,
        "named_source": None,
        "filters": {"providers": ["notion"]},
        "presentation": "grouped_inventory",
    })
    assert inventory.filters.providers == ["notion"]

    scoped = RetrievalPlan.model_validate({
        "operation": "discover_then_answer",
        "topic": "恢复方法",
        "named_source": "行动启动与动力不足",
        "filters": {"providers": ["notion"], "object_kinds": ["page"]},
        "presentation": "answer",
    })
    assert scoped.named_source == "行动启动与动力不足"

    try:
        RetrievalPlan.model_validate({
            "operation": "discover_sources",
            "filters": {"providers": ["dropbox"]},
            "presentation": "grouped_inventory",
        })
        raise AssertionError("invented provider must be rejected")
    except ValidationError:
        pass

    for invalid_range in (
        {"start": "2026/09/01"},
        {"start": "2026-09-03", "end": "2026-09-01"},
    ):
        try:
            RetrievalPlan.model_validate({
                "operation": "discover_sources",
                "filters": {"date_range": invalid_range},
                "presentation": "grouped_inventory",
            })
            raise AssertionError("invalid date range must be rejected")
        except ValidationError:
            pass


def test_planner_failure_falls_back_to_content_search() -> None:
    from app.services.structured_llm import StructuredLLMError

    async def execute():
        with patch(
            "app.services.retrieval_plan.generate_structured_json",
            new=AsyncMock(side_effect=StructuredLLMError("RETRIEVAL_PLAN_SCHEMA_INVALID")),
        ):
            return await build_retrieval_plan("运动后怎么恢复")

    result = asyncio.run(execute())
    assert result.degraded is True
    assert result.plan.operation == "answer_content"
    assert result.plan.presentation == "answer"
    assert result.plan.topic


def test_source_descriptor_filters_and_pending_semantics() -> None:
    notion = _notion_ref()
    todo = _todo_ref()
    descriptor = source_descriptor_from_reference(notion)
    assert descriptor.provider == "notion"
    assert descriptor.object_kind == "database_row"
    assert descriptor.breadcrumb == "健康资料 / 训练数据库 / 训练计划"
    assert descriptor.answerable is True

    inventory = RetrievalPlan(
        operation="discover_sources",
        filters=RetrievalFilters(providers=["notion"]),
        presentation="grouped_inventory",
    )
    filtered = filter_references_for_plan([todo, notion], inventory)
    assert [item["reference_key"] for item in filtered] == ["notion_page:source:91"]

    pending = _notion_ref(status="pending")
    assert source_descriptor_from_reference(pending).answerable is False
    assert filter_references_for_plan([pending], inventory, answer_context=False)
    assert not filter_references_for_plan([pending], inventory, answer_context=True)


def test_ranker_summary_has_attributes_without_internal_identity() -> None:
    from app.services.relevance_ranker import build_safe_summary

    ref = _notion_ref()
    ref["chunk_id"] = 888
    ref["metadata"]["internal_path"] = "/opt/private"
    summary = build_safe_summary(ref, alias="S1", hybrid_rank=1)
    assert summary["source_provider"] == "notion"
    assert summary["source_object_kind"] == "database_row"
    assert summary["source_breadcrumb"] == "健康资料 / 训练数据库 / 训练计划"
    dumped = json.dumps(summary, ensure_ascii=False)
    for forbidden in ("notion_page:source:91", "chunk_id", "/opt/private", "source_id"):
        assert forbidden not in dumped


def test_inventory_query_returns_only_catalog_provider() -> None:
    from app.services.ai_retrieval_service import run_shared_retrieval_turn

    plan = RetrievalPlan(
        operation="discover_sources",
        filters=RetrievalFilters(providers=["notion"]),
        presentation="grouped_inventory",
    )
    plan_result = RetrievalPlanResult(plan=plan, degraded=False, repaired=False, error_code=None)
    discovery = {
        "items": [_notion_ref()],
        "total_count": 1,
        "offset": 0,
        "limit": 40,
        "has_more": False,
        "provider_groups": {"Notion": 1},
        "object_kind_groups": {"database_row": 1},
        "status_groups": {"indexed": 1},
        "answerable_reference_keys": {"notion_page:source:91"},
    }
    personalization = MagicMock(safe_diag=lambda: {})

    embedding = MagicMock(side_effect=AssertionError("inventory must not embed query"))
    candidate_builder = MagicMock(side_effect=AssertionError("inventory must not scan content chunks"))

    async def execute():
        with (
            patch("app.services.ai_retrieval_service.build_retrieval_plan", new=AsyncMock(return_value=plan_result)),
            patch("app.services.ai_retrieval_service.generate_embedding", new=embedding),
            patch("app.services.ai_retrieval_service.load_personalization_context", return_value=personalization),
            patch("app.services.ai_retrieval_service.compose_system_with_personalization", return_value=""),
            patch("app.services.ai_retrieval_service.build_lookup_candidate_pool", new=candidate_builder),
            patch("app.services.ai_retrieval_service.discover_sources", return_value=discovery),
            patch(
                "app.services.ai_retrieval_service.rank_candidates_by_relevance",
                new=AsyncMock(side_effect=AssertionError("inventory must not call answer ranker")),
            ),
        ):
            return await run_shared_retrieval_turn(MagicMock(), user_id=7, query="我有什么 Notion 文档")

    result = asyncio.run(execute())
    embedding.assert_not_called()
    candidate_builder.assert_not_called()
    assert result["found"] is True
    assert [item["source_type"] for item in result["retrieval_results"]] == ["notion_page"]
    assert all(item["source_type"] != "todo" for item in result["used_references"])
    assert "Notion" in result["answer"]
    assert result["meta"]["retrieval_plan"]["providers"] == ["notion"]


def test_explainer_inventory_uses_catalog_without_rag() -> None:
    from app.services.ai_explainer import run_planned_global_rag

    plan = RetrievalPlan(
        operation="discover_sources",
        filters=RetrievalFilters(providers=["notion"]),
        presentation="grouped_inventory",
    )
    plan_result = RetrievalPlanResult(
        plan=plan, degraded=False, repaired=False, error_code=None
    )

    async def execute():
        with (
            patch(
                "app.services.ai_explainer.build_retrieval_plan",
                new=AsyncMock(return_value=plan_result),
            ),
            patch(
                "app.services.ai_explainer.run_global_rag",
                side_effect=AssertionError("inventory must not scan content chunks"),
            ) as rag,
            patch(
                "app.services.ai_explainer.discover_source_scope",
                return_value=[_notion_ref()],
            ) as discovery,
        ):
            result = await run_planned_global_rag(
                MagicMock(), user_id=7, query="我有什么 Notion 文档", reason="test"
            )
            rag.assert_not_called()
            discovery.assert_called_once()
            return result

    result = asyncio.run(execute())
    assert [item["source_type"] for item in result] == ["notion_page"]
def test_discover_then_answer_scopes_content_search() -> None:
    from app.services.ai_retrieval_service import run_shared_retrieval_turn

    plan = RetrievalPlan(
        operation="discover_then_answer",
        topic="跑步恢复",
        filters=RetrievalFilters(providers=["notion"]),
        presentation="answer",
    )
    plan_result = RetrievalPlanResult(plan=plan, degraded=False, repaired=False, error_code=None)
    captured = {}

    async def ranker(*, candidates, **kwargs):
        captured["keys"] = [item["reference_key"] for item in candidates]
        ranked = [dict(item, relevance_level=3) for item in candidates]
        return {
            "ranked": ranked,
            "retrieval_results": ranked,
            "answer_refs": ranked,
            "answer": "跑步恢复需要循序渐进〔1〕",
            "found": True,
            "meta": {"selection_input_count": len(ranked), "relevant_count": len(ranked)},
        }

    personalization = MagicMock(safe_diag=lambda: {})

    async def execute():
        with (
            patch("app.services.ai_retrieval_service.build_retrieval_plan", new=AsyncMock(return_value=plan_result)),
            patch("app.services.ai_retrieval_service.generate_embedding", return_value=[0.1, 0.2]),
            patch("app.services.ai_retrieval_service.load_personalization_context", return_value=personalization),
            patch("app.services.ai_retrieval_service.compose_system_with_personalization", return_value=""),
            patch("app.services.ai_retrieval_service.build_lookup_candidate_pool", return_value={
                "candidates": [_todo_ref(), _notion_ref()],
                "result_scope": "exhaustive",
                "query_vector_reused": True,
            }),
            patch("app.services.ai_retrieval_service.discover_source_scope", return_value=[_notion_ref()]),
            patch("app.services.ai_retrieval_service.rank_candidates_by_relevance", new=AsyncMock(side_effect=ranker)),
            patch("app.services.ai_retrieval_service.expand_ranked_hits_to_family_context", side_effect=lambda db, uid, refs: list(refs)),
            patch("app.services.notion_knowledge.expand_notion_page_context", return_value=[]),
            patch("app.services.ai_retrieval_service.select_answer_context_refs", side_effect=lambda expanded, ranked: list(expanded)),
        ):
            return await run_shared_retrieval_turn(MagicMock(), user_id=7, query="Notion 里的跑步恢复内容讲了什么")

    result = asyncio.run(execute())
    assert captured["keys"] == ["notion_page:source:91"]
    assert result["found"] is True
    assert result["meta"]["source_scope_count"] == 1
    assert all(item["source_type"] == "notion_page" for item in result["retrieval_results"])


def test_notion_object_kind_does_not_mislabel_orphan_child() -> None:
    from app.services.notion_reference import infer_notion_object_kind

    database_row = SimpleNamespace(
        parent_notion_page_uuid="data-source",
        breadcrumb="父页面 / 数据库 / 数据行",
    )
    orphan_child = SimpleNamespace(
        parent_notion_page_uuid="unavailable-parent",
        breadcrumb="普通子页面",
    )
    known_child = SimpleNamespace(
        parent_notion_page_uuid="known-parent",
        breadcrumb="父页面 / 普通子页面",
    )
    assert infer_notion_object_kind(database_row, {"parent-page"}) == "database_row"
    assert infer_notion_object_kind(orphan_child, {"other-page"}) == "page"
    assert infer_notion_object_kind(known_child, {"known-parent"}) == "page"


def test_normalize_data_source_title_and_parent() -> None:
    from app.services.notion_normalize import extract_data_source_title, parent_uuid

    data_source = {
        "title": [{"plain_text": "训练数据库"}],
        "parent": {"type": "page_id", "page_id": "parent-page"},
    }
    row = {"parent": {"type": "data_source_id", "data_source_id": "data-source"}}
    assert extract_data_source_title(data_source) == "训练数据库"
    assert parent_uuid(data_source) == "parent-page"
    assert parent_uuid(row) == "data-source"


def main() -> int:
    tests = [
        test_plan_schema_and_examples,
        test_planner_failure_falls_back_to_content_search,
        test_source_descriptor_filters_and_pending_semantics,
        test_ranker_summary_has_attributes_without_internal_identity,
        test_inventory_query_returns_only_catalog_provider,
        test_explainer_inventory_uses_catalog_without_rag,
        test_discover_then_answer_scopes_content_search,
        test_notion_object_kind_does_not_mislabel_orphan_child,
        test_normalize_data_source_title_and_parent,
    ]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as exc:
            failed += 1
            BLOCKERS.append(f"{fn.__name__}:{type(exc).__name__}:{exc}")
            print(f"FAIL {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()
    if BLOCKERS:
        print("BLOCKERS:")
        for blocker in BLOCKERS:
            print(f" - {blocker}")
    print(f"failed={failed}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
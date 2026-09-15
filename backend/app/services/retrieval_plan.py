"""Structured query planning for provider-neutral retrieval.

The planner decides *how* to retrieve. It never emits SQL, internal IDs, or
provider-specific implementation details. Invalid/provider-failed plans fall
back to the pre-R11.13 content-search path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.services.query_normalize import extract_retrieval_topic
from app.services.structured_llm import StructuredLLMError, generate_structured_json

RetrievalOperation = Literal["discover_sources", "answer_content", "discover_then_answer"]
RetrievalProvider = Literal["notion", "growthlog", "future_connector"]
RetrievalObjectKind = Literal["record", "todo", "page", "database_row", "attachment"]
RetrievalPresentation = Literal["grouped_inventory", "ranked_sources", "answer"]


class RetrievalDateRange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: Optional[str] = Field(default=None, max_length=32)
    end: Optional[str] = Field(default=None, max_length=32)

    @field_validator("start", "end")
    @classmethod
    def _validate_iso_date(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("date_must_be_iso_yyyy_mm_dd") from exc

    @model_validator(mode="after")
    def _validate_order(self) -> "RetrievalDateRange":
        if self.start and self.end and self.start > self.end:
            raise ValueError("date_range_start_after_end")
        return self


class RetrievalFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    providers: List[RetrievalProvider] = Field(default_factory=list, max_length=3)
    object_kinds: List[RetrievalObjectKind] = Field(default_factory=list, max_length=5)
    tags: List[str] = Field(default_factory=list, max_length=12)
    date_range: Optional[RetrievalDateRange] = None
    breadcrumb: Optional[str] = Field(default=None, max_length=240)
    todo_status: Optional[Literal["open", "completed"]] = None

    @field_validator("providers", "object_kinds")
    @classmethod
    def _dedupe_enums(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @field_validator("tags")
    @classmethod
    def _clean_tags(cls, value: list[str]) -> list[str]:
        cleaned = []
        for item in value:
            text = " ".join(str(item or "").split())[:40]
            if text and text not in cleaned:
                cleaned.append(text)
        return cleaned

    @field_validator("breadcrumb")
    @classmethod
    def _clean_breadcrumb(cls, value: Optional[str]) -> Optional[str]:
        text = " ".join(str(value or "").split())[:240]
        return text or None


class RetrievalPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: RetrievalOperation
    topic: Optional[str] = Field(default=None, max_length=240)
    named_source: Optional[str] = Field(default=None, max_length=240)
    filters: RetrievalFilters = Field(default_factory=RetrievalFilters)
    presentation: RetrievalPresentation

    @field_validator("topic", "named_source")
    @classmethod
    def _clean_text(cls, value: Optional[str]) -> Optional[str]:
        text = " ".join(str(value or "").split())[:240]
        return text or None

    @model_validator(mode="after")
    def _coherent_presentation(self) -> "RetrievalPlan":
        if self.operation == "answer_content" and self.presentation != "answer":
            raise ValueError("answer_content_requires_answer")
        if self.operation == "discover_sources" and self.presentation == "answer":
            raise ValueError("discover_sources_cannot_answer")
        if self.operation == "discover_then_answer" and self.presentation != "answer":
            raise ValueError("discover_then_answer_requires_answer")
        return self


@dataclass(frozen=True)
class RetrievalPlanResult:
    plan: RetrievalPlan
    degraded: bool
    repaired: bool
    error_code: Optional[str]


PLANNER_SYSTEM = """你是 GrowthLog 的问题理解器，只输出 JSON 检索计划，不回答用户问题。

可选 operation：discover_sources、answer_content、discover_then_answer。
可选 provider：notion、growthlog、future_connector。
可选 object_kind：record、todo、page、database_row、attachment。
可选 presentation：grouped_inventory、ranked_sources、answer。

判断原则：
1. 用户要盘点、列出或查找某类文档/来源对象，用 discover_sources。
2. 用户询问具体事实、观点、方法或内容，用 answer_content。
3. 用户明确限定某个来源、页面或文档范围并要求解释正文，用 discover_then_answer。
4. Notion、GrowthLog、小要事、附件是来源属性；运动训练、产品设计等是 topic。
5. 明确书名、页面名、文档名放 named_source；父级路径约束放 breadcrumb。
6. 不得创造枚举外的 provider、object_kind、字段或数据库条件。
7. 没有明确过滤条件时数组为空。不要把普通语气词放进 topic。

只输出与以下形状一致的 JSON：
{"operation":"...","topic":null,"named_source":null,"filters":{"providers":[],"object_kinds":[],"tags":[],"date_range":null,"breadcrumb":null,"todo_status":null},"presentation":"..."}
"""


def fallback_retrieval_plan(query: str) -> RetrievalPlan:
    """Preserve the pre-planner behavior when structured planning is unavailable."""
    topic = extract_retrieval_topic(query or "")
    retrieval_query = str(topic.get("retrieval_query") or "").strip()
    return RetrievalPlan(
        operation="answer_content",
        topic=retrieval_query or (query or "").strip() or None,
        named_source=None,
        filters=RetrievalFilters(),
        presentation="answer",
    )


async def build_retrieval_plan(
    query: str,
    *,
    model_key: Optional[str] = None,
    user_id: Optional[int] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> RetrievalPlanResult:
    """Build one validated plan; protocol/provider failures use the legacy path."""
    text = " ".join(str(query or "").split()).strip()
    fallback = fallback_retrieval_plan(text)
    if not text:
        return RetrievalPlanResult(fallback, True, False, "QUERY_PLAN_EMPTY")
    try:
        result = await generate_structured_json(
            system=PLANNER_SYSTEM,
            messages=[{"role": "user", "content": text}],
            mode="retrieval",
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            max_tokens=600,
            temperature=0.0,
            schema_model=RetrievalPlan,
            allow_repair=True,
            prefer_json_mode=True,
            stage="retrieval_plan",
            provider_error_code="RETRIEVAL_PLAN_PROVIDER_FAILED",
            syntax_error_code="RETRIEVAL_PLAN_JSON_INVALID",
            schema_error_code="RETRIEVAL_PLAN_SCHEMA_INVALID",
        )
        plan = result.model
        if not isinstance(plan, RetrievalPlan):
            plan = RetrievalPlan.model_validate(result.data)
        return RetrievalPlanResult(plan, False, bool(result.repaired), None)
    except StructuredLLMError as exc:
        return RetrievalPlanResult(fallback, True, False, str(exc.code))

"""Provider-neutral source metadata used by planning, filtering, and UI."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

from app.services.retrieval_plan import RetrievalPlan


@dataclass(frozen=True)
class SourceDescriptor:
    provider: str
    provider_label: str
    object_kind: str
    title: str
    breadcrumb: str
    user_tags: tuple[str, ...]
    sync_status: str
    external_url: Optional[str]
    canonical_reference: str
    answerable: bool

    def safe_dict(self) -> Dict[str, Any]:
        """Fields safe for an LLM candidate summary or public display."""
        return {
            "provider": self.provider,
            "provider_label": self.provider_label,
            "object_kind": self.object_kind,
            "title": self.title[:120],
            "breadcrumb": self.breadcrumb[:240],
            "user_tags": list(self.user_tags[:6]),
            "sync_status": self.sync_status,
        }


def _clean(value: Any, maximum: int) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _tags(ref: Mapping[str, Any]) -> tuple[str, ...]:
    raw: List[Any] = []
    labels = ref.get("labels")
    if isinstance(labels, (list, tuple)):
        raw.extend(labels)
    for key in ("label_name", "label_code"):
        if ref.get(key):
            raw.append(ref.get(key))
    result: List[str] = []
    for item in raw:
        value = item.get("name") if isinstance(item, dict) else item
        text = _clean(value, 40)
        if text and text not in result:
            result.append(text)
    return tuple(result)


def source_descriptor_from_reference(ref: Mapping[str, Any]) -> SourceDescriptor:
    metadata = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
    source_type = str(ref.get("source_type") or "entry").strip().lower()
    canonical = _clean(ref.get("reference_key"), 128)

    if source_type == "notion_page":
        provider = "notion"
        provider_label = "Notion"
        object_kind = str(metadata.get("object_kind") or "page")
        if object_kind not in {"page", "database_row"}:
            object_kind = "page"
        sync_status = str(metadata.get("sync_status") or "")
        answerable = sync_status in {"indexed", "partial"}
        external_url = str(metadata.get("external_url") or "").strip() or None
    else:
        provider = "growthlog"
        provider_label = "GrowthLog"
        external_url = None
        sync_status = "indexed"
        answerable = True
        if source_type == "todo":
            object_kind = "todo"
        elif source_type in {"attachment", "attachment_chunk"}:
            object_kind = "attachment"
        else:
            object_kind = "record"

    return SourceDescriptor(
        provider=provider,
        provider_label=provider_label,
        object_kind=object_kind,
        title=_clean(ref.get("title"), 120) or "未命名来源",
        breadcrumb=_clean(metadata.get("breadcrumb"), 240),
        user_tags=_tags(ref),
        sync_status=sync_status,
        external_url=external_url,
        canonical_reference=canonical,
        answerable=answerable,
    )


def enrich_reference_descriptor(ref: Mapping[str, Any]) -> Dict[str, Any]:
    item = dict(ref)
    metadata = dict(item.get("metadata") or {})
    descriptor = source_descriptor_from_reference(item)
    metadata["provider"] = descriptor.provider
    metadata["provider_label"] = descriptor.provider_label
    metadata["object_kind"] = descriptor.object_kind
    metadata["breadcrumb"] = descriptor.breadcrumb
    metadata["sync_status"] = descriptor.sync_status
    item["metadata"] = metadata
    if not item.get("label_name"):
        item["label_name"] = descriptor.provider_label
    return item


def _date_matches(ref: Mapping[str, Any], plan: RetrievalPlan) -> bool:
    date_range = plan.filters.date_range
    if date_range is None:
        return True
    value = str(
        ref.get("created_at")
        or (ref.get("metadata") or {}).get("synced_at")
        or ""
    )[:10]
    if not value:
        return False
    if date_range.start and value < str(date_range.start)[:10]:
        return False
    if date_range.end and value > str(date_range.end)[:10]:
        return False
    return True


def reference_matches_plan(
    ref: Mapping[str, Any],
    plan: RetrievalPlan,
    *,
    require_named_source: bool = True,
    answer_context: bool = False,
) -> bool:
    descriptor = source_descriptor_from_reference(ref)
    filters = plan.filters

    if filters.providers and descriptor.provider not in set(filters.providers):
        return False
    if filters.object_kinds and descriptor.object_kind not in set(filters.object_kinds):
        return False
    if filters.tags:
        available = {item.casefold() for item in descriptor.user_tags}
        if not all(str(item).casefold() in available for item in filters.tags):
            return False
    if filters.breadcrumb and str(filters.breadcrumb).casefold() not in descriptor.breadcrumb.casefold():
        return False
    if filters.todo_status:
        metadata = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
        if descriptor.object_kind != "todo" or str(metadata.get("status")) != filters.todo_status:
            return False
    if require_named_source and plan.named_source:
        needle = str(plan.named_source).casefold()
        hay = f"{descriptor.title}\n{descriptor.breadcrumb}".casefold()
        if needle not in hay:
            return False
    if not _date_matches(ref, plan):
        return False
    if answer_context and not descriptor.answerable:
        return False
    return True


def filter_references_for_plan(
    references: Iterable[Mapping[str, Any]],
    plan: RetrievalPlan,
    *,
    require_named_source: bool = True,
    answer_context: bool = False,
) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for raw in references:
        item = enrich_reference_descriptor(raw)
        if not reference_matches_plan(
            item,
            plan,
            require_named_source=require_named_source,
            answer_context=answer_context,
        ):
            continue
        key = str(item.get("reference_key") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(item)
    return result

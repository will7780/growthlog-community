"""Catalog-level source discovery for R11.13.

Discovery works on source objects and metadata. It does not scan every chunk or
pretend pending external pages can answer content questions.
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import Entry, EntryAttachment, EntryLabel, NotionPage, Todo
from app.services.keyword_search import extract_query_terms
from app.services.notion_reference import infer_notion_object_kind, notion_page_reference
from app.services.reference_identity import annotate_with_canonical_keys
from app.services.retrieval_plan import RetrievalPlan
from app.services.source_descriptor import (
    enrich_reference_descriptor,
    filter_references_for_plan,
    source_descriptor_from_reference,
)
from app.services.todo_hierarchy import load_index
from app.services.todo_knowledge import todo_reference

DEFAULT_DISCOVERY_PAGE_SIZE = 40
MAX_DISCOVERY_PAGE_SIZE = 80
MAX_INVENTORY_TITLES = 12


def _has_table(db: Session, name: str) -> bool:
    try:
        return name in set(inspect(db.connection()).get_table_names())
    except Exception:
        return False


def _has_model_columns(db: Session, model: Any) -> bool:
    try:
        actual = {
            str(column["name"])
            for column in inspect(db.connection()).get_columns(model.__tablename__)
        }
        required = {str(column.name) for column in model.__table__.columns}
        return required.issubset(actual)
    except Exception:
        return False


def _entry_ref(entry: Entry, label_name: str) -> Dict[str, Any]:
    content = str(entry.content or "")
    lines = [line.strip() for line in content.splitlines() if line.strip()]
    title = (lines[0] if lines else "记录")[:120]
    snippet = "\n".join(lines[1:])[:420] if len(lines) > 1 else content[:420]
    return {
        "source_type": "entry",
        "source_id": int(entry.id),
        "entry_id": int(entry.id),
        "chunk_id": None,
        "reference_key": f"entry:source:{int(entry.id)}",
        "title": title,
        "label_name": str(label_name or entry.label_code or ""),
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "snippet": snippet,
        "content": content,
        "relevance_score": 0.0,
        "retrieval_method": "source_catalog",
        "source_reason": "source_catalog",
        "metadata": {},
    }


def _attachment_ref(att: EntryAttachment, entry: Entry) -> Dict[str, Any]:
    parent_title = next(
        (line.strip() for line in str(entry.content or "").splitlines() if line.strip()),
        "所属记录",
    )
    return {
        "source_type": "attachment_chunk",
        "source_id": int(att.id),
        "attachment_id": int(att.id),
        "entry_id": int(entry.id),
        "chunk_id": None,
        "reference_key": f"attachment:source:{int(att.id)}",
        "title": str(att.original_filename or "附件")[:120],
        "label_name": "附件",
        "created_at": att.created_at.isoformat() if att.created_at else None,
        "snippet": f"所属记录：{parent_title}"[:420],
        "content": "",
        "relevance_score": 0.0,
        "retrieval_method": "source_catalog",
        "source_reason": "source_catalog",
        "metadata": {"sync_status": str(att.status or "")},
    }


def load_source_catalog(db: Session, *, user_id: int) -> List[Dict[str, Any]]:
    refs: List[Dict[str, Any]] = []

    entries = (
        db.query(Entry, EntryLabel.name)
        .outerjoin(EntryLabel, Entry.label_code == EntryLabel.code)
        .filter(Entry.user_id == int(user_id))
        .order_by(Entry.created_at.desc(), Entry.id.desc())
        .all()
    )
    refs.extend(_entry_ref(entry, label or entry.label_code) for entry, label in entries)

    if _has_table(db, "todos") and _has_model_columns(db, Todo):
        todo_index = load_index(db, int(user_id))
        for todo_id in sorted(
            todo_index.by_id,
            key=lambda item: (
                str(todo_index.by_id[item].created_at or ""),
                int(item),
            ),
            reverse=True,
        ):
            refs.append(
                todo_reference(
                    todo_index.by_id[todo_id],
                    todo_index,
                    method="source_catalog",
                    score=0.0,
                )
            )

    attachments = (
        db.query(EntryAttachment, Entry)
        .join(Entry, Entry.id == EntryAttachment.entry_id)
        .filter(
            EntryAttachment.user_id == int(user_id),
            Entry.user_id == int(user_id),
            EntryAttachment.status == "indexed",
        )
        .order_by(EntryAttachment.created_at.desc(), EntryAttachment.id.desc())
        .all()
    )
    refs.extend(_attachment_ref(att, entry) for att, entry in attachments)

    if _has_table(db, "notion_pages") and _has_model_columns(db, NotionPage):
        pages = (
            db.query(NotionPage)
            .filter(
                NotionPage.user_id == int(user_id),
                NotionPage.sync_status.notin_(("deleted", "permission_lost")),
            )
            .order_by(NotionPage.created_at.desc(), NotionPage.id.desc())
            .all()
        )
        page_uuids = {str(page.notion_page_uuid) for page in pages}
        for page in pages:
            ref = notion_page_reference(
                page,
                method="source_catalog",
                score=0.0,
                snippet=str(page.normalized_text or "")[:420],
            )
            metadata = dict(ref.get("metadata") or {})
            metadata["object_kind"] = infer_notion_object_kind(page, page_uuids)
            ref["metadata"] = metadata
            refs.append(ref)

    return [
        enrich_reference_descriptor(ref)
        for ref in annotate_with_canonical_keys(refs)
    ]


def discover_source_scope(
    db: Session,
    *,
    user_id: int,
    plan: RetrievalPlan,
) -> List[Dict[str, Any]]:
    """Resolve the complete server-side, answerable source scope for a plan."""
    return filter_references_for_plan(
        load_source_catalog(db, user_id=int(user_id)),
        plan,
        require_named_source=bool(plan.named_source),
        answer_context=True,
    )


def _catalog_score(ref: Dict[str, Any], plan: RetrievalPlan) -> int:
    descriptor = source_descriptor_from_reference(ref)
    hay = "\n".join(
        [descriptor.title, descriptor.breadcrumb, *descriptor.user_tags]
    ).casefold()
    score = 0
    if plan.named_source:
        named = str(plan.named_source).casefold()
        if descriptor.title.casefold() == named:
            score += 100
        elif named in hay:
            score += 40
    terms = extract_query_terms(plan.topic or "")
    for term in terms:
        if str(term).casefold() in hay:
            score += 3
    return score


def discover_sources(
    db: Session,
    *,
    user_id: int,
    plan: RetrievalPlan,
    offset: int = 0,
    limit: int = DEFAULT_DISCOVERY_PAGE_SIZE,
) -> Dict[str, Any]:
    """Return one deterministic page of source objects plus aggregate groups."""
    limit = max(1, min(int(limit), MAX_DISCOVERY_PAGE_SIZE))
    catalog = load_source_catalog(db, user_id=int(user_id))
    filtered = filter_references_for_plan(
        catalog,
        plan,
        require_named_source=bool(plan.named_source),
        answer_context=False,
    )

    has_semantic_selector = bool(plan.topic or plan.named_source)
    scored = [(_catalog_score(ref, plan), ref) for ref in filtered]
    if has_semantic_selector:
        scored = [item for item in scored if item[0] > 0]
    scored.sort(
        key=lambda item: (
            -item[0],
            str(item[1].get("created_at") or ""),
            str(item[1].get("title") or ""),
        )
    )
    ordered = [item[1] for item in scored]
    total = len(ordered)
    page = ordered[max(0, int(offset)) : max(0, int(offset)) + limit]

    provider_groups = Counter()
    kind_groups = Counter()
    status_groups = Counter()
    for ref in ordered:
        descriptor = source_descriptor_from_reference(ref)
        provider_groups[descriptor.provider_label] += 1
        kind_groups[descriptor.object_kind] += 1
        status_groups[descriptor.sync_status or "ready"] += 1

    answerable_keys = {
        source_descriptor_from_reference(ref).canonical_reference
        for ref in ordered
        if source_descriptor_from_reference(ref).answerable
    }
    return {
        "items": page,
        "total_count": total,
        "offset": max(0, int(offset)),
        "limit": limit,
        "has_more": max(0, int(offset)) + len(page) < total,
        "provider_groups": dict(provider_groups),
        "object_kind_groups": dict(kind_groups),
        "status_groups": dict(status_groups),
        "answerable_reference_keys": answerable_keys,
    }


def build_inventory_answer(discovery: Dict[str, Any]) -> str:
    total = int(discovery.get("total_count") or 0)
    if total <= 0:
        return "没有找到符合这些条件的来源。"
    provider_groups = dict(discovery.get("provider_groups") or {})
    status_groups = dict(discovery.get("status_groups") or {})
    group_text = "、".join(
        f"{name} {count} 项" for name, count in sorted(provider_groups.items())
    )
    lines = [f"找到 {total} 个符合条件的来源" + (f"：{group_text}。" if group_text else "。")]
    pending = sum(
        int(status_groups.get(name) or 0)
        for name in ("pending", "processing")
    )
    if pending:
        lines.append(f"其中 {pending} 项仍在同步，可在清单中查看，但暂不用于正文回答。")
    items = list(discovery.get("items") or [])[:MAX_INVENTORY_TITLES]
    for index, ref in enumerate(items, start=1):
        descriptor = source_descriptor_from_reference(ref)
        path = f" · {descriptor.breadcrumb}" if descriptor.breadcrumb and descriptor.breadcrumb != descriptor.title else ""
        state = "（同步中）" if not descriptor.answerable else ""
        lines.append(
            f"{index}. {descriptor.title}{path} · {descriptor.provider_label}{state}〔{index}〕"
        )
    if bool(discovery.get("has_more")):
        lines.append("结果较多，当前先展示第一页；其余来源可继续分页查看。")
    return "\n".join(lines)

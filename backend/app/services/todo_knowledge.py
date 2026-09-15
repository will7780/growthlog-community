"""Todo mirrors for the existing Knowledge RAG layer.

Todo business writes own the source of truth. This module mirrors committed
state for retrieval, rejects stale vectors by content hash, and builds live
read-only tree context. It never changes Todo state.
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeEmbedding, KnowledgeSource, Todo
from app.services.todo_hierarchy import (
    TodoHierarchyIndex,
    ancestor_ids,
    build_index,
    family_root_id,
    load_index,
    subtree_ids,
)

logger = logging.getLogger(__name__)
TODO_SOURCE_TYPE = "todo"
TODO_CHUNK_TYPE = "entry_text"


def generate_embedding(text: str) -> List[float]:
    """Load the E5 runtime only when a mirror write actually needs a vector."""
    from app.services.embedding import generate_embedding as _generate_embedding

    return _generate_embedding(text)


def _iso(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def todo_visible_payload(todo: Todo) -> Dict[str, Any]:
    """Fields that affect Todo embedding/search semantics. Path and order are live."""
    is_step = getattr(todo, "parent_id", None) is not None
    return {
        "content": str(todo.content or "").strip(),
        "status": "completed" if bool(todo.is_done) else "open",
        "is_step": is_step,
        "priority": None if is_step else str(todo.priority or "P4"),
        "urgent": False if is_step else bool(todo.is_urgent),
        "due_date": None if is_step else _iso(todo.due_date),
        "completed_at": _iso(todo.completed_at),
        "completion_note": str(todo.completion_note or "").strip() or None,
    }


def todo_content_hash(todo: Todo) -> str:
    canonical = json.dumps(
        todo_visible_payload(todo), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def todo_index_text(todo: Todo) -> str:
    payload = todo_visible_payload(todo)
    parts = [
        f"{'子任务步骤' if payload['is_step'] else '小要事'}：{payload['content']}",
        f"状态：{'已完成' if payload['status'] == 'completed' else '未完成'}",
    ]
    if not payload["is_step"]:
        parts.extend(
            [
                f"优先级：{payload['priority']}",
                f"紧急：{'是' if payload['urgent'] else '否'}",
            ]
        )
    if payload["due_date"]:
        parts.append(f"截止日期：{payload['due_date']}")
    if payload["completed_at"]:
        parts.append(f"完成时间：{payload['completed_at']}")
    if payload["completion_note"]:
        parts.append(f"完成批注：{payload['completion_note']}")
    return "\n".join(parts)


def todo_path_titles(todo_id: int, index: TodoHierarchyIndex) -> List[str]:
    ids = [*ancestor_ids(int(todo_id), index), int(todo_id)]
    return [str(index.by_id[item].content or "").strip() for item in ids if item in index.by_id]


def todo_reference(todo: Todo, index: TodoHierarchyIndex, *, method: str, score: float) -> Dict[str, Any]:
    tid = int(todo.id)
    path = todo_path_titles(tid, index)
    payload = todo_visible_payload(todo)
    text = todo_index_text(todo)
    return {
        "source_type": "todo",
        "source_id": tid,
        "todo_id": tid,
        "entry_id": None,
        "chunk_id": None,
        "reference_key": f"todo:source:{tid}",
        "title": str(todo.content or "").strip()[:120] or "小要事",
        "label_name": "小要事",
        "created_at": _iso(todo.created_at),
        "snippet": text[:420],
        "content": text,
        "relevance_score": float(score),
        "retrieval_method": method,
        "source_reason": "todo_match",
        "root_todo_id": family_root_id(tid, index),
        "metadata": {
            "content_hash": todo_content_hash(todo),
            "path_titles": path,
            "status": payload["status"],
            "is_step": payload["is_step"],
            "sort_order": (
                int(todo.sort_order)
                if getattr(todo, "sort_order", None) is not None
                else None
            ),
            "priority": payload["priority"],
            "urgent": payload["urgent"],
            "due_date": payload["due_date"],
            "completed_at": payload["completed_at"],
        },
    }


def _source_for_todo(db: Session, user_id: int, todo_id: int) -> Optional[KnowledgeSource]:
    return (
        db.query(KnowledgeSource)
        .filter(
            KnowledgeSource.user_id == int(user_id),
            KnowledgeSource.source_type == TODO_SOURCE_TYPE,
            KnowledgeSource.source_id == int(todo_id),
        )
        .first()
    )


def sync_todo_to_knowledge(db: Session, todo: Todo, *, commit: bool = True) -> Dict[str, Any]:
    """Mirror one committed Todo. Existing matching hash is a no-op."""
    from app.services.knowledge_rag import has_knowledge_tables

    if not has_knowledge_tables(db):
        return {"ok": False, "reason": "knowledge_tables_missing", "changed": False}
    current_hash = todo_content_hash(todo)
    source = _source_for_todo(db, int(todo.user_id), int(todo.id))
    if source is not None:
        chunk = (
            db.query(KnowledgeChunk)
            .filter(KnowledgeChunk.source_id == int(source.id), KnowledgeChunk.chunk_index == 0)
            .first()
        )
        meta = chunk.metadata_json if chunk is not None and isinstance(chunk.metadata_json, dict) else {}
        embedding_exists = bool(
            chunk is not None
            and db.query(KnowledgeEmbedding.id)
            .filter(
                KnowledgeEmbedding.chunk_id == int(chunk.id),
                KnowledgeEmbedding.embedding_model == settings.embedding_model,
            )
            .first()
        )
        if source.status == "indexed" and meta.get("content_hash") == current_hash and embedding_exists:
            return {"ok": True, "changed": False, "content_hash": current_hash}
    else:
        source = KnowledgeSource(
            user_id=int(todo.user_id),
            source_type=TODO_SOURCE_TYPE,
            source_id=int(todo.id),
            title=str(todo.content or "")[:255],
            status="pending",
            metadata_json={"content_hash": current_hash},
        )
        db.add(source)
        db.flush()

    source.title = str(todo.content or "")[:255]
    source.status = "pending"
    source.metadata_json = {"content_hash": current_hash}
    chunk_ids = [
        int(row.id)
        for row in db.query(KnowledgeChunk.id).filter(KnowledgeChunk.source_id == int(source.id)).all()
    ]
    if chunk_ids:
        db.query(KnowledgeEmbedding).filter(KnowledgeEmbedding.chunk_id.in_(chunk_ids)).delete(
            synchronize_session=False
        )
    db.query(KnowledgeChunk).filter(KnowledgeChunk.source_id == int(source.id)).delete(
        synchronize_session=False
    )
    db.flush()

    text = todo_index_text(todo)
    chunk = KnowledgeChunk(
        source_id=int(source.id),
        user_id=int(todo.user_id),
        origin_type=TODO_SOURCE_TYPE,
        origin_id=int(todo.id),
        entry_id=None,
        chunk_index=0,
        chunk_type=TODO_CHUNK_TYPE,
        title=str(todo.content or "")[:255],
        content=text,
        metadata_json={"content_hash": current_hash},
    )
    db.add(chunk)
    db.flush()
    db.add(
        KnowledgeEmbedding(
            chunk_id=int(chunk.id),
            user_id=int(todo.user_id),
            embedding_model=settings.embedding_model,
            vector=generate_embedding(f"passage: {text}"),
        )
    )
    source.status = "indexed"
    if commit:
        db.commit()
    return {"ok": True, "changed": True, "content_hash": current_hash}


def best_effort_sync_todo_ids(db: Session, *, user_id: int, todo_ids: Iterable[int]) -> Dict[str, Any]:
    """Post-business-commit sync. Failure is logged and never reverses Todo state."""
    processed = changed = failed = 0
    for todo_id in sorted({int(item) for item in todo_ids}):
        todo = db.query(Todo).filter(Todo.id == todo_id, Todo.user_id == int(user_id)).first()
        if todo is None:
            continue
        processed += 1
        try:
            result = sync_todo_to_knowledge(db, todo, commit=True)
            changed += int(bool(result.get("changed")))
        except Exception as exc:
            db.rollback()
            failed += 1
            logger.warning(
                "todo knowledge sync degraded user=%s todo_hash=%s error=%s",
                user_id,
                hashlib.sha256(str(todo_id).encode("ascii")).hexdigest()[:12],
                type(exc).__name__,
            )
    return {"processed": processed, "changed": changed, "failed": failed}


def delete_todo_knowledge(db: Session, *, user_id: int, todo_ids: Iterable[int]) -> int:
    """Delete mirrors in caller's Todo delete transaction."""
    from app.services.knowledge_rag import has_knowledge_tables

    if not has_knowledge_tables(db):
        return 0
    ids = sorted({int(item) for item in todo_ids})
    if not ids:
        return 0
    sources = (
        db.query(KnowledgeSource)
        .filter(
            KnowledgeSource.user_id == int(user_id),
            KnowledgeSource.source_type == TODO_SOURCE_TYPE,
            KnowledgeSource.source_id.in_(ids),
        )
        .all()
    )
    for source in sources:
        db.delete(source)
    return len(sources)


def backfill_user_todos(db: Session, *, user_id: int) -> Dict[str, int]:
    rows = db.query(Todo).filter(Todo.user_id == int(user_id)).order_by(Todo.id.asc()).all()
    result = best_effort_sync_todo_ids(db, user_id=user_id, todo_ids=[int(row.id) for row in rows])
    result["todo_count"] = len(rows)
    return result


def search_keyword_todos(
    db: Session,
    *,
    user_id: int,
    terms: Sequence[str],
    top_k: int = 80,
) -> List[Dict[str, Any]]:
    clean = []
    seen = set()
    for raw in terms:
        term = str(raw or "").strip().lower()
        if len(term) < 2 or term in seen:
            continue
        seen.add(term)
        clean.append(term)
    if not clean:
        return []
    rows = db.query(Todo).filter(Todo.user_id == int(user_id)).order_by(Todo.id.asc()).all()
    index = build_index(rows)
    ranked = []
    for todo in rows:
        title = str(todo.content or "")
        body = todo_index_text(todo)
        title_hits = sum(1 for term in clean if term in title.lower())
        body_hits = sum(1 for term in clean if term in body.lower())
        if title_hits + body_hits == 0:
            continue
        ref = todo_reference(todo, index, method="lexical_todo", score=1.0)
        ref["judge_evidence"] = {
            "title": ref["title"],
            "hit_window": ref["snippet"],
            "source_type": "todo",
            "title_term_hit": title_hits > 0,
            "body_term_hit": body_hits > 0,
            "title_term_hit_count": title_hits,
            "body_term_hit_count": body_hits,
        }
        ranked.append((0 if title_hits else 1, -title_hits, -body_hits, int(todo.id), ref))
    ranked.sort(key=lambda item: item[:4])
    return [item[4] for item in ranked[: max(1, int(top_k))]]


def expand_todo_hits_to_tree_context(
    db: Session,
    *,
    user_id: int,
    ranked_hits: Sequence[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    rows = db.query(Todo).filter(Todo.user_id == int(user_id)).order_by(Todo.id.asc()).all()
    index = build_index(rows)
    out: List[Dict[str, Any]] = []
    seen: set[int] = set()
    for hit in ranked_hits:
        if str(hit.get("source_type") or "") != "todo":
            continue
        try:
            hit_id = int(hit.get("todo_id") or hit.get("source_id"))
        except (TypeError, ValueError):
            continue
        if hit_id not in index.by_id:
            continue
        root_id = family_root_id(hit_id, index)
        for todo_id in subtree_ids(root_id, index):
            if todo_id in seen:
                continue
            seen.add(todo_id)
            ref = todo_reference(index.by_id[todo_id], index, method="todo_tree_expand", score=0.0)
            ref["relevance_level"] = hit.get("relevance_level") if todo_id == hit_id else 0
            ref["is_precise_hit"] = todo_id == hit_id
            out.append(ref)
    return out
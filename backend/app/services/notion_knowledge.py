"""Notion Knowledge mirror: hash skip, atomic replace, disconnect cleanup."""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeEmbedding, KnowledgeSource, NotionPage
from app.services.knowledge_rag import has_knowledge_tables, split_entry_content
from app.services.notion_reference import (
    infer_notion_object_kind,
    notion_page_reference,
    page_is_searchable,
)

logger = logging.getLogger(__name__)
NOTION_SOURCE_TYPE = "notion_page"
NOTION_CHUNK_TYPE = "notion_text"


def has_notion_page_table(db: Session) -> bool:
    try:
        return "notion_pages" in set(inspect(db.connection()).get_table_names())
    except Exception:
        return False


def generate_embedding(text: str) -> List[float]:
    from app.services.embedding import generate_embedding as _generate_embedding

    return _generate_embedding(text)


def source_for_page(db: Session, user_id: int, page_id: int) -> Optional[KnowledgeSource]:
    return (
        db.query(KnowledgeSource)
        .filter(
            KnowledgeSource.user_id == int(user_id),
            KnowledgeSource.source_type == NOTION_SOURCE_TYPE,
            KnowledgeSource.source_id == int(page_id),
        )
        .first()
    )


def delete_notion_knowledge(db: Session, *, user_id: int, page_ids: List[int]) -> None:
    if not page_ids or not has_knowledge_tables(db):
        return
    sources = (
        db.query(KnowledgeSource)
        .filter(
            KnowledgeSource.user_id == int(user_id),
            KnowledgeSource.source_type == NOTION_SOURCE_TYPE,
            KnowledgeSource.source_id.in_([int(item) for item in page_ids]),
        )
        .all()
    )
    source_ids = [int(source.id) for source in sources]
    if not source_ids:
        return
    chunk_ids = [
        int(row.id)
        for row in db.query(KnowledgeChunk.id).filter(KnowledgeChunk.source_id.in_(source_ids)).all()
    ]
    if chunk_ids:
        db.query(KnowledgeEmbedding).filter(KnowledgeEmbedding.chunk_id.in_(chunk_ids)).delete(
            synchronize_session=False
        )
    db.query(KnowledgeChunk).filter(KnowledgeChunk.source_id.in_(source_ids)).delete(synchronize_session=False)
    db.query(KnowledgeSource).filter(KnowledgeSource.id.in_(source_ids)).delete(synchronize_session=False)


def prepare_embeddings(text: str) -> List[Dict[str, Any]]:
    chunks = split_entry_content(text)
    prepared: List[Dict[str, Any]] = []
    for index, chunk in enumerate(chunks):
        prepared.append(
            {
                "chunk_index": index,
                "content": chunk,
                "vector": generate_embedding(f"passage: {chunk}"),
            }
        )
    return prepared


def replace_page_mirror(
    db: Session,
    page: NotionPage,
    *,
    prepared: List[Dict[str, Any]],
    content_hash: str,
    status: str,
) -> None:
    if not has_knowledge_tables(db):
        return
    source = source_for_page(db, int(page.user_id), int(page.id))
    if source is None:
        source = KnowledgeSource(
            user_id=int(page.user_id),
            source_type=NOTION_SOURCE_TYPE,
            source_id=int(page.id),
            title=str(page.title or "")[:255],
            status="pending",
            metadata_json={"content_hash": content_hash},
        )
        db.add(source)
        db.flush()
    source.title = str(page.title or "")[:255]
    source.status = "indexed" if status in {"indexed", "partial"} else "pending"
    source.metadata_json = {"content_hash": content_hash, "sync_status": status}
    chunk_ids = [
        int(row.id)
        for row in db.query(KnowledgeChunk.id).filter(KnowledgeChunk.source_id == int(source.id)).all()
    ]
    if chunk_ids:
        db.query(KnowledgeEmbedding).filter(KnowledgeEmbedding.chunk_id.in_(chunk_ids)).delete(
            synchronize_session=False
        )
    db.query(KnowledgeChunk).filter(KnowledgeChunk.source_id == int(source.id)).delete(synchronize_session=False)
    db.flush()
    for item in prepared:
        chunk = KnowledgeChunk(
            source_id=int(source.id),
            user_id=int(page.user_id),
            origin_type=NOTION_SOURCE_TYPE,
            origin_id=int(page.id),
            entry_id=None,
            chunk_index=int(item["chunk_index"]),
            chunk_type=NOTION_CHUNK_TYPE,
            title=str(page.title or "")[:255],
            content=item["content"],
            metadata_json={"content_hash": content_hash, "breadcrumb": page.breadcrumb},
        )
        db.add(chunk)
        db.flush()
        db.add(
            KnowledgeEmbedding(
                chunk_id=int(chunk.id),
                user_id=int(page.user_id),
                embedding_model=settings.embedding_model,
                vector=item["vector"],
            )
        )


def search_keyword_notion_pages(
    db: Session,
    *,
    user_id: int,
    terms: List[str],
    top_k: int = 80,
) -> List[Dict[str, Any]]:
    if not terms or not has_notion_page_table(db):
        return []
    pages = (
        db.query(NotionPage)
        .filter(NotionPage.user_id == int(user_id))
        .order_by(NotionPage.id.asc())
        .all()
    )
    page_uuids = {str(page.notion_page_uuid) for page in pages}
    scored: List[Dict[str, Any]] = []
    lowered_terms = [str(term).lower() for term in terms if str(term).strip()]
    for page in pages:
        if not page_is_searchable(page):
            continue
        hay = f"{page.title or ''}\n{page.breadcrumb or ''}\n{page.normalized_text or ''}".lower()
        hits = sum(1 for term in lowered_terms if term in hay)
        if hits <= 0:
            continue
        snippet = ""
        body = str(page.normalized_text or "")
        for term in lowered_terms:
            pos = body.lower().find(term)
            if pos >= 0:
                snippet = body[max(0, pos - 40): pos + 200]
                break
        ref = notion_page_reference(
            page,
            method="lexical_notion",
            score=float(hits),
            snippet=snippet or body[:420],
            object_kind=infer_notion_object_kind(page, page_uuids),
        )
        scored.append(ref)
    scored.sort(key=lambda item: (-float(item.get("relevance_score") or 0), int(item["source_id"])))
    return scored[:top_k]


def expand_notion_page_context(
    db: Session,
    *,
    user_id: int,
    ranked_hits: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    page_ids: set[int] = set()
    for hit in ranked_hits:
        if str(hit.get("source_type") or "") != "notion_page":
            continue
        try:
            page_ids.add(int(hit.get("notion_page_id") or hit.get("source_id")))
        except (TypeError, ValueError):
            continue
    if not page_ids or not has_knowledge_tables(db) or not has_notion_page_table(db):
        return list(ranked_hits)
    chunks = (
        db.query(KnowledgeChunk)
        .filter(
            KnowledgeChunk.user_id == int(user_id),
            KnowledgeChunk.origin_type == NOTION_SOURCE_TYPE,
            KnowledgeChunk.origin_id.in_(list(page_ids)),
        )
        .order_by(KnowledgeChunk.origin_id.asc(), KnowledgeChunk.chunk_index.asc())
        .all()
    )
    by_page: dict[int, List[KnowledgeChunk]] = {}
    for chunk in chunks:
        by_page.setdefault(int(chunk.origin_id), []).append(chunk)
    extra: List[Dict[str, Any]] = []
    seen = {str(item.get("reference_key") or "") for item in ranked_hits}
    for hit in ranked_hits:
        if str(hit.get("source_type") or "") != "notion_page":
            continue
        try:
            page_id = int(hit.get("notion_page_id") or hit.get("source_id"))
        except (TypeError, ValueError):
            continue
        page_chunks = by_page.get(page_id) or []
        hit_index = None
        meta = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
        object_kind = str(meta.get("object_kind") or "page")
        if meta.get("chunk_index") is not None:
            try:
                hit_index = int(meta["chunk_index"])
            except (TypeError, ValueError):
                hit_index = None
        wanted = set()
        if hit_index is None:
            wanted.update(range(min(3, len(page_chunks))))
        else:
            wanted.update({max(0, hit_index - 1), hit_index, hit_index + 1})
        page = (
            db.query(NotionPage)
            .filter(NotionPage.id == page_id, NotionPage.user_id == int(user_id))
            .first()
        )
        if page is None or not page_is_searchable(page):
            continue
        for chunk in page_chunks:
            if int(chunk.chunk_index) not in wanted:
                continue
            key = f"notion_page:source:{page_id}"
            if key in seen and int(chunk.chunk_index) == (hit_index or 0):
                continue
            ref = notion_page_reference(
                page,
                method="notion_adjacent",
                score=0.0,
                snippet=chunk.content[:420],
                object_kind=object_kind,
            )
            ref["metadata"] = {**dict(ref.get("metadata") or {}), "chunk_index": int(chunk.chunk_index)}
            extra.append(ref)
    return list(ranked_hits) + extra

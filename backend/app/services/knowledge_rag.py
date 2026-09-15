"""
Unified knowledge source/chunk/embedding services.

These helpers are additive. If the knowledge_* tables have not been migrated,
search and sync return safely instead of breaking existing entry-only RAG.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Iterable, List, Optional

import numpy as np
from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Entry, EntryAttachment, EntryLabel, KnowledgeChunk, KnowledgeEmbedding, KnowledgeSource, NotionPage, Todo

logger = logging.getLogger(__name__)


def generate_embedding(text: str) -> List[float]:
    """Load SentenceTransformers only for mirror writes, not module import."""
    from app.services.embedding import generate_embedding as _generate_embedding

    return _generate_embedding(text)

REQUIRED_TABLES = {"knowledge_sources", "knowledge_chunks", "knowledge_embeddings"}
_KNOWLEDGE_TABLES_CACHED: Optional[bool] = None


def has_knowledge_tables(db: Session) -> bool:
    global _KNOWLEDGE_TABLES_CACHED
    if _KNOWLEDGE_TABLES_CACHED is not None:
        return _KNOWLEDGE_TABLES_CACHED
    try:
        existing = set(inspect(db.connection()).get_table_names())
        _KNOWLEDGE_TABLES_CACHED = REQUIRED_TABLES.issubset(existing)
        return _KNOWLEDGE_TABLES_CACHED
    except Exception as exc:
        logger.warning("failed to inspect knowledge tables: %s", exc)
        return False


def split_entry_content(content: str, max_chars: int = 900, overlap: int = 120) -> List[str]:
    text = (content or "").strip()
    if not text:
        return []
    paragraphs = [part.strip() for part in text.splitlines() if part.strip()]
    chunks: List[str] = []
    current = ""
    for paragraph in paragraphs or [text]:
        if not current:
            current = paragraph
            continue
        if len(current) + 1 + len(paragraph) <= max_chars:
            current = f"{current}\n{paragraph}"
        else:
            chunks.append(current)
            tail = current[-overlap:] if overlap > 0 and len(current) > overlap else ""
            current = f"{tail}\n{paragraph}".strip() if tail else paragraph
    if current:
        chunks.append(current)

    final: List[str] = []
    for chunk in chunks:
        if len(chunk) <= max_chars * 1.5:
            final.append(chunk)
            continue
        start = 0
        while start < len(chunk):
            final.append(chunk[start: start + max_chars])
            start += max_chars - overlap
    return final


def _title(content: str) -> str:
    return ((content or "").split("\n")[0].strip() or "未命名记录")[:255]


def sync_entry_to_knowledge(db: Session, entry: Entry, force: bool = False) -> int:
    """Create/update unified chunks for one entry. Returns chunk count."""
    if not has_knowledge_tables(db):
        return 0

    source = (
        db.query(KnowledgeSource)
        .filter(
            KnowledgeSource.user_id == entry.user_id,
            KnowledgeSource.source_type == "entry",
            KnowledgeSource.source_id == entry.id,
        )
        .first()
    )
    if source and source.status == "indexed" and not force:
        return db.query(KnowledgeChunk).filter(KnowledgeChunk.source_id == source.id).count()

    if not source:
        source = KnowledgeSource(
            user_id=entry.user_id,
            source_type="entry",
            source_id=entry.id,
            title=_title(entry.content),
            status="pending",
            metadata_json={"label_code": entry.label_code},
        )
        db.add(source)
        db.flush()
    else:
        source.title = _title(entry.content)
        source.status = "pending"
        source.metadata_json = {"label_code": entry.label_code}
        chunk_ids = [row.id for row in db.query(KnowledgeChunk.id).filter(KnowledgeChunk.source_id == source.id)]
        if chunk_ids:
            db.query(KnowledgeEmbedding).filter(KnowledgeEmbedding.chunk_id.in_(chunk_ids)).delete(synchronize_session=False)
        db.query(KnowledgeChunk).filter(KnowledgeChunk.source_id == source.id).delete(synchronize_session=False)
        db.flush()

    chunks = split_entry_content(entry.content)
    for index, content in enumerate(chunks):
        chunk = KnowledgeChunk(
            source_id=source.id,
            user_id=entry.user_id,
            origin_type="entry",
            origin_id=entry.id,
            entry_id=entry.id,
            chunk_index=index,
            chunk_type="entry_text",
            title=_title(entry.content),
            content=content,
            metadata_json={"label_code": entry.label_code},
        )
        db.add(chunk)
        db.flush()
        vector = generate_embedding(f"passage: {content}")
        db.add(KnowledgeEmbedding(
            chunk_id=chunk.id,
            user_id=entry.user_id,
            embedding_model=settings.embedding_model,
            vector=vector,
        ))

    source.status = "indexed"
    db.commit()
    return len(chunks)


def sync_user_entries_to_knowledge(
    db: Session,
    user_id: int,
    *,
    limit: Optional[int] = None,
    force: bool = False,
) -> Dict:
    if not has_knowledge_tables(db):
        return {"ok": False, "reason": "knowledge tables not migrated", "processed": 0, "chunks": 0}

    query = db.query(Entry).filter(Entry.user_id == user_id).order_by(Entry.updated_at.desc(), Entry.created_at.desc())
    if limit:
        query = query.limit(limit)

    processed = 0
    chunks = 0
    for entry in query.all():
        chunks += sync_entry_to_knowledge(db, entry, force=force)
        processed += 1
    return {"ok": True, "processed": processed, "chunks": chunks}


def sync_attachment_chunks_to_knowledge(
    db: Session,
    attachment: EntryAttachment,
    extracted_chunks: List[Dict],
    *,
    commit: bool = False,
) -> int:
    """Mirror extracted attachment text into the unified knowledge chunk layer."""
    if not has_knowledge_tables(db):
        return 0

    source = (
        db.query(KnowledgeSource)
        .filter(
            KnowledgeSource.user_id == attachment.user_id,
            KnowledgeSource.source_type == "attachment",
            KnowledgeSource.source_id == attachment.id,
        )
        .first()
    )
    if not source:
        source = KnowledgeSource(
            user_id=attachment.user_id,
            source_type="attachment",
            source_id=attachment.id,
            title=attachment.original_filename or f"附件 #{attachment.id}",
            status="pending",
            metadata_json={
                "entry_id": attachment.entry_id,
                "mime_type": attachment.mime_type,
                "file_ext": attachment.file_ext,
            },
        )
        db.add(source)
        db.flush()
    else:
        source.title = attachment.original_filename or f"附件 #{attachment.id}"
        source.status = "pending"
        source.metadata_json = {
            "entry_id": attachment.entry_id,
            "mime_type": attachment.mime_type,
            "file_ext": attachment.file_ext,
        }
        chunk_ids = [row.id for row in db.query(KnowledgeChunk.id).filter(KnowledgeChunk.source_id == source.id)]
        if chunk_ids:
            db.query(KnowledgeEmbedding).filter(KnowledgeEmbedding.chunk_id.in_(chunk_ids)).delete(synchronize_session=False)
        db.query(KnowledgeChunk).filter(KnowledgeChunk.source_id == source.id).delete(synchronize_session=False)
        db.flush()

    for index, item in enumerate(extracted_chunks):
        content = item["content"]
        chunk_type = item.get("modality") or "pdf_text"
        chunk = KnowledgeChunk(
            source_id=source.id,
            user_id=attachment.user_id,
            origin_type="attachment",
            origin_id=attachment.id,
            entry_id=attachment.entry_id,
            chunk_index=index,
            chunk_type=chunk_type,
            title=attachment.original_filename or f"附件 #{attachment.id}",
            content=content,
            page_no=item.get("page_no"),
            slide_no=item.get("slide_no"),
            metadata_json=item.get("metadata_json"),
        )
        db.add(chunk)
        db.flush()
        vector = generate_embedding(f"passage: {content}")
        db.add(KnowledgeEmbedding(
            chunk_id=chunk.id,
            user_id=attachment.user_id,
            embedding_model=settings.embedding_model,
            vector=vector,
        ))

    source.status = "indexed"
    if commit:
        db.commit()
    return len(extracted_chunks)


def _parse_vector(raw) -> np.ndarray | None:
    value = raw
    for _ in range(5):
        if isinstance(value, list):
            return np.array(value, dtype=np.float32)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except Exception:
                return None
        else:
            return None
    return None


def search_knowledge_chunks(
    db: Session,
    *,
    user_id: int,
    query_vector: List[float],
    top_k: int = 30,
    label_code: Optional[str] = None,
    min_score: float = 0.35,
) -> List[Dict]:
    if not has_knowledge_tables(db):
        return []

    rows = (
        db.query(KnowledgeChunk, KnowledgeEmbedding, Entry, EntryLabel.name)
        .join(KnowledgeEmbedding, KnowledgeEmbedding.chunk_id == KnowledgeChunk.id)
        .outerjoin(Entry, Entry.id == KnowledgeChunk.entry_id)
        .outerjoin(EntryLabel, EntryLabel.code == Entry.label_code)
        .filter(KnowledgeChunk.user_id == user_id)
    )
    if label_code:
        rows = rows.filter(Entry.label_code == label_code)
    rows = rows.all()
    if not rows:
        return []

    query = np.array(query_vector, dtype=np.float32)
    query_norm = float(np.linalg.norm(query))
    if query_norm == 0:
        return []

    # R5.2.3: vectorized cosine — avoid per-row Python score loops.
    vectors: List[np.ndarray] = []
    payloads: List[Dict] = []
    has_todo_origins = any(
        str(chunk.origin_type or "") == "todo"
        for chunk, _embedding, _entry, _label in rows
    )
    has_notion_origins = any(
        str(chunk.origin_type or "") == "notion_page"
        for chunk, _embedding, _entry, _label in rows
    )
    todo_rows = (
        db.query(Todo).filter(Todo.user_id == int(user_id)).order_by(Todo.id.asc()).all()
        if has_todo_origins
        else []
    )
    todo_by_id = {int(todo.id): todo for todo in todo_rows}
    notion_rows = []
    if has_notion_origins:
        from app.services.notion_knowledge import has_notion_page_table

        if has_notion_page_table(db):
            notion_rows = (
                db.query(NotionPage)
                .filter(NotionPage.user_id == int(user_id))
                .order_by(NotionPage.id.asc())
                .all()
            )
    notion_by_id = {int(page.id): page for page in notion_rows}
    notion_page_uuids = {str(page.notion_page_uuid) for page in notion_rows}
    from app.services.todo_hierarchy import build_index
    from app.services.todo_knowledge import todo_content_hash, todo_reference
    from app.services.notion_reference import (
        infer_notion_object_kind,
        notion_page_reference,
        page_is_searchable,
    )

    todo_index = build_index(todo_rows)
    for chunk, embedding, entry, label_name in rows:
        vector = _parse_vector(embedding.vector)
        if vector is None:
            continue
        if str(chunk.origin_type or "") == "todo":
            todo = todo_by_id.get(int(chunk.origin_id))
            chunk_meta = chunk.metadata_json if isinstance(chunk.metadata_json, dict) else {}
            if todo is None or chunk_meta.get("content_hash") != todo_content_hash(todo):
                continue
            item = todo_reference(todo, todo_index, method="knowledge_dense", score=0.0)
            item["metadata"] = {**dict(item.get("metadata") or {}), "origin_type": "todo"}
            vectors.append(vector.astype(np.float32, copy=False))
            payloads.append(item)
            continue
        if str(chunk.origin_type or "") == "notion_page":
            page = notion_by_id.get(int(chunk.origin_id))
            chunk_meta = chunk.metadata_json if isinstance(chunk.metadata_json, dict) else {}
            if page is None or not page_is_searchable(page):
                continue
            if chunk_meta.get("content_hash") and chunk_meta.get("content_hash") != page.indexed_content_hash:
                continue
            item = notion_page_reference(
                page,
                method="knowledge_dense",
                score=0.0,
                snippet=(chunk.content or "")[:420],
                object_kind=infer_notion_object_kind(page, notion_page_uuids),
            )
            item["chunk_id"] = int(chunk.id)
            item["metadata"] = {
                **dict(item.get("metadata") or {}),
                "origin_type": "notion_page",
                "chunk_index": int(chunk.chunk_index),
            }
            vectors.append(vector.astype(np.float32, copy=False))
            payloads.append(item)
            continue
        if chunk.origin_type == "entry":
            hit_source_type = "entry"
            hit_source_id = int(chunk.origin_id)
        else:
            hit_source_type = "knowledge_source"
            hit_source_id = int(chunk.source_id)
        meta = {
            "origin_type": chunk.origin_type,
            "origin_id": int(chunk.origin_id),
            "chunk_type": chunk.chunk_type,
            "chunk_index": int(chunk.chunk_index),
        }
        if chunk.title and chunk.origin_type == "attachment":
            meta["filename"] = str(chunk.title).split("（", 1)[0].strip()
        vectors.append(vector.astype(np.float32, copy=False))
        payloads.append({
            "source_type": hit_source_type,
            "source_id": hit_source_id,
            "entry_id": int(chunk.entry_id or chunk.origin_id),
            "chunk_id": int(chunk.id),
            "title": chunk.title or (entry.content.split("\n")[0] if entry and entry.content else f"片段 #{chunk.id}"),
            "label_name": label_name or (entry.label_code if entry else ""),
            "created_at": entry.created_at.isoformat() if entry and entry.created_at else None,
            "snippet": chunk.content[:260],
            "content": chunk.content,
            "page_no": chunk.page_no,
            "slide_no": chunk.slide_no,
            "modality": chunk.chunk_type,
            "retrieval_method": "knowledge_dense",
            "source_reason": "knowledge_chunk_semantic_match",
            "metadata": meta,
        })
    if not vectors:
        return []
    mat = np.vstack(vectors)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    sims = (mat @ query) / (norms * query_norm)
    order = np.argsort(sims)[::-1]
    out: List[Dict] = []
    for idx in order:
        score = float(sims[idx])
        if score < min_score and out:
            break
        if score < min_score:
            continue
        item = dict(payloads[int(idx)])
        item["relevance_score"] = score
        out.append(item)
        if len(out) >= top_k:
            break
    return out

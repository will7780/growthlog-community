"""
Attachment text search service.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, List

import numpy as np
from sqlalchemy.orm import Session

from app.models import AttachmentChunk, AttachmentEmbedding, Entry, EntryAttachment, EntryLabel

logger = logging.getLogger(__name__)


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


def search_similar_attachment_chunks(
    db: Session,
    query_vector: List[float],
    user_id: int,
    top_k: int = 8,
    min_score: float = 0.35,
) -> list[dict]:
    """Search indexed attachment chunks using NumPy cosine similarity."""
    rows = (
        db.query(AttachmentChunk, AttachmentEmbedding, EntryAttachment, Entry, EntryLabel.name)
        .join(AttachmentEmbedding, AttachmentEmbedding.chunk_id == AttachmentChunk.id)
        .join(EntryAttachment, EntryAttachment.id == AttachmentChunk.attachment_id)
        .join(Entry, Entry.id == AttachmentChunk.entry_id)
        .outerjoin(EntryLabel, EntryLabel.code == Entry.label_code)
        .filter(AttachmentChunk.user_id == user_id)
        .all()
    )
    if not rows:
        return []

    query = np.array(query_vector, dtype=np.float32)
    query_norm = float(np.linalg.norm(query))
    if query_norm == 0:
        return []

    # R5.2.3: vectorized cosine scoring.
    vectors: list[np.ndarray] = []
    payloads: list[dict] = []
    for chunk, embedding, attachment, entry, label_name in rows:
        vector = _parse_vector(embedding.vector)
        if vector is None:
            continue
        location = ""
        if chunk.page_no:
            location = f"第 {chunk.page_no} 页"
        elif chunk.slide_no:
            location = f"第 {chunk.slide_no} 张幻灯片"
        title = attachment.original_filename
        vectors.append(vector.astype(np.float32, copy=False))
        parent_id = int(entry.parent_id) if entry.parent_id is not None else None
        payloads.append({
            "source_type": "attachment_chunk",
            "entry_id": int(entry.id),
            "parent_id": parent_id,
            "root_entry_id": parent_id if parent_id is not None else int(entry.id),
            "attachment_id": int(attachment.id),
            "chunk_id": int(chunk.id),
            "chunk_index": int(chunk.chunk_index),
            "title": f"{title}{f'（{location}）' if location else ''}",
            "label_name": label_name or entry.label_code,
            "created_at": attachment.created_at.isoformat() if attachment.created_at else None,
            "snippet": chunk.content[:240],
            "content": chunk.content,
            "page_no": chunk.page_no,
            "slide_no": chunk.slide_no,
            "modality": chunk.modality,
            "metadata": {
                "filename": attachment.original_filename,
                "mime_type": attachment.mime_type,
                "chunk_index": int(chunk.chunk_index),
                "parent_id": parent_id,
                "root_entry_id": parent_id if parent_id is not None else int(entry.id),
            },
        })
    if not vectors:
        return []
    mat = np.vstack(vectors)
    norms = np.linalg.norm(mat, axis=1)
    norms[norms == 0] = 1.0
    sims = (mat @ query) / (norms * query_norm)
    order = np.argsort(sims)[::-1]
    out: list[dict] = []
    for idx in order:
        score = float(sims[idx])
        if score < min_score:
            if out:
                break
            continue
        item = dict(payloads[int(idx)])
        item["relevance_score"] = score
        out.append(item)
        if len(out) >= top_k:
            break
    return out

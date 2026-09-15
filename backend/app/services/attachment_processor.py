"""
Attachment processing service.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List

from sqlalchemy.orm import Session

from app.config import settings
from app.database import SessionLocal
from app.models import AttachmentChunk, AttachmentEmbedding, EntryAttachment
from app.services.attachment_storage import resolve_storage_path
from app.services.db_retry import is_retryable_db_error, with_session_retry
from app.services.document_extractors import extract_attachment_chunks
from app.services.embedding import generate_embedding
from app.services.knowledge_rag import sync_attachment_chunks_to_knowledge

logger = logging.getLogger(__name__)


def process_attachment_background(attachment_id: int) -> None:
    """Background task wrapper with an isolated DB session."""
    db = SessionLocal()
    try:
        process_attachment(db, attachment_id)
    finally:
        db.close()


def _mark_processing(db: Session, attachment_id: int) -> EntryAttachment | None:
    attachment = (
        db.query(EntryAttachment)
        .filter(EntryAttachment.id == attachment_id)
        .with_for_update()
        .first()
    )
    if not attachment:
        return None
    attachment.status = "processing"
    attachment.error_message = None
    db.commit()
    db.refresh(attachment)
    return attachment


def _mark_failed(db: Session, attachment_id: int, exc: Exception) -> None:
    attachment = (
        db.query(EntryAttachment)
        .filter(EntryAttachment.id == attachment_id)
        .with_for_update()
        .first()
    )
    if not attachment:
        return
    attachment.status = "failed"
    attachment.error_message = str(exc)[:1000]
    db.commit()


def _persist_indexed(
    db: Session,
    attachment_id: int,
    extracted_chunks: List[Dict],
    vectors: List[List[float]],
) -> None:
    attachment = (
        db.query(EntryAttachment)
        .filter(EntryAttachment.id == attachment_id)
        .with_for_update()
        .first()
    )
    if not attachment:
        return

    db.query(AttachmentEmbedding).filter(
        AttachmentEmbedding.chunk_id.in_(
            db.query(AttachmentChunk.id).filter(AttachmentChunk.attachment_id == attachment.id)
        )
    ).delete(synchronize_session=False)
    db.query(AttachmentChunk).filter(AttachmentChunk.attachment_id == attachment.id).delete()
    db.flush()

    page_numbers = set()
    slide_numbers = set()
    for index, (item, vector) in enumerate(zip(extracted_chunks, vectors)):
        page_no = item.get("page_no")
        slide_no = item.get("slide_no")
        if page_no:
            page_numbers.add(page_no)
        if slide_no:
            slide_numbers.add(slide_no)

        content = item["content"]
        chunk = AttachmentChunk(
            attachment_id=attachment.id,
            entry_id=attachment.entry_id,
            user_id=attachment.user_id,
            chunk_index=index,
            modality=item["modality"],
            page_no=page_no,
            slide_no=slide_no,
            content=content,
            token_count=len(content),
            metadata_json=item.get("metadata_json"),
        )
        db.add(chunk)
        db.flush()
        db.add(
            AttachmentEmbedding(
                chunk_id=chunk.id,
                user_id=attachment.user_id,
                embedding_model=settings.embedding_model,
                vector=vector,
            )
        )

    attachment.page_count = max(page_numbers) if page_numbers else None
    attachment.slide_count = max(slide_numbers) if slide_numbers else None
    sync_attachment_chunks_to_knowledge(db, attachment, extracted_chunks, commit=False)
    attachment.status = "indexed"
    attachment.error_message = None
    attachment.processed_at = datetime.now()
    db.commit()


def process_attachment(db: Session, attachment_id: int) -> None:
    """Extract chunks and embeddings for an attachment."""
    try:
        attachment = with_session_retry(
            db=db,
            fn=lambda: _mark_processing(db, attachment_id),
            operation="attachment_mark_processing",
        )
        if not attachment:
            return

        path = resolve_storage_path(attachment.storage_path)
        # OCR (including RapidOCR subprocess) finishes inside extract; only then embed.
        extracted_chunks = extract_attachment_chunks(
            path,
            attachment.mime_type,
            original_filename=attachment.original_filename,
        )
        if not extracted_chunks:
            raise RuntimeError("未提取到可检索文本")

        from app.services.ocr_quality import is_ocr_chunk_search_eligible

        # Low-quality OCR must not enter embedding / knowledge high-quality paths.
        indexable = []
        for item in extracted_chunks:
            modality = str(item.get("modality") or "")
            meta = item.get("metadata_json") or {}
            if modality == "ocr" and not is_ocr_chunk_search_eligible(meta):
                continue
            indexable.append(item)
        if not indexable:
            # Caption-only is still indexable when OCR was rejected for quality.
            indexable = [
                item
                for item in extracted_chunks
                if str(item.get("modality") or "") != "ocr"
            ]
        if not indexable:
            raise RuntimeError("未提取到可检索文本")

        # Embedding runs only after OCR child process has exited (no ONNX+E5 co-resident).
        vectors = [generate_embedding(f"passage: {item['content']}") for item in indexable]
        extracted_chunks = indexable

        with_session_retry(
            db=db,
            fn=lambda: _persist_indexed(db, attachment_id, extracted_chunks, vectors),
            operation="attachment_persist_indexed",
        )
        logger.info("attachment %s indexed with %s chunks", attachment_id, len(extracted_chunks))
    except Exception as exc:
        if is_retryable_db_error(exc):
            raise
        try:
            with_session_retry(
                db=db,
                fn=lambda: _mark_failed(db, attachment_id, exc),
                operation="attachment_mark_failed",
            )
        except Exception:
            db.rollback()
            attachment = db.query(EntryAttachment).filter(EntryAttachment.id == attachment_id).first()
            if attachment:
                attachment.status = "failed"
                attachment.error_message = str(exc)[:1000]
                db.commit()
        logger.warning("attachment %s processing failed: %s", attachment_id, exc)

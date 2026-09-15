"""
Visual RAG service boundary (Phase 5C).

Default disabled; optional fake provider for testable sync/search loop.
Visual vectors stay separate from E5 text embeddings and main /api/ai/search ranking.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlalchemy import inspect
from sqlalchemy.orm import Session

from app.models import AttachmentVisualEmbedding, EntryAttachment
from app.services.attachment_storage import resolve_storage_path
from app.services.visual_embedding_provider import (
    OPENCLIP_PROVIDER_NAME,
    VisualEmbeddingProvider,
    get_visual_embedding_provider,
)
from app.services.visual_vector_index import search_visual_embeddings_index

logger = logging.getLogger(__name__)


@dataclass
class VisualRagStatus:
    """Runtime status for visual retrieval."""

    enabled: bool = False
    reason: str = "visual_rag_disabled"
    provider: str = "disabled"
    model: str = "disabled"
    embedding_dim: int = 0


@dataclass
class VisualSearchResult:
    """Single visual search hit."""

    attachment_id: int
    entry_id: int
    image_ref: str | None = None
    page_no: int | None = None
    score: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class VisualEmbeddingTarget:
    """Image-like target to embed for visual retrieval."""

    image_ref: str
    page_no: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def has_attachment_visual_embeddings_table(db: Session) -> bool:
    try:
        bind = db.get_bind()
        if bind is None:
            return False
        return bool(inspect(bind).has_table("attachment_visual_embeddings"))
    except Exception as exc:
        logger.debug("attachment_visual_embeddings table check failed: %s", exc)
        return False


def _is_image_attachment(mime_type: str | None) -> bool:
    return (mime_type or "").startswith("image/")


def _is_visual_document_attachment(mime_type: str | None) -> bool:
    return mime_type in {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }


def _coerce_positive_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _manifest_items(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        raw_items = payload.get("items") or payload.get("pages") or payload.get("slides") or []
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raw_items = []
    return [item for item in raw_items if isinstance(item, dict)]


def load_visual_page_manifest(preview_path: str | None) -> list[VisualEmbeddingTarget]:
    """Load page/slide image targets from a JSON manifest under upload root.

    Manifest shape:
    {"items": [{"image_ref": "user/1/doc/page-1.png", "page_no": 1, "kind": "pdf_page"}]}
    PPT slides use slide_no in metadata and may leave page_no empty.
    """
    if not preview_path:
        return []
    manifest_path = resolve_storage_path(preview_path)
    if manifest_path.suffix.lower() != ".json":
        return []
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    targets: list[VisualEmbeddingTarget] = []
    seen_refs: set[str] = set()
    for item in _manifest_items(payload):
        image_ref = str(item.get("image_ref") or item.get("path") or item.get("storage_path") or "").strip()
        if not image_ref or image_ref in seen_refs:
            continue
        # Validate the referenced page image stays inside upload root. The provider
        # gets the resolved absolute path later for real image encoders.
        resolve_storage_path(image_ref)
        seen_refs.add(image_ref)
        page_no = _coerce_positive_int(item.get("page_no"))
        slide_no = _coerce_positive_int(item.get("slide_no"))
        target_kind = str(item.get("kind") or ("ppt_slide" if slide_no else "pdf_page"))
        metadata = {
            "visual_target_kind": target_kind,
            "image_ref": image_ref,
        }
        if page_no is not None:
            metadata["page_no"] = page_no
        if slide_no is not None:
            metadata["slide_no"] = slide_no
        for key in ("width", "height", "render_dpi", "renderer"):
            if item.get(key) is not None:
                metadata[key] = item.get(key)
        targets.append(VisualEmbeddingTarget(
            image_ref=image_ref,
            page_no=page_no,
            metadata=metadata,
        ))
    return targets


def build_visual_embedding_targets(attachment: EntryAttachment) -> list[VisualEmbeddingTarget]:
    if _is_image_attachment(attachment.mime_type):
        return [VisualEmbeddingTarget(
            image_ref=attachment.storage_path,
            page_no=None,
            metadata={"visual_target_kind": "image"},
        )]
    if _is_visual_document_attachment(attachment.mime_type):
        return load_visual_page_manifest(getattr(attachment, "preview_path", None))
    return []


def _compute_visual_content_hash(
    attachment: EntryAttachment,
    image_ref: str,
    provider: VisualEmbeddingProvider,
    page_no: int | None = None,
    target_metadata: dict[str, Any] | None = None,
) -> str:
    metadata = target_metadata or {}
    payload = "|".join([
        str(attachment.content_hash or ""),
        image_ref,
        str(page_no or ""),
        str(metadata.get("slide_no") or ""),
        str(metadata.get("visual_target_kind") or ""),
        provider.provider_name,
        provider.model_name,
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_visual_rag_enabled() -> bool:
    return get_visual_embedding_provider().enabled


def get_visual_rag_status() -> VisualRagStatus:
    provider = get_visual_embedding_provider()
    return VisualRagStatus(
        enabled=provider.enabled,
        reason=provider.reason,
        provider=provider.provider_name,
        model=provider.model_name,
        embedding_dim=provider.embedding_dim,
    )


def sync_visual_embeddings_for_attachment(
    db: Session,
    attachment_id: int,
    *,
    force: bool = False,
) -> dict[str, Any]:
    provider = get_visual_embedding_provider()
    if not provider.enabled:
        return {
            "enabled": False,
            "reason": provider.reason,
            "synced": 0,
            "skipped": 1,
            "failed": 0,
        }

    if not has_attachment_visual_embeddings_table(db):
        return {
            "enabled": False,
            "reason": "attachment_visual_embeddings_table_missing",
            "synced": 0,
            "skipped": 1,
            "failed": 0,
        }

    attachment = (
        db.query(EntryAttachment)
        .filter(EntryAttachment.id == attachment_id)
        .first()
    )
    if attachment is None:
        return {
            "enabled": True,
            "reason": "attachment_not_found",
            "synced": 0,
            "skipped": 1,
            "failed": 0,
        }

    try:
        targets = build_visual_embedding_targets(attachment)
    except Exception as exc:
        logger.warning(
            "visual sync build targets failed attachment_id=%s error=%s",
            attachment_id,
            exc,
        )
        return {
            "enabled": True,
            "reason": "visual_target_manifest_failed",
            "synced": 0,
            "skipped": 0,
            "failed": 1,
        }
    if not targets:
        reason = (
            "visual_page_manifest_missing"
            if _is_visual_document_attachment(attachment.mime_type) and getattr(attachment, "preview_path", None)
            else "non_image_attachment"
        )
        return {
            "enabled": True,
            "reason": reason,
            "synced": 0,
            "skipped": 1,
            "failed": 0,
        }

    if force:
        try:
            db.query(AttachmentVisualEmbedding).filter(
                AttachmentVisualEmbedding.attachment_id == attachment_id,
                AttachmentVisualEmbedding.provider == provider.provider_name,
                AttachmentVisualEmbedding.model == provider.model_name,
            ).delete(synchronize_session=False)
        except Exception as exc:
            logger.warning(
                "visual sync delete old embeddings failed attachment_id=%s error=%s",
                attachment_id,
                exc,
            )
            try:
                db.rollback()
            except Exception:
                logger.debug("rollback after visual delete failure skipped", exc_info=True)
            return {
                "enabled": True,
                "reason": "delete_old_embeddings_failed",
                "synced": 0,
                "skipped": 0,
                "failed": 1,
            }

    existing_hashes: set[str] = set()
    if not force:
        existing_rows = (
            db.query(AttachmentVisualEmbedding)
            .filter(
                AttachmentVisualEmbedding.attachment_id == attachment_id,
                AttachmentVisualEmbedding.provider == provider.provider_name,
                AttachmentVisualEmbedding.model == provider.model_name,
            )
            .all()
        )
        for row in existing_rows:
            row_meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
            if row_meta.get("content_hash"):
                existing_hashes.add(str(row_meta.get("content_hash")))

    synced = 0
    skipped = 0

    try:
        for target in targets:
            resolved_path = resolve_storage_path(target.image_ref)
            content_hash = _compute_visual_content_hash(
                attachment,
                target.image_ref,
                provider,
                target.page_no,
                target.metadata,
            )
            if content_hash in existing_hashes:
                skipped += 1
                continue
            metadata = {
                **target.metadata,
                "content_hash": content_hash,
                "mime_type": attachment.mime_type,
                "original_filename": attachment.original_filename,
                "attachment_content_hash": attachment.content_hash,
                "storage_path": attachment.storage_path,
                "image_ref": target.image_ref,
            }
            embedding_image_ref = (
                str(resolved_path)
                if provider.provider_name == OPENCLIP_PROVIDER_NAME
                else target.image_ref
            )
            vector = provider.embed_image(embedding_image_ref, metadata)
            if len(vector) != provider.embedding_dim:
                raise ValueError("provider returned unexpected embedding dimension")

            db.add(AttachmentVisualEmbedding(
                user_id=int(attachment.user_id),
                attachment_id=int(attachment.id),
                entry_id=int(attachment.entry_id),
                image_ref=target.image_ref,
                page_no=target.page_no,
                provider=provider.provider_name,
                model=provider.model_name,
                embedding_dim=provider.embedding_dim,
                vector_json=vector,
                metadata_json=metadata,
            ))
            synced += 1

        if synced == 0 and skipped > 0:
            return {
                "enabled": True,
                "reason": "already_indexed",
                "synced": 0,
                "skipped": skipped,
                "failed": 0,
            }
        db.commit()
        return {
            "enabled": True,
            "reason": provider.reason,
            "synced": synced,
            "skipped": skipped,
            "failed": 0,
        }
    except Exception as exc:
        logger.warning(
            "visual sync failed attachment_id=%s error=%s",
            attachment_id,
            exc,
        )
        try:
            db.rollback()
        except Exception:
            logger.debug("rollback after visual sync failure skipped", exc_info=True)
        return {
            "enabled": True,
            "reason": "sync_failed",
            "synced": 0,
            "skipped": 0,
            "failed": 1,
        }


def rebuild_visual_index(
    db: Session,
    *,
    user_id: int | None = None,
    force: bool = False,
) -> dict[str, Any]:
    provider = get_visual_embedding_provider()
    if not provider.enabled:
        status = get_visual_rag_status()
        return {
            "enabled": False,
            "reason": status.reason,
            "processed": 0,
            "indexed": 0,
            "skipped": 0,
            "failed": 0,
        }

    if not has_attachment_visual_embeddings_table(db):
        return {
            "enabled": False,
            "reason": "attachment_visual_embeddings_table_missing",
            "processed": 0,
            "indexed": 0,
            "skipped": 0,
            "failed": 0,
        }

    stats = {"processed": 0, "indexed": 0, "skipped": 0, "failed": 0}
    query = db.query(EntryAttachment).filter(
        EntryAttachment.mime_type.in_([
            "image/jpeg",
            "image/png",
            "image/webp",
            "application/pdf",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ])
    )
    if user_id is not None:
        query = query.filter(EntryAttachment.user_id == user_id)

    for attachment in query.all():
        stats["processed"] += 1
        outcome = sync_visual_embeddings_for_attachment(db, int(attachment.id), force=force)
        if outcome.get("synced"):
            stats["indexed"] += 1
        elif outcome.get("failed"):
            stats["failed"] += 1
        else:
            stats["skipped"] += 1

    return {
        "enabled": True,
        "reason": provider.reason,
        **stats,
    }


def search_visual(
    db: Session,
    *,
    user_id: int,
    query: str,
    top_k: int = 5,
    min_score: float = 0.0,
) -> list[VisualSearchResult]:
    provider = get_visual_embedding_provider()
    query_text = (query or "").strip()
    if not provider.enabled or not query_text:
        return []
    if not has_attachment_visual_embeddings_table(db):
        logger.debug("attachment_visual_embeddings table missing; visual search skipped")
        return []

    try:
        query_vector = provider.embed_text(query_text)
    except Exception as exc:
        logger.warning("visual search query embedding failed user_id=%s error=%s", user_id, exc)
        return []

    index_hits = search_visual_embeddings_index(
        db,
        user_id=user_id,
        provider=provider.provider_name,
        model=provider.model_name,
        embedding_dim=provider.embedding_dim,
        query_vector=query_vector,
        top_k=top_k,
    )

    hits: List[VisualSearchResult] = []
    for hit in index_hits:
        score = float(hit.score)
        if score < float(min_score):
            continue
        row = hit.row
        row_meta = row.metadata_json if isinstance(row.metadata_json, dict) else {}
        hits.append(VisualSearchResult(
            attachment_id=int(row.attachment_id),
            entry_id=int(row.entry_id),
            image_ref=row.image_ref,
            page_no=row.page_no,
            score=score,
            metadata={
                **row_meta,
                "provider": provider.provider_name,
                "model": provider.model_name,
                "retrieval_method": hit.retrieval_method,
            },
        ))
        if len(hits) >= top_k:
            break

    return hits

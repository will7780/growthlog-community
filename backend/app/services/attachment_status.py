"""
Aggregate OCR visibility fields from persisted attachment chunk metadata.
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from sqlalchemy.orm import Session

from app.config import settings
from app.models.attachment_chunk import AttachmentChunk
from app.models.entry_attachment import EntryAttachment
from app.schemas.attachments import AttachmentResponse
from app.services.ocr_runtime import normalize_ocr_status


def _meta(chunk: AttachmentChunk) -> Dict[str, Any]:
    raw = chunk.metadata_json
    return raw if isinstance(raw, dict) else {}


def aggregate_image_ocr_fields(
    chunks: Sequence[AttachmentChunk],
    *,
    current_pipeline_version: Optional[str] = None,
) -> Dict[str, Any]:
    """Build public OCR fields and detect historical image indexes."""
    methods: list[str] = []
    seen: set[str] = set()
    caption_status: Optional[str] = None
    caption_provider: Optional[str] = None
    any_succeeded = False
    ocr_provider: Optional[str] = None

    expected_version = (
        current_pipeline_version
        if current_pipeline_version is not None
        else settings.attachment_ocr_pipeline_version
    )
    expected_version = str(expected_version or "").strip()
    relevant_versions: list[str] = []
    for chunk in chunks:
        meta = _meta(chunk)
        method = meta.get("extraction_method")
        if chunk.modality in {"caption", "ocr"} or method in {"image_metadata", "ocr"}:
            relevant_versions.append(str(meta.get("ocr_pipeline_version") or "").strip())
        if isinstance(method, str) and method and method not in seen:
            seen.add(method)
            methods.append(method)
        # Legacy source-only rows
        if not method:
            source = meta.get("source")
            if source == "image_metadata" and "image_metadata" not in seen:
                seen.add("image_metadata")
                methods.append("image_metadata")
            elif source in {"pytesseract", "paddleocr", "pdf_page_ocr"} and "ocr" not in seen:
                seen.add("ocr")
                methods.append("ocr")

        status = normalize_ocr_status(meta.get("ocr_status"))
        provider = meta.get("ocr_provider")
        if isinstance(provider, str) and provider:
            if chunk.modality == "ocr" or status == "succeeded":
                ocr_provider = provider
            if chunk.modality == "caption":
                caption_provider = provider

        if chunk.modality == "caption" and status:
            caption_status = status
        if chunk.modality == "ocr" and status == "succeeded":
            any_succeeded = True
            if isinstance(provider, str) and provider:
                ocr_provider = provider

    if any_succeeded:
        final_status = "succeeded"
    else:
        final_status = caption_status

    reindex_required = bool(
        expected_version
        and relevant_versions
        and any(version != expected_version for version in relevant_versions)
    )
    return {
        "ocr_status": final_status,
        "ocr_provider": ocr_provider or caption_provider,
        "extraction_methods": methods or None,
        "ocr_reindex_required": reindex_required,
    }


def attachment_to_response(
    attachment: EntryAttachment,
    *,
    chunks: Optional[Sequence[AttachmentChunk]] = None,
    db: Optional[Session] = None,
) -> AttachmentResponse:
    """Map ORM attachment to API response; enrich images from chunk metadata."""
    data = AttachmentResponse.model_validate(attachment)
    mime = (attachment.mime_type or "").lower()
    if not mime.startswith("image/"):
        return data

    resolved_chunks: Sequence[AttachmentChunk]
    if chunks is not None:
        resolved_chunks = chunks
    elif db is not None:
        resolved_chunks = (
            db.query(AttachmentChunk)
            .filter(
                AttachmentChunk.attachment_id == attachment.id,
                AttachmentChunk.user_id == attachment.user_id,
            )
            .all()
        )
    else:
        return data

    fields = aggregate_image_ocr_fields(resolved_chunks)
    return data.model_copy(update=fields)


def attachments_to_responses(db: Session, attachments: Iterable[EntryAttachment]) -> List[AttachmentResponse]:
    items = list(attachments)
    image_ids = [int(a.id) for a in items if (a.mime_type or "").lower().startswith("image/")]
    by_id: Dict[int, List[AttachmentChunk]] = {i: [] for i in image_ids}
    if image_ids:
        rows = (
            db.query(AttachmentChunk)
            .filter(AttachmentChunk.attachment_id.in_(image_ids))
            .all()
        )
        for row in rows:
            by_id.setdefault(int(row.attachment_id), []).append(row)

    result: List[AttachmentResponse] = []
    for attachment in items:
        if int(attachment.id) in by_id:
            result.append(attachment_to_response(attachment, chunks=by_id[int(attachment.id)]))
        else:
            result.append(attachment_to_response(attachment, chunks=[]))
    return result

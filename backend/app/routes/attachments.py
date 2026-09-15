"""
Attachment routes.
"""
from fastapi import APIRouter, BackgroundTasks, Depends, File, Header, HTTPException, UploadFile, status
from fastapi.responses import FileResponse, Response
from sqlalchemy.orm import Session

from app.auth import User, get_current_user
from app.database import get_db
from app.models import AttachmentProcessingJob, Entry, EntryAttachment
from app.schemas.attachments import AttachmentResponse
from app.config import settings
from app.services.attachment_storage import (
    ALLOWED_EXTENSIONS_BY_MIME,
    resolve_storage_path,
    save_upload_file,
)
from app.services.attachment_processor import process_attachment_background
from app.services.attachment_jobs import (
    choose_upload_background_handler,
    create_attachment_processing_job,
    process_attachment_job_background,
)
from app.services.attachment_status import attachment_to_response, attachments_to_responses
from app.services.attachment_thumbnail import (
    AttachmentThumbnailError,
    THUMBNAIL_MEDIA_TYPE,
    attachment_etag,
    get_or_create_thumbnail,
)

router = APIRouter()

_PRIVATE_PREVIEW_CACHE_CONTROL = "private, max-age=86400"
_PRIVATE_THUMBNAIL_CACHE_CONTROL = "private, max-age=604800, immutable"


def _image_response_headers(etag: str, cache_control: str) -> dict[str, str]:
    return {
        "Cache-Control": cache_control,
        "Content-Disposition": "inline",
        "ETag": etag,
        "Vary": "Authorization",
        "X-Content-Type-Options": "nosniff",
        "Cross-Origin-Resource-Policy": "same-origin",
    }


def _etag_matches(raw_header: object, etag: str) -> bool:
    if not isinstance(raw_header, str):
        return False
    for candidate in raw_header.split(","):
        normalized = candidate.strip()
        if normalized == "*" or normalized.removeprefix("W/") == etag:
            return True
    return False


def _previewable_image_mime_type(attachment: EntryAttachment) -> str:
    mime_type = (attachment.mime_type or "").lower()
    previewable_mime_types = {
        value for value in ALLOWED_EXTENSIONS_BY_MIME if value.startswith("image/")
    }
    if mime_type not in previewable_mime_types:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="当前附件不支持图片预览",
        )
    return mime_type


def _get_user_entry(db: Session, entry_id: int, user_id: int) -> Entry:
    entry = db.query(Entry).filter(Entry.id == entry_id, Entry.user_id == user_id).first()
    if not entry:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="记录不存在")
    return entry


def _get_user_attachment(db: Session, attachment_id: int, user_id: int) -> EntryAttachment:
    attachment = db.query(EntryAttachment).filter(
        EntryAttachment.id == attachment_id,
        EntryAttachment.user_id == user_id,
    ).first()
    if not attachment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在")
    return attachment


def _schedule_attachment_processing(
    background_tasks: BackgroundTasks,
    *,
    job: AttachmentProcessingJob | None,
    attachment_id: int,
) -> None:
    handler = choose_upload_background_handler(
        job=job,
        attachment_id=attachment_id,
        fallback_enabled=settings.attachment_upload_background_fallback_enabled,
    )
    if handler is None:
        return
    kind, target_id = handler
    if kind == "legacy":
        background_tasks.add_task(process_attachment_background, target_id)
    elif kind == "job_fallback":
        background_tasks.add_task(process_attachment_job_background, target_id)


@router.post(
    "/entries/{entry_id}/attachments",
    response_model=AttachmentResponse,
    status_code=status.HTTP_201_CREATED,
)
def upload_entry_attachment(
    entry_id: int,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Upload an attachment for an entry owned by the current user."""
    _get_user_entry(db, entry_id, current_user.id)

    metadata = save_upload_file(file, current_user.id, entry_id)
    from app.services.attachment_sort_order import allocate_attachment_sort_order

    sort_order = allocate_attachment_sort_order(db, entry_id, current_user.id)
    attachment = EntryAttachment(
        entry_id=entry_id,
        user_id=current_user.id,
        sort_order=sort_order,
        **metadata,
        status="uploaded",
    )
    db.add(attachment)
    db.flush()
    job = create_attachment_processing_job(db, attachment)
    db.commit()
    db.refresh(attachment)
    _schedule_attachment_processing(
        background_tasks,
        job=job,
        attachment_id=attachment.id,
    )
    return attachment_to_response(attachment, db=db)


@router.get("/entries/{entry_id}/attachments", response_model=list[AttachmentResponse])
def list_entry_attachments(
    entry_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """List attachments for an entry owned by the current user."""
    _get_user_entry(db, entry_id, current_user.id)
    rows = (
        db.query(EntryAttachment)
        .filter(EntryAttachment.entry_id == entry_id, EntryAttachment.user_id == current_user.id)
        .order_by(
            EntryAttachment.sort_order.asc(),
            EntryAttachment.created_at.asc(),
            EntryAttachment.id.asc(),
        )
        .all()
    )
    return attachments_to_responses(db, rows)


@router.get("/attachments/{attachment_id}", response_model=AttachmentResponse)
def get_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get attachment metadata."""
    attachment = _get_user_attachment(db, attachment_id, current_user.id)
    return attachment_to_response(attachment, db=db)


@router.get("/attachments/{attachment_id}/status", response_model=AttachmentResponse)
def get_attachment_status(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Get attachment processing status (images include aggregated OCR visibility)."""
    attachment = _get_user_attachment(db, attachment_id, current_user.id)
    return attachment_to_response(attachment, db=db)


@router.post(
    "/attachments/{attachment_id}/reprocess",
    response_model=AttachmentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def reprocess_attachment(
    attachment_id: int,
    background_tasks: BackgroundTasks,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Rebuild one user-owned image's OCR text and derived indexes."""
    attachment = _get_user_attachment(db, attachment_id, current_user.id)
    if not (attachment.mime_type or "").lower().startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="当前仅支持重新识别图片文字",
        )

    file_path = resolve_storage_path(attachment.storage_path)
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件文件不存在")

    active_job = (
        db.query(AttachmentProcessingJob)
        .filter(
            AttachmentProcessingJob.attachment_id == attachment.id,
            AttachmentProcessingJob.status.in_(("pending", "processing")),
        )
        .order_by(AttachmentProcessingJob.created_at.desc())
        .first()
    )
    if active_job is not None:
        return attachment_to_response(attachment, db=db)

    attachment.status = "uploaded"
    attachment.error_message = None
    job = create_attachment_processing_job(
        db,
        attachment,
        job_type="reindex_text",
        priority=10,
        metadata={
            "reason": "user_reprocess",
            "ocr_pipeline_version": settings.attachment_ocr_pipeline_version,
        },
    )
    db.commit()
    db.refresh(attachment)
    _schedule_attachment_processing(
        background_tasks,
        job=job,
        attachment_id=attachment.id,
    )
    return attachment_to_response(attachment, db=db)


@router.get("/attachments/{attachment_id}/download")
def download_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Download an attachment after authorization."""
    attachment = _get_user_attachment(db, attachment_id, current_user.id)
    file_path = resolve_storage_path(attachment.storage_path)
    if not file_path.exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件文件不存在")
    return FileResponse(
        path=str(file_path),
        media_type=attachment.mime_type,
        filename=attachment.original_filename,
    )


@router.get("/attachments/{attachment_id}/preview")
def preview_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
):
    """Display a user-owned raster image inline without exposing its storage path."""
    attachment = _get_user_attachment(db, attachment_id, current_user.id)
    mime_type = _previewable_image_mime_type(attachment)
    file_path = resolve_storage_path(attachment.storage_path)
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件文件不存在")

    etag = attachment_etag(attachment.content_hash, "preview")
    headers = _image_response_headers(etag, _PRIVATE_PREVIEW_CACHE_CONTROL)
    if _etag_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    return FileResponse(
        path=str(file_path),
        media_type=mime_type,
        headers=headers,
    )


@router.get("/attachments/{attachment_id}/thumbnail")
def thumbnail_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
):
    """Return a cached, bounded thumbnail for a user-owned raster image."""
    attachment = _get_user_attachment(db, attachment_id, current_user.id)
    _previewable_image_mime_type(attachment)
    file_path = resolve_storage_path(attachment.storage_path)
    if not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件文件不存在")

    etag = attachment_etag(attachment.content_hash, "thumbnail")
    headers = _image_response_headers(etag, _PRIVATE_THUMBNAIL_CACHE_CONTROL)
    if _etag_matches(if_none_match, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers=headers)
    try:
        thumbnail = get_or_create_thumbnail(file_path, attachment.content_hash)
    except AttachmentThumbnailError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="图片缩略图生成失败",
        ) from exc
    return FileResponse(
        path=str(thumbnail.path),
        media_type=THUMBNAIL_MEDIA_TYPE,
        headers=headers,
    )


@router.delete("/attachments/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_attachment(
    attachment_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Delete an attachment and its stored file."""
    from app.services.attachment_cleanup import (
        cancel_attachment_jobs,
        delete_attachment_knowledge,
        delete_stored_files_best_effort,
    )

    attachment = _get_user_attachment(db, attachment_id, current_user.id)
    storage_path = attachment.storage_path
    aid = int(attachment.id)

    cancel_attachment_jobs(db, [aid])
    delete_attachment_knowledge(
        db, user_id=int(current_user.id), attachment_ids=[aid]
    )

    db.delete(attachment)
    db.commit()
    delete_stored_files_best_effort([storage_path])
    return None

"""
Attachment storage service.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status

from app.config import settings


ALLOWED_EXTENSIONS_BY_MIME = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "application/pdf": {".pdf"},
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": {".pptx"},
}


def get_upload_root() -> Path:
    """Return the absolute upload root."""
    root = Path(settings.upload_dir)
    if not root.is_absolute():
        root = Path(__file__).parent.parent.parent / root
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _safe_extension(filename: str, mime_type: str) -> str:
    ext = Path(filename or "").suffix.lower()
    allowed = ALLOWED_EXTENSIONS_BY_MIME.get(mime_type, set())
    if ext not in allowed:
        allowed_text = ", ".join(sorted(allowed)) or mime_type
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"文件类型与扩展名不匹配，仅允许 {allowed_text}",
        )
    return ext


def validate_upload_file(file: UploadFile) -> str:
    """Validate MIME type and extension, returning the normalized extension."""
    mime_type = file.content_type or ""
    if mime_type not in settings.allowed_mime_types:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"不支持的文件类型: {mime_type}",
        )
    return _safe_extension(file.filename or "", mime_type)


def _ensure_inside_root(path: Path, root: Path) -> None:
    resolved = path.resolve()
    if root not in resolved.parents and resolved != root:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="非法文件路径",
        )


def save_upload_file(file: UploadFile, user_id: int, entry_id: int) -> dict:
    """Save an uploaded file and return storage metadata."""
    ext = validate_upload_file(file)
    root = get_upload_root()
    target_dir = root / str(user_id) / str(entry_id)
    target_dir.mkdir(parents=True, exist_ok=True)

    storage_filename = f"{uuid4().hex}{ext}"
    target_path = target_dir / storage_filename
    _ensure_inside_root(target_path, root)

    sha256 = hashlib.sha256()
    total_size = 0

    try:
        with target_path.open("wb") as out:
            while True:
                chunk = file.file.read(1024 * 1024)
                if not chunk:
                    break
                total_size += len(chunk)
                if total_size > settings.max_file_size:
                    out.close()
                    target_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"文件过大，最大允许 {settings.max_file_size // 1024 // 1024}MB",
                    )
                sha256.update(chunk)
                out.write(chunk)
    finally:
        file.file.close()

    if total_size <= 0:
        target_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="文件为空",
        )

    return {
        "original_filename": Path(file.filename or "attachment").name,
        "storage_filename": storage_filename,
        # Keep legacy file_name in sync when the old NOT NULL column still exists.
        "file_name": Path(file.filename or "attachment").name,
        "mime_type": file.content_type or "",
        "file_ext": ext,
        "file_size": total_size,
        "content_hash": sha256.hexdigest(),
        "storage_path": str(target_path.relative_to(root)),
    }


def resolve_storage_path(storage_path: str) -> Path:
    """Resolve a stored relative path to an absolute path."""
    root = get_upload_root()
    path = (root / storage_path).resolve()
    _ensure_inside_root(path, root)
    return path


def delete_stored_file(storage_path: str) -> None:
    """Delete a stored file and exact generated thumbnail sidecars."""
    path = resolve_storage_path(storage_path)
    try:
        # Local import avoids a module cycle: thumbnail generation reuses the
        # upload-root jail from this module.
        from app.services.attachment_thumbnail import delete_thumbnail_sidecars

        delete_thumbnail_sidecars(path)
    finally:
        path.unlink(missing_ok=True)

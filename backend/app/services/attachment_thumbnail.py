"""Authenticated raster thumbnail generation and sidecar cleanup."""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from uuid import uuid4

from PIL import Image, ImageOps, UnidentifiedImageError

from app.services.attachment_storage import get_upload_root


THUMBNAIL_MAX_EDGE = 480
THUMBNAIL_QUALITY = 82
THUMBNAIL_VERSION = "v1"
THUMBNAIL_MEDIA_TYPE = "image/jpeg"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_THUMBNAIL_LOCKS = tuple(Lock() for _ in range(64))


class AttachmentThumbnailError(RuntimeError):
    """Raised when a safe thumbnail cannot be produced."""


@dataclass(frozen=True)
class AttachmentThumbnail:
    path: Path
    width: int
    height: int


def _ensure_inside_upload_root(path: Path) -> Path:
    root = get_upload_root()
    resolved = path.resolve()
    if root not in resolved.parents:
        raise AttachmentThumbnailError("thumbnail_path_outside_upload_root")
    return resolved


def _cache_key(source_path: Path, content_hash: str) -> str:
    normalized = (content_hash or "").strip().lower()
    if _HASH_RE.fullmatch(normalized):
        return normalized[:16]
    stat = source_path.stat()
    fallback = f"{source_path.name}:{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:16]


def thumbnail_path_for(source_path: Path, content_hash: str) -> Path:
    source = _ensure_inside_upload_root(source_path)
    key = _cache_key(source, content_hash)
    target = source.with_name(
        f".{source.name}.{key}.thumb-{THUMBNAIL_VERSION}-{THUMBNAIL_MAX_EDGE}.jpg"
    )
    return _ensure_inside_upload_root(target)


def _rgb_frame(image: Image.Image) -> Image.Image:
    image.seek(0)
    try:
        image.draft("RGB", (THUMBNAIL_MAX_EDGE * 2, THUMBNAIL_MAX_EDGE * 2))
    except (AttributeError, OSError):
        pass
    frame = ImageOps.exif_transpose(image)
    if frame.mode == "RGB":
        return frame.copy()
    if frame.mode in {"RGBA", "LA"} or "transparency" in frame.info:
        rgba = frame.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return frame.convert("RGB")


def _read_thumbnail_dimensions(path: Path) -> tuple[int, int]:
    with Image.open(path) as image:
        return int(image.width), int(image.height)


def _lock_for(target: Path) -> Lock:
    digest = hashlib.sha256(str(target).encode("utf-8")).digest()
    return _THUMBNAIL_LOCKS[int.from_bytes(digest[:2], "big") % len(_THUMBNAIL_LOCKS)]


def get_or_create_thumbnail(source_path: Path, content_hash: str) -> AttachmentThumbnail:
    source = _ensure_inside_upload_root(source_path)
    if not source.is_file():
        raise AttachmentThumbnailError("source_missing")
    target = thumbnail_path_for(source, content_hash)
    with _lock_for(target):
        return _get_or_create_thumbnail_locked(source, target)


def _get_or_create_thumbnail_locked(source: Path, target: Path) -> AttachmentThumbnail:
    if target.is_file() and target.stat().st_size > 0:
        try:
            width, height = _read_thumbnail_dimensions(target)
            if max(width, height) <= THUMBNAIL_MAX_EDGE:
                return AttachmentThumbnail(target, width, height)
        except (OSError, UnidentifiedImageError):
            target.unlink(missing_ok=True)

    temp_path = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    temp_path = _ensure_inside_upload_root(temp_path)
    try:
        with Image.open(source) as image:
            frame = _rgb_frame(image)
        try:
            frame.thumbnail(
                (THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE),
                Image.Resampling.LANCZOS,
            )
            width, height = int(frame.width), int(frame.height)
            frame.save(
                temp_path,
                format="JPEG",
                quality=THUMBNAIL_QUALITY,
                optimize=True,
                progressive=True,
            )
        finally:
            frame.close()
        try:
            temp_path.chmod(0o600)
        except OSError:
            pass
        os.replace(temp_path, target)
        return AttachmentThumbnail(target, width, height)
    except (OSError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AttachmentThumbnailError("thumbnail_generation_failed") from exc
    finally:
        temp_path.unlink(missing_ok=True)


def delete_thumbnail_sidecars(source_path: Path) -> int:
    source = _ensure_inside_upload_root(source_path)
    prefix = f".{source.name}."
    suffix = f".thumb-{THUMBNAIL_VERSION}-{THUMBNAIL_MAX_EDGE}.jpg"
    removed = 0
    for candidate in source.parent.iterdir():
        if not candidate.name.startswith(prefix) or not candidate.name.endswith(suffix):
            continue
        cache_key = candidate.name[len(prefix) : -len(suffix)]
        if not re.fullmatch(r"[0-9a-f]{16}", cache_key):
            continue
        safe_candidate = _ensure_inside_upload_root(candidate)
        safe_candidate.unlink(missing_ok=True)
        removed += 1
    return removed


def attachment_etag(content_hash: str, variant: str) -> str:
    raw = f"{variant}:{(content_hash or '').strip().lower()}:{THUMBNAIL_VERSION}"
    return f'"{hashlib.sha256(raw.encode("utf-8")).hexdigest()}"'

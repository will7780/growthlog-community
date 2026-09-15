"""
Normalize arbitrary image containers for OCR engines.

Content/Pillow-format based (not extension-based). Original file is read-only.
Yields a temporary RGB PNG path suitable for Tesseract and RapidOCR.
"""
from __future__ import annotations

import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Generator, Iterator, List, Optional, Tuple


def _frame_area(image) -> int:
    try:
        return int(image.width) * int(image.height)
    except Exception:
        return 0


def _iter_frames(image) -> List[Any]:
    frames: List[Any] = []
    try:
        n = int(getattr(image, "n_frames", 1) or 1)
    except Exception:
        n = 1
    if n <= 1:
        frames.append(image.copy())
        return frames
    for idx in range(n):
        try:
            image.seek(idx)
            frames.append(image.copy())
        except Exception:
            continue
    if not frames:
        frames.append(image.copy())
    return frames


def _pick_primary_frame(frames: List[Any]) -> Tuple[Any, int]:
    """Prefer largest area; ties keep the earliest frame (stable)."""
    best_idx = 0
    best_area = -1
    for idx, frame in enumerate(frames):
        area = _frame_area(frame)
        if area > best_area:
            best_area = area
            best_idx = idx
    return frames[best_idx], best_idx


def _to_rgb_white_bg(image) -> Any:
    from PIL import Image  # type: ignore

    if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    if image.mode != "RGB":
        return image.convert("RGB")
    return image


@contextmanager
def normalized_ocr_image(source_path: Path) -> Generator[Tuple[Path, Dict[str, Any]], None, None]:
    """
    Context manager yielding (temp_png_path, safe_meta).

    safe_meta may include source_format / frame_count / normalized_format / selected_frame.
    Never includes filesystem paths.
    """
    from PIL import Image, ImageOps  # type: ignore

    temp_path: Optional[Path] = None
    meta: Dict[str, Any] = {
        "normalized_format": "PNG",
    }
    try:
        with Image.open(source_path) as opened:
            source_format = str(getattr(opened, "format", None) or "unknown")
            meta["source_format"] = source_format
            frames = _iter_frames(opened)
            meta["frame_count"] = len(frames)
            primary, selected = _pick_primary_frame(frames)
            meta["selected_frame"] = int(selected)
            try:
                primary = ImageOps.exif_transpose(primary)
            except Exception:
                pass
            rgb = _to_rgb_white_bg(primary)
            fd, name = tempfile.mkstemp(prefix="growthlog_ocr_", suffix=".png")
            import os

            os.close(fd)
            temp_path = Path(name)
            rgb.save(temp_path, format="PNG")
        yield temp_path, meta
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except TypeError:
                try:
                    if temp_path.exists():
                        temp_path.unlink()
                except Exception:
                    pass
            except Exception:
                pass

"""
Confirmed merge helpers for Agent long-term memories.

The service never chooses memories automatically. Callers must provide the
target memory and source memories after user confirmation.
"""
from __future__ import annotations

from typing import Iterable, List, Optional

from app.services.agent_memory_quality import normalize_memory_content


def _unique_parts(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    parts: List[str] = []
    for value in values:
        normalized = normalize_memory_content(value)
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        parts.append(normalized)
    return parts


def build_merged_memory_content(
    target_content: str,
    source_contents: Iterable[str],
    *,
    replacement_content: Optional[str] = None,
    max_len: int = 2000,
) -> str:
    """Build merged content without inventing new facts."""
    if replacement_content is not None:
        return normalize_memory_content(replacement_content, max_len=max_len)
    parts = _unique_parts([target_content, *list(source_contents)])
    return normalize_memory_content("；".join(parts), max_len=max_len)


def merge_memory_rows(
    *,
    target,
    sources: Iterable,
    replacement_content: Optional[str] = None,
    confidence: Optional[float] = None,
) -> List[int]:
    """
    Mutate already-loaded ORM-like rows for a confirmed merge.

    Returns source ids that were soft-deactivated.
    """
    source_list = list(sources)
    target.content = build_merged_memory_content(
        getattr(target, "content", "") or "",
        [getattr(item, "content", "") or "" for item in source_list],
        replacement_content=replacement_content,
    )
    if confidence is not None:
        target.confidence = float(confidence)
    else:
        values = [float(getattr(target, "confidence", 0.0) or 0.0)]
        values.extend(float(getattr(item, "confidence", 0.0) or 0.0) for item in source_list)
        target.confidence = max(values) if values else float(getattr(target, "confidence", 0.0) or 0.0)
    target.is_active = True
    target.source = "user_merge"

    deactivated: List[int] = []
    for item in source_list:
        item.is_active = False
        deactivated.append(int(item.id))
    return deactivated

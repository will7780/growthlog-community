"""
Lightweight quality checks for Agent long-term memories.

This module is intentionally rule-based and read-only. It helps surface
possible duplicates or conflicts before a memory is saved, but never merges,
deactivates, or rewrites existing memories by itself.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
else:
    Session = object

NEGATION_MARKERS = ("不", "不要", "不能", "避免", "禁止", "不再", "无需", "别")
PUNCT_RE = re.compile(r"[\s\r\n\t，。！？、；：,.!?;:'\"“”‘’（）()【】\[\]{}<>《》\-_/\\|`~]+")


@dataclass
class MemoryQualityResult:
    normalized_content: str
    warnings: List[Dict]


def normalize_memory_content(content: str, *, max_len: int = 2000) -> str:
    """Trim user-facing memory content without changing its meaning."""
    return re.sub(r"\s+", " ", str(content or "").strip())[:max_len]


def _fingerprint(content: str) -> str:
    return PUNCT_RE.sub("", normalize_memory_content(content).lower())


def _without_negation_markers(text: str) -> str:
    cleaned = text
    for marker in NEGATION_MARKERS:
        cleaned = cleaned.replace(marker, "")
    return cleaned


def _char_bigrams(text: str) -> set[str]:
    if len(text) <= 1:
        return {text} if text else set()
    return {text[idx:idx + 2] for idx in range(len(text) - 1)}


def _similarity(left: str, right: str) -> float:
    left_terms = _char_bigrams(left)
    right_terms = _char_bigrams(right)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _has_negation(text: str) -> bool:
    return any(marker in text for marker in NEGATION_MARKERS)


def _warning(kind: str, memory, similarity: float, message: str) -> Dict:
    return {
        "kind": kind,
        "memory_id": int(memory.id),
        "memory_type": memory.memory_type,
        "similarity": round(float(similarity), 4),
        "message": message,
    }


def analyze_memory_candidate_rows(
    rows: Iterable,
    *,
    memory_type: str,
    content: str,
    exclude_memory_id: Optional[int] = None,
) -> MemoryQualityResult:
    """Return duplicate/conflict warnings from already-loaded memory rows."""
    normalized_content = normalize_memory_content(content)
    candidate_fp = _fingerprint(normalized_content)
    warnings: List[Dict] = []
    if not candidate_fp:
        return MemoryQualityResult(normalized_content=normalized_content, warnings=warnings)

    candidate_without_negation = _without_negation_markers(candidate_fp)
    candidate_has_negation = _has_negation(candidate_fp)
    for memory in rows:
        if exclude_memory_id is not None and int(memory.id) == int(exclude_memory_id):
            continue
        if getattr(memory, "memory_type", None) != memory_type:
            continue
        existing_fp = _fingerprint(memory.content)
        if not existing_fp:
            continue

        similarity = _similarity(candidate_fp, existing_fp)
        contained = (
            min(len(candidate_fp), len(existing_fp)) >= 8
            and (candidate_fp in existing_fp or existing_fp in candidate_fp)
        )
        if candidate_fp == existing_fp or contained or similarity >= 0.82:
            warnings.append(_warning(
                "possible_duplicate",
                memory,
                1.0 if candidate_fp == existing_fp else similarity,
                "存在同类型近似记忆，建议复用或更新原记忆，避免重复保存。",
            ))
            continue

        existing_has_negation = _has_negation(existing_fp)
        if candidate_has_negation == existing_has_negation:
            continue

        existing_without_negation = _without_negation_markers(existing_fp)
        conflict_similarity = _similarity(candidate_without_negation, existing_without_negation)
        conflict_contained = (
            min(len(candidate_without_negation), len(existing_without_negation)) >= 8
            and (
                candidate_without_negation in existing_without_negation
                or existing_without_negation in candidate_without_negation
            )
        )
        if conflict_contained or conflict_similarity >= 0.68:
            warnings.append(_warning(
                "possible_conflict",
                memory,
                conflict_similarity,
                "存在同类型相近但表达方向可能相反的记忆，建议人工确认后再保存。",
            ))

    return MemoryQualityResult(
        normalized_content=normalized_content,
        warnings=warnings[:5],
    )


def analyze_memory_candidate(
    db: "Session",
    *,
    user_id: int,
    memory_type: str,
    content: str,
    exclude_memory_id: Optional[int] = None,
    limit: int = 80,
) -> MemoryQualityResult:
    """Load active same-type memories and analyze a candidate memory."""
    from app.models import AgentMemory

    rows = (
        db.query(AgentMemory)
        .filter(
            AgentMemory.user_id == user_id,
            AgentMemory.is_active.is_(True),
            AgentMemory.memory_type == memory_type,
        )
        .order_by(AgentMemory.updated_at.desc(), AgentMemory.created_at.desc())
        .limit(limit)
        .all()
    )
    return analyze_memory_candidate_rows(
        rows,
        memory_type=memory_type,
        content=content,
        exclude_memory_id=exclude_memory_id,
    )

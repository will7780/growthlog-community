"""
Lightweight answer style guard (Generation Phase 4).

Stdlib-only pure helpers: detect internal field leaks / template voice,
and apply conservative sanitization. No LLM rewrite, no new dependencies.
"""
from __future__ import annotations

import logging
import re
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

INTERNAL_FIELD_RE = re.compile(
    r"\b(entry_id|chunk_id|source_type|source_id|reference_key|metadata|valid_references|"
    r"invalid_references|valid_entry_ids|invalid_entry_ids|retrieval_method|relevance_score)\b",
    re.IGNORECASE,
)
INTERNAL_ASSIGN_RE = re.compile(
    r"\b(entry_id|chunk_id|source_type|source_id|reference_key)\s*=\s*\S+",
    re.IGNORECASE,
)
JSON_FENCE_RE = re.compile(r"```(?:json|javascript|js|python)?\s*", re.IGNORECASE)
CODE_FENCE_CLOSE_RE = re.compile(r"```+")
JSONISH_LINE_RE = re.compile(
    r"^\s*[\[{].*[\]}]\s*$|"
    r'^\s*"[^"]+"\s*:\s*',
    re.IGNORECASE,
)

TEMPLATE_PHRASE_PATTERNS: List[re.Pattern[str]] = [
    re.compile(r"根据您的参考笔记[，,、]?\s*", re.IGNORECASE),
    re.compile(r"根据你的参考笔记[，,、]?\s*", re.IGNORECASE),
    re.compile(r"根据参考笔记[，,、]?\s*", re.IGNORECASE),
    re.compile(r"根据检索结果[，,、]?\s*", re.IGNORECASE),
    re.compile(r"根据您的检索结果[，,、]?\s*", re.IGNORECASE),
    re.compile(r"根据工具结果[，,、]?\s*", re.IGNORECASE),
    re.compile(r"基于以上参考笔记[，,、]?\s*", re.IGNORECASE),
    re.compile(r"基于参考笔记[，,、]?\s*", re.IGNORECASE),
    re.compile(r"基于检索结果[，,、]?\s*", re.IGNORECASE),
    re.compile(r"我找到了\s*\d+\s*条(?:相关)?记录[，,、。]?\s*", re.IGNORECASE),
    re.compile(r"我找到了以下几条记录[：:，,]?\s*", re.IGNORECASE),
    re.compile(r"在您的参考笔记中[，,、]?\s*", re.IGNORECASE),
    re.compile(r"从检索到的记录来看[，,、]?\s*", re.IGNORECASE),
    re.compile(r"根据系统检索到的内容[，,、]?\s*", re.IGNORECASE),
]

TEMPLATE_OPENING_RE = re.compile(
    r"^\s*(根据您的参考笔记|根据你的参考笔记|根据参考笔记|根据检索结果|"
    r"根据您的检索结果|根据工具结果|基于以上参考笔记|基于参考笔记|"
    r"基于检索结果|我找到了|在您的参考笔记中|从检索到的记录来看|"
    r"根据系统检索到的内容)",
    re.IGNORECASE,
)


def has_internal_leak(text: str) -> bool:
    if not text:
        return False
    if INTERNAL_FIELD_RE.search(text):
        return True
    if INTERNAL_ASSIGN_RE.search(text):
        return True
    if "```json" in text.lower():
        return True
    return False


def has_template_voice(text: str) -> bool:
    if not text or not text.strip():
        return False
    if TEMPLATE_OPENING_RE.search(text):
        return True
    for pattern in TEMPLATE_PHRASE_PATTERNS:
        if pattern.search(text):
            return True
    return False


def detect_template_phrases(text: str) -> List[str]:
    hits: List[str] = []
    if not text:
        return hits
    for pattern in TEMPLATE_PHRASE_PATTERNS:
        match = pattern.search(text)
        if match:
            phrase = match.group(0).strip()
            if phrase and phrase not in hits:
                hits.append(phrase)
    return hits


def _strip_code_fences(text: str) -> str:
    cleaned = JSON_FENCE_RE.sub("", text)
    cleaned = CODE_FENCE_CLOSE_RE.sub("", cleaned)
    return cleaned


def _drop_internal_lines(text: str) -> str:
    kept: List[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            kept.append(line)
            continue
        if INTERNAL_ASSIGN_RE.search(stripped):
            continue
        if INTERNAL_FIELD_RE.search(stripped) and (
            stripped.startswith("-")
            or stripped.startswith("*")
            or ":" in stripped
            or "=" in stripped
            or stripped.startswith("{")
            or stripped.startswith("[")
        ):
            continue
        if JSONISH_LINE_RE.match(stripped) and INTERNAL_FIELD_RE.search(stripped):
            continue
        kept.append(line)
    return "\n".join(kept)


def _rewrite_template_openings(text: str) -> str:
    result = text
    for pattern in TEMPLATE_PHRASE_PATTERNS:
        result = pattern.sub("", result, count=1)
    # Collapse leftover leading punctuation / whitespace after removals.
    result = re.sub(r"^[\s，,、：:。]+", "", result)
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip()


def sanitize_answer_text(text: Optional[str]) -> str:
    """
    Conservatively clean answer text.

    Removes obvious internal field lines, JSON/code fences, and common
    retrieval-report openings. Does not call an LLM.
    """
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    if not text.strip():
        return text

    cleaned = _strip_code_fences(text)
    cleaned = _drop_internal_lines(cleaned)
    # Soft-remove remaining field tokens when they appear as bare labels.
    cleaned = INTERNAL_ASSIGN_RE.sub("", cleaned)
    cleaned = re.sub(
        r"\b(entry_id|chunk_id|source_type|source_id|reference_key|valid_references|invalid_references)\b\s*[:=]?\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = _rewrite_template_openings(cleaned)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip() or text.strip()


def apply_answer_style_guard(text: Optional[str]) -> Tuple[str, dict]:
    """
    Apply sanitize and return (text, debug_info). Never raises to callers.
    """
    original = "" if text is None else str(text)
    debug = {
        "internal_leak_before": False,
        "template_voice_before": False,
        "changed": False,
        "error": None,
    }
    try:
        debug["internal_leak_before"] = has_internal_leak(original)
        debug["template_voice_before"] = has_template_voice(original)
        cleaned = sanitize_answer_text(original)
        debug["changed"] = cleaned != original.strip()
        return cleaned, debug
    except Exception as exc:
        logger.debug("answer style guard failed: %s", exc)
        debug["error"] = type(exc).__name__
        return original, debug

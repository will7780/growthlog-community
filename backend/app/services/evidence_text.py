"""
Shared evidence serialization for answer prompts and eval verifiers.

Answer model and claim-evidence verifier MUST see the same title/content/location
text and the same character budget. Public reports must not dump bodies.
"""
from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Tuple

EVIDENCE_CHAR_LIMIT = 4000


def _filename(ref: Dict[str, Any]) -> str:
    meta = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
    name = meta.get("filename")
    if isinstance(name, str) and name.strip():
        return name.strip()
    title = str(ref.get("title") or "")
    return title.split("（", 1)[0].strip()


def _location(ref: Dict[str, Any]) -> str:
    if ref.get("page_no") not in (None, ""):
        return f"第 {ref.get('page_no')} 页"
    if ref.get("slide_no") not in (None, ""):
        return f"第 {ref.get('slide_no')} 张幻灯片"
    return ""


def _created_at_prefix(value: Any, n: int = 10) -> str:
    if value is None or value == "":
        return ""
    return str(value)[:n]


def serialize_evidence_body(
    ref: Dict[str, Any],
    *,
    char_limit: int = EVIDENCE_CHAR_LIMIT,
) -> str:
    """Canonical evidence body used by answer + verifier (no reference_key)."""
    source_type = str(ref.get("source_type") or "entry")
    title = str(ref.get("title") or "")
    created = _created_at_prefix(ref.get("created_at"))
    filename = _filename(ref)
    location = _location(ref)
    # Prefer full content; fall back to snippet/text.
    content = str(
        ref.get("content")
        or ref.get("text")
        or ref.get("snippet")
        or ""
    ).strip()
    if source_type == "todo":
        meta = ref.get("metadata") if isinstance(ref.get("metadata"), dict) else {}
        path = " > ".join(str(item) for item in (meta.get("path_titles") or []) if str(item).strip())
        header = f"小要事 路径: {path or title}"
        if created:
            header += f" (创建时间: {created})"
    elif source_type in {"attachment_chunk", "knowledge_source", "attachment"}:
        header_bits = [f"附件 {filename or title}"]
        if location:
            header_bits.append(location)
        if created:
            header_bits.append(f"时间 {created}")
        header = "，".join(header_bits)
    else:
        header = f"记录 标题: {title}"
        if created:
            header += f" (时间: {created})"
    body = f"{header}\n{content}".strip()
    if len(body) > char_limit:
        body = body[:char_limit]
    return body


def evidence_sha256(
    ref: Dict[str, Any],
    *,
    char_limit: int = EVIDENCE_CHAR_LIMIT,
) -> str:
    text = serialize_evidence_body(ref, char_limit=char_limit)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def annotate_evidence_serialization(
    refs: Sequence[Dict[str, Any]],
    *,
    char_limit: int = EVIDENCE_CHAR_LIMIT,
) -> List[Dict[str, Any]]:
    """Return shallow copies with evidence_body + evidence_sha256 attached."""
    out: List[Dict[str, Any]] = []
    for ref in refs or []:
        if not isinstance(ref, dict):
            continue
        item = dict(ref)
        body = serialize_evidence_body(item, char_limit=char_limit)
        item["evidence_body"] = body
        item["evidence_sha256"] = hashlib.sha256(body.encode("utf-8")).hexdigest()
        item["evidence_char_limit"] = char_limit
        out.append(item)
    return out


def evidence_blocks_for_prompt(
    refs: Sequence[Dict[str, Any]],
    *,
    char_limit: int = EVIDENCE_CHAR_LIMIT,
    include_key: bool = True,
) -> Tuple[str, List[Dict[str, str]]]:
    """
    Build prompt blocks and private hash ledger.

    Public reports should only keep the ledger hashes, never bodies.
    """
    annotated = annotate_evidence_serialization(refs, char_limit=char_limit)
    parts: List[str] = []
    ledger: List[Dict[str, str]] = []
    for idx, ref in enumerate(annotated, start=1):
        key = str(ref.get("reference_key") or "")
        if include_key and key:
            header = f"【参考 {idx}，key={key}】"
        else:
            header = f"【参考 {idx}】"
        parts.append(f"{header}\n{ref['evidence_body']}")
        ledger.append(
            {
                "reference_key": key,
                "evidence_sha256": str(ref["evidence_sha256"]),
                "evidence_char_limit": str(char_limit),
            }
        )
    return "\n\n".join(parts), ledger

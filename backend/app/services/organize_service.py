"""
整理师：范围预览 → 记录族证据 → 分组提取 → 汇总生成 → 严格校验 → confirm 派生表。

R11.6：模型只见 S1/S2 别名；不改 Entry；preview 失败不写半消息（由路由保证）。
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from fastapi import HTTPException
from fastapi import status as http_status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from pathlib import PurePosixPath, PureWindowsPath

from app.models import AttachmentChunk, Entry, EntryAttachment
from app.services.ai_stream_protocol import (
    PURPOSE_ANSWER_REFERENCE,
    sign_organize_preview_token,
    sign_reference_token,
    verify_organize_preview_token,
)
from app.services.ai_personalization_context import (
    compose_system_with_personalization,
    load_personalization_context,
)
from app.services.derived_content import build_derived_metadata, create_derived_content
from app.services.entry_family import build_record_family, resolve_root_entry_id
from app.services.llm_gateway import LLMGatewayError
from app.services.ocr_quality import is_ocr_chunk_search_eligible
from app.services.reference_identity import canonical_reference_key
from app.services.structured_llm import StructuredLLMError, generate_structured_json
from app.timeutil import now_local

logger = logging.getLogger(__name__)

ALLOWED_ACTIONS = frozenset(
    {
        "create_summary",
        "suggest_tag",
        "create_action_plan",
        "rewrite_content",
        "memory_note",
    }
)
ALLOWED_RISKS = frozenset({"low", "medium", "high"})
ORGANIZE_CONFIRM_ACTION_MAP = {
    "create_summary": "organize_summary",
    "suggest_tag": "tag_suggestion",
    "create_action_plan": "action_plan",
    "rewrite_content": "annotation",
    "memory_note": "memory_note",
}
MAX_ORGANIZE_ENTRIES = 40
MAX_DIFF_ITEMS = 12
DEFAULT_DAYS = 30
ALLOWED_DAYS = frozenset({7, 30, 90})
GROUP_CHAR_BUDGET = 6000
MAX_EVIDENCE_CHARS_PER_ENTRY = 2400
EXTRACT_SYSTEM = """你是成长记录整理助手的分组提取器。
只根据本分组证据提取主题、事实、待办与矛盾。必须输出严格 JSON：
{
  "topics": ["..."],
  "facts": [{"text":"...","sources":["S1"]}],
  "actions": [{"text":"...","sources":["S1"]}],
  "contradictions": [{"text":"...","sources":["S1","S2"]}]
}
规则：sources 只能使用提供的 S# 别名；不得编造；不要 markdown。
"""
SUMMARIZE_SYSTEM = """你是成长记录整理助手的汇总器。
基于已验证的分组提取结果，生成最终整理建议。必须输出严格 JSON：
{
  "items": [
    {
      "action": "create_summary|suggest_tag|create_action_plan|rewrite_content|memory_note",
      "title": "短标题",
      "content": "建议正文",
      "reason": "理由",
      "risk": "low|medium|high",
      "confidence": 0.0,
      "sources": ["S1"]
    }
  ]
}
规则：sources 只能使用提供的 S# 别名；最多 12 条；不得编造；不要 markdown。
"""


class OrganizeError(Exception):
    def __init__(self, code: str, message: str, *, http_status: int = 503):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class ExtractFact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=2000)
    sources: List[str] = Field(min_length=1)


class ExtractBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topics: List[str] = Field(default_factory=list)
    facts: List[ExtractFact] = Field(default_factory=list)
    actions: List[ExtractFact] = Field(default_factory=list)
    contradictions: List[ExtractFact] = Field(default_factory=list)


class OrganizeItemModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: str
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1, max_length=8000)
    reason: str = ""
    risk: str = "low"
    confidence: float = 0.5
    sources: List[str] = Field(min_length=1)

    @field_validator("action")
    @classmethod
    def _action_ok(cls, v: str) -> str:
        if v not in ALLOWED_ACTIONS:
            raise ValueError("action")
        return v

    @field_validator("risk")
    @classmethod
    def _risk_ok(cls, v: str) -> str:
        if v not in ALLOWED_RISKS:
            raise ValueError("risk")
        return v


class OrganizeItemsModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: List[OrganizeItemModel] = Field(default_factory=list, max_length=MAX_DIFF_ITEMS)


def load_recent_entries(db: Session, user_id: int, limit: int = 10) -> List[Entry]:
    return (
        db.query(Entry)
        .filter(Entry.user_id == user_id)
        .order_by(Entry.created_at.desc())
        .limit(limit)
        .all()
    )


def load_user_entries_by_ids(db: Session, user_id: int, entry_ids: Iterable[int]) -> List[Entry]:
    ids = sorted({int(item) for item in entry_ids if item})
    if not ids:
        return []
    return (
        db.query(Entry)
        .filter(Entry.user_id == user_id, Entry.id.in_(ids))
        .order_by(Entry.created_at.desc())
        .all()
    )


def _normalize_days(days: Any) -> Optional[int]:
    if days is None:
        return None
    try:
        value = int(days)
    except Exception:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "ORGANIZE_DAYS_INVALID", "message": "days 必须是 7/30/90 或 null"},
        )
    if value not in ALLOWED_DAYS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "ORGANIZE_DAYS_INVALID", "message": "days 必须是 7/30/90 或 null"},
        )
    return value


def resolve_organize_scope(
    db: Session,
    user_id: int,
    *,
    days: Any = DEFAULT_DAYS,
    label_codes: Optional[Sequence[str]] = None,
    entry_ids: Optional[Sequence[int]] = None,
    max_entries: int = MAX_ORGANIZE_ENTRIES,
) -> Dict[str, Any]:
    labels = [str(c).strip() for c in (label_codes or []) if str(c).strip()]
    explicit_ids = sorted({int(i) for i in (entry_ids or []) if i})
    day_n = None if days is None else _normalize_days(days)
    date_from: Optional[datetime] = None
    if day_n is not None:
        date_from = now_local() - timedelta(days=int(day_n))

    matched: List[Entry] = []
    if explicit_ids:
        owned = load_user_entries_by_ids(db, user_id, explicit_ids)
        owned_ids = {int(e.id) for e in owned}
        forbidden = [i for i in explicit_ids if i not in owned_ids]
        if forbidden:
            raise HTTPException(
                status_code=http_status.HTTP_403_FORBIDDEN,
                detail={"code": "ORGANIZE_ENTRY_FORBIDDEN", "message": "包含无权访问的记录"},
            )
        matched = list(owned)
        child_rows = (
            db.query(Entry)
            .filter(Entry.user_id == user_id, Entry.parent_id.in_(list(owned_ids)))
            .order_by(Entry.created_at.desc())
            .all()
        )
        seen = {int(e.id) for e in matched}
        for ch in child_rows:
            if int(ch.id) not in seen:
                matched.append(ch)
                seen.add(int(ch.id))
    else:
        q = db.query(Entry).filter(Entry.user_id == user_id)
        if date_from is not None:
            q = q.filter(Entry.created_at >= date_from.replace(tzinfo=None))
        if labels:
            q = q.filter(Entry.label_code.in_(labels))
        tops = q.order_by(Entry.created_at.desc()).limit(max_entries * 3).all()
        matched = list(tops)
        top_ids = [int(e.id) for e in tops]
        if top_ids:
            children = (
                db.query(Entry)
                .filter(Entry.user_id == user_id, Entry.parent_id.in_(top_ids))
                .order_by(Entry.created_at.desc())
                .all()
            )
            seen = {int(e.id) for e in matched}
            for ch in children:
                if int(ch.id) not in seen:
                    matched.append(ch)
                    seen.add(int(ch.id))

    matched_sorted = sorted(
        matched,
        key=lambda e: (e.created_at or datetime.min, int(e.id)),
        reverse=True,
    )
    matched_count = len(matched_sorted)
    truncated = matched_count > max_entries
    selected = matched_sorted[:max_entries]
    return {
        "days": day_n,
        "label_codes": labels,
        "entry_ids_requested": explicit_ids,
        "date_from": date_from.isoformat() if date_from else None,
        "matched_count": matched_count,
        "selected_count": len(selected),
        "truncated": truncated,
        "max_entries": max_entries,
        "entries": selected,
        "source_entry_ids": [int(e.id) for e in selected],
        "scope_type": "entries" if explicit_ids else "days_labels",
    }


def preview_organize_scope(
    db: Session,
    user_id: int,
    *,
    days: Any = DEFAULT_DAYS,
    label_codes: Optional[Sequence[str]] = None,
    entry_ids: Optional[Sequence[int]] = None,
) -> Dict[str, Any]:
    scope = resolve_organize_scope(
        db, user_id, days=days, label_codes=label_codes, entry_ids=entry_ids
    )
    return {
        "matched_count": scope["matched_count"],
        "selected_count": scope["selected_count"],
        "date_from": scope["date_from"],
        "days": scope["days"],
        "label_codes": scope["label_codes"],
        "truncated": scope["truncated"],
        "max_entries": scope["max_entries"],
        "source_entry_ids": scope["source_entry_ids"],
    }


def _stable_preview_id(
    user_id: int,
    source_ids: Sequence[int],
    goal: str,
    source_fingerprint: str,
) -> str:
    raw = (
        f"{user_id}|{','.join(str(i) for i in sorted(source_ids))}|"
        f"{goal.strip()}|{source_fingerprint}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _stable_item_id(preview_id: str, index: int, action: str, title: str) -> str:
    raw = f"{preview_id}|{index}|{action}|{title}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _safe_attachment_label(chunk: AttachmentChunk) -> str:
    if chunk.page_no is not None:
        return f"PDF第{int(chunk.page_no)}页"
    if chunk.slide_no is not None:
        return f"PPTX第{int(chunk.slide_no)}页"
    if str(chunk.modality or "") == "ocr":
        return "OCR图片"
    return "附件"


def _safe_display_filename(name: Optional[str]) -> str:
    """Basename-only safe label for UI; never expose storage paths."""
    raw = (name or "").strip()
    if not raw:
        return "附件"
    base = PureWindowsPath(raw).name
    base = PurePosixPath(base).name
    cleaned = "".join(ch for ch in base if ch.isprintable() and ch not in {"/", "\\"})
    return (cleaned or "附件")[:120]


def _source_display_title(meta: Mapping[str, Any]) -> str:
    kind = str(meta.get("kind") or "entry")
    if kind == "entry":
        return str(meta.get("title") or "记录")[:120]
    filename = _safe_display_filename(str(meta.get("filename") or ""))
    if meta.get("page_no") is not None:
        return f"{filename} · 第{int(meta['page_no'])}页"
    if meta.get("slide_no") is not None:
        return f"{filename} · 第{int(meta['slide_no'])}张"
    if str(meta.get("modality") or "") == "ocr":
        return f"{filename} · 原图"
    return filename


def _alias_canonical_key(meta: Mapping[str, Any]) -> str:
    explicit = meta.get("reference_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    kind = str(meta.get("kind") or "entry")
    if kind == "attachment_chunk" and meta.get("chunk_id") is not None:
        return canonical_reference_key(
            {
                "source_type": "attachment_chunk",
                "chunk_id": int(meta["chunk_id"]),
                "attachment_id": meta.get("attachment_id"),
                "entry_id": meta.get("entry_id"),
            }
        )
    if meta.get("entry_id") is not None:
        return canonical_reference_key(
            {"source_type": "entry", "entry_id": int(meta["entry_id"])}
        )
    raise OrganizeError("SOURCE_INVALID", _organize_user_message("SOURCE_INVALID"))


def _build_sources_display_from_aliases(
    sources: Sequence[str],
    alias_to_source: Mapping[str, Dict[str, Any]],
    *,
    user_id: int,
    session_id: str,
) -> List[Dict[str, Any]]:
    """
    Build clickable sources_display from model aliases in order.
    Does NOT collapse attachment chunks into parent Entry tokens.
    Dedupes by canonical reference identity. Never exposes raw IDs/keys/paths.
    """
    out: List[Dict[str, Any]] = []
    seen_keys: Set[str] = set()
    for alias in sources:
        meta = alias_to_source.get(str(alias))
        if not isinstance(meta, dict):
            raise OrganizeError("SOURCE_INVALID", _organize_user_message("SOURCE_INVALID"))
        ref_key = _alias_canonical_key(meta)
        if ref_key in seen_keys:
            continue
        seen_keys.add(ref_key)
        kind = str(meta.get("kind") or "entry")
        entry_id = int(meta["entry_id"]) if meta.get("entry_id") is not None else None
        attachment_id = (
            int(meta["attachment_id"])
            if kind == "attachment_chunk" and meta.get("attachment_id") is not None
            else None
        )
        token = sign_reference_token(
            user_id=user_id,
            session_id=session_id,
            display_index=len(out) + 1,
            purpose=PURPOSE_ANSWER_REFERENCE,
            entry_id=entry_id,
            attachment_id=attachment_id,
            reference_key=ref_key,
        )
        public_kind = "entry" if kind == "entry" else "attachment"
        out.append(
            {
                "title": _source_display_title(meta),
                "created_at": str(meta.get("created_at") or ""),
                "kind": public_kind,
                "label_code": str(meta.get("label_code") or ""),
                "ref_token": token,
            }
        )
    if not out:
        raise OrganizeError("SOURCE_INVALID", _organize_user_message("SOURCE_INVALID"))
    return out


def _content_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _head_mid_tail(text: str, budget: int = MAX_EVIDENCE_CHARS_PER_ENTRY) -> str:
    raw = (text or "").strip()
    if len(raw) <= budget:
        return raw
    third = max(budget // 3, 80)
    head = raw[:third]
    mid_start = max((len(raw) - third) // 2, 0)
    mid = raw[mid_start : mid_start + third]
    tail = raw[-third:]
    return f"{head}\n…\n{mid}\n…\n{tail}"


def _chunk_search_eligible(chunk: AttachmentChunk) -> bool:
    meta = chunk.metadata_json if isinstance(chunk.metadata_json, dict) else {}
    if chunk.modality == "ocr":
        return is_ocr_chunk_search_eligible(meta)
    # Non-OCR text/image captions: treat as eligible when content non-empty.
    return bool((chunk.content or "").strip())


def build_organize_evidence_pack(
    db: Session,
    user_id: int,
    source_entry_ids: Sequence[int],
) -> Dict[str, Any]:
    """
    Full record-family evidence with request-local S# aliases.
    Model-facing text must never include entry/attachment/chunk IDs or paths.
    """
    alias_to_source: Dict[str, Dict[str, Any]] = {}
    source_units: List[Dict[str, Any]] = []
    fingerprint_parts: List[str] = []
    seen_roots: Set[int] = set()
    seen_entry_ids: Set[int] = set()
    seen_attachment_ids: Set[int] = set()
    seen_chunk_keys: Set[str] = set()
    entry_display: Dict[int, Dict[str, Any]] = {}
    alias_i = 0

    def _next_alias() -> str:
        nonlocal alias_i
        alias_i += 1
        return f"S{alias_i}"

    for seed_id in source_entry_ids:
        root_id = resolve_root_entry_id(db, user_id, int(seed_id)) or int(seed_id)
        if root_id in seen_roots:
            continue
        seen_roots.add(root_id)
        family = build_record_family(db, user_id, root_id, hit_entry_id=int(seed_id))
        if family is None:
            continue
        entry_ids = [fe.entry_id for fe in family.entries]
        entries = {
            int(e.id): e
            for e in db.query(Entry)
            .filter(Entry.user_id == user_id, Entry.id.in_(entry_ids))
            .all()
        }
        att_ids = [aid for fe in family.entries for aid in [a.attachment_id for a in fe.attachments]]
        chunks: List[AttachmentChunk] = []
        filename_by_att: Dict[int, str] = {}
        if att_ids:
            chunks = (
                db.query(AttachmentChunk)
                .filter(
                    AttachmentChunk.user_id == user_id,
                    AttachmentChunk.attachment_id.in_(att_ids),
                )
                .order_by(AttachmentChunk.attachment_id, AttachmentChunk.chunk_index)
                .all()
            )
            for row in (
                db.query(EntryAttachment)
                .filter(
                    EntryAttachment.user_id == user_id,
                    EntryAttachment.id.in_(att_ids),
                )
                .all()
            ):
                filename_by_att[int(row.id)] = _safe_display_filename(
                    str(getattr(row, "original_filename", None) or getattr(row, "file_name", None) or "")
                )
        chunks_by_att: Dict[int, List[AttachmentChunk]] = {}
        for ch in chunks:
            if not _chunk_search_eligible(ch):
                continue
            # knowledge mirror dedupe: modality+page/slide+content hash
            key = (
                f"{ch.attachment_id}:{ch.modality}:{ch.page_no}:{ch.slide_no}:"
                f"{_content_hash(ch.content or '')[:16]}"
            )
            if key in seen_chunk_keys:
                continue
            seen_chunk_keys.add(key)
            chunks_by_att.setdefault(int(ch.attachment_id), []).append(ch)

        for fe in family.entries:
            entry = entries.get(int(fe.entry_id))
            if entry is None or int(entry.id) in seen_entry_ids:
                continue
            seen_entry_ids.add(int(entry.id))
            alias = _next_alias()
            body = _head_mid_tail(entry.content or "")
            title = (entry.content or "").strip().split("\n", 1)[0][:80] or "记录"
            created_raw = entry.created_at
            if created_raw is None:
                created = ""
            elif hasattr(created_raw, "isoformat"):
                created = created_raw.isoformat()
            else:
                created = str(created_raw)
            label = str(entry.label_code or "")
            entry_ref_key = f"entry:source:{int(entry.id)}"
            alias_to_source[alias] = {
                "kind": "entry",
                "entry_id": int(entry.id),
                "root_entry_id": root_id,
                "title": title,
                "created_at": created,
                "label_code": label,
                "reference_key": entry_ref_key,
            }
            entry_display[int(entry.id)] = {
                "title": title,
                "created_at": created,
                "label_code": label,
                "kind": "entry",
            }
            # Model-facing: alias + safe record label only (no IDs/paths).
            source_units.append(
                {
                    "alias": alias,
                    "kind": "entry",
                    "entry_id": int(entry.id),
                    "text": f"[{alias}] 记录\n{body}",
                    "chars": len(body),
                }
            )
            fingerprint_parts.append(
                f"E|{fe.order}|{int(entry.id)}|{label}|{_content_hash(entry.content or '')}"
            )
            for att in fe.attachments:
                aid = int(att.attachment_id)
                if aid in seen_attachment_ids:
                    continue
                seen_attachment_ids.add(aid)
                att_sha = str(att.content_hash or "")
                att_status = str(att.status or "")
                sort_order = int(att.sort_order or 0)
                fingerprint_parts.append(
                    f"A|{int(fe.entry_id)}|{aid}|{sort_order}|{att_sha}|{att_status}"
                )
                for ch in chunks_by_att.get(aid, []):
                    alias = _next_alias()
                    label_safe = _safe_attachment_label(ch)
                    text = _head_mid_tail((ch.content or "").strip(), budget=1200)
                    if not text:
                        continue
                    chunk_ref_key = f"attachment_chunk:chunk:{int(ch.id)}"
                    alias_to_source[alias] = {
                        "kind": "attachment_chunk",
                        "entry_id": int(fe.entry_id),
                        "attachment_id": aid,
                        "chunk_id": int(ch.id),
                        "page_no": ch.page_no,
                        "slide_no": ch.slide_no,
                        "modality": str(ch.modality),
                        "root_entry_id": root_id,
                        "title": label_safe,
                        "filename": filename_by_att.get(aid) or "附件",
                        "created_at": created,
                        "label_code": label,
                        "reference_key": chunk_ref_key,
                    }
                    source_units.append(
                        {
                            "alias": alias,
                            "kind": "attachment_chunk",
                            "entry_id": int(fe.entry_id),
                            "text": f"[{alias}] {label_safe}\n{text}",
                            "chars": len(text),
                        }
                    )
                    fingerprint_parts.append(
                        f"C|{aid}|{int(ch.chunk_index)}|{ch.modality}|"
                        f"{ch.page_no}|{ch.slide_no}|{_content_hash(ch.content or '')}"
                    )

    fingerprint = hashlib.sha256(
        "\n".join(fingerprint_parts).encode("utf-8")
    ).hexdigest()
    return {
        "units": source_units,
        "alias_to_source": alias_to_source,
        "entry_ids": sorted(seen_entry_ids),
        "unit_count": len(source_units),
        "source_fingerprint": fingerprint,
        "entry_display": entry_display,
    }


def _fair_groups(units: Sequence[Dict[str, Any]], budget: int = GROUP_CHAR_BUDGET) -> List[List[Dict[str, Any]]]:
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    used = 0
    for u in units:
        cost = int(u.get("chars") or len(str(u.get("text") or ""))) + 16
        if current and used + cost > budget:
            groups.append(current)
            current = []
            used = 0
        current.append(u)
        used += cost
    if current:
        groups.append(current)
    return groups or [[]]


async def _extract_group(
    *,
    group: Sequence[Dict[str, Any]],
    goal: str,
    allowed_aliases: Sequence[str],
    user_id: int,
    model_key: Optional[str],
    fallback_candidates: Optional[List[str]],
    on_stage: Optional[Callable[[str], None]],
    system_prompt: str = EXTRACT_SYSTEM,
) -> Dict[str, Any]:
    if on_stage:
        on_stage("extract")
    blob = "\n\n".join(str(u["text"]) for u in group)
    prompt = (
        f"整理目标: {goal}\n"
        f"允许别名: {', '.join(allowed_aliases)}\n"
        f"证据:\n{blob}"
    )

    def _validate(data: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(data, dict):
            raise StructuredLLMError("SCHEMA_INVALID", "root")

        def _norm_rows(raw_rows: Any) -> List[Dict[str, Any]]:
            rows: List[Dict[str, Any]] = []
            for row in raw_rows or []:
                if isinstance(row, str):
                    text = row.strip()
                    if text:
                        rows.append({"text": text, "sources": [allowed_aliases[0]] if allowed_aliases else ["S1"]})
                    continue
                if not isinstance(row, dict):
                    continue
                text = str(row.get("text") or "").strip()
                src = [str(s) for s in (row.get("sources") or []) if str(s).strip()]
                if text:
                    rows.append({"text": text, "sources": src or ([allowed_aliases[0]] if allowed_aliases else ["S1"])})
            return rows

        normalized = {
            "topics": [str(t) for t in (data.get("topics") or []) if str(t).strip()],
            "facts": _norm_rows(data.get("facts")),
            "actions": _norm_rows(data.get("actions")),
            "contradictions": _norm_rows(data.get("contradictions")),
        }
        model = ExtractBundle.model_validate(normalized)
        allowed = set(allowed_aliases)

        def _filt(rows: List[ExtractFact]) -> List[Dict[str, Any]]:
            out = []
            for r in rows:
                src = [s for s in r.sources if s in allowed]
                if not src:
                    raise StructuredLLMError("SOURCE_INVALID", "alias")
                out.append({"text": r.text, "sources": src})
            return out

        return {
            "topics": list(model.topics)[:20],
            "facts": _filt(model.facts)[:40],
            "actions": _filt(model.actions)[:40],
            "contradictions": _filt(model.contradictions)[:20],
        }

    try:
        result = await generate_structured_json(
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            mode="organize",
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            max_tokens=1800,
            temperature=0.0,
            validate_fn=_validate,
            allow_repair=True,
            stage="organize_extract",
            candidate_count=len(group),
            syntax_error_code="JSON_SYNTAX",
            schema_error_code="SCHEMA_INVALID",
            provider_error_code="ORGANIZE_PROVIDER_FAILED",
        )
        return result.data
    except StructuredLLMError as exc:
        raise OrganizeError(exc.code, _organize_user_message(exc.code)) from exc
    except LLMGatewayError as exc:
        raise OrganizeError("ORGANIZE_PROVIDER_FAILED", _organize_user_message("ORGANIZE_PROVIDER_FAILED")) from exc


def _organize_user_message(code: str) -> str:
    return {
        "ORGANIZE_PROVIDER_FAILED": "整理服务暂时不可用，请稍后重试",
        "JSON_SYNTAX": "整理结果格式异常，请重试",
        "SCHEMA_INVALID": "整理结果不符合约定，请重试",
        "SOURCE_INVALID": "整理结果引用了无效来源，请重试",
        "EMPTY_RESULT": "当前范围没有可保存的整理建议",
        "SCOPE_STALE": "整理范围已变化，请重新生成预览",
        "ORGANIZE_SCOPE_EMPTY": "当前范围没有记录，请调整筛选后重试",
        "INTERNAL_ERROR": "整理过程出错，请稍后重试",
    }.get(code, "整理失败，请稍后重试")


async def _summarize_extracts(
    *,
    extracts: Sequence[Dict[str, Any]],
    goal: str,
    allowed_aliases: Sequence[str],
    allowed_actions: Set[str],
    user_id: int,
    model_key: Optional[str],
    fallback_candidates: Optional[List[str]],
    on_stage: Optional[Callable[[str], None]],
    system_prompt: str = SUMMARIZE_SYSTEM,
) -> List[Dict[str, Any]]:
    if on_stage:
        on_stage("summarize")
    prompt = (
        f"整理目标: {goal}\n"
        f"允许 action: {', '.join(sorted(allowed_actions))}\n"
        f"允许别名: {', '.join(allowed_aliases)}\n"
        f"分组提取结果: {extracts}"
    )

    def _validate(data: Dict[str, Any]) -> Dict[str, Any]:
        model = OrganizeItemsModel.model_validate(data)
        allowed = set(allowed_aliases)
        items = []
        for it in model.items:
            if it.action not in allowed_actions:
                raise StructuredLLMError("SCHEMA_INVALID", "action")
            src = [s for s in it.sources if s in allowed]
            if not src:
                raise StructuredLLMError("SOURCE_INVALID", "alias")
            items.append(
                {
                    "action": it.action,
                    "title": it.title.strip()[:255],
                    "content": it.content.strip()[:8000],
                    "reason": (it.reason or "")[:400],
                    "risk": it.risk,
                    "confidence": float(it.confidence),
                    "sources": src,
                }
            )
        return {"items": items[:MAX_DIFF_ITEMS]}

    try:
        result = await generate_structured_json(
            system=system_prompt,
            messages=[{"role": "user", "content": prompt}],
            mode="organize",
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            max_tokens=2500,
            temperature=0.1,
            validate_fn=_validate,
            allow_repair=True,
            stage="organize_summarize",
            candidate_count=len(allowed_aliases),
            syntax_error_code="JSON_SYNTAX",
            schema_error_code="SCHEMA_INVALID",
            provider_error_code="ORGANIZE_PROVIDER_FAILED",
        )
        return list(result.data.get("items") or [])
    except StructuredLLMError as exc:
        raise OrganizeError(exc.code, _organize_user_message(exc.code)) from exc


def _map_sources_to_entry_ids(
    sources: Sequence[str], alias_to_source: Mapping[str, Dict[str, Any]]
) -> List[int]:
    ids: List[int] = []
    seen: Set[int] = set()
    for alias in sources:
        meta = alias_to_source.get(alias) or {}
        eid = meta.get("entry_id")
        if eid is None:
            continue
        eid_i = int(eid)
        if eid_i in seen:
            continue
        seen.add(eid_i)
        ids.append(eid_i)
    return ids


async def generate_organize_preview(
    db: Session,
    *,
    goal: str,
    user_id: int,
    days: Any = DEFAULT_DAYS,
    label_codes: Optional[Sequence[str]] = None,
    entry_ids: Optional[Sequence[int]] = None,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
    allowed_actions: Optional[List[str]] = None,
    on_stage: Optional[Callable[[str], None]] = None,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    if on_stage:
        on_stage("scope")
    scope = resolve_organize_scope(
        db,
        user_id,
        days=days,
        label_codes=label_codes,
        entry_ids=entry_ids,
    )
    source_ids = list(scope["source_entry_ids"])
    if not source_ids:
        return {
            "status": "preview",
            "message": "整合范围内没有记录，请调整时间或标签后重试。",
            "diff_preview": [],
            "source_entry_ids": [],
            "used_default_entries": False,
            "preview_id": None,
            "scope": {
                "matched_count": 0,
                "selected_count": 0,
                "date_from": scope["date_from"],
                "days": scope["days"],
                "label_codes": scope["label_codes"],
                "truncated": False,
            },
            "mode": "organize",
            "stage": "done",
            "error_code": "ORGANIZE_SCOPE_EMPTY",
        }

    allowed = set(ALLOWED_ACTIONS)
    if allowed_actions:
        allowed &= {str(a) for a in allowed_actions}
    if not allowed:
        allowed = set(ALLOWED_ACTIONS)

    pack = build_organize_evidence_pack(db, user_id, source_ids)
    units = list(pack["units"])
    alias_map = dict(pack["alias_to_source"])
    aliases = [u["alias"] for u in units]
    final_source_ids = sorted({int(i) for i in (pack.get("entry_ids") or source_ids)})
    source_fingerprint = str(pack.get("source_fingerprint") or "")
    goal_text = goal or "整理记录"
    personalization = load_personalization_context(
        db,
        int(user_id),
        query=goal_text,
    )
    extract_system = compose_system_with_personalization(
        EXTRACT_SYSTEM,
        personalization,
    )
    summarize_system = compose_system_with_personalization(
        SUMMARIZE_SYSTEM,
        personalization,
    )
    preview_id = _stable_preview_id(
        user_id, final_source_ids, goal_text, source_fingerprint
    )
    if not units:
        raise OrganizeError("EMPTY_RESULT", _organize_user_message("EMPTY_RESULT"), http_status=400)

    groups = _fair_groups(units)
    extracts: List[Dict[str, Any]] = []
    for group in groups:
        group_aliases = [u["alias"] for u in group]
        extracts.append(
            await _extract_group(
                group=group,
                goal=goal_text,
                allowed_aliases=group_aliases,
                user_id=user_id,
                model_key=model_key,
                fallback_candidates=fallback_candidates,
                on_stage=on_stage,
                system_prompt=extract_system,
            )
        )

    if on_stage:
        on_stage("validate")
    items_raw = await _summarize_extracts(
        extracts=extracts,
        goal=goal_text,
        allowed_aliases=aliases,
        allowed_actions=allowed,
        user_id=user_id,
        model_key=model_key,
        fallback_candidates=fallback_candidates,
        on_stage=on_stage,
        system_prompt=summarize_system,
    )
    if not items_raw:
        raise OrganizeError("EMPTY_RESULT", _organize_user_message("EMPTY_RESULT"), http_status=400)

    items: List[Dict[str, Any]] = []
    token_items: List[Dict[str, Any]] = []
    sid = str(session_id or f"organize:{user_id}:{preview_id}")
    for idx, item in enumerate(items_raw):
        # Ownership / confirm scope still uses parent Entry IDs.
        entry_ids_mapped = _map_sources_to_entry_ids(item["sources"], alias_map)
        if not entry_ids_mapped:
            raise OrganizeError("SOURCE_INVALID", _organize_user_message("SOURCE_INVALID"))
        item_id = _stable_item_id(preview_id, idx, item["action"], item["title"])
        # Display/preview tokens must keep precise alias identity (Entry/OCR/PDF/PPTX).
        sources_display = _build_sources_display_from_aliases(
            item["sources"],
            alias_map,
            user_id=user_id,
            session_id=sid,
        )
        row = {
            "action": item["action"],
            "title": item["title"],
            "content": item["content"],
            "reason": item.get("reason") or "",
            "risk": item.get("risk") or "low",
            "confidence": float(item.get("confidence") or 0.5),
            "source_entry_ids": entry_ids_mapped,
            "sources_display": sources_display,
            "item_id": item_id,
            "preview_id": preview_id,
        }
        items.append(row)
        token_items.append(
            {
                "item_id": item_id,
                "action": item["action"],
                "source_entry_ids": entry_ids_mapped,
            }
        )

    preview_token = sign_organize_preview_token(
        user_id=user_id,
        preview_id=preview_id,
        goal=goal_text,
        source_fingerprint=source_fingerprint,
        source_entry_ids=final_source_ids,
        items=token_items,
    )

    return {
        "status": "preview",
        "message": f"已根据范围生成 {len(items)} 条整合建议，请勾选后保存。",
        "goal": goal_text,
        "diff_preview": items,
        "source_entry_ids": final_source_ids,
        "used_default_entries": False,
        "preview_id": preview_id,
        "preview_token": preview_token,
        "preview_session_id": sid,
        "scope": {
            "matched_count": scope["matched_count"],
            "selected_count": scope["selected_count"],
            "date_from": scope["date_from"],
            "days": scope["days"],
            "label_codes": scope["label_codes"],
            "truncated": scope["truncated"],
            "evidence_units": len(units),
            "extract_groups": len(groups),
        },
        "mode": "organize",
        "stage": "done",
        "hermes_protocol": None,
        "alias_count": len(aliases),
    }


def confirm_organize_items(
    db: Session,
    user_id: int,
    *,
    source_entry_ids: Sequence[int],
    items: Sequence[Dict[str, Any]],
    goal: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    preview_id: Optional[str] = None,
    preview_token: Optional[str] = None,
) -> List[Dict[str, Any]]:
    token_payload = verify_organize_preview_token(
        str(preview_token or ""), user_id=user_id
    )
    if token_payload is None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "code": "SCOPE_STALE",
                "message": _organize_user_message("SCOPE_STALE"),
            },
        )

    bound_source_ids = [int(i) for i in (token_payload.get("source_entry_ids") or [])]
    bound_goal = str(token_payload.get("goal") or "整理记录")
    bound_preview_id = str(token_payload.get("preview_id") or "")
    bound_fp = str(token_payload.get("source_fingerprint") or "")
    bound_items = {
        str(it.get("item_id")): it
        for it in (token_payload.get("items") or [])
        if isinstance(it, dict) and it.get("item_id")
    }
    if preview_id and str(preview_id) != bound_preview_id:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "code": "SCOPE_STALE",
                "message": _organize_user_message("SCOPE_STALE"),
            },
        )
    if goal is not None and str(goal).strip() and str(goal).strip() != bound_goal:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "code": "SCOPE_STALE",
                "message": _organize_user_message("SCOPE_STALE"),
            },
        )

    # Recompute fingerprint; any content/attachment/chunk change → SCOPE_STALE.
    live_pack = build_organize_evidence_pack(db, user_id, bound_source_ids)
    live_fp = str(live_pack.get("source_fingerprint") or "")
    if not bound_fp or live_fp != bound_fp:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail={
                "code": "SCOPE_STALE",
                "message": _organize_user_message("SCOPE_STALE"),
            },
        )

    if not items:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "EMPTY_RESULT", "message": _organize_user_message("EMPTY_RESULT")},
        )
    if len(items) > MAX_DIFF_ITEMS:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "SCHEMA_INVALID", "message": "一次最多确认 12 条建议"},
        )

    seen_item_ids: Set[str] = set()
    created: List[Dict[str, Any]] = []
    try:
        for raw in items:
            item_id = str(raw.get("item_id") or "").strip()
            if not item_id:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail={
                        "code": "ORGANIZE_ITEM_ID_REQUIRED",
                        "message": "缺少预览项标识，请重新生成预览",
                    },
                )
            if item_id in seen_item_ids:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail={"code": "SCHEMA_INVALID", "message": "存在重复的建议项"},
                )
            seen_item_ids.add(item_id)
            bound = bound_items.get(item_id)
            if bound is None:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail={"code": "SCHEMA_INVALID", "message": "包含未知建议项"},
                )
            action = str(bound.get("action") or "").strip()
            if action not in ORGANIZE_CONFIRM_ACTION_MAP:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail={"code": "SCHEMA_INVALID", "message": "不允许的建议类型"},
                )
            # Client may not alter action; ignore client action if present and mismatched.
            client_action = str(raw.get("action") or "").strip()
            if client_action and client_action != action:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail={"code": "SCHEMA_INVALID", "message": "建议类型不可篡改"},
                )
            src = [int(x) for x in (bound.get("source_entry_ids") or [])]
            if not src:
                raise HTTPException(
                    status_code=http_status.HTTP_400_BAD_REQUEST,
                    detail={"code": "SOURCE_INVALID", "message": "建议来源无效或无权访问"},
                )
            title = str(raw.get("title") or action).strip()[:255]
            content = str(raw.get("content") or "").strip()
            if not content:
                continue
            confirm_key = hashlib.sha256(
                f"{bound_preview_id}:{item_id}".encode("utf-8")
            ).hexdigest()[:64]
            derived_type = ORGANIZE_CONFIRM_ACTION_MAP[action]
            meta = build_derived_metadata(
                metadata,
                mode="organize",
                scope_type="days_labels",
                confidence=raw.get("confidence"),
                risk=raw.get("risk"),
                reason=raw.get("reason"),
                preview_id=bound_preview_id,
                item_id=item_id,
                action=action,
                goal=bound_goal,
            )
            row = create_derived_content(
                db,
                user_id,
                type=derived_type,
                title=title or derived_type,
                content=content,
                scope_type="days_labels",
                source_entry_ids=src,
                metadata=meta,
                status="confirmed",
                confirm_key=confirm_key,
                commit=False,
            )
            created.append(
                {
                    "id": int(row.id),
                    "type": row.type,
                    "title": row.title,
                    "source_entry_ids": list(row.source_entry_ids or []),
                }
            )
        db.commit()
    except HTTPException:
        db.rollback()
        raise
    except Exception as exc:
        db.rollback()
        logger.warning("organize confirm failed: %s", type(exc).__name__)
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "INTERNAL_ERROR", "message": "保存失败，请稍后重试"},
        ) from exc
    return created

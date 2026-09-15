"""
AI 派生内容读写（只写 ai_derived_contents，不改 Entry）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from fastapi import HTTPException
from fastapi import status as http_status
from sqlalchemy import desc
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AIDerivedContent, Entry

DERIVED_CONTENT_TYPES = frozenset(
    {
        "review",
        "annotation",
        "tag_suggestion",
        "organize_summary",
        "action_plan",
        "memory_note",
    }
)

REVIEW_CONFIRM_TYPES = frozenset({"review", "action_plan", "tag_suggestion"})
DERIVED_CONTENT_STATUSES = frozenset({"draft", "confirmed", "dismissed"})


def validate_source_entry_ids(
    db: Session,
    user_id: int,
    source_entry_ids: Sequence[int],
    *,
    require_non_empty: bool = True,
) -> List[int]:
    ids = sorted({int(i) for i in source_entry_ids if i})
    if require_non_empty and not ids:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "DERIVED_SCOPE_EMPTY", "message": "来源记录为空，无法保存"},
        )
    if not ids:
        return []
    valid = load_user_entry_ids(db, user_id, ids)
    if valid != ids:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN,
            detail={
                "code": "DERIVED_ENTRY_FORBIDDEN",
                "message": "来源记录无效或不属于当前用户",
            },
        )
    return ids


def load_user_entry_ids(db: Session, user_id: int, entry_ids: Sequence[int]) -> List[int]:
    ids = [int(i) for i in entry_ids if int(i) > 0]
    if not ids:
        return []
    rows = db.query(Entry.id).filter(Entry.user_id == user_id, Entry.id.in_(ids)).all()
    return sorted(int(r[0]) for r in rows)


def build_derived_metadata(
    metadata: Optional[Dict[str, Any]] = None,
    *,
    mode: Optional[str] = None,
    scope_type: Optional[str] = None,
    model_key: Optional[str] = None,
    confidence: Optional[float] = None,
    **extra: Any,
) -> Dict[str, Any]:
    result = dict(metadata or {})
    if mode:
        result["mode"] = mode
    if scope_type:
        result["scope_type"] = scope_type
    if model_key:
        result["model_key"] = model_key
    if confidence is not None:
        result["confidence"] = confidence
    for k, v in extra.items():
        if v is not None:
            result[k] = v
    return result


def compose_action_plan_content(suggestions: List[str]) -> str:
    return "\n".join(f"- {item}" for item in suggestions if item)


def compose_tag_suggestion_content(keywords: List[str]) -> str:
    return "\n".join(f"- {item}" for item in keywords if item)


def create_derived_content(
    db: Session,
    user_id: int,
    *,
    type: str,
    title: str,
    content: str,
    scope_type: Optional[str],
    source_entry_ids: List[int],
    metadata: Optional[Dict[str, Any]] = None,
    status: str = "confirmed",
    confirm_key: Optional[str] = None,
    commit: bool = True,
) -> AIDerivedContent:
    if status not in DERIVED_CONTENT_STATUSES:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "DERIVED_STATUS_INVALID", "message": f"不支持的派生状态: {status}"},
        )
    if type not in DERIVED_CONTENT_TYPES:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "DERIVED_TYPE_INVALID", "message": f"不支持的派生类型: {type}"},
        )
    validated_ids = validate_source_entry_ids(db, user_id, source_entry_ids)
    key = (confirm_key or "").strip() or None
    if key:
        existing = (
            db.query(AIDerivedContent)
            .filter(AIDerivedContent.user_id == user_id, AIDerivedContent.confirm_key == key)
            .first()
        )
        if existing:
            return existing
    row = AIDerivedContent(
        user_id=user_id,
        type=type,
        title=title.strip()[:255],
        content=content.strip(),
        scope_type=scope_type,
        source_entry_ids=validated_ids,
        meta=metadata or {},
        status=status,
        confirm_key=key,
    )
    db.add(row)
    try:
        if commit:
            db.commit()
            db.refresh(row)
        else:
            db.flush()
    except IntegrityError:
        db.rollback()
        if key:
            existing = (
                db.query(AIDerivedContent)
                .filter(AIDerivedContent.user_id == user_id, AIDerivedContent.confirm_key == key)
                .first()
            )
            if existing:
                return existing
        raise
    return row


def _is_auto_derived(row: AIDerivedContent) -> bool:
    meta = row.meta if isinstance(row.meta, dict) else {}
    if meta.get("auto") is True:
        return True
    source = str(meta.get("source") or meta.get("origin") or "").lower()
    return source in {"auto", "auto_structure", "hermes_auto_structure"}


def list_derived_contents(
    db: Session,
    user_id: int,
    *,
    type: Optional[str] = None,
    types: Optional[List[str]] = None,
    status: Optional[str] = None,
    auto: Optional[bool] = None,
    limit: int = 50,
) -> List[AIDerivedContent]:
    query = db.query(AIDerivedContent).filter(AIDerivedContent.user_id == user_id)
    if status:
        normalized = status.strip().lower()
        if normalized not in DERIVED_CONTENT_STATUSES:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail={"code": "DERIVED_STATUS_INVALID", "message": "派生状态筛选无效"},
            )
        query = query.filter(AIDerivedContent.status == normalized)
    else:
        query = query.filter(AIDerivedContent.status == "confirmed")
    filter_types: List[str] = []
    if types:
        filter_types = [t.strip() for t in types if t.strip()]
    elif type:
        filter_types = [t.strip() for t in type.split(",") if t.strip()] if "," in type else [type.strip()]
    if filter_types:
        invalid = [t for t in filter_types if t not in DERIVED_CONTENT_TYPES]
        if invalid:
            raise HTTPException(
                status_code=http_status.HTTP_400_BAD_REQUEST,
                detail={"code": "DERIVED_TYPE_INVALID", "message": "派生类型筛选无效"},
            )
        query = query.filter(AIDerivedContent.type.in_(filter_types))
    # Fetch a bit wider when auto filter needs in-Python metadata inspection.
    fetch_limit = limit if auto is None else max(limit * 3, 50)
    rows = query.order_by(desc(AIDerivedContent.created_at)).limit(fetch_limit).all()
    if auto is None:
        return rows[:limit]
    return [row for row in rows if _is_auto_derived(row) is bool(auto)][:limit]


def get_derived_content(
    db: Session,
    user_id: int,
    content_id: int,
) -> Optional[AIDerivedContent]:
    return (
        db.query(AIDerivedContent)
        .filter(AIDerivedContent.id == content_id, AIDerivedContent.user_id == user_id)
        .first()
    )


def confirm_derived_content_draft(db: Session, user_id: int, content_id: int) -> AIDerivedContent:
    row = get_derived_content(db, user_id, content_id)
    if not row:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "DERIVED_NOT_FOUND", "message": "派生内容不存在或无权访问"},
        )
    if row.status != "draft":
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "DERIVED_NOT_DRAFT", "message": "仅 draft 状态可确认"},
        )
    meta = dict(row.meta or {})
    meta["requires_confirmation"] = False
    row.status = "confirmed"
    row.meta = meta
    db.commit()
    db.refresh(row)
    return row


def dismiss_derived_content_draft(db: Session, user_id: int, content_id: int) -> AIDerivedContent:
    row = get_derived_content(db, user_id, content_id)
    if not row:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail={"code": "DERIVED_NOT_FOUND", "message": "派生内容不存在或无权访问"},
        )
    if row.status != "draft":
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail={"code": "DERIVED_NOT_DRAFT", "message": "仅 draft 状态可忽略"},
        )
    meta = dict(row.meta or {})
    meta["requires_confirmation"] = False
    row.status = "dismissed"
    row.meta = meta
    db.commit()
    db.refresh(row)
    return row

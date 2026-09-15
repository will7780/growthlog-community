"""
Persona-bound AI chat session lifecycle service.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import desc, func, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    AIChatSession,
    AIConversation,
    AIConversationEmbedding,
    AIConversationSourceSet,
    AIConversationSummary,
    AIConversationSummaryEmbedding,
)
from app.services.ai_session_constants import (
    SESSION_STATUS_ACTIVE,
    SessionPersonaError,
    normalize_persona,
    assert_mode_matches_persona,
)

logger = logging.getLogger(__name__)

MAX_MESSAGES_PER_SESSION = 100


class SessionNotFoundError(Exception):
    code = "SESSION_NOT_FOUND"
    message = "会话不存在或无权访问。"


class SessionConflictError(Exception):
    code = "SESSION_CONFLICT"
    message = "会话标识冲突，请重试。"


def generate_session_id() -> str:
    return uuid.uuid4().hex[:16]


def get_session_metadata(
    db: Session,
    user_id: int,
    session_id: str,
) -> Optional[AIChatSession]:
    return (
        db.query(AIChatSession)
        .filter(
            AIChatSession.user_id == user_id,
            AIChatSession.session_id == session_id,
        )
        .first()
    )


def is_legacy_session(db: Session, user_id: int, session_id: str) -> bool:
    has_messages = (
        db.query(AIConversation.id)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == session_id,
        )
        .first()
        is not None
    )
    if not has_messages:
        return False
    return get_session_metadata(db, user_id, session_id) is None


def get_or_create_persona_session(
    db: Session,
    user_id: int,
    persona: str,
    *,
    session_id: Optional[str] = None,
    title: Optional[str] = None,
) -> AIChatSession:
    """
    Persona-aware session resolve/create for write paths (R11.2-Fix).

    - Missing session_id → atomically create AIChatSession with the given persona.
    - Existing persona session → validate mode match.
    - Legacy session_id → SESSION_LEGACY_READONLY (no auto-upgrade).
    """
    from app.services.ai_session_constants import normalize_persona

    expected = normalize_persona(persona)
    if session_id:
        if is_legacy_session(db, user_id, session_id):
            raise SessionPersonaError(
                "SESSION_LEGACY_READONLY",
                "历史会话不可继续写入，请创建全新对话。",
            )
        meta = get_session_metadata(db, user_id, session_id)
        if meta is None:
            raise SessionPersonaError("SESSION_NOT_FOUND", "会话不存在或无权访问。")
        if meta.persona != expected:
            raise SessionPersonaError(
                "SESSION_PERSONA_MISMATCH",
                "当前请求与会话角色不匹配，请新建对应角色的对话。",
            )
        return meta
    return create_persona_session(db, user_id, expected, title=title)


def create_persona_session(
    db: Session,
    user_id: int,
    persona: str,
    *,
    session_id: Optional[str] = None,
    title: Optional[str] = None,
) -> AIChatSession:
    normalized = normalize_persona(persona)
    sid = session_id or generate_session_id()

    # Never upgrade a legacy message-only session into a persona session.
    if is_legacy_session(db, user_id, sid):
        raise SessionPersonaError(
            "SESSION_LEGACY_READONLY",
            "历史会话不可升级，请创建全新对话。",
        )
    if get_session_metadata(db, user_id, sid) is not None:
        raise SessionConflictError()

    row = AIChatSession(
        user_id=user_id,
        session_id=sid,
        persona=normalized,
        title=(title or "").strip() or None,
        status=SESSION_STATUS_ACTIVE,
        current_source_set_version=None,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise SessionConflictError() from exc
    db.refresh(row)
    return row


def touch_session_activity(
    db: Session,
    user_id: int,
    session_id: str,
    *,
    at: Optional[datetime] = None,
    commit: bool = True,
) -> None:
    """Update last_message_at. commit=False keeps the change in the caller's transaction."""
    meta = get_session_metadata(db, user_id, session_id)
    if meta is None:
        return
    meta.last_message_at = at or datetime.utcnow()
    db.add(meta)
    if commit:
        db.commit()
    else:
        db.flush()


def list_user_sessions(
    db: Session,
    user_id: int,
    *,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Merged persona sessions and legacy message-only sessions."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))

    meta_rows = (
        db.query(AIChatSession)
        .filter(AIChatSession.user_id == user_id)
        .order_by(desc(AIChatSession.last_message_at), desc(AIChatSession.created_at))
        .all()
    )
    meta_by_sid = {row.session_id: row for row in meta_rows}

    subquery = (
        db.query(
            AIConversation.session_id,
            func.max(AIConversation.created_at).label("max_created"),
        )
        .filter(AIConversation.user_id == user_id)
        .group_by(AIConversation.session_id)
        .subquery()
    )
    latest_msgs = (
        db.query(AIConversation)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == subquery.c.session_id,
            AIConversation.created_at == subquery.c.max_created,
        )
        .all()
    )
    latest_by_sid = {msg.session_id: msg for msg in latest_msgs}

    items: List[Dict[str, Any]] = []
    seen: set[str] = set()

    for meta in meta_rows:
        seen.add(meta.session_id)
        last_msg = latest_by_sid.get(meta.session_id)
        items.append(_session_list_item(db, meta, last_msg, legacy=False))

    for sid, last_msg in latest_by_sid.items():
        if sid in seen:
            continue
        items.append(_session_list_item(db, None, last_msg, legacy=True))

    items.sort(
        key=lambda x: (
            x.get("updated_at") or x.get("created_at") or "",
        ),
        reverse=True,
    )
    return items[offset : offset + limit]


def _session_list_item(
    db: Session,
    meta: Optional[AIChatSession],
    last_msg: Optional[AIConversation],
    *,
    legacy: bool,
) -> Dict[str, Any]:
    preview = ""
    if last_msg and last_msg.content:
        preview = last_msg.content[:50] + ("..." if len(last_msg.content) > 50 else "")

    source_set_status = None
    if meta and meta.persona == "explainer":
        source_set_status = _current_source_set_status(db, meta)

    created_at = None
    updated_at = None
    if meta:
        created_at = meta.created_at.isoformat() if meta.created_at else None
        updated_at = meta.updated_at.isoformat() if meta.updated_at else None
        if meta.last_message_at:
            updated_at = meta.last_message_at.isoformat()
    elif last_msg and last_msg.created_at:
        created_at = last_msg.created_at.isoformat()
        updated_at = created_at

    return {
        "session_id": meta.session_id if meta else (last_msg.session_id if last_msg else ""),
        "persona": None if legacy else meta.persona,
        "title": meta.title if meta else None,
        "status": meta.status if meta else "active",
        "legacy": legacy,
        "continuable": False if legacy else True,
        "source_set_status": source_set_status,
        "current_source_set_version": meta.current_source_set_version if meta else None,
        "last_message": preview,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _current_source_set_status(db: Session, meta: AIChatSession) -> Optional[str]:
    if meta.current_source_set_version is None:
        return None
    row = (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == meta.user_id,
            AIConversationSourceSet.session_id == meta.session_id,
            AIConversationSourceSet.version == meta.current_source_set_version,
        )
        .order_by(AIConversationSourceSet.id.desc())
        .first()
    )
    return row.status if row else None


def validate_session_for_mode(
    db: Session,
    user_id: int,
    session_id: str,
    mode: str,
) -> None:
    meta = get_session_metadata(db, user_id, session_id)
    if meta is None:
        if is_legacy_session(db, user_id, session_id):
            raise SessionPersonaError(
                "SESSION_PERSONA_MISMATCH",
                "历史会话不可继续，请新建对应角色的对话。",
            )
        return
    assert_mode_matches_persona(mode, meta.persona)


def _has_table(db: Session, name: str) -> bool:
    bind = db.get_bind()
    if bind is None:
        return False
    try:
        return bool(inspect(bind).has_table(name))
    except Exception:
        return False


def delete_session_complete(db: Session, user_id: int, session_id: str) -> bool:
    """Delete session metadata, messages, summaries, embeddings, source sets and claims atomically."""
    from app.services.ai_stream_claim import claims_exist_for_session, delete_claims_for_session

    has_any = (
        db.query(AIConversation.id)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == session_id,
        )
        .first()
        is not None
    )
    meta = get_session_metadata(db, user_id, session_id)
    has_claims = claims_exist_for_session(db, user_id, session_id)
    if not has_any and meta is None and not has_claims:
        return False

    try:
        conv_ids = [
            row.id
            for row in db.query(AIConversation.id)
            .filter(
                AIConversation.user_id == user_id,
                AIConversation.session_id == session_id,
            )
            .all()
        ]
        if conv_ids and _has_table(db, "ai_conversation_embeddings"):
            db.query(AIConversationEmbedding).filter(
                AIConversationEmbedding.conversation_id.in_(conv_ids)
            ).delete(synchronize_session=False)

        summary_ids: List[int] = []
        if _has_table(db, "ai_conversation_summaries"):
            summary_ids = [
                row.id
                for row in db.query(AIConversationSummary.id)
                .filter(
                    AIConversationSummary.user_id == user_id,
                    AIConversationSummary.session_id == session_id,
                )
                .all()
            ]
        if summary_ids and _has_table(db, "ai_conversation_summary_embeddings"):
            db.query(AIConversationSummaryEmbedding).filter(
                AIConversationSummaryEmbedding.summary_id.in_(summary_ids)
            ).delete(synchronize_session=False)

        if _has_table(db, "ai_conversation_summaries"):
            db.query(AIConversationSummary).filter(
                AIConversationSummary.user_id == user_id,
                AIConversationSummary.session_id == session_id,
            ).delete(synchronize_session=False)

        db.query(AIConversation).filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == session_id,
        ).delete(synchronize_session=False)

        if _has_table(db, "ai_conversation_source_sets"):
            db.query(AIConversationSourceSet).filter(
                AIConversationSourceSet.user_id == user_id,
                AIConversationSourceSet.session_id == session_id,
            ).delete(synchronize_session=False)

        if _has_table(db, "ai_chat_sessions"):
            db.query(AIChatSession).filter(
                AIChatSession.user_id == user_id,
                AIChatSession.session_id == session_id,
            ).delete(synchronize_session=False)

        # Claims have user FK only (no session FK); purge by exact user+session in same txn.
        delete_claims_for_session(db, user_id, session_id)

        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("delete_session_complete failed user_id=%s session_id=%s", user_id, session_id)
        raise


def count_session_messages(db: Session, user_id: int, session_id: str) -> int:
    return (
        db.query(AIConversation)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == session_id,
        )
        .count()
    )


def enforce_message_limit(db: Session, user_id: int, session_id: str) -> None:
    """R11.1: do not silently trim messages when over limit."""
    count = count_session_messages(db, user_id, session_id)
    if count >= MAX_MESSAGES_PER_SESSION:
        raise SessionPersonaError(
            "SESSION_MESSAGE_LIMIT",
            f"会话消息已达上限（{MAX_MESSAGES_PER_SESSION}），请新建对话。",
        )

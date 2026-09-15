"""
AI 聊天会话管理服务（消息层；persona 元数据见 ai_session.py）
"""
from typing import List, Dict, Optional

from sqlalchemy.orm import Session
from sqlalchemy import func, desc
from sqlalchemy.exc import IntegrityError

from app.models import AIConversation, AIConversationSourceSet
from app.services.ai_session import (
    delete_session_complete,
    enforce_message_limit,
    generate_session_id as _generate_session_id,
    get_session_metadata,
    touch_session_activity,
)
from app.services.ai_source_set import (
    SourceSetError,
    validate_source_set_version_for_message,
)
from app.services.ai_session_constants import PERSONA_EXPLAINER, SessionPersonaError
from app.services.ai_stream_idempotency import StreamIdempotencyError, validate_request_id

# 配置（R11.1：不再静默删除会话或截断消息）
MAX_SESSIONS_PER_USER = 10  # 保留常量供兼容；新 persona 会话不自动 prune
MAX_MESSAGES_PER_SESSION = 100


class SessionLimitReachedError(Exception):
    """Kept for route compatibility; R11.1 不再自动删除最旧会话。"""


def generate_session_id() -> str:
    return _generate_session_id()


def _validate_proposal_id_for_message(
    db: Session,
    user_id: int,
    session_id: str,
    proposal_id: Optional[int],
) -> Optional[int]:
    if proposal_id is None:
        return None
    meta = get_session_metadata(db, user_id, session_id)
    if meta is None or meta.persona != PERSONA_EXPLAINER:
        raise SourceSetError(
            "SOURCE_REFERENCE_INVALID",
            "非讲解员消息不得绑定来源 proposal。",
        )
    row = (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.id == int(proposal_id),
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
        )
        .first()
    )
    if row is None:
        raise SourceSetError("SOURCE_SET_NOT_FOUND", "来源 proposal 不存在或无权访问。")
    return int(proposal_id)


def _validate_request_id_for_message(request_id: Optional[str]) -> Optional[str]:
    """R11.3: request_id is optional and persona-agnostic (retriever + explainer)."""
    if request_id is None:
        return None
    try:
        return validate_request_id(request_id)
    except StreamIdempotencyError:
        raise


def save_message(
    db: Session,
    user_id: int,
    session_id: str,
    role: str,
    content: str,
    references: Optional[List[Dict]] = None,
    *,
    source_set_version: Optional[int] = None,
    proposal_id: Optional[int] = None,
    request_id: Optional[str] = None,
    commit: bool = True,
) -> AIConversation:
    """保存消息；超过上限时拒绝写入，不截断历史。commit=False 时仅 flush，供原子双写。"""
    enforce_message_limit(db, user_id, session_id)
    try:
        validated_version = validate_source_set_version_for_message(
            db,
            user_id,
            session_id,
            source_set_version,
        )
        validated_proposal = _validate_proposal_id_for_message(
            db, user_id, session_id, proposal_id
        )
        validated_request_id = _validate_request_id_for_message(request_id)
    except (SourceSetError, SessionPersonaError, StreamIdempotencyError):
        raise

    message = AIConversation(
        user_id=user_id,
        session_id=session_id,
        role=role,
        content=content,
        references=references,
        source_set_version=validated_version,
        proposal_id=validated_proposal,
        request_id=validated_request_id,
    )
    db.add(message)
    try:
        if commit:
            db.flush()
            db.refresh(message)
            touch_session_activity(
                db,
                user_id,
                session_id,
                at=message.created_at,
                commit=False,
            )
            db.commit()
            db.refresh(message)
        else:
            db.flush()
            db.refresh(message)
    except IntegrityError:
        db.rollback()
        raise
    return message


def trim_session_if_needed(db: Session, user_id: int, session_id: str):
    """R11.1 兼容占位：不再删除最旧一半消息。"""
    return None


def get_session_history(
    db: Session,
    user_id: int,
    session_id: str,
) -> List[Dict]:
    """获取会话历史"""
    messages = (
        db.query(AIConversation)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == session_id,
        )
        .order_by(AIConversation.created_at.asc())
        .all()
    )

    return [
        {
            "role": msg.role,
            "content": msg.content,
            "references": msg.references,
            "source_set_version": msg.source_set_version,
            "proposal_id": msg.proposal_id,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        }
        for msg in messages
    ]


def get_user_sessions(
    db: Session,
    user_id: int,
) -> List[Dict]:
    """Legacy 列表（保留旧路由兼容；新路由请用 ai_session.list_user_sessions）。"""
    subquery = (
        db.query(
            AIConversation.session_id,
            func.max(AIConversation.created_at).label("max_created"),
        )
        .filter(AIConversation.user_id == user_id)
        .group_by(AIConversation.session_id)
        .subquery()
    )

    sessions = (
        db.query(AIConversation)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == subquery.c.session_id,
            AIConversation.created_at == subquery.c.max_created,
        )
        .order_by(desc(AIConversation.created_at))
        .all()
    )

    return [
        {
            "session_id": msg.session_id,
            "last_message": msg.content[:50] + "..." if len(msg.content) > 50 else msg.content,
            "role": msg.role,
            "created_at": msg.created_at.isoformat() if msg.created_at else None,
        }
        for msg in sessions
    ]


def create_new_session(db: Session, user_id: int) -> str:
    """Legacy：仅生成 session_id，不删除旧会话（无 persona 元数据）。"""
    return generate_session_id()


def delete_session(db: Session, user_id: int, session_id: str) -> bool:
    """删除指定会话（含 summary / embeddings / source sets）。"""
    return delete_session_complete(db, user_id, session_id)

"""
R11.3 / R11.3-Fix streaming turn idempotency (request_id).

- Completed turns: unique `(user_id, session_id, request_id, role)` on ai_conversations.
- In-flight turns: durable cross-worker claim rows in `ai_stream_request_claims`
  (migration 030) with lease / recovery. Never hold a DB row lock across LLM I/O.
"""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AIConversation

REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
# Legacy constant; runtime lease comes from claim_lease_seconds().
CLAIM_LEASE_SECONDS = 120
CLAIM_STATUS_CLAIMED = "claimed"
CLAIM_STATUS_COMPLETED = "completed"
CLAIM_STATUS_FAILED = "failed"

STREAM_REQUEST_IN_PROGRESS = "STREAM_REQUEST_IN_PROGRESS"
STREAM_REQUEST_CONFLICT = "STREAM_REQUEST_CONFLICT"
STREAM_CLAIM_LOST = "STREAM_CLAIM_LOST"


def claim_lease_seconds() -> int:
    from app.config import settings

    return max(15, int(getattr(settings, "stream_claim_lease_seconds", 120) or 120))


class StreamIdempotencyError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def validate_request_id(request_id: Optional[str]) -> str:
    value = (request_id or "").strip()
    if not REQUEST_ID_RE.fullmatch(value):
        raise StreamIdempotencyError(
            "STREAM_REQUEST_ID_INVALID",
            "request_id 格式无效，需为 8-64 位字母、数字、下划线或短横线。",
        )
    return value


def content_hash(message_text: str) -> str:
    return hashlib.sha256((message_text or "").strip().encode("utf-8")).hexdigest()


def find_completed_turn(
    db: Session,
    user_id: int,
    session_id: str,
    request_id: str,
) -> Optional[Dict[str, Any]]:
    """Return {"user_msg", "assistant_msg"} for an existing turn, or None."""
    rows: List[AIConversation] = (
        db.query(AIConversation)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == session_id,
            AIConversation.request_id == request_id,
        )
        .order_by(AIConversation.id.asc())
        .all()
    )
    user_msg = next((r for r in rows if r.role == "user"), None)
    if user_msg is None:
        return None
    assistant_msg = next((r for r in rows if r.role == "assistant"), None)
    return {"user_msg": user_msg, "assistant_msg": assistant_msg}


def ensure_no_conflict(
    existing: Optional[Dict[str, Any]],
    *,
    message_text: str,
) -> None:
    """Same request_id bound to different user content → STREAM_REQUEST_CONFLICT."""
    if existing is None:
        return
    prior = existing.get("user_msg")
    if prior is None:
        return
    if (prior.content or "").strip() != (message_text or "").strip():
        raise StreamIdempotencyError(
            STREAM_REQUEST_CONFLICT,
            "该 request_id 已绑定不同内容的请求，请使用新的 request_id。",
        )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _claims_table_ready(db: Session) -> bool:
    try:
        from sqlalchemy import text

        row = db.execute(
            text(
                "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() "
                "AND TABLE_NAME = 'ai_stream_request_claims'"
            )
        ).scalar()
        return int(row or 0) > 0
    except Exception:
        return False


def try_acquire_stream_claim(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    message_text: str,
) -> Tuple[str, Optional[str]]:
    """
    Acquire a durable claim for this request_id.

    Returns (status, owner_token):
      - ("acquired", owner_token): caller may invoke the provider
      - ("completed", None): turn already persisted; caller should replay
      - raises StreamIdempotencyError for IN_PROGRESS / CONFLICT
    """
    digest = content_hash(message_text)
    existing = find_completed_turn(db, user_id, session_id, request_id)
    ensure_no_conflict(existing, message_text=message_text)
    if existing is not None and existing.get("assistant_msg") is not None:
        return "completed", None

    if not _claims_table_ready(db):
        # Isolation / pre-030 safety: fall back to completed-turn check only.
        return "acquired", secrets.token_hex(16)

    from sqlalchemy import text

    now = _utcnow()
    lease = now + timedelta(seconds=claim_lease_seconds())
    owner = secrets.token_hex(16)

    try:
        db.execute(
            text(
                "INSERT INTO ai_stream_request_claims "
                "(user_id, session_id, request_id, content_hash, status, lease_until, owner_token) "
                "VALUES (:uid, :sid, :rid, :ch, :st, :lease, :owner)"
            ),
            {
                "uid": int(user_id),
                "sid": str(session_id),
                "rid": str(request_id),
                "ch": digest,
                "st": CLAIM_STATUS_CLAIMED,
                "lease": lease,
                "owner": owner,
            },
        )
        db.commit()
        return "acquired", owner
    except IntegrityError:
        db.rollback()

    row = db.execute(
        text(
            "SELECT content_hash, status, lease_until, owner_token "
            "FROM ai_stream_request_claims "
            "WHERE user_id=:uid AND session_id=:sid AND request_id=:rid "
            "FOR UPDATE"
        ),
        {"uid": int(user_id), "sid": str(session_id), "rid": str(request_id)},
    ).mappings().first()
    if row is None:
        db.rollback()
        raise StreamIdempotencyError(
            "STREAM_PROTOCOL_ERROR",
            "流式协议异常，请重试。",
        )
    if str(row["content_hash"]) != digest:
        db.rollback()
        raise StreamIdempotencyError(
            STREAM_REQUEST_CONFLICT,
            "该 request_id 已绑定不同内容的请求，请使用新的 request_id。",
        )

    status = str(row["status"] or "")
    lease_until = row["lease_until"]
    if status == CLAIM_STATUS_COMPLETED:
        db.commit()
        return "completed", None

    expired = lease_until is None or lease_until <= now
    if status == CLAIM_STATUS_CLAIMED and not expired:
        db.rollback()
        raise StreamIdempotencyError(
            STREAM_REQUEST_IN_PROGRESS,
            "相同请求正在生成中，请稍后重试。",
        )

    # Lease expired or failed → take over (short UPDATE, then release lock).
    db.execute(
        text(
            "UPDATE ai_stream_request_claims "
            "SET status=:st, lease_until=:lease, owner_token=:owner, "
            "content_hash=:ch, updated_at=UTC_TIMESTAMP(6) "
            "WHERE user_id=:uid AND session_id=:sid AND request_id=:rid"
        ),
        {
            "st": CLAIM_STATUS_CLAIMED,
            "lease": lease,
            "owner": owner,
            "ch": digest,
            "uid": int(user_id),
            "sid": str(session_id),
            "rid": str(request_id),
        },
    )
    db.commit()
    return "acquired", owner


def complete_stream_claim_in_txn(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    owner_token: Optional[str],
    result_fingerprint: Optional[str] = None,
) -> bool:
    """
    Mark claim completed inside caller's open transaction (no commit).

    Requires status=claimed, matching owner_token, and lease_until > DB now.
    Returns True iff exactly one row was updated. When claims table is absent,
    returns True (pre-030 safety).
    """
    if not owner_token:
        return False
    if not _claims_table_ready(db):
        return True
    from sqlalchemy import text

    result = db.execute(
        text(
            "UPDATE ai_stream_request_claims "
            "SET status=:st, result_fingerprint=:fp, updated_at=UTC_TIMESTAMP(6) "
            "WHERE user_id=:uid AND session_id=:sid AND request_id=:rid "
            "AND owner_token=:owner AND status=:claimed "
            "AND lease_until > UTC_TIMESTAMP(6)"
        ),
        {
            "st": CLAIM_STATUS_COMPLETED,
            "fp": result_fingerprint,
            "uid": int(user_id),
            "sid": str(session_id),
            "rid": str(request_id),
            "owner": owner_token,
            "claimed": CLAIM_STATUS_CLAIMED,
        },
    )
    return int(getattr(result, "rowcount", 0) or 0) == 1


def mark_stream_claim_completed(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    owner_token: Optional[str],
    result_fingerprint: Optional[str] = None,
) -> bool:
    """Legacy helper: complete claim and commit. Prefer finalize_stream_turn_with_claim."""
    try:
        ok = complete_stream_claim_in_txn(
            db,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            owner_token=owner_token,
            result_fingerprint=result_fingerprint,
        )
        if not ok:
            db.rollback()
            return False
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False


def release_stream_claim(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    owner_token: Optional[str],
) -> None:
    """Mark claim failed so another retry / worker can reclaim immediately."""
    if not owner_token or not _claims_table_ready(db):
        return
    from sqlalchemy import text

    try:
        db.execute(
            text(
                "UPDATE ai_stream_request_claims "
                "SET status=:st, lease_until=UTC_TIMESTAMP(6), updated_at=UTC_TIMESTAMP(6) "
                "WHERE user_id=:uid AND session_id=:sid AND request_id=:rid "
                "AND owner_token=:owner AND status=:claimed"
            ),
            {
                "st": CLAIM_STATUS_FAILED,
                "uid": int(user_id),
                "sid": str(session_id),
                "rid": str(request_id),
                "owner": owner_token,
                "claimed": CLAIM_STATUS_CLAIMED,
            },
        )
        db.commit()
    except Exception:
        db.rollback()


def _flush_stream_turn_rows(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    user_content: str,
    assistant_content: str,
    assistant_references: Optional[List[Dict[str, Any]]] = None,
    source_set_version: Optional[int] = None,
    proposal_id: Optional[int] = None,
    persist_user: bool = True,
) -> Dict[str, Any]:
    """Flush user/assistant/activity into the open transaction (no commit)."""
    from app.services.ai_chat import save_message
    from app.services.ai_session import touch_session_activity

    user_row = None
    if persist_user:
        user_row = save_message(
            db,
            user_id,
            session_id,
            "user",
            user_content,
            request_id=request_id,
            commit=False,
        )
    else:
        existing = find_completed_turn(db, user_id, session_id, request_id)
        if existing is not None:
            user_row = existing.get("user_msg")
        if user_row is None and proposal_id is not None:
            user_row = (
                db.query(AIConversation)
                .filter(
                    AIConversation.user_id == user_id,
                    AIConversation.session_id == session_id,
                    AIConversation.proposal_id == int(proposal_id),
                    AIConversation.role == "user",
                )
                .order_by(AIConversation.id.asc())
                .first()
            )
            if user_row is not None and not user_row.request_id:
                user_row.request_id = request_id
                db.flush()
    assistant_row = save_message(
        db,
        user_id,
        session_id,
        "assistant",
        assistant_content,
        assistant_references,
        source_set_version=source_set_version,
        proposal_id=proposal_id,
        request_id=request_id,
        commit=False,
    )
    touch_session_activity(db, user_id, session_id, commit=False)
    return {"user_msg": user_row, "assistant_msg": assistant_row, "idempotent": False}


def _idempotent_turn_or_raise(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    proposal_id: Optional[int],
) -> Dict[str, Any]:
    existing = find_completed_turn(db, user_id, session_id, request_id)
    if existing is not None and existing.get("assistant_msg") is not None:
        return {
            "user_msg": existing["user_msg"],
            "assistant_msg": existing["assistant_msg"],
            "idempotent": True,
        }
    if proposal_id is not None:
        winner = (
            db.query(AIConversation)
            .filter(
                AIConversation.user_id == user_id,
                AIConversation.session_id == session_id,
                AIConversation.proposal_id == int(proposal_id),
                AIConversation.role == "assistant",
            )
            .order_by(AIConversation.id.asc())
            .first()
        )
        if winner is not None:
            return {
                "user_msg": None,
                "assistant_msg": winner,
                "idempotent": True,
            }
    raise


def save_stream_turn(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    user_content: str,
    assistant_content: str,
    assistant_references: Optional[List[Dict[str, Any]]] = None,
    source_set_version: Optional[int] = None,
    proposal_id: Optional[int] = None,
    persist_user: bool = True,
    commit: bool = True,
) -> Dict[str, Any]:
    """
    Persist stream turn rows. When commit=True (legacy), commits once.
    When commit=False, only flushes into the caller's open transaction.
    Prefer finalize_stream_turn_with_claim for production streaming paths.
    """
    try:
        saved = _flush_stream_turn_rows(
            db,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            user_content=user_content,
            assistant_content=assistant_content,
            assistant_references=assistant_references,
            source_set_version=source_set_version,
            proposal_id=proposal_id,
            persist_user=persist_user,
        )
        if not commit:
            return saved
        db.commit()
        if saved.get("user_msg") is not None:
            db.refresh(saved["user_msg"])
        db.refresh(saved["assistant_msg"])
        return saved
    except IntegrityError:
        db.rollback()
        return _idempotent_turn_or_raise(
            db,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            proposal_id=proposal_id,
        )


def finalize_stream_turn_with_claim(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    request_id: str,
    owner_token: Optional[str],
    user_content: str,
    assistant_content: str,
    assistant_references: Optional[List[Dict[str, Any]]] = None,
    source_set_version: Optional[int] = None,
    proposal_id: Optional[int] = None,
    persist_user: bool = True,
    result_fingerprint: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Atomically persist messages + activity + claim completed in one commit.

    If claim completion rowcount != 1, rolls back message writes and raises
    STREAM_CLAIM_LOST. SSE done must only be emitted after this returns.
    """
    try:
        saved = _flush_stream_turn_rows(
            db,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            user_content=user_content,
            assistant_content=assistant_content,
            assistant_references=assistant_references,
            source_set_version=source_set_version,
            proposal_id=proposal_id,
            persist_user=persist_user,
        )
        ok = complete_stream_claim_in_txn(
            db,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            owner_token=owner_token,
            result_fingerprint=result_fingerprint,
        )
        if not ok:
            db.rollback()
            raise StreamIdempotencyError(
                STREAM_CLAIM_LOST,
                "生成请求所有权已失效，请重试。",
            )
        db.commit()
        if saved.get("user_msg") is not None:
            db.refresh(saved["user_msg"])
        db.refresh(saved["assistant_msg"])
        return saved
    except StreamIdempotencyError:
        raise
    except IntegrityError:
        db.rollback()
        return _idempotent_turn_or_raise(
            db,
            user_id=user_id,
            session_id=session_id,
            request_id=request_id,
            proposal_id=proposal_id,
        )

"""Notion OAuth start/callback. State is hashed; tokens are encrypted."""
from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta
from typing import Any, Optional
from urllib.parse import urlencode

from sqlalchemy import inspect, text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import NotionConnection, NotionOAuthState
from app.services.notion_crypto import (
    PURPOSE_ACCESS,
    PURPOSE_REFRESH,
    encrypt_token,
    require_crypto_configured,
)
from app.services.notion_errors import (
    NOTION_CONNECTION_CONFLICT,
    NOTION_DISABLED,
    NOTION_NOT_CONFIGURED,
    NOTION_OAUTH_EXCHANGE_FAILED,
    NOTION_OAUTH_STATE_EXPIRED,
    NOTION_OAUTH_STATE_INVALID,
    NotionServiceError,
    public_message,
    raise_notion,
)
from app.services.notion_provider import authorization_url, get_notion_provider
from app.timeutil import now_local

STATE_TTL_MINUTES = 10


def has_notion_tables(db: Session) -> bool:
    try:
        names = set(inspect(db.connection()).get_table_names())
    except Exception:
        return False
    return {"notion_connections", "notion_oauth_states", "notion_pages", "notion_sync_jobs"}.issubset(names)


def integration_enabled() -> bool:
    return bool(settings.notion_integration_enabled)


def oauth_configured() -> bool:
    return bool(
        (settings.notion_oauth_client_id or "").strip()
        and (settings.notion_oauth_client_secret or "").strip()
        and (settings.notion_oauth_redirect_uri or "").strip()
    )


def require_enabled() -> None:
    if not integration_enabled():
        raise_notion(NOTION_DISABLED, status_code=501)
    if not oauth_configured():
        raise_notion(NOTION_NOT_CONFIGURED, status_code=503)
    require_crypto_configured()


def preflight_booleans() -> dict[str, bool]:
    return {
        "enabled": integration_enabled(),
        "client_configured": bool((settings.notion_oauth_client_id or "").strip()),
        "secret_configured": bool((settings.notion_oauth_client_secret or "").strip()),
        "redirect_configured": bool((settings.notion_oauth_redirect_uri or "").strip()),
        "token_key_configured": bool((settings.notion_token_encryption_key or "").strip()),
        "webhook_token_configured": bool((settings.notion_webhook_verification_token or "").strip()),
        "api_version_configured": bool((settings.notion_api_version or "").strip()),
        "sync_enabled": bool(settings.notion_sync_enabled),
    }


def _hash_state(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def start_oauth(db: Session, user_id: int) -> dict[str, str]:
    require_enabled()
    if not has_notion_tables(db):
        raise_notion(NOTION_NOT_CONFIGURED, status_code=503)
    now = now_local()
    (
        db.query(NotionOAuthState)
        .filter(
            NotionOAuthState.user_id == int(user_id),
            NotionOAuthState.consumed_at.is_(None),
        )
        .update({"expires_at": now}, synchronize_session=False)
    )
    raw_state = secrets.token_hex(32)
    row = NotionOAuthState(
        user_id=int(user_id),
        state_hash=_hash_state(raw_state),
        expires_at=now + timedelta(minutes=STATE_TTL_MINUTES),
    )
    db.add(row)
    db.commit()
    return {"authorization_url": authorization_url(raw_state)}


def _consume_state(db: Session, raw_state: str) -> NotionOAuthState:
    if not raw_state:
        raise_notion(NOTION_OAUTH_STATE_INVALID, status_code=400)
    digest = _hash_state(raw_state)
    now = now_local()
    row = (
        db.query(NotionOAuthState)
        .filter(NotionOAuthState.state_hash == digest)
        .with_for_update()
        .first()
    )
    if row is None:
        raise_notion(NOTION_OAUTH_STATE_INVALID, status_code=400)
    if row.consumed_at is not None:
        raise_notion(NOTION_OAUTH_STATE_INVALID, status_code=400)
    if row.expires_at <= now:
        raise_notion(NOTION_OAUTH_STATE_EXPIRED, status_code=400)
    row.consumed_at = now
    db.add(row)
    db.flush()
    return row


def handle_callback(db: Session, *, code: Optional[str], state: Optional[str], error: Optional[str]) -> int:
    require_enabled()
    if error:
        raise_notion(NOTION_OAUTH_STATE_INVALID, status_code=400)
    if not code:
        raise_notion(NOTION_OAUTH_EXCHANGE_FAILED, status_code=400)
    state_row = _consume_state(db, state or "")
    user_id = int(state_row.user_id)
    provider = get_notion_provider()
    try:
        bundle = provider.exchange_code(code, (settings.notion_oauth_redirect_uri or "").strip())
    except NotionServiceError:
        db.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        db.commit()
        raise NotionServiceError(
            NOTION_OAUTH_EXCHANGE_FAILED,
            public_message(NOTION_OAUTH_EXCHANGE_FAILED),
            status_code=400,
        ) from exc
    if not bundle.access_token or not bundle.bot_id or not bundle.workspace_id:
        raise_notion(NOTION_OAUTH_EXCHANGE_FAILED, status_code=400)

    lock_sql = ""
    dialect = str(getattr(getattr(db.bind, "dialect", None), "name", "") or "")
    if dialect.startswith("mysql"):
        lock_sql = " FOR UPDATE"
    locked = db.execute(
        text(f"SELECT id FROM notion_connections WHERE user_id = :user_id{lock_sql}"),
        {"user_id": user_id},
    ).fetchone()
    existing = (
        db.query(NotionConnection)
        .filter(NotionConnection.user_id == user_id)
        .with_for_update()
        .first()
        if locked
        else None
    )
    other = (
        db.query(NotionConnection)
        .filter(NotionConnection.bot_id == bundle.bot_id, NotionConnection.user_id != user_id)
        .first()
    )
    if other is not None:
        raise_notion(NOTION_CONNECTION_CONFLICT, status_code=409)

    access_blob = encrypt_token(
        bundle.access_token,
        purpose=PURPOSE_ACCESS,
        user_id=user_id,
        bot_id=bundle.bot_id,
        workspace_id=bundle.workspace_id,
    )
    refresh_blob = None
    if bundle.refresh_token:
        refresh_blob = encrypt_token(
            bundle.refresh_token,
            purpose=PURPOSE_REFRESH,
            user_id=user_id,
            bot_id=bundle.bot_id,
            workspace_id=bundle.workspace_id,
        )

    switched_workspace = bool(
        existing is not None
        and existing.status != "disconnected"
        and existing.workspace_id != bundle.workspace_id
    )
    if existing is None:
        existing = NotionConnection(
            user_id=user_id,
            bot_id=bundle.bot_id,
            workspace_id=bundle.workspace_id,
            workspace_name=bundle.workspace_name[:255],
            owner_notion_user_id=bundle.owner_notion_user_id,
            access_token_encrypted=access_blob,
            refresh_token_encrypted=refresh_blob,
            status="active",
            sync_status="pending",
        )
        db.add(existing)
        db.flush()
        job_type = "initial_discovery"
    else:
        if switched_workspace:
            from app.services.notion_sync import cleanup_connection_mirrors

            cleanup_connection_mirrors(db, existing, mark_disconnected=False)
        existing.bot_id = bundle.bot_id
        existing.workspace_id = bundle.workspace_id
        existing.workspace_name = bundle.workspace_name[:255]
        existing.owner_notion_user_id = bundle.owner_notion_user_id
        existing.access_token_encrypted = access_blob
        existing.refresh_token_encrypted = refresh_blob
        existing.status = "active"
        existing.sync_status = "pending"
        existing.last_error_code = None
        db.add(existing)
        db.flush()
        job_type = "initial_discovery" if switched_workspace else "reconcile"

    from app.services.notion_jobs import enqueue_job

    enqueue_job(db, user_id=user_id, connection_id=int(existing.id), job_type=job_type)
    db.commit()
    return user_id


def app_return_url(*, error: Optional[str] = None) -> str:
    base = (settings.notification_public_base_url or "http://localhost:8000").rstrip("/")
    query = {"notion": "1"}
    if error:
        query["notion_error"] = error
    return f"{base}/profile?{urlencode(query)}"


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))

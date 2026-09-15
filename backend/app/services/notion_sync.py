"""Notion discovery, page sync, webhook enqueue, and connection cleanup."""
from __future__ import annotations

import hmac
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.models import NotionConnection, NotionPage
from app.services.notion_crypto import (
    PURPOSE_ACCESS,
    PURPOSE_REFRESH,
    decrypt_token,
    encrypt_token,
)
from app.services.notion_errors import (
    NOTION_CONNECTION_REQUIRED,
    NOTION_DISABLED,
    NOTION_INTERNAL_ERROR,
    NOTION_PAGE_NOT_FOUND,
    NOTION_PERMISSION_LOST,
    NOTION_RATE_LIMITED,
    NOTION_REAUTH_REQUIRED,
    NOTION_REMOTE_DELETED,
    NOTION_SYNC_FAILED,
    NOTION_SYNC_IN_PROGRESS,
    NOTION_WEBHOOK_INVALID,
    PAGE_TEXT_LIMIT,
    NotionServiceError,
    public_message,
    raise_notion,
)
from app.services.notion_jobs import enqueue_job, require_active_claim
from app.services.notion_knowledge import (
    delete_notion_knowledge,
    prepare_embeddings,
    replace_page_mirror,
)
from app.services.notion_normalize import (
    extract_data_source_title,
    extract_property_text,
    extract_title,
    normalize_page,
    parent_uuid,
    safe_page_url,
)
from app.services.notion_oauth import integration_enabled, oauth_configured
from app.services.notion_provider import NotionProviderError, get_notion_provider
from app.services.notion_reference import sanitize_notion_url
from app.timeutil import now_local

logger = logging.getLogger(__name__)
WEBHOOK_BODY_LIMIT = 64 * 1024
MANUAL_SYNC_IDEMPOTENT_SECONDS = 60
RECONCILE_EVERY = timedelta(hours=6)


def _connection_for_user(db: Session, user_id: int, *, for_update: bool = False) -> Optional[NotionConnection]:
    query = db.query(NotionConnection).filter(NotionConnection.user_id == int(user_id))
    if for_update:
        query = query.with_for_update()
    return query.first()


def _require_active_connection(db: Session, user_id: int) -> NotionConnection:
    if not integration_enabled():
        raise_notion(NOTION_DISABLED, status_code=501)
    row = _connection_for_user(db, user_id)
    if row is None or row.status == "disconnected":
        raise_notion(NOTION_CONNECTION_REQUIRED, status_code=404)
    if row.status == "reauth_required":
        raise_notion(NOTION_REAUTH_REQUIRED, status_code=409)
    return row


def _decrypt_access(connection: NotionConnection) -> str:
    return decrypt_token(
        bytes(connection.access_token_encrypted),
        purpose=PURPOSE_ACCESS,
        user_id=int(connection.user_id),
        bot_id=str(connection.bot_id),
        workspace_id=str(connection.workspace_id),
    )


def _refresh_connection(db: Session, connection: NotionConnection) -> str:
    if not connection.refresh_token_encrypted:
        connection.status = "reauth_required"
        db.add(connection)
        db.commit()
        raise NotionServiceError(NOTION_REAUTH_REQUIRED, public_message(NOTION_REAUTH_REQUIRED), status_code=409)
    refresh = decrypt_token(
        bytes(connection.refresh_token_encrypted),
        purpose=PURPOSE_REFRESH,
        user_id=int(connection.user_id),
        bot_id=str(connection.bot_id),
        workspace_id=str(connection.workspace_id),
    )
    bundle = get_notion_provider().refresh_token(refresh)
    connection.access_token_encrypted = encrypt_token(
        bundle.access_token,
        purpose=PURPOSE_ACCESS,
        user_id=int(connection.user_id),
        bot_id=str(connection.bot_id),
        workspace_id=str(connection.workspace_id),
    )
    if bundle.refresh_token:
        connection.refresh_token_encrypted = encrypt_token(
            bundle.refresh_token,
            purpose=PURPOSE_REFRESH,
            user_id=int(connection.user_id),
            bot_id=str(connection.bot_id),
            workspace_id=str(connection.workspace_id),
        )
    db.add(connection)
    db.commit()
    return bundle.access_token


def _provider_call(db: Session, connection: NotionConnection, fn):
    token = _decrypt_access(connection)
    key = str(connection.id)
    try:
        return fn(token)
    except NotionProviderError as exc:
        if exc.code == NOTION_REAUTH_REQUIRED:
            try:
                token = _refresh_connection(db, connection)
                return fn(token)
            except NotionServiceError:
                connection.status = "reauth_required"
                db.add(connection)
                db.commit()
                raise
        raise


def _collect_all_pages(db: Session, connection: NotionConnection) -> List[dict[str, Any]]:
    provider = get_notion_provider()
    found: Dict[str, dict[str, Any]] = {}

    def _search(kind: str) -> None:
        cursor = None
        while True:
            if kind == "page":
                payload = _provider_call(
                    db,
                    connection,
                    lambda token, c=cursor: provider.search_pages(
                        token, connection_key=str(connection.id), start_cursor=c
                    ),
                )
            else:
                payload = _provider_call(
                    db,
                    connection,
                    lambda token, c=cursor: provider.search_data_sources(
                        token, connection_key=str(connection.id), start_cursor=c
                    ),
                )
            for item in payload.get("results") or []:
                if not isinstance(item, dict):
                    continue
                if kind == "data_source":
                    ds_id = str(item.get("id") or "")
                    ds_title = extract_data_source_title(item)
                    ds_parent_uuid = parent_uuid(item)
                    ds_cursor = None
                    while ds_id:
                        rows = _provider_call(
                            db,
                            connection,
                            lambda token, d=ds_id, c=ds_cursor: provider.query_data_source(
                                token,
                                d,
                                connection_key=str(connection.id),
                                start_cursor=c,
                            ),
                        )
                        for row in rows.get("results") or []:
                            if isinstance(row, dict) and row.get("id"):
                                annotated = {**dict(found.get(str(row["id"])) or {}), **dict(row)}
                                annotated["_growthlog_object_kind"] = "database_row"
                                annotated["_growthlog_data_source_id"] = ds_id
                                annotated["_growthlog_data_source_title"] = ds_title
                                annotated["_growthlog_data_source_parent_uuid"] = ds_parent_uuid
                                found[str(row["id"])] = annotated
                        if not rows.get("has_more"):
                            break
                        ds_cursor = rows.get("next_cursor")
                elif item.get("id"):
                    found[str(item["id"])] = item
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")

    _search("page")
    _search("data_source")

    extra: Dict[str, dict[str, Any]] = {}
    for page in list(found.values()):
        try:
            _walk_child_pages(db, connection, str(page.get("id") or ""), found, extra)
        except NotionProviderError as exc:
            if exc.code not in {NOTION_PERMISSION_LOST, NOTION_REMOTE_DELETED}:
                raise
    found.update(extra)
    return list(found.values())

def _walk_child_pages(
    db: Session,
    connection: NotionConnection,
    block_id: str,
    known: Dict[str, dict[str, Any]],
    extra: Dict[str, dict[str, Any]],
    *,
    depth: int = 0,
) -> None:
    if not block_id or depth > 20:
        return
    provider = get_notion_provider()
    cursor = None
    while True:
        try:
            payload = _provider_call(
                db,
                connection,
                lambda token, b=block_id, c=cursor: provider.list_block_children(
                    token, b, connection_key=str(connection.id), start_cursor=c
                ),
            )
        except NotionProviderError as exc:
            if exc.code in {NOTION_PERMISSION_LOST, NOTION_REMOTE_DELETED}:
                return
            raise
        for block in payload.get("results") or []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "child_page" and block.get("id") and str(block["id"]) not in known:
                extra[str(block["id"])] = block
                _walk_child_pages(db, connection, str(block["id"]), known, extra, depth=depth + 1)
            if block.get("has_children") and block.get("id"):
                _walk_child_pages(db, connection, str(block["id"]), known, extra, depth=depth + 1)
        if not payload.get("has_more"):
            break
        cursor = payload.get("next_cursor")


def _collect_blocks(db: Session, connection: NotionConnection, root_id: str) -> List[dict[str, Any]]:
    provider = get_notion_provider()
    ordered: List[dict[str, Any]] = []

    def _walk(block_id: str, depth: int = 0) -> None:
        if depth > 30:
            return
        cursor = None
        while True:
            try:
                payload = _provider_call(
                    db,
                    connection,
                    lambda token, b=block_id, c=cursor: provider.list_block_children(
                        token, b, connection_key=str(connection.id), start_cursor=c
                    ),
                )
            except NotionProviderError as exc:
                if exc.code in {NOTION_PERMISSION_LOST, NOTION_REMOTE_DELETED}:
                    return
                raise
            for block in payload.get("results") or []:
                if not isinstance(block, dict):
                    continue
                ordered.append(block)
                if block.get("has_children") and block.get("id") and block.get("type") != "child_page":
                    _walk(str(block["id"]), depth + 1)
            if not payload.get("has_more"):
                break
            cursor = payload.get("next_cursor")

    _walk(root_id)
    return ordered


def _upsert_page_row(db: Session, connection: NotionConnection, remote: dict[str, Any]) -> NotionPage:
    uuid = str(remote.get("id") or "")
    page = (
        db.query(NotionPage)
        .filter(NotionPage.connection_id == int(connection.id), NotionPage.notion_page_uuid == uuid)
        .first()
    )
    if page is None:
        page = NotionPage(
            connection_id=int(connection.id),
            user_id=int(connection.user_id),
            notion_page_uuid=uuid,
            title=extract_title(remote) or "未命名页面",
            breadcrumb="",
            sync_status="pending",
        )
        db.add(page)
        db.flush()
    page.title = extract_title(remote) or page.title
    page.parent_notion_page_uuid = parent_uuid(remote)
    page.notion_url = safe_page_url(remote)
    remote_edited = _parse_remote_time(remote.get("last_edited_time"))
    if remote_edited is not None:
        if page.remote_last_edited_at and remote_edited < page.remote_last_edited_at and page.sync_status in {"indexed", "partial"}:
            return page
        page.remote_last_edited_at = remote_edited
    if page.sync_status not in {"deleted"}:
        page.sync_status = "pending"
    db.add(page)
    db.flush()
    return page


def _parse_remote_time(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    text_v = str(raw).replace("Z", "+00:00")
    try:
        value = datetime.fromisoformat(text_v)
        return value.replace(tzinfo=None)
    except ValueError:
        return None


def _rebuild_breadcrumbs(
    db: Session,
    connection_id: int,
    *,
    data_source_hints: Optional[Dict[str, Dict[str, Optional[str]]]] = None,
) -> None:
    pages = db.query(NotionPage).filter(NotionPage.connection_id == int(connection_id)).all()
    by_uuid = {page.notion_page_uuid: page for page in pages}
    hints = data_source_hints or {}
    for page in pages:
        parts = [page.title]
        seen = {page.notion_page_uuid}
        hint = hints.get(str(page.notion_page_uuid))
        if hint:
            data_source_title = str(hint.get("title") or "").strip()
            if data_source_title:
                parts.append(data_source_title)
            parent = hint.get("parent_uuid")
            data_source_id = str(hint.get("data_source_id") or "")
            if data_source_id:
                seen.add(data_source_id)
        else:
            parent = page.parent_notion_page_uuid
        while parent and parent in by_uuid and parent not in seen:
            seen.add(parent)
            ancestor = by_uuid[parent]
            parts.append(ancestor.title)
            parent = ancestor.parent_notion_page_uuid
        page.breadcrumb = " / ".join(reversed(parts))[:1024]
        db.add(page)


def run_discovery(
    db: Session,
    connection: NotionConnection,
    *,
    claim_check: Optional[Callable[[], None]] = None,
) -> None:
    if claim_check:
        claim_check()
    connection.sync_status = "syncing"
    connection.last_sync_started_at = now_local()
    db.add(connection)
    db.commit()
    remotes = _collect_all_pages(db, connection)
    data_source_hints: Dict[str, Dict[str, Optional[str]]] = {}
    for remote in remotes:
        if str(remote.get("_growthlog_object_kind") or "") != "database_row":
            continue
        remote_uuid = str(remote.get("id") or "")
        if not remote_uuid:
            continue
        data_source_hints[remote_uuid] = {
            "data_source_id": str(remote.get("_growthlog_data_source_id") or "") or None,
            "title": str(remote.get("_growthlog_data_source_title") or "") or None,
            "parent_uuid": str(remote.get("_growthlog_data_source_parent_uuid") or "") or None,
        }
    if claim_check:
        claim_check()
    live_uuids = set()
    for remote in remotes:
        if claim_check:
            claim_check()
        uuid = str(remote.get("id") or "")
        if not uuid:
            continue
        live_uuids.add(uuid)
        page = _upsert_page_row(db, connection, remote)
        enqueue_job(
            db,
            user_id=int(connection.user_id),
            connection_id=int(connection.id),
            job_type="sync_page",
            notion_page_id=int(page.id),
        )
    omitted = (
        db.query(NotionPage)
        .filter(
            NotionPage.connection_id == int(connection.id),
            NotionPage.sync_status.notin_(("deleted", "permission_lost")),
        )
        .all()
    )
    for page in omitted:
        if page.notion_page_uuid not in live_uuids:
            # Search is a discovery feed, not an authoritative inventory.
            # Re-fetch known omissions and purge only on an explicit 403/404.
            enqueue_job(
                db,
                user_id=int(connection.user_id),
                connection_id=int(connection.id),
                job_type="sync_page",
                notion_page_id=int(page.id),
            )
    _rebuild_breadcrumbs(db, int(connection.id), data_source_hints=data_source_hints)
    connection.last_reconcile_at = now_local()
    db.add(connection)
    if claim_check:
        claim_check()
    db.commit()


def sync_one_page(
    db: Session,
    connection: NotionConnection,
    page: NotionPage,
    *,
    claim_check: Optional[Callable[[], None]] = None,
) -> None:
    if claim_check:
        claim_check()
    provider = get_notion_provider()
    page.sync_status = "processing"
    db.add(page)
    db.commit()
    try:
        remote = _provider_call(
            db,
            connection,
            lambda token: provider.get_page(token, page.notion_page_uuid, connection_key=str(connection.id)),
        )
        if claim_check:
            claim_check()
        blocks = _collect_blocks(db, connection, page.notion_page_uuid)
        if claim_check:
            claim_check()
        title = extract_title(remote) or page.title
        normalized = normalize_page(
            title=title,
            breadcrumb=page.breadcrumb,
            properties=extract_property_text(remote),
            blocks=blocks,
        )
        page.title = title[:512]
        page.notion_url = safe_page_url(remote) or page.notion_url
        page.parent_notion_page_uuid = parent_uuid(remote) or page.parent_notion_page_uuid
        page.normalized_text = normalized["normalized_text"]
        page.observed_content_hash = normalized["observed_content_hash"]
        page.unsupported_block_count = int(normalized["unsupported_block_count"])
        page.last_error_code = PAGE_TEXT_LIMIT if normalized["truncated"] else None
        remote_edited = _parse_remote_time(remote.get("last_edited_time"))
        if remote_edited is not None:
            page.remote_last_edited_at = remote_edited
        db.add(page)
        if claim_check:
            claim_check()
        db.commit()
        unchanged = (
            page.indexed_content_hash
            and page.indexed_content_hash == page.observed_content_hash
            and page.sync_status in {"processing"}
        )
        source_ready = False
        if unchanged:
            from app.services.notion_knowledge import source_for_page

            source = source_for_page(db, int(page.user_id), int(page.id))
            source_ready = source is not None and source.status == "indexed"
        if unchanged and source_ready:
            page.sync_status = "partial" if normalized["partial"] else "indexed"
            db.add(page)
            db.commit()
            return
        prepared = prepare_embeddings(normalized["normalized_text"])
        if claim_check:
            claim_check()
        status = "partial" if normalized["partial"] else "indexed"
        page = (
            db.query(NotionPage)
            .filter(NotionPage.id == int(page.id), NotionPage.user_id == int(connection.user_id))
            .with_for_update()
            .first()
        )
        if page is None:
            return
        replace_page_mirror(db, page, prepared=prepared, content_hash=normalized["observed_content_hash"], status=status)
        page.indexed_content_hash = normalized["observed_content_hash"]
        page.sync_status = status
        db.add(page)
        if claim_check:
            claim_check()
        db.commit()
    except NotionProviderError as exc:
        page = db.query(NotionPage).filter(NotionPage.id == int(page.id)).first()
        if page is None:
            raise
        if exc.code == NOTION_PERMISSION_LOST:
            mark_page_permission_lost(db, page)
            return
        if exc.code == NOTION_REMOTE_DELETED:
            mark_page_deleted(db, page)
            return
        page.sync_status = "failed"
        page.last_error_code = exc.code
        db.add(page)
        db.commit()
        raise


def mark_page_deleted(db: Session, page: NotionPage) -> None:
    delete_notion_knowledge(db, user_id=int(page.user_id), page_ids=[int(page.id)])
    page.sync_status = "deleted"
    page.indexed_content_hash = None
    db.add(page)
    db.commit()


def mark_page_permission_lost(db: Session, page: NotionPage) -> None:
    delete_notion_knowledge(db, user_id=int(page.user_id), page_ids=[int(page.id)])
    page.sync_status = "permission_lost"
    page.indexed_content_hash = None
    db.add(page)
    db.commit()


def cleanup_connection_mirrors(db: Session, connection: NotionConnection, *, mark_disconnected: bool) -> None:
    pages = db.query(NotionPage).filter(NotionPage.connection_id == int(connection.id)).all()
    delete_notion_knowledge(db, user_id=int(connection.user_id), page_ids=[int(page.id) for page in pages])
    for page in pages:
        page.sync_status = "deleted"
        page.indexed_content_hash = None
        db.add(page)
    if mark_disconnected:
        connection.status = "disconnected"
        connection.sync_status = "failed"
        connection.access_token_encrypted = b"\x00"
        connection.refresh_token_encrypted = None
        db.add(connection)


def refresh_connection_rollup(db: Session, connection: NotionConnection) -> None:
    pages = (
        db.query(NotionPage)
        .filter(
            NotionPage.connection_id == int(connection.id),
            NotionPage.sync_status.notin_(("deleted",)),
        )
        .all()
    )
    statuses = {page.sync_status for page in pages}
    if connection.status == "reauth_required":
        return
    if any(status == "failed" for status in statuses) and not any(status in {"indexed", "partial"} for status in statuses):
        connection.sync_status = "failed"
    elif any(status in {"pending", "processing"} for status in statuses):
        connection.sync_status = "syncing"
    elif any(status in {"partial", "failed", "permission_lost"} for status in statuses):
        connection.sync_status = "partial"
    else:
        connection.sync_status = "ready"
        connection.last_sync_completed_at = now_local()
    db.add(connection)
    db.commit()


def process_claimed_job(
    job_id: int,
    *,
    worker_id: str,
    claim_check: Optional[Callable[[], None]] = None,
) -> str:
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        from app.models import NotionSyncJob

        require_active_claim(db, int(job_id), worker_id)
        job = db.query(NotionSyncJob).filter(NotionSyncJob.id == int(job_id)).first()
        if job is None:
            return "skipped"
        connection = db.query(NotionConnection).filter(NotionConnection.id == int(job.connection_id)).first()
        if connection is None or connection.status == "disconnected":
            return "skipped"
        if connection.status == "reauth_required":
            return "failed"
        if job.job_type in {"initial_discovery", "reconcile"}:
            run_discovery(db, connection, claim_check=claim_check)
            refresh_connection_rollup(db, connection)
            return "processed"
        page = None
        if job.notion_page_id:
            page = db.query(NotionPage).filter(NotionPage.id == int(job.notion_page_id)).first()
        if job.job_type == "delete_page" and page is not None:
            mark_page_deleted(db, page)
            refresh_connection_rollup(db, connection)
            return "processed"
        if job.job_type == "sync_page" and page is not None:
            sync_one_page(db, connection, page, claim_check=claim_check)
            refresh_connection_rollup(db, connection)
            return "processed"
        return "skipped"
    except NotionServiceError as exc:
        logger.warning("notion_job_failed code=%s", exc.code)
        raise
    finally:
        db.close()


def connection_status_view(db: Session, user_id: int) -> dict[str, Any]:
    if not integration_enabled():
        return {
            "enabled": False,
            "configured": oauth_configured(),
            "ui_state": "disabled",
            "preflight": __import__("app.services.notion_oauth", fromlist=["preflight_booleans"]).preflight_booleans(),
        }
    row = _connection_for_user(db, user_id)
    if row is None or row.status == "disconnected":
        return {
            "enabled": True,
            "configured": oauth_configured(),
            "connected": False,
            "ui_state": "unbound",
            "preflight": __import__("app.services.notion_oauth", fromlist=["preflight_booleans"]).preflight_booleans(),
        }
    status_rows = (
        db.query(NotionPage.sync_status)
        .filter(
            NotionPage.connection_id == int(row.id),
            NotionPage.sync_status.notin_(("deleted", "permission_lost")),
        )
        .all()
    )
    status_counts: Dict[str, int] = {}
    for (sync_status,) in status_rows:
        key = str(sync_status or "pending")
        status_counts[key] = status_counts.get(key, 0) + 1
    discovered_count = len(status_rows)
    indexed_count = int(status_counts.get("indexed", 0))
    partial_count = int(status_counts.get("partial", 0))
    pending_count = int(status_counts.get("pending", 0)) + int(status_counts.get("processing", 0))
    page_count = indexed_count + partial_count
    ui_state = "connected"
    if row.status == "reauth_required":
        ui_state = "reauth_required"
    elif row.sync_status == "syncing" or row.sync_status == "pending":
        ui_state = "syncing"
    elif row.sync_status == "partial":
        ui_state = "partial"
    elif row.sync_status == "failed":
        ui_state = "failed"
    return {
        "enabled": True,
        "configured": oauth_configured(),
        "connected": True,
        "ui_state": ui_state,
        "workspace_name": row.workspace_name,
        "sync_status": row.sync_status,
        "connection_status": row.status,
        "page_count": page_count,
        "discovered_count": discovered_count,
        "indexed_count": indexed_count,
        "partial_count": partial_count,
        "pending_count": pending_count,
        "last_sync_completed_at": row.last_sync_completed_at.isoformat() if row.last_sync_completed_at else None,
        "last_error_code": row.last_error_code,
        "preflight": __import__("app.services.notion_oauth", fromlist=["preflight_booleans"]).preflight_booleans(),
    }


def request_manual_sync(db: Session, user_id: int) -> dict[str, Any]:
    connection = _require_active_connection(db, user_id)
    now = now_local()
    if (
        connection.last_sync_started_at
        and (now - connection.last_sync_started_at).total_seconds() < MANUAL_SYNC_IDEMPOTENT_SECONDS
        and connection.sync_status == "syncing"
    ):
        return {"accepted": True, "idempotent": True}
    enqueue_job(db, user_id=user_id, connection_id=int(connection.id), job_type="reconcile")
    connection.sync_status = "syncing"
    connection.last_sync_started_at = now
    db.add(connection)
    db.commit()
    return {"accepted": True, "idempotent": False}


def list_pages(db: Session, user_id: int, *, cursor: Optional[int], limit: int) -> dict[str, Any]:
    connection = _require_active_connection(db, user_id)
    limit = max(1, min(int(limit or 20), 50))
    query = (
        db.query(NotionPage)
        .filter(
            NotionPage.connection_id == int(connection.id),
            NotionPage.sync_status.notin_(("deleted",)),
        )
        .order_by(NotionPage.id.asc())
    )
    if cursor:
        query = query.filter(NotionPage.id > int(cursor))
    rows = query.limit(limit + 1).all()
    items = []
    for page in rows[:limit]:
        items.append(
            {
                "title": page.title,
                "breadcrumb": page.breadcrumb,
                "sync_status": page.sync_status,
                "unsupported_block_count": int(page.unsupported_block_count or 0),
            }
        )
    next_cursor = str(rows[limit - 1].id) if len(rows) > limit else None
    return {"items": items, "next_cursor": next_cursor}


def disconnect(db: Session, user_id: int) -> None:
    connection = _connection_for_user(db, user_id, for_update=True)
    if connection is None or connection.status == "disconnected":
        return
    cleanup_connection_mirrors(db, connection, mark_disconnected=True)
    db.commit()


def maybe_enqueue_reconcile(db: Session, connection: NotionConnection) -> None:
    if connection.last_reconcile_at and now_local() - connection.last_reconcile_at < RECONCILE_EVERY:
        return
    enqueue_job(db, user_id=int(connection.user_id), connection_id=int(connection.id), job_type="reconcile")


def accept_webhook_verification(payload: dict[str, Any], signature: Optional[str]) -> bool:
    """Accept Notion's unsigned first-time verification without side effects."""
    if signature or set(payload.keys()) != {"verification_token"}:
        return False
    token = payload.get("verification_token")
    if not isinstance(token, str) or not 16 <= len(token) <= 512:
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=400)
    configured = (settings.notion_webhook_verification_token or "").strip()
    if configured:
        if not hmac.compare_digest(configured, token):
            raise_notion(NOTION_WEBHOOK_INVALID, status_code=401)
    else:
        _capture_webhook_bootstrap_token(token)
    logger.info("notion_webhook_verification accepted configured=%s", bool(configured))
    return True


def verify_webhook_signature(raw_body: bytes, signature: Optional[str]) -> None:
    if len(raw_body or b"") > WEBHOOK_BODY_LIMIT:
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=413)
    secret = _webhook_verification_secret()
    if not secret:
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=401)
    if not signature:
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=401)
    digest = hmac.new(secret.encode("utf-8"), raw_body, "sha256").hexdigest()
    expected = f"sha256={digest}"
    if not (hmac.compare_digest(signature, expected) or hmac.compare_digest(signature, digest)):
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=401)



def _webhook_bootstrap_path() -> Optional[Path]:
    raw = (settings.notion_webhook_bootstrap_file or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=503)
    return path


def _read_webhook_bootstrap_token() -> str:
    path = _webhook_bootstrap_path()
    if path is None:
        return ""
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        token = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    return token if 16 <= len(token) <= 512 else ""


def _webhook_verification_secret() -> str:
    configured = (settings.notion_webhook_verification_token or "").strip()
    return configured or _read_webhook_bootstrap_token()


def _capture_webhook_bootstrap_token(token: str) -> None:
    path = _webhook_bootstrap_path()
    if path is None or not path.parent.is_dir():
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=503)
    existing = _read_webhook_bootstrap_token()
    if existing:
        if not hmac.compare_digest(existing, token):
            raise_notion(NOTION_WEBHOOK_INVALID, status_code=401)
        return
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags, 0o600)
    except FileExistsError:
        existing = _read_webhook_bootstrap_token()
        if not existing or not hmac.compare_digest(existing, token):
            raise_notion(NOTION_WEBHOOK_INVALID, status_code=401)
        return
    except OSError:
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=503)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        path.unlink(missing_ok=True)
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=503)


def enqueue_webhook_event(db: Session, payload: dict[str, Any]) -> None:
    event_id = str(payload.get("id") or payload.get("event_id") or "") or None
    workspace_id = str(payload.get("workspace_id") or "")
    entity = payload.get("entity") if isinstance(payload.get("entity"), dict) else {}
    page_uuid = str(entity.get("id") or payload.get("page_id") or "")
    event_type = str(payload.get("type") or "")
    if not workspace_id or not event_type:
        raise_notion(NOTION_WEBHOOK_INVALID, status_code=400)
    connections = db.query(NotionConnection).filter(NotionConnection.status == "active")
    connections = connections.filter(NotionConnection.workspace_id == workspace_id)
    accessible_by = payload.get("accessible_by")
    if isinstance(accessible_by, list):
        bot_ids = {
            str(item.get("id") or "")
            for item in accessible_by
            if isinstance(item, dict) and str(item.get("type") or "") == "bot" and item.get("id")
        }
        if not bot_ids:
            return
        connections = connections.filter(NotionConnection.bot_id.in_(bot_ids))
    for connection in connections.all():
        page = None
        if page_uuid:
            page = (
                db.query(NotionPage)
                .filter(
                    NotionPage.connection_id == int(connection.id),
                    NotionPage.notion_page_uuid == page_uuid,
                )
                .first()
            )
        job_type = "delete_page" if "deleted" in event_type else "sync_page"
        if page is None and job_type == "sync_page":
            job_type = "reconcile"
        enqueue_job(
            db,
            user_id=int(connection.user_id),
            connection_id=int(connection.id),
            job_type=job_type,
            notion_page_id=int(page.id) if page is not None else None,
            provider_event_id=f"{connection.id}:{event_id}" if event_id else None,
        )
    db.commit()

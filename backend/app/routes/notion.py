"""Notion integration routes: JWT APIs plus public OAuth callback and webhook."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.auth import User, get_current_user
from app.database import get_db
from app.schemas.notion import (
    NotionOAuthStartResponse,
    NotionPageListResponse,
    NotionStatusResponse,
    NotionSyncResponse,
)
from app.services.notion_errors import (
    NOTION_WEBHOOK_INVALID,
    NotionServiceError,
    public_message,
)
from app.services.notion_oauth import app_return_url, handle_callback, start_oauth
from app.services.notion_sync import (
    WEBHOOK_BODY_LIMIT,
    accept_webhook_verification,
    connection_status_view,
    disconnect,
    enqueue_webhook_event,
    list_pages,
    request_manual_sync,
    verify_webhook_signature,
)

router = APIRouter()


def _http_error(exc: NotionServiceError) -> HTTPException:
    detail = {"code": exc.code, "message": exc.message}
    if exc.retry_after_seconds is not None:
        detail["retry_after_seconds"] = exc.retry_after_seconds
    return HTTPException(status_code=exc.status_code, detail=detail)


@router.get("/status", response_model=NotionStatusResponse)
def notion_status(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return connection_status_view(db, int(current_user.id))


@router.post("/oauth/start", response_model=NotionOAuthStartResponse)
def notion_oauth_start(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return start_oauth(db, int(current_user.id))
    except NotionServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/oauth/callback")
def notion_oauth_callback(
    request: Request,
    db: Session = Depends(get_db),
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
):
    _ = request
    try:
        handle_callback(db, code=code, state=state, error=error)
        return RedirectResponse(app_return_url(), status_code=302)
    except NotionServiceError as exc:
        return RedirectResponse(app_return_url(error=exc.code), status_code=302)


@router.post("/sync", response_model=NotionSyncResponse)
def notion_sync(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return request_manual_sync(db, int(current_user.id))
    except NotionServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/pages", response_model=NotionPageListResponse)
def notion_pages(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
    cursor: Optional[str] = Query(default=None),
    limit: int = Query(default=20, ge=1, le=50),
):
    try:
        parsed = int(cursor) if cursor else None
        return list_pages(db, int(current_user.id), cursor=parsed, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"code": "NOTION_PAGE_NOT_FOUND", "message": public_message("NOTION_PAGE_NOT_FOUND")}) from exc
    except NotionServiceError as exc:
        raise _http_error(exc) from exc


@router.delete("/connection")
def notion_disconnect(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        disconnect(db, int(current_user.id))
        return {"ok": True}
    except NotionServiceError as exc:
        raise _http_error(exc) from exc


@router.post("/webhook")
async def notion_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    if len(raw) > WEBHOOK_BODY_LIMIT:
        raise HTTPException(
            status_code=413,
            detail={"code": NOTION_WEBHOOK_INVALID, "message": public_message(NOTION_WEBHOOK_INVALID)},
        )
    try:
        payload = {}
        if raw:
            import json

            parsed = json.loads(raw.decode("utf-8"))
            if isinstance(parsed, dict):
                payload = parsed
        signature = request.headers.get("X-Notion-Signature")
        if accept_webhook_verification(payload, signature):
            return {"ok": True}
        verify_webhook_signature(raw, signature)
        enqueue_webhook_event(db, payload)
        return {"ok": True}
    except NotionServiceError as exc:
        raise _http_error(exc) from exc
    except Exception:
        raise HTTPException(
            status_code=400,
            detail={"code": NOTION_WEBHOOK_INVALID, "message": public_message(NOTION_WEBHOOK_INVALID)},
        )

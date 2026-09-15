"""Notification settings and Web Push APIs."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import User, get_current_user
from app.database import get_db
from app.schemas.notifications import (
    NotificationSettingsPatch,
    NotificationSettingsResponse,
    PreflightResponse,
    TestSendRequest,
    TestSendResponse,
    WebPushOkResponse,
    WebPushPublicKeyResponse,
    WebPushStatusRequest,
    WebPushStatusResponse,
    WebPushSubscriptionDeleteRequest,
    WebPushSubscriptionRequest,
)
from app.services import notification_service as ns
from app.services import web_push_service as wps

router = APIRouter()


def _http_error(exc: ns.NotificationServiceError) -> HTTPException:
    detail: dict[str, Any] = {"code": exc.code, "message": "notification_error"}
    if exc.retry_after_seconds is not None:
        detail["retry_after_seconds"] = exc.retry_after_seconds
    return HTTPException(
        status_code=exc.status_code,
        detail=detail,
    )


@router.get("/preflight", response_model=PreflightResponse)
def notification_preflight(
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    return ns.preflight_config()


@router.get("/settings", response_model=NotificationSettingsResponse)
def get_settings(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return ns.get_settings_view(db, int(current_user.id))


@router.patch("/settings", response_model=NotificationSettingsResponse)
def patch_settings(
    body: NotificationSettingsPatch,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return ns.patch_settings(db, int(current_user.id), body.model_dump(exclude_unset=True))
    except ns.NotificationServiceError as exc:
        raise _http_error(exc) from exc


@router.get("/web-push/public-key", response_model=WebPushPublicKeyResponse)
def web_push_public_key(
    current_user: User = Depends(get_current_user),
):
    _ = current_user
    return wps.public_key_view()


@router.post("/web-push/subscriptions", response_model=WebPushOkResponse)
def web_push_subscribe(
    body: WebPushSubscriptionRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return wps.upsert_subscription(db, int(current_user.id), body.model_dump())
    except ns.NotificationServiceError as exc:
        raise _http_error(exc) from exc


@router.post("/web-push/subscriptions/status", response_model=WebPushStatusResponse)
def web_push_subscription_status(
    body: WebPushStatusRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    # Endpoint stays in JSON body only; response never echoes it.
    return wps.subscription_status_for_endpoint(db, int(current_user.id), body.endpoint)


@router.delete("/web-push/subscriptions", response_model=WebPushOkResponse)
def web_push_unsubscribe(
    body: WebPushSubscriptionDeleteRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return wps.revoke_subscription(db, int(current_user.id), body.endpoint)
    except ns.NotificationServiceError as exc:
        raise _http_error(exc) from exc


@router.post("/test", response_model=TestSendResponse)
def send_test(
    body: TestSendRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    try:
        return ns.send_test_notification(db, int(current_user.id), endpoint=body.endpoint)
    except ns.NotificationServiceError as exc:
        raise _http_error(exc) from exc

"""Web Push preference, outbox scheduling, and dispatch service."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
from typing import Any, Optional

from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import NotificationOutbox, NotificationPreference, Todo, TodoDailyPlanItem, WebPushSubscription
from app.services.db_retry import with_session_retry
from app.timeutil import now_local, today_local

STALE_PROCESSING_MINUTES = 10
FORBIDDEN_PAYLOAD_KEYS = {
    "todo_title", "todo_content", "todo_id", "entry_id", "entry_body",
    "ocr_text", "attachment_id", "ai_reply", "ai_message", "completion_note",
}


class NotificationServiceError(Exception):
    def __init__(self, code: str, status_code: int = 400, *, retry_after_seconds: Optional[int] = None):
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def relative_deep_link() -> str:
    return "/app?tab=todos&review=1"


def preflight_config() -> dict[str, Any]:
    from app.services.web_push_crypto import subscription_crypto_configured
    from app.services.web_push_provider import vapid_configured, web_push_ready

    return {
        "public_base_url_configured": bool((settings.notification_public_base_url or "").strip()),
        "worker_enabled": bool(settings.notification_worker_enabled),
        "web_push_enabled": bool(settings.web_push_enabled or settings.web_push_use_fake),
        "web_push_vapid_configured": bool(vapid_configured() or settings.web_push_use_fake),
        "web_push_crypto_configured": subscription_crypto_configured(),
        "web_push_ready": bool(web_push_ready()),
    }


def get_settings_view(db: Session, user_id: int) -> dict[str, Any]:
    from app.services.notification_preferences import (
        PreferenceError, get_or_create_preferences, preferences_as_dict, preferences_table_ready,
    )
    from app.services.web_push_service import count_active_subscriptions

    if not preferences_table_ready(db):
        raise NotificationServiceError("PREFERENCES_SCHEMA_MISSING", 503)
    try:
        pref = get_or_create_preferences(db, user_id)
    except PreferenceError as exc:
        raise NotificationServiceError(exc.code, exc.status_code) from exc
    cfg = preflight_config()
    return {
        "state": "web_push_enabled" if count_active_subscriptions(db, user_id) else "unsubscribed",
        **preferences_as_dict(pref),
        "privacy_note": "默认只推送任务数量、紧急数量和 GrowthLog 链接，不发送 Todo 正文。",
        "web_push_ready": bool(cfg["web_push_ready"]),
        "active_device_count": count_active_subscriptions(db, user_id),
        "sound_note": "通知声音由 iOS/系统通知设置、静音与专注模式决定；PWA 无法指定自定义提示音。",
    }


def patch_settings(db: Session, user_id: int, patch: dict[str, Any]) -> dict[str, Any]:
    from app.services.notification_preferences import PreferenceError, patch_preferences, preferences_table_ready

    if not preferences_table_ready(db):
        raise NotificationServiceError("PREFERENCES_SCHEMA_MISSING", 503)
    try:
        patch_preferences(db, user_id, patch)
    except PreferenceError as exc:
        raise NotificationServiceError(exc.code, exc.status_code) from exc
    return get_settings_view(db, user_id)


def cancel_pending_outbox(db: Session, user_id: int) -> int:
    return (
        db.query(NotificationOutbox)
        .filter(
            NotificationOutbox.user_id == user_id,
            NotificationOutbox.status.in_(("pending", "retry", "processing")),
        )
        .update(
            {
                "status": "cancelled",
                "locked_by": None,
                "locked_at": None,
                "updated_at": now_local(),
            },
            synchronize_session=False,
        )
    )


def _assert_safe_payload(payload: dict[str, Any]) -> None:
    for key in payload:
        if str(key).lower() in FORBIDDEN_PAYLOAD_KEYS:
            raise NotificationServiceError("PAYLOAD_PRIVACY_VIOLATION", 500)
    for key in ("summary", "content"):
        text_val = str(payload.get(key) or "")
        if any(banned in text_val.lower() for banned in FORBIDDEN_PAYLOAD_KEYS):
            raise NotificationServiceError("PAYLOAD_PRIVACY_VIOLATION", 500)


def build_safe_message(plan_count: int, urgent_count: int) -> dict[str, Any]:
    payload = {
        "summary": "GrowthLog提醒",
        "content": (
            f"你今天还有 {plan_count} 项计划未完成，其中 {urgent_count} 项为紧急任务。"
            "打开 GrowthLog 查看。"
        ),
        "url": relative_deep_link(),
        "plan_count": int(plan_count),
        "urgent_count": int(urgent_count),
    }
    _assert_safe_payload(payload)
    return payload


def _today_plan_counts(db: Session, user_id: int, day: date) -> tuple[int, int]:
    rows = (
        db.query(Todo)
        .join(TodoDailyPlanItem, TodoDailyPlanItem.todo_id == Todo.id)
        .filter(
            TodoDailyPlanItem.user_id == user_id,
            TodoDailyPlanItem.plan_date == day,
            Todo.parent_id.is_(None),
            Todo.is_done.is_(False),
        )
        .all()
    )
    return len(rows), sum(1 for todo in rows if bool(todo.is_urgent))


def _urgent_overdue_count(db: Session, user_id: int, day: date) -> int:
    return (
        db.query(Todo)
        .filter(
            Todo.user_id == user_id,
            Todo.parent_id.is_(None),
            Todo.is_done.is_(False),
            Todo.is_urgent.is_(True),
            Todo.due_date.isnot(None),
            Todo.due_date < day,
        )
        .count()
    )


def enqueue_due_notifications(db: Session, *, now: Optional[datetime] = None) -> int:
    """Fan out due reminders only to active Web Push subscriptions."""
    from app.services.notification_preferences import (
        in_quiet_hours, next_quiet_end, preferences_table_ready, web_push_table_ready,
    )

    if not preferences_table_ready(db) or not web_push_table_ready(db):
        return 0
    if not (settings.web_push_enabled or settings.web_push_use_fake):
        return 0
    now = now or now_local()
    day = now.date()
    created = 0
    prefs = (
        db.query(NotificationPreference)
        .filter(NotificationPreference.reminders_enabled.is_(True))
        .all()
    )
    for pref in prefs:
        user_id = int(pref.user_id)
        subscriptions = (
            db.query(WebPushSubscription)
            .filter(WebPushSubscription.user_id == user_id, WebPushSubscription.status == "active")
            .all()
        )
        if not subscriptions:
            continue
        for notification_type, count_fn, due_time in (
            ("today_plan", lambda uid=user_id: _today_plan_counts(db, uid, day)[0], pref.today_plan_time or time(9, 0)),
            ("unfinished", lambda uid=user_id: _today_plan_counts(db, uid, day)[0], pref.unfinished_time or time(20, 0)),
            ("urgent_overdue", lambda uid=user_id: _urgent_overdue_count(db, uid, day) if pref.urgent_overdue_enabled else 0, time(9, 0)),
        ):
            if count_fn() <= 0 or now.time() < due_time:
                continue
            plan_count, urgent_count = _today_plan_counts(db, user_id, day)
            if notification_type == "urgent_overdue":
                plan_count = urgent_count = _urgent_overdue_count(db, user_id, day)
            available_at = next_quiet_end(pref, now) if in_quiet_hours(pref, now) else now
            payload = build_safe_message(plan_count, urgent_count)
            payload.update({"tag": f"growthlog-{notification_type}", "notification_type": notification_type})
            for subscription in subscriptions:
                db.add(NotificationOutbox(
                    user_id=user_id,
                    binding_id=None,
                    web_push_subscription_id=int(subscription.id),
                    notification_type=notification_type,
                    local_date=day,
                    dedupe_key=f"{user_id}:{day.isoformat()}:{notification_type}:wp:{int(subscription.id)}",
                    safe_payload_json=payload,
                    status="pending",
                    available_at=available_at,
                ))
                try:
                    db.commit()
                    created += 1
                except IntegrityError:
                    db.rollback()
    return created


def reclaim_stale_processing(db: Session, *, stuck_minutes: int = STALE_PROCESSING_MINUTES) -> int:
    cutoff = now_local() - timedelta(minutes=stuck_minutes)
    return db.execute(
        text(
            """
            UPDATE notification_outbox
            SET status='retry', locked_by=NULL, locked_at=NULL,
                available_at=:now, error_code='STALE_PROCESSING'
            WHERE status='processing' AND locked_at IS NOT NULL AND locked_at < :cutoff
            """
        ),
        {"now": now_local(), "cutoff": cutoff},
    ).rowcount or 0


def claim_outbox_jobs(db: Session, *, worker_id: str, batch_size: int = 10) -> list[int]:
    now = now_local()

    def _claim() -> list[int]:
        reclaim_stale_processing(db)
        db.commit()
        rows = db.execute(
            text(
                """
                SELECT id FROM notification_outbox
                WHERE status IN ('pending', 'retry')
                  AND available_at <= :now
                  AND attempt_count < max_attempts
                ORDER BY id ASC LIMIT :lim FOR UPDATE SKIP LOCKED
                """
            ),
            {"now": now, "lim": batch_size},
        ).fetchall()
        ids = [int(row[0]) for row in rows]
        if not ids:
            return []
        db.execute(
            text(
                """
                UPDATE notification_outbox
                SET status='processing', locked_by=:wid, locked_at=:now,
                    attempt_count=attempt_count+1, updated_at=:now
                WHERE id IN :ids
                """
            ).bindparams(bindparam("ids", expanding=True)),
            {"wid": worker_id, "now": now, "ids": ids},
        )
        db.commit()
        return ids

    return with_session_retry(db=db, fn=_claim)


def _cancel_outbox(db: Session, row: NotificationOutbox, *, error_code: Optional[str] = None) -> str:
    row.status = "cancelled"
    row.error_code = error_code
    row.locked_by = None
    row.locked_at = None
    db.commit()
    return "cancelled"


def finalize_outbox_sent(db: Session, outbox_id: int, *, message_id: Optional[str]) -> None:
    row = db.query(NotificationOutbox).filter(NotificationOutbox.id == outbox_id).first()
    if not row or row.status != "processing":
        return
    row.status, row.provider_message_id, row.sent_at = "sent", message_id, now_local()
    row.locked_by = row.locked_at = None
    row.error_code = None
    db.commit()


def finalize_outbox_failure(
    db: Session, outbox_id: int, *, error_code: str, retryable: bool,
) -> None:
    row = db.query(NotificationOutbox).filter(NotificationOutbox.id == outbox_id).first()
    if not row:
        return
    row.error_code, row.locked_by, row.locked_at = error_code[:64], None, None
    if retryable and row.attempt_count < row.max_attempts:
        row.status = "retry"
        row.available_at = now_local() + timedelta(seconds=min(3600, 30 * (2 ** max(0, row.attempt_count - 1))))
    else:
        row.status = "failed"
    db.commit()


def process_one_outbox(db: Session, outbox_id: int) -> str:
    from app.services.web_push_crypto import decrypt_subscription
    from app.services.web_push_provider import WebPushProviderError, send_web_push
    from app.services.web_push_service import mark_subscription_failure, mark_subscription_revoked, mark_subscription_success

    row = db.query(NotificationOutbox).filter(NotificationOutbox.id == outbox_id).first()
    if not row:
        return "missing"
    if row.status != "processing":
        return "skipped_not_processing"
    if row.binding_id is not None and row.web_push_subscription_id is None:
        return _cancel_outbox(db, row, error_code="WXPUSHER_RETIRED")
    if row.web_push_subscription_id is None:
        return _cancel_outbox(db, row, error_code="WEB_PUSH_SUBSCRIPTION_MISSING")
    pref = db.query(NotificationPreference).filter(NotificationPreference.user_id == row.user_id).first()
    if pref is None or not pref.reminders_enabled:
        return _cancel_outbox(db, row)
    try:
        _assert_safe_payload(dict(row.safe_payload_json or {}))
    except NotificationServiceError:
        finalize_outbox_failure(db, outbox_id, error_code="PAYLOAD_PRIVACY_VIOLATION", retryable=False)
        return "failed"
    subscription_row = (
        db.query(WebPushSubscription)
        .filter(WebPushSubscription.id == row.web_push_subscription_id)
        .first()
    )
    if not subscription_row or subscription_row.status != "active":
        return _cancel_outbox(db, row)
    try:
        subscription = decrypt_subscription(bytes(subscription_row.subscription_ciphertext))
    except Exception:  # noqa: BLE001
        finalize_outbox_failure(db, outbox_id, error_code="SUBSCRIPTION_DECRYPT_FAILED", retryable=False)
        return "failed"
    db.commit()  # Network I/O must not hold the database transaction.
    try:
        result = send_web_push(subscription, payload=dict(row.safe_payload_json or {}))
    except WebPushProviderError as exc:
        if exc.revoke:
            mark_subscription_revoked(db, int(subscription_row.id), error_code=exc.error_code)
            finalize_outbox_failure(db, outbox_id, error_code=exc.error_code, retryable=False)
            return "failed"
        mark_subscription_failure(db, int(subscription_row.id), error_code=exc.error_code)
        finalize_outbox_failure(db, outbox_id, error_code=exc.error_code, retryable=exc.retryable)
        return "retry" if exc.retryable else "failed"
    except Exception:  # noqa: BLE001
        mark_subscription_failure(db, int(subscription_row.id), error_code="WORKER_EXCEPTION")
        finalize_outbox_failure(db, outbox_id, error_code="WORKER_EXCEPTION", retryable=True)
        return "retry"
    mark_subscription_success(db, int(subscription_row.id))
    finalize_outbox_sent(db, outbox_id, message_id=result.message_id)
    return "sent"


def send_test_notification(db: Session, user_id: int, *, endpoint: str) -> dict[str, Any]:
    if not endpoint:
        raise NotificationServiceError("ENDPOINT_REQUIRED", 400)
    from app.services.web_push_service import send_test_to_endpoint
    return send_test_to_endpoint(db, user_id, endpoint)

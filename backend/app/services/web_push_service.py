"""Web Push subscribe / revoke / status / test-send (R10)."""
from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import WebPushSubscription
from app.services.notification_service import (
    NotificationServiceError,
    _assert_safe_payload,
    _today_plan_counts,
    build_safe_message,
)
from app.services.web_push_crypto import (
    decrypt_subscription,
    encrypt_subscription,
    hash_endpoint,
    subscription_crypto_configured,
)
from app.services.web_push_endpoint import validate_push_endpoint
from app.services.web_push_provider import (
    WebPushProviderError,
    send_web_push,
    vapid_keys_valid,
    web_push_ready,
)
from app.timeutil import now_local, today_local

logger = logging.getLogger(__name__)

MAX_ENDPOINT_LEN = 2048
MAX_KEY_LEN = 512
MIN_KEY_LEN = 8


def validate_subscription_payload(body: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(body, dict):
        raise NotificationServiceError("INVALID_SUBSCRIPTION", 400)
    endpoint_raw = body.get("endpoint")
    keys = body.get("keys")
    try:
        endpoint = validate_push_endpoint(endpoint_raw if isinstance(endpoint_raw, str) else "")
    except ValueError as exc:
        raise NotificationServiceError(str(exc.args[0] if exc.args else "INVALID_ENDPOINT"), 400) from exc

    if not isinstance(keys, dict):
        raise NotificationServiceError("INVALID_KEYS", 400)
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        raise NotificationServiceError("INVALID_KEYS", 400)
    if not (MIN_KEY_LEN <= len(p256dh) <= MAX_KEY_LEN and MIN_KEY_LEN <= len(auth) <= MAX_KEY_LEN):
        raise NotificationServiceError("INVALID_KEYS", 400)

    exp = body.get("expirationTime", None)
    if exp is not None and type(exp) not in (int, float):
        raise NotificationServiceError("INVALID_EXPIRATION_TIME", 400)
    if isinstance(exp, bool):
        raise NotificationServiceError("INVALID_EXPIRATION_TIME", 400)

    return {
        "endpoint": endpoint,
        "expirationTime": float(exp) if exp is not None else None,
        "keys": {"p256dh": p256dh, "auth": auth},
    }


def public_key_view() -> dict[str, Any]:
    enabled = bool(settings.web_push_enabled or settings.web_push_use_fake)
    ready = web_push_ready()
    # Only return a usable public key when cryptographically valid and ready.
    if enabled and ready and vapid_keys_valid():
        pub = (settings.web_push_vapid_public_key or "").strip()
        return {"public_key": pub, "ready": True, "enabled": True}
    return {
        "public_key": "",
        "ready": bool(ready),
        "enabled": enabled,
    }


def count_active_subscriptions(db: Session, user_id: int) -> int:
    from app.services.notification_preferences import web_push_table_ready

    if not web_push_table_ready(db):
        return 0
    return (
        db.query(WebPushSubscription)
        .filter(
            WebPushSubscription.user_id == user_id,
            WebPushSubscription.status == "active",
        )
        .count()
    )


def subscription_status_for_endpoint(db: Session, user_id: int, endpoint: str) -> dict[str, Any]:
    """
    Endpoint-specific status. Cross-user and missing endpoints share inactive semantics.
    Never echoes endpoint; never 404 for enumeration.
    """
    active_count = count_active_subscriptions(db, user_id)
    current_active = False
    try:
        endpoint_norm = validate_push_endpoint(endpoint if isinstance(endpoint, str) else "")
    except ValueError:
        return {"current_device_active": False, "active_device_count": active_count}

    if not subscription_crypto_configured():
        return {"current_device_active": False, "active_device_count": active_count}

    ehash = hash_endpoint(endpoint_norm)
    row = (
        db.query(WebPushSubscription)
        .filter(
            WebPushSubscription.endpoint_hash == ehash,
            WebPushSubscription.user_id == user_id,
            WebPushSubscription.status == "active",
        )
        .first()
    )
    current_active = bool(row)
    return {
        "current_device_active": current_active,
        "active_device_count": active_count,
    }


def upsert_subscription(db: Session, user_id: int, body: dict[str, Any]) -> dict[str, Any]:
    if not subscription_crypto_configured():
        raise NotificationServiceError("WEB_PUSH_CRYPTO_NOT_READY", 503)
    if not (settings.web_push_enabled or settings.web_push_use_fake):
        raise NotificationServiceError("WEB_PUSH_DISABLED", 503)
    sub = validate_subscription_payload(body)
    ehash = hash_endpoint(sub["endpoint"])
    ciphertext = encrypt_subscription(sub)

    existing = (
        db.query(WebPushSubscription)
        .filter(WebPushSubscription.endpoint_hash == ehash)
        .with_for_update()
        .first()
    )
    if existing and int(existing.user_id) != int(user_id):
        raise NotificationServiceError("NOT_FOUND", 404)

    if existing:
        existing.subscription_ciphertext = ciphertext
        existing.status = "active"
        existing.failure_count = 0
        existing.last_error_code = None
        existing.last_failure_at = None
        existing.updated_at = now_local()
        db.commit()
        return {"ok": True, "active_device_count": count_active_subscriptions(db, user_id)}

    row = WebPushSubscription(
        user_id=user_id,
        subscription_ciphertext=ciphertext,
        endpoint_hash=ehash,
        status="active",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise NotificationServiceError("NOT_FOUND", 404)
    return {"ok": True, "active_device_count": count_active_subscriptions(db, user_id)}


def revoke_subscription(db: Session, user_id: int, endpoint: str) -> dict[str, Any]:
    if not isinstance(endpoint, str) or not endpoint.strip():
        raise NotificationServiceError("INVALID_ENDPOINT", 400)
    if not subscription_crypto_configured():
        raise NotificationServiceError("WEB_PUSH_CRYPTO_NOT_READY", 503)
    try:
        endpoint_norm = validate_push_endpoint(endpoint)
    except ValueError as exc:
        raise NotificationServiceError(str(exc.args[0] if exc.args else "INVALID_ENDPOINT"), 400) from exc
    ehash = hash_endpoint(endpoint_norm)
    row = (
        db.query(WebPushSubscription)
        .filter(
            WebPushSubscription.endpoint_hash == ehash,
            WebPushSubscription.user_id == user_id,
        )
        .with_for_update()
        .first()
    )
    if not row:
        raise NotificationServiceError("NOT_FOUND", 404)
    row.status = "revoked"
    row.updated_at = now_local()
    db.execute(
        text(
            """
            UPDATE notification_outbox
            SET status='cancelled', locked_by=NULL, locked_at=NULL, updated_at=:now
            WHERE web_push_subscription_id=:sid
              AND status IN ('pending','retry','processing')
            """
        ),
        {"now": now_local(), "sid": int(row.id)},
    )
    db.commit()
    return {"ok": True, "active_device_count": count_active_subscriptions(db, user_id)}


def mark_subscription_revoked(db: Session, subscription_id: int, *, error_code: str) -> None:
    row = db.query(WebPushSubscription).filter(WebPushSubscription.id == subscription_id).first()
    if not row:
        return
    row.status = "revoked"
    row.last_failure_at = now_local()
    row.last_error_code = error_code[:64]
    row.failure_count = int(row.failure_count or 0) + 1
    db.commit()


def mark_subscription_failure(db: Session, subscription_id: int, *, error_code: str) -> None:
    """Retryable provider failures: update counters without revoking."""
    row = db.query(WebPushSubscription).filter(WebPushSubscription.id == subscription_id).first()
    if not row:
        return
    row.last_failure_at = now_local()
    row.last_error_code = error_code[:64]
    row.failure_count = int(row.failure_count or 0) + 1
    db.commit()


def mark_subscription_success(db: Session, subscription_id: int) -> None:
    row = db.query(WebPushSubscription).filter(WebPushSubscription.id == subscription_id).first()
    if not row:
        return
    row.last_success_at = now_local()
    row.failure_count = 0
    row.last_error_code = None
    row.last_failure_at = None
    db.commit()


def send_test_to_endpoint(db: Session, user_id: int, endpoint: str) -> dict[str, Any]:
    if not web_push_ready() and not settings.web_push_use_fake:
        raise NotificationServiceError("WEB_PUSH_NOT_READY", 503)
    try:
        endpoint_norm = validate_push_endpoint(endpoint if isinstance(endpoint, str) else "")
    except ValueError as exc:
        raise NotificationServiceError(str(exc.args[0] if exc.args else "INVALID_ENDPOINT"), 400) from exc
    ehash = hash_endpoint(endpoint_norm)
    locked = db.execute(
        text(
            """
            SELECT id, status, last_test_sent_at, subscription_ciphertext
            FROM web_push_subscriptions
            WHERE user_id=:u AND endpoint_hash=:h AND status='active'
            FOR UPDATE
            """
        ),
        {"u": user_id, "h": ehash},
    ).mappings().first()
    if not locked:
        raise NotificationServiceError("NOT_FOUND", 404)

    if locked["last_test_sent_at"]:
        elapsed = (now_local() - locked["last_test_sent_at"]).total_seconds()
        if elapsed < settings.notification_test_cooldown_seconds:
            db.commit()
            remaining = max(
                1,
                int(settings.notification_test_cooldown_seconds - elapsed + 0.999),
            )
            raise NotificationServiceError("TEST_RATE_LIMITED", 429, retry_after_seconds=remaining)

    reserved_at = now_local().replace(microsecond=0)
    db.execute(
        text("UPDATE web_push_subscriptions SET last_test_sent_at=:now WHERE id=:id"),
        {"now": reserved_at, "id": int(locked["id"])},
    )
    db.commit()

    plan_count, urgent_count = _today_plan_counts(db, user_id, today_local())
    payload = build_safe_message(max(plan_count, 0), max(urgent_count, 0))
    payload["content"] = (
        f"这是一条 GrowthLog 测试提醒。你今天有 {plan_count} 项计划未完成，"
        f"其中 {urgent_count} 项紧急。打开 GrowthLog 查看。"
    )
    payload["tag"] = "growthlog-test"
    payload["url"] = "/app?tab=todos&review=1"
    _assert_safe_payload(payload)

    try:
        subscription = decrypt_subscription(bytes(locked["subscription_ciphertext"]))
        result = send_web_push(subscription, payload=payload)
        mark_subscription_success(db, int(locked["id"]))
        return {"ok": True, "message_id_present": bool(result.message_id), "cooldown_seconds": 60}
    except WebPushProviderError as exc:
        if exc.revoke:
            mark_subscription_revoked(db, int(locked["id"]), error_code=exc.error_code)
        else:
            mark_subscription_failure(db, int(locked["id"]), error_code=exc.error_code)
        try:
            db.execute(
                text(
                    "UPDATE web_push_subscriptions SET last_test_sent_at=NULL "
                    "WHERE id=:id AND last_test_sent_at=:reserved_at"
                ),
                {"id": int(locked["id"]), "reserved_at": reserved_at},
            )
            db.commit()
        except Exception:
            db.rollback()
        raise NotificationServiceError(exc.error_code, 502) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning("web_push_test_failed code=%s", type(exc).__name__)
        try:
            db.execute(
                text(
                    "UPDATE web_push_subscriptions SET last_test_sent_at=NULL "
                    "WHERE id=:id AND last_test_sent_at=:reserved_at"
                ),
                {"id": int(locked["id"]), "reserved_at": reserved_at},
            )
            db.commit()
        except Exception:
            db.rollback()
        raise NotificationServiceError("WEB_PUSH_TEST_FAILED", 502) from exc

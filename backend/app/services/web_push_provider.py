"""Web Push send via pywebpush (+ fake provider for isolate tests)."""
from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)


class WebPushProviderError(Exception):
    def __init__(self, error_code: str, *, retryable: bool = False, revoke: bool = False):
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable
        self.revoke = revoke


@dataclass(frozen=True)
class WebPushSendResult:
    message_id: Optional[str]


def _b64url_decode(raw: str) -> bytes:
    text = (raw or "").strip()
    pad = "=" * ((4 - len(text) % 4) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _uncompressed_p256_point(pub_b64: str) -> bytes:
    raw = _b64url_decode(pub_b64)
    if len(raw) != 65 or raw[0] != 0x04:
        raise ValueError("VAPID_PUBLIC_KEY_INVALID")
    return raw


def vapid_keys_valid() -> bool:
    """True only when private parses and public matches derived P-256 uncompressed point."""
    pub = (settings.web_push_vapid_public_key or "").strip()
    priv = (settings.web_push_vapid_private_key or "").strip()
    if not pub or not priv:
        return False
    try:
        expected = _uncompressed_p256_point(pub)
    except Exception:
        return False
    try:
        from py_vapid import Vapid
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        vapid = Vapid.from_string(private_key=priv)
        derived = vapid.public_key.public_bytes(
            encoding=Encoding.X962,
            format=PublicFormat.UncompressedPoint,
        )
        return derived == expected
    except Exception:
        logger.warning("web_push_vapid_validate_failed")
        return False


def vapid_configured() -> bool:
    """Backward-compatible name: now means cryptographically valid key pair."""
    return vapid_keys_valid()


def web_push_ready() -> bool:
    from app.services.web_push_crypto import subscription_crypto_configured

    crypto_ok = subscription_crypto_configured()
    if settings.web_push_use_fake:
        # Fake send path skips network VAPID, but still requires crypto + enabled flag path.
        return crypto_ok
    return bool(settings.web_push_enabled) and vapid_keys_valid() and crypto_ok


def _fake_state_path() -> Path:
    override = (os.environ.get("WEB_PUSH_FAKE_STATE_PATH") or "").strip()
    if override:
        path = Path(override)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path
    return Path(tempfile.gettempdir()) / f"growthlog_fake_webpush_{os.getpid()}_{time.time_ns()}.json"


class FakeWebPushProvider:
    def __init__(self, state_path: Optional[Path] = None) -> None:
        self._path = state_path or _fake_state_path()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._write({"sent": [], "fail_next_with": None, "seq": 0})

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {"sent": [], "fail_next_with": None, "seq": 0}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.write_text(json.dumps(data), encoding="utf-8")

    def set_fail_next(self, code: Optional[str]) -> None:
        data = self._read()
        data["fail_next_with"] = code
        self._write(data)

    @property
    def sent_count(self) -> int:
        return len(self._read().get("sent") or [])

    def send(self, subscription: dict[str, Any], *, payload: dict[str, Any]) -> WebPushSendResult:
        data = self._read()
        fail = data.get("fail_next_with")
        if fail:
            data["fail_next_with"] = None
            self._write(data)
            if fail in {"GONE", "NOT_FOUND"}:
                raise WebPushProviderError(fail, retryable=False, revoke=True)
            if fail in {"TIMEOUT", "SERVER_ERROR", "RATE_LIMITED"}:
                raise WebPushProviderError(fail, retryable=True, revoke=False)
            raise WebPushProviderError(fail, retryable=False, revoke=False)
        data["seq"] = int(data.get("seq") or 0) + 1
        # Store only non-sensitive counters for tests — never endpoint/keys/body.
        data.setdefault("sent", []).append(
            {
                "seq": data["seq"],
                "has_endpoint": bool((subscription or {}).get("endpoint")),
                "plan_count": payload.get("plan_count"),
                "urgent_count": payload.get("urgent_count"),
                "tag": payload.get("tag"),
            }
        )
        self._write(data)
        return WebPushSendResult(message_id=f"fake-{data['seq']}")


_FAKE: Optional[FakeWebPushProvider] = None


def get_fake_web_push_provider(state_path: Optional[Path] = None) -> FakeWebPushProvider:
    global _FAKE
    if state_path is not None:
        _FAKE = FakeWebPushProvider(state_path)
        return _FAKE
    if _FAKE is None:
        _FAKE = FakeWebPushProvider()
    return _FAKE


def reset_fake_web_push_provider(state_path: Optional[Path] = None) -> FakeWebPushProvider:
    global _FAKE
    _FAKE = FakeWebPushProvider(state_path)
    return _FAKE


def send_web_push(subscription: dict[str, Any], *, payload: dict[str, Any]) -> WebPushSendResult:
    if settings.web_push_use_fake or (os.environ.get("WEB_PUSH_USE_FAKE") or "").lower() in {
        "1",
        "true",
        "yes",
    }:
        return get_fake_web_push_provider().send(subscription, payload=payload)

    if not vapid_keys_valid():
        raise WebPushProviderError("VAPID_NOT_CONFIGURED", retryable=False)

    try:
        from pywebpush import WebPushException, webpush
    except Exception as exc:  # noqa: BLE001
        raise WebPushProviderError("PYWEBPUSH_IMPORT_FAILED", retryable=False) from exc

    # Relative deep link for SW; never put absolute external URLs in push body.
    url = str(payload.get("url") or "/app?tab=todos&review=1")
    if not url.startswith("/") or url.startswith("//") or "://" in url:
        url = "/app?tab=todos&review=1"

    body = json.dumps(
        {
            "title": "GrowthLog",
            "body": str(payload.get("content") or payload.get("summary") or "GrowthLog提醒"),
            "url": url,
            "tag": str(payload.get("tag") or "growthlog-reminder"),
            "plan_count": payload.get("plan_count"),
            "urgent_count": payload.get("urgent_count"),
        },
        ensure_ascii=False,
    )
    vapid_claims = {"sub": (settings.web_push_vapid_subject or "mailto:admin@example.com").strip()}
    try:
        response = webpush(
            subscription_info=subscription,
            data=body,
            vapid_private_key=settings.web_push_vapid_private_key,
            vapid_claims=vapid_claims,
            ttl=60,
            timeout=15,
        )
        status = getattr(response, "status_code", None) or 201
        if 200 <= int(status) < 300:
            return WebPushSendResult(message_id=str(status))
        raise WebPushProviderError(f"HTTP_{status}", retryable=int(status) >= 500)
    except WebPushException as exc:
        status = None
        resp = getattr(exc, "response", None)
        if resp is not None:
            status = getattr(resp, "status_code", None)
        logger.warning("web_push_send_failed status=%s", status)
        if status in (404, 410):
            raise WebPushProviderError("SUBSCRIPTION_GONE", retryable=False, revoke=True) from exc
        if status == 429:
            raise WebPushProviderError("RATE_LIMITED", retryable=True) from exc
        if status is not None and int(status) >= 500:
            raise WebPushProviderError("SERVER_ERROR", retryable=True) from exc
        raise WebPushProviderError("PROVIDER_PROTOCOL_ERROR", retryable=False) from exc
    except TimeoutError as exc:
        raise WebPushProviderError("TIMEOUT", retryable=True) from exc
    except Exception as exc:  # noqa: BLE001
        name = type(exc).__name__.lower()
        if "timeout" in name:
            raise WebPushProviderError("TIMEOUT", retryable=True) from exc
        logger.warning("web_push_send_exception code=%s", type(exc).__name__)
        raise WebPushProviderError("PROVIDER_NETWORK", retryable=True) from exc

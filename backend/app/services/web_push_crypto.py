"""Encrypt / hash Web Push subscription JSON. Never log plaintext."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings


class WebPushCryptoError(RuntimeError):
    pass


def _decode_master_key(raw: str) -> bytes:
    text = (raw or "").strip()
    if not text:
        raise WebPushCryptoError("WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY_MISSING")
    for decoder in (base64.urlsafe_b64decode, base64.b64decode):
        try:
            pad = "=" * ((4 - len(text) % 4) % 4)
            key = decoder(text + pad)
            if len(key) == 32:
                return key
        except Exception:
            continue
    if len(text) == 64 and all(c in "0123456789abcdefABCDEF" for c in text):
        return bytes.fromhex(text)
    raise WebPushCryptoError("WEB_PUSH_SUBSCRIPTION_ENCRYPTION_KEY_INVALID")


def subscription_crypto_configured() -> bool:
    try:
        _decode_master_key(settings.web_push_subscription_encryption_key or "")
        return True
    except Exception:
        return False


def _derived_keys() -> tuple[bytes, bytes]:
    master = _decode_master_key(settings.web_push_subscription_encryption_key or "")
    aes_key = hmac.new(master, b"growthlog-webpush-aes-v1", hashlib.sha256).digest()
    mac_key = hmac.new(master, b"growthlog-webpush-hmac-v1", hashlib.sha256).digest()
    return aes_key, mac_key


def hash_endpoint(endpoint: str) -> str:
    _, mac_key = _derived_keys()
    return hmac.new(mac_key, endpoint.encode("utf-8"), hashlib.sha256).hexdigest()


def encrypt_subscription(subscription: dict[str, Any]) -> bytes:
    aes_key, _ = _derived_keys()
    raw = json.dumps(subscription, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(raw) > 3500:
        raise WebPushCryptoError("SUBSCRIPTION_TOO_LARGE")
    nonce = os.urandom(12)
    ct = AESGCM(aes_key).encrypt(nonce, raw, b"growthlog-webpush-v1")
    return nonce + ct


def decrypt_subscription(blob: bytes) -> dict[str, Any]:
    if not blob or len(blob) < 13:
        raise WebPushCryptoError("SUBSCRIPTION_CIPHERTEXT_INVALID")
    aes_key, _ = _derived_keys()
    nonce, ct = blob[:12], blob[12:]
    try:
        pt = AESGCM(aes_key).decrypt(nonce, ct, b"growthlog-webpush-v1")
    except Exception as exc:  # noqa: BLE001
        raise WebPushCryptoError("SUBSCRIPTION_DECRYPT_FAILED") from exc
    data = json.loads(pt.decode("utf-8"))
    if not isinstance(data, dict):
        raise WebPushCryptoError("SUBSCRIPTION_JSON_INVALID")
    return data

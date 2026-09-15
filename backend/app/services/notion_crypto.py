"""Encrypt Notion tokens with AES-256-GCM. Never log plaintext."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings
from app.services.notion_errors import NOTION_NOT_CONFIGURED, NotionServiceError, public_message


PURPOSE_ACCESS = "growthlog-notion-access-v1"
PURPOSE_REFRESH = "growthlog-notion-refresh-v1"


class NotionCryptoError(RuntimeError):
    pass


def _decode_master_key(raw: str) -> bytes:
    text = (raw or "").strip()
    if not text:
        raise NotionCryptoError("NOTION_TOKEN_ENCRYPTION_KEY_MISSING")
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
    raise NotionCryptoError("NOTION_TOKEN_ENCRYPTION_KEY_INVALID")


def token_crypto_configured() -> bool:
    try:
        _decode_master_key(settings.notion_token_encryption_key or "")
        return True
    except Exception:
        return False


def _purpose_key(purpose: str) -> bytes:
    master = _decode_master_key(settings.notion_token_encryption_key or "")
    return hmac.new(master, purpose.encode("utf-8"), hashlib.sha256).digest()


def _aad(user_id: int, bot_id: str, workspace_id: str) -> bytes:
    return f"{int(user_id)}|{str(bot_id or '')}|{str(workspace_id or '')}".encode("utf-8")


def encrypt_token(plaintext: str, *, purpose: str, user_id: int, bot_id: str, workspace_id: str) -> bytes:
    if purpose not in {PURPOSE_ACCESS, PURPOSE_REFRESH}:
        raise NotionCryptoError("NOTION_TOKEN_PURPOSE_INVALID")
    raw = (plaintext or "").encode("utf-8")
    if not raw:
        raise NotionCryptoError("NOTION_TOKEN_EMPTY")
    nonce = os.urandom(12)
    ct = AESGCM(_purpose_key(purpose)).encrypt(nonce, raw, _aad(user_id, bot_id, workspace_id))
    return nonce + ct


def decrypt_token(blob: bytes, *, purpose: str, user_id: int, bot_id: str, workspace_id: str) -> str:
    if purpose not in {PURPOSE_ACCESS, PURPOSE_REFRESH}:
        raise NotionCryptoError("NOTION_TOKEN_PURPOSE_INVALID")
    if not blob or len(blob) < 13:
        raise NotionCryptoError("NOTION_TOKEN_CIPHERTEXT_INVALID")
    nonce, ct = blob[:12], blob[12:]
    try:
        pt = AESGCM(_purpose_key(purpose)).decrypt(nonce, ct, _aad(user_id, bot_id, workspace_id))
    except Exception as exc:  # noqa: BLE001
        raise NotionCryptoError("NOTION_TOKEN_DECRYPT_FAILED") from exc
    return pt.decode("utf-8")


def require_crypto_configured() -> None:
    if not token_crypto_configured():
        raise NotionServiceError(NOTION_NOT_CONFIGURED, public_message(NOTION_NOT_CONFIGURED), status_code=503)

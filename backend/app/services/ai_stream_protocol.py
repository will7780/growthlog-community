"""
R11.3 / R11.3-Fix SSE streaming protocol helpers.

- format_sse_event(): stable `event: ...\\ndata: ...\\n\\n` framing.
- Opaque AES-GCM ref_token (never Base64-readable JSON); purpose-isolated keys.
- Reference sanitization: strip reference_key / chunk_id / storage_path / model
  names; only display_index + opaque ref_token + safe display fields leave.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from typing import Any, Dict, Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.config import settings

# ---------------------------------------------------------------------------
# SSE event names
# ---------------------------------------------------------------------------
SSE_EVENT_META = "meta"
SSE_EVENT_STATUS = "status"
SSE_EVENT_SOURCE_CANDIDATE = "source_candidate"
SSE_EVENT_SOURCE_SET = "source_set"
SSE_EVENT_TEXT_DELTA = "text_delta"
SSE_EVENT_REFERENCE = "reference"
SSE_EVENT_DONE = "done"
SSE_EVENT_ERROR = "error"

# Status phase labels (safe, user-facing; no internal identifiers).
STATUS_RETRIEVING = "retrieving"
STATUS_AWAITING_SOURCE_CONFIRM = "awaiting_source_confirm"
STATUS_AWAITING_EXPANSION_CONFIRM = "awaiting_expansion_confirm"
STATUS_GENERATING = "generating"
STATUS_GROUNDING_CHECK = "grounding_check"

# Token purposes (key isolation + verify-time check).
PURPOSE_CANDIDATE_CONFIRM = "candidate_confirm"
PURPOSE_ANSWER_REFERENCE = "answer_reference"
PURPOSE_ORGANIZE_PREVIEW = "organize_preview"

TOKEN_VERSION = 1
ORGANIZE_PREVIEW_TTL_SECONDS = 2 * 60 * 60

# ---------------------------------------------------------------------------
# Stable stream error codes
# ---------------------------------------------------------------------------
STREAM_ABORTED = "STREAM_ABORTED"
STREAM_PROTOCOL_ERROR = "STREAM_PROTOCOL_ERROR"
STREAM_REQUEST_CONFLICT = "STREAM_REQUEST_CONFLICT"
STREAM_REQUEST_IN_PROGRESS = "STREAM_REQUEST_IN_PROGRESS"
STREAM_REQUEST_ID_INVALID = "STREAM_REQUEST_ID_INVALID"
STREAM_GROUNDING_FAILED = "STREAM_GROUNDING_FAILED"
SOURCE_SET_STALE = "SOURCE_SET_STALE"
SOURCE_REFERENCE_INVALID = "SOURCE_REFERENCE_INVALID"

_DEFAULT_ERROR_MESSAGES: Dict[str, str] = {
    STREAM_ABORTED: "生成已取消。",
    STREAM_PROTOCOL_ERROR: "流式协议异常，请重试。",
    STREAM_REQUEST_CONFLICT: "该 request_id 已绑定不同内容的请求，请使用新的 request_id。",
    STREAM_REQUEST_IN_PROGRESS: "相同请求正在生成中，请稍后重试。",
    STREAM_REQUEST_ID_INVALID: "request_id 格式无效。",
    STREAM_GROUNDING_FAILED: "生成内容未通过引用校验，请重试。",
    SOURCE_SET_STALE: "固定来源已失效，请新建讲解员对话。",
    SOURCE_REFERENCE_INVALID: "所选来源令牌无效。",
}


def default_error_message(code: str) -> str:
    return _DEFAULT_ERROR_MESSAGES.get(code, "AI 服务暂时不可用，请稍后重试。")


def format_sse_event(event: str, data: Dict[str, Any]) -> str:
    """Render one SSE frame. `data` is JSON-encoded on a single line."""
    payload = json.dumps(data or {}, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n"


def build_error_event(code: str, message: Optional[str] = None) -> str:
    return format_sse_event(
        SSE_EVENT_ERROR,
        {"code": code, "message": message or default_error_message(code)},
    )


def build_meta_event(*, request_id: str, session_id: str, persona: str) -> str:
    return format_sse_event(
        SSE_EVENT_META,
        {"request_id": request_id, "session_id": session_id, "persona": persona},
    )


def build_status_event(status: str, **extra: Any) -> str:
    data: Dict[str, Any] = {"status": status}
    data.update({k: v for k, v in extra.items() if v is not None})
    return format_sse_event(SSE_EVENT_STATUS, data)


# ---------------------------------------------------------------------------
# Opaque AES-GCM reference token
# ---------------------------------------------------------------------------

def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _purpose_key(purpose: str) -> bytes:
    """Derive a 32-byte AES key isolated by purpose from the server secret."""
    material = f"growthlog:ref_token:v{TOKEN_VERSION}:{purpose}".encode("utf-8")
    secret = (settings.jwt_secret or "").encode("utf-8")
    return hashlib.sha256(secret + b"|" + material).digest()


def _encrypt_purpose_payload(purpose: str, payload: Dict[str, Any]) -> str:
    plaintext = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    nonce = os.urandom(12)
    aesgcm = AESGCM(_purpose_key(purpose))
    ciphertext = aesgcm.encrypt(nonce, plaintext, None)
    blob = bytes([TOKEN_VERSION]) + nonce + ciphertext
    return _b64url_encode(blob)


def _decrypt_purpose_payload(token: str, purpose: str) -> Optional[Dict[str, Any]]:
    if not isinstance(token, str) or not token.strip():
        return None
    normalized = token.strip()
    try:
        raw = _b64url_decode(normalized)
    except Exception:
        return None
    # Reject alternate/non-canonical Base64 spellings of the same ciphertext.
    # Without this check, changing unused tail bits can leave decoded bytes
    # unchanged and make a text-level tamper appear valid.
    if _b64url_encode(raw) != normalized:
        return None
    if len(raw) < 1 + 12 + 16:
        return None
    version = raw[0]
    if version != TOKEN_VERSION:
        return None
    nonce = raw[1:13]
    ciphertext = raw[13:]
    try:
        aesgcm = AESGCM(_purpose_key(purpose))
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("v") or 0) != TOKEN_VERSION:
        return None
    if str(payload.get("purpose") or "") != purpose:
        return None
    return payload


def sign_reference_token(
    *,
    user_id: int,
    session_id: str,
    display_index: int,
    purpose: str,
    entry_id: Optional[int] = None,
    attachment_id: Optional[int] = None,
    reference_key: Optional[str] = None,
) -> str:
    """Stateless authenticated-encrypted opaque token (AES-GCM).

    Wire format: base64url(version || nonce || ciphertext+tag). Payload may
    include canonical reference_key only inside ciphertext — never on the wire
    as plaintext. Never log token or decrypted IDs.
    """
    if purpose not in {PURPOSE_CANDIDATE_CONFIRM, PURPOSE_ANSWER_REFERENCE}:
        raise ValueError("invalid_ref_token_purpose")
    key = str(reference_key).strip() if reference_key else None
    payload = {
        "v": TOKEN_VERSION,
        "purpose": purpose,
        "user_id": int(user_id),
        "session_id": str(session_id),
        "display_index": int(display_index),
        "entry_id": int(entry_id) if entry_id is not None else None,
        "attachment_id": int(attachment_id) if attachment_id is not None else None,
        "reference_key": key or None,
    }
    return _encrypt_purpose_payload(purpose, payload)


def sign_organize_preview_token(
    *,
    user_id: int,
    preview_id: str,
    goal: str,
    source_fingerprint: str,
    source_entry_ids: list,
    items: list,
    ttl_seconds: int = ORGANIZE_PREVIEW_TTL_SECONDS,
) -> str:
    """Opaque AES-GCM token binding organize preview trust boundary."""
    import time

    now = int(time.time())
    payload = {
        "v": TOKEN_VERSION,
        "purpose": PURPOSE_ORGANIZE_PREVIEW,
        "user_id": int(user_id),
        "preview_id": str(preview_id),
        "goal": str(goal or ""),
        "source_fingerprint": str(source_fingerprint),
        "source_entry_ids": [int(i) for i in source_entry_ids],
        "items": items,
        "iat": now,
        "exp": now + int(ttl_seconds),
    }
    return _encrypt_purpose_payload(PURPOSE_ORGANIZE_PREVIEW, payload)


def verify_organize_preview_token(
    token: str,
    *,
    user_id: int,
) -> Optional[Dict[str, Any]]:
    """Decrypt organize preview token; enforce user + expiry. Never log token."""
    import time

    payload = _decrypt_purpose_payload(token, PURPOSE_ORGANIZE_PREVIEW)
    if payload is None:
        return None
    try:
        if int(payload.get("user_id")) != int(user_id):
            return None
    except (TypeError, ValueError):
        return None
    try:
        exp = int(payload.get("exp") or 0)
    except (TypeError, ValueError):
        return None
    if exp < int(time.time()):
        return None
    return payload


def verify_reference_token(
    token: str,
    *,
    user_id: int,
    session_id: str,
    purpose: str,
) -> Optional[Dict[str, Any]]:
    """Decrypt + authenticate token; enforce purpose/user/session. Returns payload or None.

    Never logs token, plaintext, or source IDs.
    """
    if not isinstance(token, str) or not token.strip():
        return None
    if purpose not in {PURPOSE_CANDIDATE_CONFIRM, PURPOSE_ANSWER_REFERENCE}:
        return None
    normalized = token.strip()
    try:
        raw = _b64url_decode(normalized)
    except Exception:
        return None
    # Reject alternate/non-canonical Base64 spellings of the same ciphertext.
    # Without this check, changing unused tail bits can leave decoded bytes
    # unchanged and make a text-level tamper appear valid.
    if _b64url_encode(raw) != normalized:
        return None
    if len(raw) < 1 + 12 + 16:
        return None
    version = raw[0]
    if version != TOKEN_VERSION:
        return None
    nonce = raw[1:13]
    ciphertext = raw[13:]
    try:
        aesgcm = AESGCM(_purpose_key(purpose))
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        payload = json.loads(plaintext.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("v") or 0) != TOKEN_VERSION:
        return None
    if str(payload.get("purpose") or "") != purpose:
        return None
    try:
        if int(payload.get("user_id")) != int(user_id):
            return None
    except (TypeError, ValueError):
        return None
    if str(payload.get("session_id") or "") != str(session_id):
        return None
    return payload


# ---------------------------------------------------------------------------
# Public reference sanitizers
# ---------------------------------------------------------------------------

def _safe_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _public_source_type(raw_source_type: str, *, attachment_id: Optional[int]) -> str:
    st = (raw_source_type or "").strip().lower()
    if st == "todo":
        return "todo"
    if st == "notion_page":
        return "notion_page"
    if st in {"attachment", "attachment_chunk"}:
        return "attachment"
    if st == "knowledge_source":
        return "attachment" if attachment_id is not None else "entry"
    return "entry"


def sanitize_reference_for_stream(
    ref: Dict[str, Any],
    *,
    user_id: int,
    session_id: str,
    display_index: int,
) -> Dict[str, Any]:
    """Sanitize a generation-reference dict for `reference` / `done` events."""
    from app.services.reference_identity import canonical_reference_key

    if not isinstance(ref, dict):
        ref = {}
    entry_id = _safe_int(ref.get("entry_id"))
    attachment_id = _safe_int(ref.get("attachment_id"))
    ref_key = canonical_reference_key(ref, index=max(0, int(display_index) - 1))
    token = sign_reference_token(
        user_id=user_id,
        session_id=session_id,
        display_index=display_index,
        purpose=PURPOSE_ANSWER_REFERENCE,
        entry_id=entry_id,
        attachment_id=attachment_id,
        reference_key=ref_key,
    )
    title = str(ref.get("title") or "").strip()[:120]
    snippet = str(ref.get("snippet") or ref.get("content") or "").strip()[:200]
    created_raw = ref.get("created_at")
    if hasattr(created_raw, "isoformat"):
        created_at = created_raw.isoformat()
    elif created_raw is None:
        created_at = None
    else:
        created_at = str(created_raw)
    return {
        "display_index": int(display_index),
        "ref_token": token,
        "source_type": _public_source_type(
            str(ref.get("source_type") or "entry"), attachment_id=attachment_id
        ),
        "title": title,
        "snippet": snippet,
        "created_at": created_at,
    }


def sanitize_candidate_for_stream(
    candidate: Dict[str, Any],
    *,
    user_id: int,
    session_id: str,
    display_index: int,
) -> Dict[str, Any]:
    """Sanitize an explainer/organizer candidate dict for `source_candidate` events."""
    from app.services.reference_identity import parse_proposal_reference_key

    if not isinstance(candidate, dict):
        candidate = {}
    entry_id: Optional[int] = None
    attachment_id: Optional[int] = None
    raw_key = str(candidate.get("reference_key") or "")
    parsed = parse_proposal_reference_key(raw_key) if raw_key else None
    if parsed is not None:
        source_type, _kind, object_id = parsed
        if source_type == "entry":
            entry_id = _safe_int(object_id)
        elif source_type == "attachment":
            attachment_id = _safe_int(object_id)
    family_root_entry_id = _safe_int(candidate.get("family_root_entry_id"))
    if entry_id is None:
        entry_id = family_root_entry_id

    token = sign_reference_token(
        user_id=user_id,
        session_id=session_id,
        display_index=display_index,
        purpose=PURPOSE_CANDIDATE_CONFIRM,
        entry_id=entry_id,
        attachment_id=attachment_id,
        reference_key=raw_key or None,
    )
    title = str(candidate.get("title") or "").strip()[:120]
    snippet = str(candidate.get("snippet") or "").strip()[:200]
    return {
        "display_index": int(display_index),
        "ref_token": token,
        "source_type": _public_source_type(
            str(candidate.get("source_type") or "entry"), attachment_id=attachment_id
        ),
        "title": title,
        "snippet": snippet,
    }


def sanitize_references_for_stream(
    refs: Any,
    *,
    user_id: int,
    session_id: str,
    start_index: int = 1,
) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    for offset, ref in enumerate(refs or []):
        if not isinstance(ref, dict):
            continue
        out.append(
            sanitize_reference_for_stream(
                ref,
                user_id=user_id,
                session_id=session_id,
                display_index=start_index + offset,
            )
        )
    return out


def sanitize_candidates_for_stream(
    candidates: Any,
    *,
    user_id: int,
    session_id: str,
    start_index: int = 1,
) -> list[Dict[str, Any]]:
    out: list[Dict[str, Any]] = []
    for offset, candidate in enumerate(candidates or []):
        if not isinstance(candidate, dict):
            continue
        out.append(
            sanitize_candidate_for_stream(
                candidate,
                user_id=user_id,
                session_id=session_id,
                display_index=start_index + offset,
            )
        )
    return out

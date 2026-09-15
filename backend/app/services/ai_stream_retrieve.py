"""Retriever persona SSE using the 11.8 single-selection result."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.services.ai_retrieval_service import (
    LOOKUP_CANDIDATE_BUILD_FAILED,
    LOOKUP_GROUNDING_FAILED,
    LOOKUP_NOT_FOUND,
    RetrievalServiceError,
    run_shared_retrieval_turn,
    safe_retrieval_user_message,
)
from app.services.ai_session import get_session_metadata
from app.services.ai_session_constants import PERSONA_RETRIEVER
from app.services.ai_stream_claim import StreamClaimGuard
from app.services.ai_stream_idempotency import (
    StreamIdempotencyError,
    ensure_no_conflict,
    find_completed_turn,
    validate_request_id,
)
from app.services.ai_stream_protocol import (
    SSE_EVENT_DONE,
    SSE_EVENT_REFERENCE,
    SSE_EVENT_TEXT_DELTA,
    STATUS_GENERATING,
    STATUS_GROUNDING_CHECK,
    STATUS_RETRIEVING,
    STREAM_PROTOCOL_ERROR,
    build_error_event,
    build_meta_event,
    build_status_event,
    format_sse_event,
    sanitize_reference_for_stream,
)
from app.services.reference_identity import annotate_with_canonical_keys

logger = logging.getLogger(__name__)
CITATION_RE = re.compile(r"〔(\d+)〕")


def _require_retriever(db: Session, user_id: int, session_id: str) -> bool:
    meta = get_session_metadata(db, user_id, session_id)
    return meta is not None and meta.persona == PERSONA_RETRIEVER


def _extract_cited_indices(answer_text: str, allowed_count: int) -> Tuple[bool, List[int]]:
    seen: List[int] = []
    for raw in CITATION_RE.findall(answer_text or ""):
        try:
            index = int(raw)
        except ValueError:
            return False, []
        if index < 1 or index > allowed_count:
            return False, []
        if index not in seen:
            seen.append(index)
    return True, seen


def _text_chunks(text: str) -> List[str]:
    """Emit readable incremental chunks without altering persisted text."""
    lines = text.splitlines(keepends=True)
    if not lines:
        return [text] if text else []
    return lines


async def stream_retriever_turn(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    message: str,
    request_id: str,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> AsyncIterator[str]:
    """Yield one independent retriever turn as SSE.

    Retrieval performs one selector LLM request. The resulting cited list is
    streamed and persisted directly; no second answer-model request exists.
    """
    if not _require_retriever(db, user_id, session_id):
        yield build_error_event(
            "SESSION_PERSONA_MISMATCH",
            "当前请求与会话角色不匹配，请新建对应角色的对话。",
        )
        return

    text = (message or "").strip()
    if not text:
        yield build_error_event("EMPTY_MESSAGE", "消息不能为空。")
        return

    try:
        rid = validate_request_id(request_id)
    except StreamIdempotencyError as exc:
        yield build_error_event(exc.code, exc.message)
        return

    yield build_meta_event(
        request_id=rid,
        session_id=session_id,
        persona=PERSONA_RETRIEVER,
    )

    existing = find_completed_turn(db, user_id, session_id, rid)
    try:
        ensure_no_conflict(existing, message_text=text)
    except StreamIdempotencyError as exc:
        yield build_error_event(exc.code, exc.message)
        return

    def _replay(assistant_msg: Any) -> List[str]:
        refs = list(assistant_msg.references or [])
        answer = str(assistant_msg.content or "")
        frames = [format_sse_event(SSE_EVENT_TEXT_DELTA, {"text": answer})]
        for ref in refs:
            if isinstance(ref, dict):
                frames.append(format_sse_event(SSE_EVENT_REFERENCE, ref))
        frames.append(
            format_sse_event(
                SSE_EVENT_DONE,
                {
                    "request_id": rid,
                    "message": answer,
                    "references": refs,
                    "idempotent": True,
                },
            )
        )
        return frames

    if existing is not None and existing.get("assistant_msg") is not None:
        for frame in _replay(existing["assistant_msg"]):
            yield frame
        return

    try:
        async with StreamClaimGuard(
            db,
            user_id=user_id,
            session_id=session_id,
            request_id=rid,
            message_text=text,
        ) as guard:
            if guard.replay:
                again = find_completed_turn(db, user_id, session_id, rid)
                if again is not None and again.get("assistant_msg") is not None:
                    for frame in _replay(again["assistant_msg"]):
                        yield frame
                return

            yield build_status_event(STATUS_RETRIEVING)
            try:
                retrieval = await guard.await_provider(run_shared_retrieval_turn(
                    db,
                    user_id=user_id,
                    query=text,
                    model_key=model_key,
                    fallback_candidates=fallback_candidates,
                    generate_answer=False,
                    answer_style="retriever",
                ))
            except asyncio.CancelledError:
                raise
            except StreamIdempotencyError as exc:
                yield build_error_event(exc.code, exc.message)
                return
            except RetrievalServiceError as exc:
                logger.warning(
                    "stream_retriever retrieval failed code=%s cause=%s",
                    exc.code,
                    type(exc.__cause__).__name__ if exc.__cause__ else "none",
                )
                yield build_error_event(
                    exc.code,
                    safe_retrieval_user_message(exc.code),
                )
                return
            except Exception as exc:
                logger.warning(
                    "stream_retriever candidate/build failed cause=%s",
                    type(exc).__name__,
                )
                yield build_error_event(
                    LOOKUP_CANDIDATE_BUILD_FAILED,
                    safe_retrieval_user_message(LOOKUP_CANDIDATE_BUILD_FAILED),
                )
                return

            try:
                guard.ensure_owned()
            except StreamIdempotencyError as exc:
                yield build_error_event(exc.code, exc.message)
                return

            answer_text = str(retrieval.get("answer") or LOOKUP_NOT_FOUND).strip()
            source_refs = list(retrieval.get("used_references") or [])
            annotated = annotate_with_canonical_keys(source_refs)

            yield build_status_event(STATUS_GENERATING)
            for chunk in _text_chunks(answer_text):
                yield format_sse_event(SSE_EVENT_TEXT_DELTA, {"text": chunk})

            yield build_status_event(STATUS_GROUNDING_CHECK)
            ok, cited_indices = _extract_cited_indices(answer_text, len(annotated))
            found = bool(retrieval.get("found"))
            if not ok or (found and (not annotated or not cited_indices)):
                yield build_error_event(
                    LOOKUP_GROUNDING_FAILED,
                    safe_retrieval_user_message(LOOKUP_GROUNDING_FAILED),
                )
                return

            used_refs_public: List[Dict[str, Any]] = []
            for index in cited_indices:
                used_refs_public.append(
                    sanitize_reference_for_stream(
                        annotated[index - 1],
                        user_id=user_id,
                        session_id=session_id,
                        display_index=index,
                    )
                )

            try:
                saved = guard.finalize_turn(
                    user_content=text,
                    assistant_content=answer_text,
                    assistant_references=used_refs_public,
                )
            except StreamIdempotencyError as exc:
                yield build_error_event(exc.code, exc.message)
                return
            except Exception:
                db.rollback()
                logger.exception("stream_retriever_turn save failed")
                yield build_error_event(STREAM_PROTOCOL_ERROR)
                return

            for ref in used_refs_public:
                yield format_sse_event(SSE_EVENT_REFERENCE, ref)
            yield format_sse_event(
                SSE_EVENT_DONE,
                {
                    "request_id": rid,
                    "message": answer_text,
                    "references": used_refs_public,
                    "idempotent": bool(saved.get("idempotent")),
                },
            )
    except asyncio.CancelledError:
        raise
    except StreamIdempotencyError as exc:
        yield build_error_event(exc.code, exc.message)
"""
Explainer persona streaming turn (R11.3 / R11.3-Fix).

- Candidate path: source_candidate only (no text_delta); bind request_id to pending user.
- Locked answer path: source_set (all fixed) → text_delta → used reference subset → done.
- Resume path: POST explain/resume/stream after confirm; persist assistant only.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, AsyncIterator, Dict, Iterator, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.services.ai_explainer import (
    orchestrate_initial_source_candidates,
    raise_or_return_active_proposal,
    resume_proposal,
)
from app.services.ai_persona_context import build_explainer_generation_context
from app.services.ai_session import get_session_metadata
from app.services.ai_session_constants import PERSONA_EXPLAINER
from app.services.ai_source_set import SourceSetError, resolve_confirmed_proposal_version
from app.services.ai_stream_claim import StreamClaimGuard
from app.services.ai_stream_idempotency import (
    STREAM_REQUEST_CONFLICT,
    StreamIdempotencyError,
    ensure_no_conflict,
    find_completed_turn,
    validate_request_id,
)
from app.services.ai_stream_protocol import (
    SSE_EVENT_DONE,
    SSE_EVENT_REFERENCE,
    SSE_EVENT_SOURCE_CANDIDATE,
    SSE_EVENT_SOURCE_SET,
    SSE_EVENT_TEXT_DELTA,
    STATUS_GENERATING,
    STATUS_GROUNDING_CHECK,
    STREAM_GROUNDING_FAILED,
    STREAM_PROTOCOL_ERROR,
    build_error_event,
    build_meta_event,
    build_status_event,
    format_sse_event,
    sanitize_candidates_for_stream,
    sanitize_reference_for_stream,
)
from app.services.llm_gateway import LLMGatewayError, stream_chat_completion

logger = logging.getLogger(__name__)

MAX_ANSWER_TOKENS = 4000
CITATION_RE = re.compile(r"〔(\d+)〕")
_REFUSAL_HINTS = (
    "资料不足",
    "不足以回答",
    "无法依据",
    "没有足够",
    "范围不够",
    "无法回答",
    "不足以支持",
)

EXPLAINER_STREAM_SYSTEM_PROMPT = (
    "你是 GrowthLog 讲解员。只能依据下面按编号给出的固定来源回答，不得使用模型常识杜撰事实，"
    "也不得引入固定来源之外的内容。\n"
    "直接输出自然语言正文：不要输出 JSON，不要使用 markdown 代码块。\n"
    "引用格式：陈述由某条来源支持的地方，使用〔n〕标注对应编号（n 为下面给出的编号，从 1 开始）。\n"
    "只能使用下面给出的编号；不得编造不存在的编号；不得输出 reference_key、chunk_id、entry_id、"
    "attachment_id、文件路径或任何内部字段名。\n"
    "当前来源不足以回答时，明确说明资料范围不够，不要杜撰，也不要引用不支持的编号。"
)


def _require_explainer(db: Session, user_id: int, session_id: str) -> bool:
    meta = get_session_metadata(db, user_id, session_id)
    return meta is not None and meta.persona == PERSONA_EXPLAINER


def _build_numbered_evidence_context(evidence: List[Any]) -> str:
    parts: List[str] = []
    for idx, block in enumerate(evidence, start=1):
        parts.append(f"[{idx}] {block.title}\n{block.text}")
    return "\n\n".join(parts)


def _evidence_to_public_dict(block: Any) -> Dict[str, Any]:
    return {
        "entry_id": block.entry_id,
        "attachment_id": block.attachment_id,
        "title": block.title,
        "snippet": (block.text or "")[:200],
        "source_type": block.source_type,
        "reference_key": getattr(block, "reference_key", None),
        "created_at": None,
    }


def _extract_cited_indices(answer_text: str, allowed_count: int) -> Tuple[bool, List[int]]:
    seen: List[int] = []
    for raw in CITATION_RE.findall(answer_text or ""):
        try:
            idx = int(raw)
        except ValueError:
            return False, []
        if idx < 1 or idx > allowed_count:
            return False, []
        if idx not in seen:
            seen.append(idx)
    return True, seen


def _looks_like_refusal(answer_text: str) -> bool:
    text = (answer_text or "").strip()
    if not text:
        return False
    return any(h in text for h in _REFUSAL_HINTS)


def _validate_grounding(answer_text: str, allowed_count: int) -> Tuple[bool, List[int]]:
    ok, cited = _extract_cited_indices(answer_text, allowed_count)
    if not ok:
        return False, []
    if cited:
        return True, cited
    # No citations: only allow clear refusal / insufficient-evidence answers.
    if _looks_like_refusal(answer_text):
        return True, []
    if allowed_count > 0:
        return False, []
    return True, []


def _emit_candidate_frames(
    payload: Dict[str, Any],
    *,
    request_id: Optional[str],
    user_id: int,
    session_id: str,
) -> Iterator[str]:
    phase = str(payload.get("phase") or "awaiting_source_confirm")
    yield build_status_event(phase)
    sanitized = sanitize_candidates_for_stream(
        payload.get("candidates") or [], user_id=user_id, session_id=session_id
    )
    for candidate in sanitized:
        yield format_sse_event(SSE_EVENT_SOURCE_CANDIDATE, candidate)
    done_payload: Dict[str, Any] = {
        "request_id": request_id,
        "proposal_id": payload.get("proposal_id"),
        "base_version": payload.get("base_version"),
        "phase": phase,
        "message": str(payload.get("message") or ""),
        "answer": None,
        "candidates": sanitized,
    }
    if payload.get("expansion_topic"):
        done_payload["expansion_topic"] = payload.get("expansion_topic")
    yield format_sse_event(SSE_EVENT_DONE, done_payload)


def _replay_completed_answer(
    *,
    assistant: Any,
    rid: str,
    current_version: int,
    evidence_public: Optional[List[Dict[str, Any]]] = None,
) -> Iterator[str]:
    used_refs = [r for r in (assistant.references or []) if isinstance(r, dict)]
    members = evidence_public if evidence_public is not None else used_refs
    version = assistant.source_set_version or current_version
    yield format_sse_event(SSE_EVENT_SOURCE_SET, {"version": version, "members": members})
    yield build_status_event(STATUS_GENERATING)
    yield format_sse_event(SSE_EVENT_TEXT_DELTA, {"text": str(assistant.content or "")})
    for ref in used_refs:
        yield format_sse_event(SSE_EVENT_REFERENCE, ref)
    yield format_sse_event(
        SSE_EVENT_DONE,
        {
            "request_id": rid,
            "message": str(assistant.content or ""),
            "references": used_refs,
            "source_set_version": version,
            "proposal_id": getattr(assistant, "proposal_id", None),
            "idempotent": True,
        },
    )


def _bind_resume_request_id(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    proposal_id: int,
    pending_user: Any,
    request_id: str,
) -> str:
    """Canonical resume request_id: reuse pending user binding; atomic bind if NULL."""
    from app.models import AIConversation

    locked = (
        db.query(AIConversation)
        .filter(
            AIConversation.id == int(pending_user.id),
            AIConversation.user_id == int(user_id),
            AIConversation.session_id == str(session_id),
            AIConversation.proposal_id == int(proposal_id),
            AIConversation.role == "user",
        )
        .with_for_update()
        .first()
    )
    if locked is None:
        db.rollback()
        raise StreamIdempotencyError("NO_PENDING_QUESTION", "没有待继续回答的问题。")
    existing = locked.request_id
    if existing:
        if str(existing) != str(request_id):
            db.rollback()
            raise StreamIdempotencyError(
                STREAM_REQUEST_CONFLICT,
                "该 proposal 已绑定不同的 request_id，请使用原 request_id 继续。",
            )
        db.commit()
        return str(existing)
    locked.request_id = str(request_id)
    db.commit()
    return str(request_id)


async def _stream_answer_over_evidence(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    text: str,
    rid: str,
    ctx: Any,
    model_key: Optional[str],
    fallback_candidates: Optional[List[str]],
    persist_user: bool,
    proposal_id: Optional[int],
) -> AsyncIterator[str]:
    current_version = int(ctx.source_set_version)
    evidence_public = [
        sanitize_reference_for_stream(
            _evidence_to_public_dict(block),
            user_id=user_id,
            session_id=session_id,
            display_index=idx + 1,
        )
        for idx, block in enumerate(ctx.evidence)
    ]

    try:
        async with StreamClaimGuard(
            db,
            user_id=user_id,
            session_id=session_id,
            request_id=rid,
            message_text=text,
        ) as guard:
            if guard.replay:
                existing = find_completed_turn(db, user_id, session_id, rid)
                if existing and existing.get("assistant_msg") is not None:
                    for frame in _replay_completed_answer(
                        assistant=existing["assistant_msg"],
                        rid=rid,
                        current_version=current_version,
                        evidence_public=evidence_public,
                    ):
                        yield frame
                return

            yield format_sse_event(
                SSE_EVENT_SOURCE_SET, {"version": current_version, "members": evidence_public}
            )
            yield build_status_event(STATUS_GENERATING)

            messages: List[Dict[str, str]] = []
            for m in ctx.conversation_history[:-1]:
                if m.get("role") in ("user", "assistant"):
                    messages.append(
                        {"role": str(m["role"]), "content": str(m.get("content") or "")}
                    )
            numbered_context = _build_numbered_evidence_context(ctx.evidence)
            user_prompt = (
                f"固定来源（编号从 1 到 {len(ctx.evidence)}；只能使用这些来源作答）："
                f"\n{numbered_context}\n\n"
                f"用户问题：{text}\n\n"
                "请直接输出正文，引用处用〔n〕标注对应编号。"
            )
            messages.append({"role": "user", "content": user_prompt})
            system_prompt = f"{ctx.system_policy}\n\n{EXPLAINER_STREAM_SYSTEM_PROMPT}"

            content_parts: List[str] = []
            try:
                async for kind, chunk in guard.iter_provider(
                    stream_chat_completion(
                        model_key=model_key,
                        system=system_prompt,
                        messages=messages,
                        mode="retrieval",
                        max_tokens=MAX_ANSWER_TOKENS,
                        temperature=0.3,
                        user_id=user_id,
                        fallback_candidates=fallback_candidates,
                    )
                ):
                    if kind == "delta" and isinstance(chunk, str):
                        content_parts.append(chunk)
                        yield format_sse_event(SSE_EVENT_TEXT_DELTA, {"text": chunk})
            except asyncio.CancelledError:
                raise
            except StreamIdempotencyError as exc:
                yield build_error_event(exc.code, exc.message)
                return
            except LLMGatewayError as exc:
                yield build_error_event(exc.code, "AI 服务暂时不可用，请稍后重试。")
                return
            except Exception:
                logger.exception("stream_explainer generation failed")
                yield build_error_event(STREAM_PROTOCOL_ERROR)
                return

            try:
                guard.ensure_owned()
            except StreamIdempotencyError as exc:
                yield build_error_event(exc.code, exc.message)
                return

            answer_text = "".join(content_parts).strip()
            if not answer_text:
                yield build_error_event(STREAM_PROTOCOL_ERROR, "AI 未生成有效回答，请稍后重试。")
                return

            yield build_status_event(STATUS_GROUNDING_CHECK)
            ok, cited = _validate_grounding(answer_text, len(ctx.evidence))
            if not ok:
                yield build_error_event(STREAM_GROUNDING_FAILED)
                return

            used_refs = [evidence_public[i - 1] for i in cited]
            for ref in used_refs:
                yield format_sse_event(SSE_EVENT_REFERENCE, ref)

            try:
                saved = guard.finalize_turn(
                    user_content=text,
                    assistant_content=answer_text,
                    assistant_references=used_refs,
                    source_set_version=current_version,
                    proposal_id=proposal_id,
                    persist_user=persist_user,
                )
            except StreamIdempotencyError as exc:
                yield build_error_event(exc.code, exc.message)
                return
            except Exception:
                db.rollback()
                logger.exception("stream_explainer save failed")
                yield build_error_event(STREAM_PROTOCOL_ERROR)
                return

            # done only after atomic finalize succeeded
            yield format_sse_event(
                SSE_EVENT_DONE,
                {
                    "request_id": rid,
                    "message": answer_text,
                    "references": used_refs,
                    "source_set_version": current_version,
                    "proposal_id": proposal_id,
                    "idempotent": bool(saved.get("idempotent")),
                },
            )
    except asyncio.CancelledError:
        raise
    except StreamIdempotencyError as exc:
        yield build_error_event(exc.code, exc.message)


async def stream_explainer_turn(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    message: str,
    request_id: str,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> AsyncIterator[str]:
    if not _require_explainer(db, user_id, session_id):
        yield build_error_event(
            "SESSION_PERSONA_MISMATCH", "当前请求与会话角色不匹配，请新建对应角色的对话。"
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

    yield build_meta_event(request_id=rid, session_id=session_id, persona=PERSONA_EXPLAINER)

    try:
        early = raise_or_return_active_proposal(
            db, user_id=user_id, session_id=session_id, query=text
        )
    except SourceSetError as exc:
        yield build_error_event(exc.code, exc.message)
        return
    if early is not None:
        for frame in _emit_candidate_frames(
            early, request_id=rid, user_id=user_id, session_id=session_id
        ):
            yield frame
        return

    meta = get_session_metadata(db, user_id, session_id)
    if meta is None:
        yield build_error_event("SESSION_NOT_FOUND", "会话不存在或无权访问。")
        return

    if meta.current_source_set_version is None:
        try:
            payload = await orchestrate_initial_source_candidates(
                db,
                user_id=user_id,
                session_id=session_id,
                query=text,
                request_id=rid,
                use_query_planner=True,
                model_key=model_key,
                fallback_candidates=fallback_candidates,
            )
        except SourceSetError as exc:
            yield build_error_event(exc.code, exc.message)
            return
        except Exception:
            logger.exception("stream_explainer_turn initial candidates failed")
            yield build_error_event(STREAM_PROTOCOL_ERROR)
            return
        for frame in _emit_candidate_frames(
            payload, request_id=rid, user_id=user_id, session_id=session_id
        ):
            yield frame
        return

    try:
        ctx = build_explainer_generation_context(db, user_id, session_id, current_query=text)
    except SourceSetError as exc:
        yield build_error_event(exc.code, exc.message)
        return
    except Exception:
        logger.exception("stream_explainer_turn context build failed")
        yield build_error_event(STREAM_PROTOCOL_ERROR)
        return

    current_version = int(ctx.source_set_version)
    evidence_public = [
        sanitize_reference_for_stream(
            _evidence_to_public_dict(block),
            user_id=user_id,
            session_id=session_id,
            display_index=idx + 1,
        )
        for idx, block in enumerate(ctx.evidence)
    ]

    existing = find_completed_turn(db, user_id, session_id, rid)
    try:
        ensure_no_conflict(existing, message_text=text)
    except StreamIdempotencyError as exc:
        yield build_error_event(exc.code, exc.message)
        return

    if existing is not None and existing.get("assistant_msg") is not None:
        for frame in _replay_completed_answer(
            assistant=existing["assistant_msg"],
            rid=rid,
            current_version=current_version,
            evidence_public=evidence_public,
        ):
            yield frame
        return

    # Once v1 is locked, every chat turn answers over the current source set.
    # Source expansion is an explicit UI action handled by the expand endpoint.
    async for frame in _stream_answer_over_evidence(
        db,
        user_id=user_id,
        session_id=session_id,
        text=text,
        rid=rid,
        ctx=ctx,
        model_key=model_key,
        fallback_candidates=fallback_candidates,
        persist_user=True,
        proposal_id=None,
    ):
        yield frame


async def stream_explainer_resume(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    proposal_id: int,
    request_id: str,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> AsyncIterator[str]:
    """SSE resume after source confirm — assistant only; user already persisted."""
    if not _require_explainer(db, user_id, session_id):
        yield build_error_event(
            "SESSION_PERSONA_MISMATCH", "当前请求与会话角色不匹配，请新建对应角色的对话。"
        )
        return

    try:
        client_rid = validate_request_id(request_id)
    except StreamIdempotencyError as exc:
        yield build_error_event(exc.code, exc.message)
        return

    try:
        _proposal, target_version = resolve_confirmed_proposal_version(
            db, user_id, session_id, int(proposal_id)
        )
    except SourceSetError as exc:
        yield build_error_event(exc.code, exc.message)
        return

    from app.models import AIConversation

    existing_assistant = (
        db.query(AIConversation)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == session_id,
            AIConversation.proposal_id == int(proposal_id),
            AIConversation.role == "assistant",
        )
        .order_by(AIConversation.id.asc())
        .first()
    )
    pending_user = (
        db.query(AIConversation)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == session_id,
            AIConversation.proposal_id == int(proposal_id),
            AIConversation.role == "user",
        )
        .order_by(AIConversation.id.asc())
        .first()
    )
    if pending_user is None:
        yield build_error_event("NO_PENDING_QUESTION", "没有待继续回答的问题。")
        return

    text = str(pending_user.content or "").strip()
    try:
        rid = _bind_resume_request_id(
            db,
            user_id=user_id,
            session_id=session_id,
            proposal_id=int(proposal_id),
            pending_user=pending_user,
            request_id=client_rid,
        )
    except StreamIdempotencyError as exc:
        yield build_error_event(exc.code, exc.message)
        return

    # meta / done / DB share the same canonical request_id.
    yield build_meta_event(request_id=rid, session_id=session_id, persona=PERSONA_EXPLAINER)

    existing_turn = find_completed_turn(db, user_id, session_id, rid)
    try:
        ensure_no_conflict(existing_turn, message_text=text)
    except StreamIdempotencyError as exc:
        yield build_error_event(exc.code, exc.message)
        return

    if existing_assistant is not None:
        replay_rid = str(existing_assistant.request_id or rid)
        if replay_rid != rid:
            yield build_error_event(
                STREAM_REQUEST_CONFLICT,
                "该 proposal 已绑定不同的 request_id，请使用原 request_id 继续。",
            )
            return
        try:
            ctx = build_explainer_generation_context(db, user_id, session_id, current_query=text)
            evidence_public = [
                sanitize_reference_for_stream(
                    _evidence_to_public_dict(block),
                    user_id=user_id,
                    session_id=session_id,
                    display_index=idx + 1,
                )
                for idx, block in enumerate(ctx.evidence)
            ]
        except Exception:
            evidence_public = None
        for frame in _replay_completed_answer(
            assistant=existing_assistant,
            rid=replay_rid,
            current_version=int(existing_assistant.source_set_version or target_version),
            evidence_public=evidence_public,
        ):
            yield frame
        return

    if existing_turn is not None and existing_turn.get("assistant_msg") is not None:
        for frame in _replay_completed_answer(
            assistant=existing_turn["assistant_msg"],
            rid=rid,
            current_version=int(target_version),
        ):
            yield frame
        return

    meta = get_session_metadata(db, user_id, session_id)
    if meta is None or meta.current_source_set_version is None:
        yield build_error_event("SOURCE_SET_NOT_LOCKED", "请先确认来源后再继续讲解。")
        return
    if int(meta.current_source_set_version) != int(target_version):
        yield build_error_event("SOURCE_SET_VERSION_CONFLICT", "来源版本已变化，请刷新后重试。")
        return

    try:
        ctx = build_explainer_generation_context(db, user_id, session_id, current_query=text)
    except SourceSetError as exc:
        yield build_error_event(exc.code, exc.message)
        return

    async for frame in _stream_answer_over_evidence(
        db,
        user_id=user_id,
        session_id=session_id,
        text=text,
        rid=rid,
        ctx=ctx,
        model_key=model_key,
        fallback_candidates=fallback_candidates,
        persist_user=False,
        proposal_id=int(proposal_id),
    ):
        yield frame


# Keep non-stream resume import surface for compatibility tests.
__all__ = [
    "stream_explainer_turn",
    "stream_explainer_resume",
    "resume_proposal",
]

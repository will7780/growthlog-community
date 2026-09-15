"""
Explainer persona orchestration (R11.2-Fix3).

Unified initial orchestration for source-candidates / first chat.
Proposal-bound resume for initial v1 and expansion v2+.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import AIChatSession, AIConversation
from app.services.ai_chat import save_message
from app.services.ai_expansion_intent import (
    ACTION_EXPAND,
    ExpansionIntentError,
    derive_expansion_topic_from_user_content,
    validate_expansion_topic,
)
from app.services.ai_persona_context import (
    build_explainer_generation_context,
    evidence_to_generation_references,
)
from app.services.ai_session import get_session_metadata, touch_session_activity
from app.services.ai_session_constants import PERSONA_EXPLAINER, SessionPersonaError
from app.services.ai_source_set import (
    SourceMemberIdentity,
    SourceSetError,
    confirm_proposal,
    create_expansion_proposal,
    create_initial_proposal,
    get_active_proposed,
    get_locked_manifest,
    parse_manifest,
    resolve_confirmed_proposal_version,
)
from app.services.ai_summary import generate_structured_summary
from app.services.entry_family import resolve_family_from_hit, resolve_root_entry_id
from app.services.lookup_all_candidates import _attach_judge_evidence
from app.services.query_normalize import extract_retrieval_topic
from app.services.rag_pipeline import expand_references_for_generation, retrieve_rag_context
from app.services.reference_identity import canonical_reference_key, parse_proposal_reference_key
from app.services.relevance_ranker import select_rag_admission_summaries
from app.services.retrieval_plan import RetrievalPlanResult, build_retrieval_plan
from app.services.source_descriptor import filter_references_for_plan
from app.services.source_discovery import discover_source_scope, discover_sources

logger = logging.getLogger(__name__)


def _require_explainer(db: Session, user_id: int, session_id: str):
    meta = get_session_metadata(db, user_id, session_id)
    if meta is None:
        raise SessionPersonaError("SESSION_NOT_FOUND", "会话不存在或无权访问。")
    if meta.persona != PERSONA_EXPLAINER:
        raise SessionPersonaError(
            "SESSION_PERSONA_MISMATCH",
            "当前请求与会话角色不匹配，请新建对应角色的对话。",
        )
    return meta


def _lock_explainer_session(db: Session, user_id: int, session_id: str) -> AIChatSession:
    row = (
        db.query(AIChatSession)
        .filter(
            AIChatSession.user_id == user_id,
            AIChatSession.session_id == session_id,
        )
        .with_for_update()
        .first()
    )
    if row is None:
        raise SourceSetError("SESSION_NOT_FOUND", "会话不存在或无权访问。")
    if row.persona != PERSONA_EXPLAINER:
        raise SessionPersonaError(
            "SESSION_PERSONA_MISMATCH",
            "当前请求与会话角色不匹配，请新建对应角色的对话。",
        )
    return row


def _safe_candidate_item(
    *,
    reference_key: str,
    source_type: str,
    title: str,
    snippet: str,
    family_root_entry_id: Optional[int],
    display_order: int,
) -> Dict[str, Any]:
    return {
        "reference_key": reference_key,
        "source_type": source_type,
        "title": title[:120],
        "snippet": (snippet or "")[:200],
        "family_root_entry_id": family_root_entry_id,
        "display_order": display_order,
    }


def _family_member_keys(
    db: Session,
    user_id: int,
    hit: Dict[str, Any],
) -> List[SourceMemberIdentity]:
    if str(hit.get("source_type") or "") == "notion_page":
        page_id = hit.get("notion_page_id") or hit.get("source_id")
        try:
            page_id = int(page_id)
        except (TypeError, ValueError):
            return []
        return [SourceMemberIdentity(reference_key=f"notion_page:source:{page_id}", display_order=0)]

    if str(hit.get("source_type") or "") == "todo":
        from app.services.todo_hierarchy import family_root_id, load_index, subtree_ids

        todo_id = hit.get("todo_id") or hit.get("source_id")
        try:
            todo_id = int(todo_id)
        except (TypeError, ValueError):
            return []
        index = load_index(db, int(user_id))
        if todo_id not in index.by_id:
            return []
        root_id = family_root_id(todo_id, index)
        return [
            SourceMemberIdentity(reference_key=f"todo:source:{member_id}", display_order=order)
            for order, member_id in enumerate(subtree_ids(root_id, index))
        ]

    entry_id = hit.get("entry_id")
    attachment_id = hit.get("attachment_id")
    try:
        eid = int(entry_id) if entry_id is not None else None
    except (TypeError, ValueError):
        eid = None
    try:
        aid = int(attachment_id) if attachment_id is not None else None
    except (TypeError, ValueError):
        aid = None

    family = resolve_family_from_hit(
        db,
        user_id,
        entry_id=eid,
        attachment_id=aid,
    )
    identities: List[SourceMemberIdentity] = []
    if family is None:
        key = canonical_reference_key(hit)
        parsed = parse_proposal_reference_key(key)
        if parsed is None:
            st = str(hit.get("source_type") or "")
            if st == "attachment_chunk" and hit.get("chunk_id") is not None:
                key = f"attachment_chunk:chunk:{int(hit['chunk_id'])}"
            elif st in {"knowledge_source", "knowledge"} and hit.get("chunk_id") is not None:
                key = f"knowledge_source:chunk:{int(hit['chunk_id'])}"
            elif eid is not None:
                key = f"entry:source:{eid}"
            else:
                return []
        identities.append(SourceMemberIdentity(reference_key=key, display_order=0))
        return identities

    order = 0
    for entry in family.entries:
        identities.append(
            SourceMemberIdentity(
                reference_key=f"entry:source:{int(entry.entry_id)}",
                display_order=order,
            )
        )
        order += 1
        for att in entry.attachments:
            identities.append(
                SourceMemberIdentity(
                    reference_key=f"attachment:source:{int(att.attachment_id)}",
                    display_order=order,
                )
            )
            order += 1
    return identities


def collect_candidate_identities_from_rag(
    db: Session,
    user_id: int,
    references: Sequence[Dict[str, Any]],
) -> List[SourceMemberIdentity]:
    seen: Set[str] = set()
    out: List[SourceMemberIdentity] = []
    order = 0
    for hit in references:
        for identity in _family_member_keys(db, user_id, hit if isinstance(hit, dict) else {}):
            if identity.reference_key in seen:
                continue
            seen.add(identity.reference_key)
            out.append(
                SourceMemberIdentity(
                    reference_key=identity.reference_key,
                    display_order=order,
                )
            )
            order += 1
    return out


def build_public_candidates(
    db: Session,
    user_id: int,
    identities: Sequence[SourceMemberIdentity],
) -> List[Dict[str, Any]]:
    from app.models import Entry, EntryAttachment, Todo

    public: List[Dict[str, Any]] = []
    for identity in identities:
        parsed = parse_proposal_reference_key(identity.reference_key)
        if parsed is None:
            continue
        st, kind, oid = parsed
        if st == "notion_page" and kind == "source":
            from app.models import NotionPage

            page = db.query(NotionPage).filter(NotionPage.id == oid, NotionPage.user_id == user_id).first()
            if page is None:
                continue
            public.append(
                _safe_candidate_item(
                    reference_key=identity.reference_key,
                    source_type="notion_page",
                    title=str(page.title or "Notion 页面")[:120],
                    snippet=str(page.breadcrumb or "")[:200],
                    family_root_entry_id=None,
                    display_order=identity.display_order,
                )
            )
        elif st == "todo" and kind == "source":
            from app.services.todo_hierarchy import load_index
            from app.services.todo_knowledge import todo_path_titles

            todo = db.query(Todo).filter(Todo.id == oid, Todo.user_id == user_id).first()
            if todo is None:
                continue
            index = load_index(db, user_id)
            path = " / ".join(todo_path_titles(int(todo.id), index))
            public.append(
                _safe_candidate_item(
                    reference_key=identity.reference_key,
                    source_type="todo",
                    title=str(todo.content or "小要事")[:120],
                    snippet=path[:200],
                    family_root_entry_id=None,
                    display_order=identity.display_order,
                )
            )
        elif st == "entry" and kind == "source":
            entry = (
                db.query(Entry)
                .filter(Entry.id == oid, Entry.user_id == user_id)
                .first()
            )
            if entry is None:
                continue
            family = resolve_family_from_hit(
                db, user_id, entry_id=oid, attachment_id=None
            )
            root_id = int(family.root_entry_id) if family else oid
            title = (entry.content or "").strip().split("\n", 1)[0][:80] or "未命名记录"
            snippet = (entry.content or "").strip()[:200]
            public.append(
                _safe_candidate_item(
                    reference_key=identity.reference_key,
                    source_type="entry",
                    title=title,
                    snippet=snippet,
                    family_root_entry_id=root_id,
                    display_order=identity.display_order,
                )
            )
        elif st == "attachment" and kind == "source":
            att = (
                db.query(EntryAttachment)
                .filter(EntryAttachment.id == oid, EntryAttachment.user_id == user_id)
                .first()
            )
            if att is None or str(att.status or "") != "indexed":
                continue
            family = resolve_family_from_hit(
                db, user_id, entry_id=int(att.entry_id), attachment_id=oid
            )
            root_id = int(family.root_entry_id) if family else int(att.entry_id)
            public.append(
                _safe_candidate_item(
                    reference_key=identity.reference_key,
                    source_type="attachment",
                    title=str(att.original_filename or att.file_name or "附件")[:120],
                    snippet="",
                    family_root_entry_id=root_id,
                    display_order=identity.display_order,
                )
            )
    return public


MIN_EXPLAINER_SOURCE_FAMILIES = 3


def _safe_optional_int(value: Any) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _rag_hit_family_key(
    db: Session,
    user_id: int,
    hit: Mapping[str, Any],
) -> str:
    """Resolve a stable family identity without expanding the record family."""
    metadata = hit.get("metadata") if isinstance(hit.get("metadata"), dict) else {}
    if str(hit.get("source_type") or "") == "todo":
        root_todo_id = _safe_optional_int(
            hit.get("root_todo_id") or metadata.get("root_todo_id") or hit.get("todo_id") or hit.get("source_id")
        )
        return f"todo:{root_todo_id}" if root_todo_id is not None else f"source:{canonical_reference_key(dict(hit))}"
    if str(hit.get("source_type") or "") == "notion_page":
        page_id = _safe_optional_int(hit.get("notion_page_id") or hit.get("source_id"))
        return f"notion_page:{page_id}" if page_id is not None else f"source:{canonical_reference_key(dict(hit))}"
    root_id = _safe_optional_int(
        hit.get("root_entry_id") or metadata.get("root_entry_id")
    )
    if root_id is not None:
        return f"entry:{root_id}"

    entry_id = _safe_optional_int(hit.get("entry_id"))
    if entry_id is None:
        attachment_id = _safe_optional_int(hit.get("attachment_id"))
        if (
            attachment_id is None
            and str(hit.get("source_type") or "") == "attachment_chunk"
        ):
            attachment_id = _safe_optional_int(hit.get("source_id"))
        if attachment_id is not None:
            from app.models import EntryAttachment

            attachment = (
                db.query(EntryAttachment)
                .filter(
                    EntryAttachment.id == attachment_id,
                    EntryAttachment.user_id == user_id,
                )
                .first()
            )
            if attachment is not None:
                entry_id = int(attachment.entry_id)

    if entry_id is not None:
        resolved_root = resolve_root_entry_id(db, user_id, entry_id)
        if resolved_root is not None:
            return f"entry:{int(resolved_root)}"

    return f"source:{canonical_reference_key(dict(hit))}"


def select_explainer_source_family_hits(
    db: Session,
    *,
    user_id: int,
    query: str,
    references: Sequence[Dict[str, Any]],
    minimum_families: int = MIN_EXPLAINER_SOURCE_FAMILIES,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Threshold RAG hits, dedupe families, then apply a three-family recall floor.

    There is deliberately no maximum family count. Family expansion remains a
    later step, so weak hits cannot make an entire record family user-visible.
    """
    refs = [dict(ref) for ref in references if isinstance(ref, dict)]
    if not refs:
        return [], {
            "input_count": 0,
            "threshold_admitted_count": 0,
            "selected_family_count": 0,
            "family_floor_added_count": 0,
        }

    topic = extract_retrieval_topic(query)
    terms = [str(term) for term in (topic.get("topic_terms") or []) if str(term)]
    annotated = _attach_judge_evidence(refs, terms)
    summaries = [{"alias": f"S{index + 1}"} for index in range(len(annotated))]
    admitted_summaries, admission_meta = select_rag_admission_summaries(
        annotated,
        summaries,
        apply_recall_floors=False,
    )
    admitted_aliases = {
        str(item.get("alias") or "")
        for item in admitted_summaries
        if str(item.get("alias") or "")
    }
    admitted_indexes = [
        index
        for index in range(len(annotated))
        if f"S{index + 1}" in admitted_aliases
    ]

    selected: List[Dict[str, Any]] = []
    selected_families: Set[str] = set()

    def add_family(index: int) -> bool:
        family_key = _rag_hit_family_key(db, user_id, annotated[index])
        if family_key in selected_families:
            return False
        selected_families.add(family_key)
        selected.append(dict(annotated[index]))
        return True

    for index in admitted_indexes:
        add_family(index)

    floor_added = 0
    floor_target = max(0, int(minimum_families))
    if len(selected_families) < floor_target:
        admitted_index_set = set(admitted_indexes)
        for index in range(len(annotated)):
            if index in admitted_index_set:
                continue
            if add_family(index):
                floor_added += 1
            if len(selected_families) >= floor_target:
                break

    return selected, {
        **dict(admission_meta),
        "threshold_admitted_count": len(admitted_indexes),
        "selected_family_count": len(selected_families),
        "family_floor_added_count": floor_added,
    }


def run_global_rag(
    db: Session,
    *,
    user_id: int,
    query: str,
    reason: str,
) -> List[Dict[str, Any]]:
    raw = (query or "").strip()
    rag_context = retrieve_rag_context(
        db,
        user_id=user_id,
        query=raw,
        top_k=30,
        dense_top_k=50,
        keyword_top_k=50,
    )
    generation_refs = expand_references_for_generation(rag_context, max_refs=40)
    selected, admission_meta = select_explainer_source_family_hits(
        db,
        user_id=user_id,
        query=raw,
        references=generation_refs,
    )
    logger.info(
        "explainer source admission reason=%s input=%s admitted_hits=%s families=%s floor_added=%s",
        reason,
        admission_meta.get("input_count", 0),
        admission_meta.get("threshold_admitted_count", 0),
        admission_meta.get("selected_family_count", 0),
        admission_meta.get("family_floor_added_count", 0),
    )
    return selected


async def run_planned_global_rag(
    db: Session,
    *,
    user_id: int,
    query: str,
    reason: str,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """Apply the shared question-understanding layer before explainer admission."""
    planned: RetrievalPlanResult = await build_retrieval_plan(
        query,
        model_key=model_key,
        user_id=int(user_id),
        fallback_candidates=fallback_candidates,
    )
    plan = planned.plan
    if (
        not planned.degraded
        and plan.operation == "discover_sources"
        and not plan.topic
        and not plan.named_source
    ):
        return discover_source_scope(db, user_id=int(user_id), plan=plan)

    effective_query = plan.topic or plan.named_source or query
    refs = run_global_rag(
        db,
        user_id=user_id,
        query=effective_query,
        reason=reason,
    )
    if planned.degraded:
        return refs

    has_filters = bool(
        plan.named_source
        or plan.filters.providers
        or plan.filters.object_kinds
        or plan.filters.tags
        or plan.filters.date_range
        or plan.filters.breadcrumb
        or plan.filters.todo_status
    )
    filtered = (
        filter_references_for_plan(
            refs,
            plan,
            require_named_source=bool(plan.named_source),
            answer_context=True,
        )
        if has_filters
        else refs
    )
    if plan.operation == "discover_sources":
        scope = discover_source_scope(db, user_id=int(user_id), plan=plan)
        by_key = {
            str(item.get("reference_key") or ""): item
            for item in [*filtered, *scope]
            if str(item.get("reference_key") or "")
        }
        return list(by_key.values())
    if plan.operation == "discover_then_answer":
        scope = discover_source_scope(db, user_id=int(user_id), plan=plan)
        scope_by_key = {
            str(item.get("reference_key") or ""): item
            for item in scope
            if str(item.get("reference_key") or "")
        }
        scoped = [
            item
            for item in filtered
            if str(item.get("reference_key") or "") in scope_by_key
        ]
        if scoped:
            return scoped
        if plan.named_source:
            return list(scope_by_key.values())
        return []
    if plan.named_source and not filtered:
        return discover_source_scope(db, user_id=int(user_id), plan=plan)
    return filtered


def _member_identities_from_proposal(
    proposal,
) -> List[SourceMemberIdentity]:
    manifest = parse_manifest(proposal.source_manifest)
    out: List[SourceMemberIdentity] = []
    for m in manifest.members:
        st = str(m.source_type or "").strip().lower()
        if st not in {"entry", "attachment", "todo", "notion_page"}:
            continue
        out.append(
            SourceMemberIdentity(
                reference_key=f"{st}:source:{int(m.source_id)}",
                display_order=int(m.display_order),
            )
        )
    return out


def _proposal_message(
    db: Session,
    user_id: int,
    session_id: str,
    proposal_id: int,
    role: str,
) -> Optional[AIConversation]:
    return (
        db.query(AIConversation)
        .filter(
            AIConversation.user_id == user_id,
            AIConversation.session_id == session_id,
            AIConversation.proposal_id == int(proposal_id),
            AIConversation.role == role,
        )
        .order_by(AIConversation.id.asc())
        .first()
    )


def _awaiting_phase(proposal) -> str:
    if int(proposal.base_version or 0) == 0:
        return "awaiting_source_confirm"
    return "awaiting_expansion_confirm"


def _candidates_response(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    proposal,
    query: str,
    expansion_topic: Optional[str] = None,
) -> Dict[str, Any]:
    identities = _member_identities_from_proposal(proposal)
    public = build_public_candidates(db, user_id, identities)
    phase = _awaiting_phase(proposal)
    base = int(proposal.base_version or 0)
    if phase == "awaiting_source_confirm":
        message = "请勾选要讲解的来源组后再确认。未确认前不会生成正式回答。"
    else:
        message = "已生成来源扩展建议，请勾选后确认。确认前不会改变讲解范围，也不会生成正式回答。"
    topic = (expansion_topic or "").strip()
    if not topic and phase == "awaiting_expansion_confirm":
        topic = derive_expansion_topic_from_user_content(query, phase=phase)
    payload: Dict[str, Any] = {
        "session_id": session_id,
        "proposal_id": int(proposal.id),
        "base_version": base,
        "status": proposal.status,
        "query": (query or "").strip(),
        "candidates": public,
        "message": message,
        "mode": "explainer",
        "phase": phase,
        "answer": None,
        "valid_references": [],
        "invalid_references": [],
        "source_set_version": None if base == 0 else base,
        "expansion": None,
        "expansion_topic": topic or None,
    }
    if phase == "awaiting_expansion_confirm":
        payload["expansion"] = {
            "proposal_id": int(proposal.id),
            "base_version": base,
            "status": proposal.status,
            "candidates": public,
            "expansion_topic": topic or None,
        }
        payload["source_set_version"] = base
    return payload


def _raise_or_return_active_proposal(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    query: str,
) -> Optional[Dict[str, Any]]:
    active = get_active_proposed(db, user_id, session_id)
    if active is None:
        return None
    user_msg = _proposal_message(db, user_id, session_id, int(active.id), "user")
    q = (query or "").strip()
    if user_msg is not None and (user_msg.content or "").strip() == q:
        return _candidates_response(
            db,
            user_id=user_id,
            session_id=session_id,
            proposal=active,
            query=q,
        )
    raise SourceSetError(
        "SOURCE_CONFIRM_PENDING",
        "当前会话仍有待确认的来源建议，请先确认或取消后再提问。",
    )


def raise_or_return_active_proposal(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    query: str,
) -> Optional[Dict[str, Any]]:
    """Public wrapper so streaming orchestration (ai_stream_explain.py) can reuse
    the exact same pending-proposal idempotent-replay / conflict check used by
    the non-stream explainer_chat entry point, without duplicating the logic."""
    return _raise_or_return_active_proposal(
        db, user_id=user_id, session_id=session_id, query=query
    )


async def orchestrate_initial_source_candidates(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    query: str,
    request_id: Optional[str] = None,
    use_query_planner: bool = False,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Shared first-turn orchestration for /explainer/source-candidates and first /explainer/chat.
    RAG runs without holding session row locks; proposal + user message commit atomically.
    When request_id is provided (streaming path), it is bound onto the pending user row.
    """
    _require_explainer(db, user_id, session_id)
    text = (query or "").strip()
    if not text:
        raise SourceSetError("EMPTY_MESSAGE", "消息不能为空。")

    early = _raise_or_return_active_proposal(
        db, user_id=user_id, session_id=session_id, query=text
    )
    if early is not None:
        return early

    meta = get_session_metadata(db, user_id, session_id)
    if meta is not None and meta.current_source_set_version is not None:
        raise SourceSetError(
            "SOURCE_SET_IMMUTABLE",
            "当前会话已有锁定来源，请使用扩展流程或新建对话。",
        )

    # Network / RAG outside row locks.
    refs = (
        await run_planned_global_rag(
            db,
            user_id=user_id,
            query=text,
            reason="explainer_initial",
            model_key=model_key,
            fallback_candidates=fallback_candidates,
        )
        if use_query_planner
        else run_global_rag(
            db, user_id=user_id, query=text, reason="explainer_initial"
        )
    )
    identities = collect_candidate_identities_from_rag(db, user_id, refs)
    if not identities:
        raise SourceSetError("SOURCE_SET_EMPTY", "未找到可用于讲解的候选来源。")

    try:
        _lock_explainer_session(db, user_id, session_id)
        again = _raise_or_return_active_proposal(
            db, user_id=user_id, session_id=session_id, query=text
        )
        if again is not None:
            db.rollback()
            return again

        meta2 = get_session_metadata(db, user_id, session_id)
        if meta2 is not None and meta2.current_source_set_version is not None:
            db.rollback()
            raise SourceSetError(
                "SOURCE_SET_IMMUTABLE",
                "当前会话已有锁定来源，请使用扩展流程或新建对话。",
            )

        proposal = create_initial_proposal(
            db, user_id, session_id, identities, commit=False
        )
        existing_user = _proposal_message(
            db, user_id, session_id, int(proposal.id), "user"
        )
        if existing_user is None:
            save_message(
                db,
                user_id,
                session_id,
                "user",
                text,
                proposal_id=int(proposal.id),
                request_id=request_id,
                commit=False,
            )
        elif (existing_user.content or "").strip() != text:
            db.rollback()
            raise SourceSetError(
                "SOURCE_CONFIRM_PENDING",
                "当前会话仍有待确认的来源建议，请先确认或取消后再提问。",
            )
        elif request_id and not existing_user.request_id:
            existing_user.request_id = request_id
        touch_session_activity(db, user_id, session_id, commit=False)
        db.commit()
        db.refresh(proposal)
    except SourceSetError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return _candidates_response(
        db,
        user_id=user_id,
        session_id=session_id,
        proposal=proposal,
        query=text,
    )


async def create_initial_source_candidates(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    query: str,
) -> Dict[str, Any]:
    """Alias kept for route / test imports."""
    return await orchestrate_initial_source_candidates(
        db, user_id=user_id, session_id=session_id, query=query
    )


async def _answer_with_locked_sources(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    text: str,
    model_key: Optional[str],
    fallback_candidates: Optional[List[str]],
    persist_user: bool,
    proposal_id: Optional[int] = None,
    expected_source_set_version: Optional[int] = None,
) -> Dict[str, Any]:
    ctx = build_explainer_generation_context(
        db, user_id, session_id, current_query=text
    )
    if (
        expected_source_set_version is not None
        and int(ctx.source_set_version) != int(expected_source_set_version)
    ):
        raise SourceSetError(
            "SOURCE_SET_VERSION_CONFLICT",
            "来源版本已变化，请刷新后重试。",
        )
    gen_refs = evidence_to_generation_references(ctx.evidence)
    if not gen_refs:
        raise SourceSetError("SOURCE_SET_EMPTY", "当前固定来源没有可用内容。")

    try:
        result = await generate_structured_summary(
            text,
            gen_refs,
            conversation_history=ctx.conversation_history,
            model_key=model_key,
            user_id=user_id,
            fallback_candidates=fallback_candidates,
            system_policy=ctx.system_policy,
            answer_style="default",
        )
    except Exception:
        db.rollback()
        raise

    answer = str(result.get("answer") or "")
    valid_refs = result.get("valid_references") or []
    allowed = {b.reference_key for b in ctx.evidence}
    filtered_valid = []
    for ref in valid_refs:
        if not isinstance(ref, dict):
            continue
        key = str(ref.get("reference_key") or "")
        ok = key in allowed
        if not ok:
            sid = ref.get("attachment_id") or ref.get("source_id") or ref.get("entry_id")
            st = str(ref.get("source_type") or "")
            try:
                if st in {"attachment", "attachment_chunk"} and sid is not None:
                    ok = f"attachment:source:{int(sid)}" in allowed
                elif st == "entry" and sid is not None:
                    ok = f"entry:source:{int(sid)}" in allowed
            except (TypeError, ValueError):
                ok = False
        if ok:
            safe = dict(ref)
            safe.pop("reference_key", None)
            filtered_valid.append(safe)

    try:
        if persist_user:
            save_message(db, user_id, session_id, "user", text, commit=False)
        save_message(
            db,
            user_id,
            session_id,
            "assistant",
            answer,
            filtered_valid if filtered_valid else None,
            source_set_version=ctx.source_set_version,
            proposal_id=proposal_id,
            commit=False,
        )
        touch_session_activity(db, user_id, session_id, commit=False)
        db.commit()
    except IntegrityError:
        db.rollback()
        if proposal_id is not None:
            winner = _proposal_message(
                db, user_id, session_id, int(proposal_id), "assistant"
            )
            if winner is not None:
                return {
                    "session_id": session_id,
                    "mode": "explainer",
                    "phase": "answered",
                    "message": winner.content,
                    "answer": winner.content,
                    "valid_references": winner.references or [],
                    "invalid_references": [],
                    "source_set_version": winner.source_set_version,
                    "proposal_id": int(proposal_id),
                    "expansion": None,
                    "resumed": True,
                    "idempotent": True,
                }
        raise
    except Exception:
        db.rollback()
        raise

    return {
        "session_id": session_id,
        "mode": "explainer",
        "phase": "answered",
        "message": answer,
        "answer": answer,
        "valid_references": filtered_valid,
        "invalid_references": result.get("invalid_references") or [],
        "source_set_version": ctx.source_set_version,
        "proposal_id": int(proposal_id) if proposal_id is not None else None,
        "expansion": None,
        "model_fallback": result.get("model_fallback"),
        "context_meta": {
            "evidence_chars": ctx.evidence_chars,
            "history_chars": ctx.history_chars,
            "summary_chars": ctx.summary_chars,
            "total_chars": ctx.total_chars,
            "truncated_evidence": ctx.truncated_evidence,
            "truncated_history": ctx.truncated_history,
            "summary_present": bool(ctx.summary_text),
            "system_policy_present": bool(ctx.system_policy),
            "personalization": (
                ctx.personalization.safe_diag() if ctx.personalization else {"present": False}
            ),
        },
    }


async def resume_proposal(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    proposal_id: int,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Answer the pending user question bound to a confirmed proposal (idempotent)."""
    _require_explainer(db, user_id, session_id)
    proposal, target_version = resolve_confirmed_proposal_version(
        db, user_id, session_id, int(proposal_id)
    )
    del proposal  # ownership already validated

    existing = _proposal_message(db, user_id, session_id, int(proposal_id), "assistant")
    if existing is not None:
        return {
            "session_id": session_id,
            "mode": "explainer",
            "phase": "answered",
            "message": existing.content,
            "answer": existing.content,
            "valid_references": existing.references or [],
            "invalid_references": [],
            "source_set_version": existing.source_set_version or target_version,
            "proposal_id": int(proposal_id),
            "expansion": None,
            "resumed": True,
            "idempotent": True,
        }

    pending = _proposal_message(db, user_id, session_id, int(proposal_id), "user")
    if pending is None:
        raise SourceSetError("NO_PENDING_QUESTION", "没有待继续回答的问题。")

    meta = get_session_metadata(db, user_id, session_id)
    if meta is None or meta.current_source_set_version is None:
        raise SourceSetError("SOURCE_SET_NOT_LOCKED", "请先确认来源后再继续讲解。")
    if int(meta.current_source_set_version) != int(target_version):
        raise SourceSetError(
            "SOURCE_SET_VERSION_CONFLICT",
            "来源版本已变化，请刷新后重试。",
        )

    payload = await _answer_with_locked_sources(
        db,
        user_id=user_id,
        session_id=session_id,
        text=str(pending.content or ""),
        model_key=model_key,
        fallback_candidates=fallback_candidates,
        persist_user=False,
        proposal_id=int(proposal_id),
        expected_source_set_version=int(target_version),
    )
    payload["resumed"] = True
    payload["idempotent"] = bool(payload.get("idempotent"))
    payload["proposal_id"] = int(proposal_id)
    return payload


# Backward-compatible name used by older Fix tests (requires proposal_id now).
async def resume_pending_first_question(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    proposal_id: int,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> Dict[str, Any]:
    return await resume_proposal(
        db,
        user_id=user_id,
        session_id=session_id,
        proposal_id=proposal_id,
        model_key=model_key,
        fallback_candidates=fallback_candidates,
    )


async def propose_source_expansion(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    text: str,
    intent: Dict[str, Any],
    current_version: int,
    request_id: Optional[str] = None,
    use_query_planner: bool = False,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Shared expansion-proposal orchestration (extracted from explainer_chat so
    ai_stream_explain.py can reuse the exact same RAG + CAS + atomic-write path
    for the streaming explain/stream expansion phase).
    RAG runs without holding session row locks; proposal + user message commit atomically.
    """
    try:
        expansion_query = validate_expansion_topic(intent.get("expansion_query"))
    except ExpansionIntentError as exc:
        raise SourceSetError(exc.code, exc.message) from exc
    intent = {**intent, "expansion_query": expansion_query, "action": ACTION_EXPAND}

    try:
        refs = (
            await run_planned_global_rag(
                db,
                user_id=user_id,
                query=expansion_query,
                reason="explainer_expansion",
                model_key=model_key,
                fallback_candidates=fallback_candidates,
            )
            if use_query_planner
            else run_global_rag(
                db,
                user_id=user_id,
                query=expansion_query,
                reason="explainer_expansion",
            )
        )
    except Exception as exc:  # noqa: BLE001
        db.rollback()
        raise SourceSetError(
            "EXPANSION_RAG_FAILED",
            "来源扩展检索暂时不可用。",
        ) from exc

    locked = get_locked_manifest(db, user_id, session_id, current_version)
    current_keys: Set[str] = set()
    if locked is not None:
        for m in locked.members:
            if m.source_type == "entry":
                current_keys.add(f"entry:source:{int(m.source_id)}")
            elif m.source_type == "attachment":
                current_keys.add(f"attachment:source:{int(m.source_id)}")
            elif m.source_type == "todo":
                current_keys.add(f"todo:source:{int(m.source_id)}")
            elif m.source_type == "notion_page":
                current_keys.add(f"notion_page:source:{int(m.source_id)}")

    identities = collect_candidate_identities_from_rag(db, user_id, refs)
    additions = [i for i in identities if i.reference_key not in current_keys]
    if not additions:
        raise SourceSetError("SOURCE_SET_EMPTY", "没有找到新的相关来源。")

    try:
        _lock_explainer_session(db, user_id, session_id)
        pending = _raise_or_return_active_proposal(
            db, user_id=user_id, session_id=session_id, query=text
        )
        if pending is not None:
            db.rollback()
            return pending

        meta2 = get_session_metadata(db, user_id, session_id)
        if meta2 is None or int(meta2.current_source_set_version or 0) != current_version:
            db.rollback()
            raise SourceSetError(
                "SOURCE_SET_VERSION_CONFLICT",
                "来源版本已变化，请刷新后重试。",
            )

        proposal = create_expansion_proposal(
            db,
            user_id,
            session_id,
            additions,
            base_version=current_version,
            commit=False,
        )
        existing_user = _proposal_message(db, user_id, session_id, int(proposal.id), "user")
        if existing_user is None:
            save_message(
                db,
                user_id,
                session_id,
                "user",
                text,
                proposal_id=int(proposal.id),
                request_id=request_id,
                commit=False,
            )
        elif request_id and not existing_user.request_id:
            existing_user.request_id = request_id
        touch_session_activity(db, user_id, session_id, commit=False)
        db.commit()
        db.refresh(proposal)
    except SourceSetError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise

    return _candidates_response(
        db,
        user_id=user_id,
        session_id=session_id,
        proposal=proposal,
        query=text,
        expansion_topic=expansion_query,
    )


async def explainer_chat(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    message: str,
    model_key: Optional[str] = None,
    fallback_candidates: Optional[List[str]] = None,
) -> Dict[str, Any]:
    meta = _require_explainer(db, user_id, session_id)
    text = (message or "").strip()
    if not text:
        raise SourceSetError("EMPTY_MESSAGE", "消息不能为空。")

    # Any active proposal blocks new work unless same query idempotent replay.
    early = _raise_or_return_active_proposal(
        db, user_id=user_id, session_id=session_id, query=text
    )
    if early is not None:
        return early

    if meta.current_source_set_version is None:
        return await orchestrate_initial_source_candidates(
            db,
            user_id=user_id,
            session_id=session_id,
            query=text,
            use_query_planner=True,
            model_key=model_key,
            fallback_candidates=fallback_candidates,
        )

    # A locked explainer session always answers over its current source set.
    # Expansion is available only through the explicit source-set expand API.
    return await _answer_with_locked_sources(
        db,
        user_id=user_id,
        session_id=session_id,
        text=text,
        model_key=model_key,
        fallback_candidates=fallback_candidates,
        persist_user=True,
        proposal_id=None,
    )


def confirm_explainer_sources(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    proposal_id: int,
    base_version: int,
    selected_reference_keys: Optional[Sequence[str]] = None,
) -> Dict[str, Any]:
    _require_explainer(db, user_id, session_id)
    row = confirm_proposal(
        db,
        user_id,
        session_id,
        proposal_id,
        base_version=int(base_version),
        selected_reference_keys=selected_reference_keys,
    )
    manifest = parse_manifest(row.source_manifest)
    return {
        "session_id": session_id,
        "version": int(row.version or 0),
        "status": row.status,
        "content_fingerprint": str(row.content_fingerprint or ""),
        "member_count": len(manifest.members),
    }

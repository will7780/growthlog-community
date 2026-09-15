"""
R11.3-Fix / Fix2: JWT-authenticated full reference preview via opaque ref_token.

Re-verifies token purpose/ownership, parses canonical identity, re-queries
source ownership/status. Never logs token, decrypted payload, or source IDs.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.models import AttachmentChunk, Entry, EntryAttachment, EntryLabel, KnowledgeChunk, KnowledgeSource, NotionPage, Todo
from app.services.ai_stream_protocol import (
    PURPOSE_ANSWER_REFERENCE,
    PURPOSE_CANDIDATE_CONFIRM,
    verify_reference_token,
)
from app.services.reference_identity import parse_canonical_reference_key, parse_proposal_reference_key


class ReferencePreviewError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _label_name(db: Session, label_code: Optional[str]) -> str:
    if not label_code:
        return ""
    lab = db.query(EntryLabel).filter(EntryLabel.code == label_code).first()
    return lab.name if lab is not None else str(label_code)


def _walk_to_root(db: Session, user_id: int, entry: Entry) -> Entry:
    walk = entry
    seen: set[int] = set()
    while walk.parent_id is not None and int(walk.parent_id) not in seen:
        seen.add(int(walk.id))
        parent = (
            db.query(Entry)
            .filter(Entry.id == int(walk.parent_id), Entry.user_id == int(user_id))
            .first()
        )
        if parent is None:
            break
        walk = parent
    return walk


def _entry_tree_payload(db: Session, user_id: int, entry_id: int) -> Optional[Dict[str, Any]]:
    hit = (
        db.query(Entry)
        .filter(Entry.id == int(entry_id), Entry.user_id == int(user_id))
        .first()
    )
    if hit is None:
        return None
    root = _walk_to_root(db, user_id, hit)

    by_id: Dict[int, Entry] = {int(root.id): root}
    frontier = [int(root.id)]
    while frontier:
        parent_ids = list(frontier)
        frontier = []
        rows = (
            db.query(Entry)
            .filter(Entry.user_id == int(user_id), Entry.parent_id.in_(parent_ids))
            .order_by(Entry.created_at.asc(), Entry.id.asc())
            .all()
        )
        for e in rows:
            eid = int(e.id)
            if eid not in by_id:
                by_id[eid] = e
                frontier.append(eid)

    children_map: Dict[Optional[int], List[Entry]] = {}
    for e in by_id.values():
        pid = int(e.parent_id) if e.parent_id is not None else None
        children_map.setdefault(pid, []).append(e)
    for pid in children_map:
        children_map[pid].sort(key=lambda x: ((x.created_at or x.id), int(x.id)))

    def _node(e: Entry, children: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "id": int(e.id),
            "user_id": int(e.user_id),
            "content": e.content or "",
            "label_code": e.label_code,
            "label_name": _label_name(db, e.label_code),
            "parent_id": int(e.parent_id) if e.parent_id is not None else None,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "updated_at": e.updated_at.isoformat() if e.updated_at else None,
            "attachments": [],
            "children": children,
        }

    def _build(e: Entry) -> Dict[str, Any]:
        kids = [_build(c) for c in children_map.get(int(e.id), [])]
        return _node(e, kids)

    return _build(root)


def _parent_bits(db: Session, user_id: int, owner: Optional[Entry]) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    if owner is None or owner.parent_id is None:
        return None, None, None
    parent_id = int(owner.parent_id)
    parent = (
        db.query(Entry)
        .filter(Entry.id == parent_id, Entry.user_id == int(user_id))
        .first()
    )
    if parent is None:
        return parent_id, None, None
    parent_snippet = (parent.content or "").strip()[:200]
    parent_title = parent_snippet[:80] or "父记录"
    return parent_id, parent_title, parent_snippet


def _resolve_identity(payload: Dict[str, Any]) -> Optional[Tuple[str, str, int]]:
    raw_key = payload.get("reference_key")
    if isinstance(raw_key, str) and raw_key.strip():
        parsed = parse_canonical_reference_key(raw_key.strip())
        if parsed is None:
            parsed = parse_proposal_reference_key(raw_key.strip())
        if parsed is not None:
            return parsed
    attachment_id = payload.get("attachment_id")
    entry_id = payload.get("entry_id")
    if attachment_id is not None:
        return ("attachment", "source", int(attachment_id))
    if entry_id is not None:
        return ("entry", "source", int(entry_id))
    return None


def _attachment_preview(
    db: Session,
    *,
    user_id: int,
    attachment_id: int,
    chunk: Optional[AttachmentChunk] = None,
    display_index: int,
    hit_snippet_override: Optional[str] = None,
) -> Dict[str, Any]:
    att = (
        db.query(EntryAttachment)
        .filter(
            EntryAttachment.id == int(attachment_id),
            EntryAttachment.user_id == int(user_id),
        )
        .first()
    )
    if att is None:
        raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
    status = str(getattr(att, "status", "") or "")
    if status and status != "indexed":
        raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")

    owner_entry_id = int(att.entry_id)
    entry_tree = _entry_tree_payload(db, user_id, owner_entry_id)
    if entry_tree is None:
        raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")

    owner = (
        db.query(Entry)
        .filter(Entry.id == owner_entry_id, Entry.user_id == int(user_id))
        .first()
    )
    parent_id, parent_title, parent_snippet = _parent_bits(db, user_id, owner)
    mime = str(att.mime_type or "")
    filename = str(att.original_filename or att.file_name or "附件")
    hit_snippet = ""
    page_no = None
    slide_no = None
    modality = None
    if chunk is not None:
        if int(chunk.user_id) != int(user_id) or int(chunk.attachment_id) != int(att.id):
            raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
        hit_snippet = (chunk.content or "").strip()[:400]
        page_no = int(chunk.page_no) if chunk.page_no is not None else None
        slide_no = int(chunk.slide_no) if chunk.slide_no is not None else None
        modality = str(chunk.modality) if chunk.modality else None
    elif hit_snippet_override is not None:
        # knowledge mirror / direct attachment: keep provided hit, never invent page/slide.
        hit_snippet = str(hit_snippet_override or "").strip()[:400]
    else:
        # Attachment-level token: surface the first OCR/text chunk snippet when present.
        first_chunk = (
            db.query(AttachmentChunk)
            .filter(
                AttachmentChunk.attachment_id == int(att.id),
                AttachmentChunk.user_id == int(user_id),
            )
            .order_by(AttachmentChunk.chunk_index.asc(), AttachmentChunk.id.asc())
            .first()
        )
        if first_chunk is not None:
            # Attachment-level identity may show a representative snippet, but
            # it must not claim an exact page/slide that the token did not bind.
            hit_snippet = (first_chunk.content or "").strip()[:400]
            modality = str(first_chunk.modality) if first_chunk.modality else None

    ref_item: Dict[str, Any] = {
        "entry_id": owner_entry_id,
        "title": filename,
        "label_name": _label_name(db, owner.label_code if owner else None),
        "created_at": att.created_at.isoformat() if att.created_at else None,
        "snippet": hit_snippet,
        "relevance_score": 0,
        "source_type": "attachment_chunk",
        "attachment_id": int(att.id),
        "page_no": page_no,
        "slide_no": slide_no,
        "modality": modality,
        "root_entry_id": int(entry_tree["id"]),
        "parent_id": parent_id,
        "parent_title": parent_title,
        "parent_snippet": parent_snippet,
        "metadata": {
            "filename": filename,
            "mime_type": mime,
            "page_count": att.page_count,
            "slide_count": att.slide_count,
        },
    }
    return {
        "display_index": display_index,
        "source_type": "attachment",
        "reference": ref_item,
        "entry": entry_tree,
        "hit_snippet": hit_snippet,
        "filename": filename,
        "page_no": page_no,
        "slide_no": slide_no,
        "modality": modality,
        "can_view_original_image": mime.lower().startswith("image/"),
        "can_preview_pdf": "pdf" in mime.lower() or filename.lower().endswith(".pdf"),
    }


def _entry_preview(
    db: Session,
    *,
    user_id: int,
    entry_id: int,
    display_index: int,
    hit_snippet: Optional[str] = None,
) -> Dict[str, Any]:
    entry_tree = _entry_tree_payload(db, user_id, int(entry_id))
    if entry_tree is None:
        raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
    hit = (
        db.query(Entry)
        .filter(Entry.id == int(entry_id), Entry.user_id == int(user_id))
        .first()
    )
    if hit is None:
        raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
    content = hit.content or ""
    snippet = (hit_snippet if hit_snippet is not None else content.strip()[:200])
    parent_id, parent_title, parent_snippet = _parent_bits(db, user_id, hit)
    ref_item = {
        "entry_id": int(entry_id),
        "title": (snippet or "记录")[:80],
        "label_name": _label_name(db, hit.label_code),
        "created_at": hit.created_at.isoformat() if hit.created_at else None,
        "snippet": snippet,
        "relevance_score": 0,
        "source_type": "entry",
        "attachment_id": None,
        "root_entry_id": int(entry_tree["id"]),
        "parent_id": parent_id,
        "parent_title": parent_title,
        "parent_snippet": parent_snippet,
    }
    return {
        "display_index": display_index,
        "source_type": "entry",
        "reference": ref_item,
        "entry": entry_tree,
        "hit_snippet": snippet,
        "filename": None,
        "page_no": None,
        "slide_no": None,
        "modality": None,
        "can_view_original_image": False,
        "can_preview_pdf": False,
    }


def _notion_page_preview(
    db: Session,
    *,
    user_id: int,
    notion_page_id: int,
    display_index: int,
    hit_snippet: Optional[str] = None,
) -> Dict[str, Any]:
    from app.services.notion_reference import page_is_searchable, resolve_notion_page_url

    page = (
        db.query(NotionPage)
        .filter(NotionPage.id == int(notion_page_id), NotionPage.user_id == int(user_id))
        .first()
    )
    if page is None or str(page.sync_status or "") in {"permission_lost", "deleted"}:
        raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
    if not page_is_searchable(page):
        raise ReferencePreviewError("NOTION_SOURCE_SYNC_PENDING", "Notion 来源仍在同步，请稍后重试。")
    open_url = resolve_notion_page_url(page.notion_url, page.notion_page_uuid)
    snippet = (hit_snippet or page.normalized_text or "").strip()[:400]
    ref_item = {
        "entry_id": 0,
        "title": str(page.title or "Notion 页面")[:120],
        "label_name": "Notion",
        "created_at": page.updated_at.isoformat() if page.updated_at else None,
        "snippet": snippet,
        "source_type": "notion_page",
        "metadata": {
            "breadcrumb": page.breadcrumb,
            "sync_status": page.sync_status,
            "open_url": open_url,
        },
    }
    return {
        "display_index": display_index,
        "source_type": "notion_page",
        "reference": ref_item,
        "entry": None,
        "todo_tree": None,
        "hit_snippet": snippet,
        "filename": None,
        "page_no": None,
        "slide_no": None,
        "open_url": open_url,
        "can_view_original_image": False,
        "can_preview_pdf": False,
    }


def _todo_preview(
    db: Session,
    *,
    user_id: int,
    todo_id: int,
    display_index: int,
) -> Dict[str, Any]:
    from app.services.todo_hierarchy import load_index, family_root_id, subtree_ids, depth_of
    from app.services.todo_knowledge import todo_path_titles, todo_visible_payload

    todo = db.query(Todo).filter(Todo.id == int(todo_id), Todo.user_id == int(user_id)).first()
    if todo is None:
        raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
    index = load_index(db, int(user_id))
    if int(todo.id) not in index.by_id:
        raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
    root_id = family_root_id(int(todo.id), index)

    def public_node(node: Todo) -> Dict[str, Any]:
        payload = todo_visible_payload(node)
        return {
            "content": payload["content"],
            "status": payload["status"],
            "priority": payload["priority"],
            "urgent": payload["urgent"],
            "due_date": payload["due_date"],
            "completed_at": payload["completed_at"],
            "completion_note": payload["completion_note"],
            "is_step": payload["is_step"],
            "sort_order": (
                int(node.sort_order)
                if getattr(node, "sort_order", None) is not None
                else None
            ),
            "depth": depth_of(int(node.id), index),
            "is_hit": int(node.id) == int(todo.id),
        }

    tree = [public_node(index.by_id[item]) for item in subtree_ids(root_id, index)]
    path = todo_path_titles(int(todo.id), index)
    payload = todo_visible_payload(todo)
    ref_item = {
        "title": payload["content"][:120] or "小要事",
        "label_name": "小要事",
        "created_at": todo.created_at.isoformat() if todo.created_at else None,
        "snippet": " / ".join(path),
        "relevance_score": 0,
        "source_type": "todo",
        "metadata": {
            "path_titles": path,
            "status": payload["status"],
            "is_step": payload["is_step"],
            "sort_order": (
                int(todo.sort_order)
                if getattr(todo, "sort_order", None) is not None
                else None
            ),
            "priority": payload["priority"],
            "urgent": payload["urgent"],
            "due_date": payload["due_date"],
            "completed_at": payload["completed_at"],
            "completion_note": payload["completion_note"],
        },
    }
    return {
        "display_index": display_index,
        "source_type": "todo",
        "reference": ref_item,
        "entry": None,
        "todo_tree": tree,
        "hit_snippet": ref_item["snippet"],
        "filename": None,
        "page_no": None,
        "slide_no": None,
        "modality": None,
        "can_view_original_image": False,
        "can_preview_pdf": False,
    }


def build_reference_preview(
    db: Session,
    *,
    user_id: int,
    session_id: str,
    ref_token: str,
    purpose: str = PURPOSE_ANSWER_REFERENCE,
) -> Dict[str, Any]:
    if purpose not in {PURPOSE_ANSWER_REFERENCE, PURPOSE_CANDIDATE_CONFIRM}:
        raise ReferencePreviewError("SOURCE_REFERENCE_INVALID", "所选来源令牌无效。")

    payload = verify_reference_token(
        ref_token,
        user_id=user_id,
        session_id=session_id,
        purpose=purpose,
    )
    if payload is None:
        raise ReferencePreviewError("SOURCE_REFERENCE_INVALID", "所选来源令牌无效。")

    identity = _resolve_identity(payload)
    if identity is None:
        raise ReferencePreviewError("SOURCE_REFERENCE_INVALID", "所选来源令牌无效。")

    source_type, kind, object_id = identity
    display_index = int(payload.get("display_index") or 0)

    if source_type == "todo" and kind == "source":
        return _todo_preview(
            db, user_id=user_id, todo_id=object_id, display_index=display_index
        )

    if source_type == "notion_page" and kind == "source":
        return _notion_page_preview(
            db, user_id=user_id, notion_page_id=object_id, display_index=display_index
        )

    if source_type == "entry" and kind == "source":
        return _entry_preview(
            db, user_id=user_id, entry_id=object_id, display_index=display_index
        )

    if source_type == "attachment" and kind == "source":
        return _attachment_preview(
            db, user_id=user_id, attachment_id=object_id, display_index=display_index
        )

    if source_type == "attachment_chunk" and kind == "chunk":
        chunk = (
            db.query(AttachmentChunk)
            .filter(
                AttachmentChunk.id == int(object_id),
                AttachmentChunk.user_id == int(user_id),
            )
            .first()
        )
        if chunk is None:
            raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
        return _attachment_preview(
            db,
            user_id=user_id,
            attachment_id=int(chunk.attachment_id),
            chunk=chunk,
            display_index=display_index,
        )

    if source_type == "knowledge_source" and kind in {"chunk", "source"}:
        if kind == "chunk":
            kc = (
                db.query(KnowledgeChunk)
                .filter(KnowledgeChunk.id == int(object_id))
                .first()
            )
            if kc is None:
                raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
            ks = (
                db.query(KnowledgeSource)
                .filter(
                    KnowledgeSource.id == int(kc.source_id),
                    KnowledgeSource.user_id == int(user_id),
                )
                .first()
            )
            if ks is None:
                raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
            hit_snippet = (kc.content or "").strip()[:400]
            origin_type = str(getattr(ks, "source_type", "") or "").lower()
            origin_id = getattr(ks, "source_id", None)
            if origin_type == "todo" and origin_id is not None:
                return _todo_preview(
                    db, user_id=user_id, todo_id=int(origin_id), display_index=display_index
                )
            if origin_type == "notion_page" and origin_id is not None:
                return _notion_page_preview(
                    db,
                    user_id=user_id,
                    notion_page_id=int(origin_id),
                    display_index=display_index,
                    hit_snippet=hit_snippet,
                )
            if origin_type == "attachment" and origin_id is not None:
                return _attachment_preview(
                    db,
                    user_id=user_id,
                    attachment_id=int(origin_id),
                    display_index=display_index,
                    hit_snippet_override=hit_snippet,
                )
            if origin_type == "entry" and origin_id is not None:
                return _entry_preview(
                    db,
                    user_id=user_id,
                    entry_id=int(origin_id),
                    display_index=display_index,
                    hit_snippet=hit_snippet,
                )
            raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")

        ks = (
            db.query(KnowledgeSource)
            .filter(
                KnowledgeSource.id == int(object_id),
                KnowledgeSource.user_id == int(user_id),
            )
            .first()
        )
        if ks is None:
            raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")
        origin_type = str(getattr(ks, "source_type", "") or "").lower()
        origin_id = getattr(ks, "source_id", None)
        if origin_type == "todo" and origin_id is not None:
            return _todo_preview(
                db, user_id=user_id, todo_id=int(origin_id), display_index=display_index
            )
        if origin_type == "notion_page" and origin_id is not None:
            return _notion_page_preview(
                db, user_id=user_id, notion_page_id=int(origin_id), display_index=display_index
            )
        if origin_type == "attachment" and origin_id is not None:
            return _attachment_preview(
                db, user_id=user_id, attachment_id=int(origin_id), display_index=display_index
            )
        if origin_type == "entry" and origin_id is not None:
            return _entry_preview(
                db, user_id=user_id, entry_id=int(origin_id), display_index=display_index
            )
        raise ReferencePreviewError("SOURCE_REFERENCE_GONE", "来源已删除或无权访问。")

    raise ReferencePreviewError("SOURCE_REFERENCE_INVALID", "所选来源令牌无效。")


def build_pending_proposal_view(
    db: Session,
    *,
    user_id: int,
    session_id: str,
) -> Optional[Dict[str, Any]]:
    """Recover a hidden pending proposal after client disconnect."""
    from app.services.ai_explainer import _candidates_response, _proposal_message
    from app.services.ai_source_set import get_active_proposed
    from app.services.ai_stream_protocol import sanitize_candidates_for_stream

    active = get_active_proposed(db, user_id, session_id)
    if active is None:
        return None
    user_msg = _proposal_message(db, user_id, session_id, int(active.id), "user")
    query = str(user_msg.content or "") if user_msg is not None else ""
    payload = _candidates_response(
        db, user_id=user_id, session_id=session_id, proposal=active, query=query
    )
    sanitized = sanitize_candidates_for_stream(
        payload.get("candidates") or [], user_id=user_id, session_id=session_id
    )
    return {
        "proposal_id": payload.get("proposal_id"),
        "base_version": payload.get("base_version"),
        "phase": payload.get("phase"),
        "message": payload.get("message"),
        "candidates": sanitized,
        "pending_user_content": query,
        "expansion_topic": payload.get("expansion_topic"),
        "request_id": user_msg.request_id if user_msg is not None else None,
    }


def build_current_locked_sources_view(
    db: Session,
    *,
    user_id: int,
    session_id: str,
) -> Dict[str, Any]:
    """Safe display of the explainer session's current locked source set.

    Returns opaque answer_reference tokens for preview only. Never exposes
    reference_key / storage paths / raw internal IDs on the wire.
    """
    from app.models import AIConversationSourceSet
    from app.services.ai_explainer import build_public_candidates
    from app.services.ai_session import get_session_metadata
    from app.services.ai_session_constants import PERSONA_EXPLAINER
    from app.services.ai_source_set import (
        STATUS_LOCKED,
        STATUS_STALE,
        SourceMemberIdentity,
        get_locked_manifest,
    )
    from app.services.ai_stream_protocol import (
        PURPOSE_ANSWER_REFERENCE,
        sign_reference_token,
    )
    from app.services.reference_identity import parse_proposal_reference_key

    empty_note = "讲解员来源只可追加；缩减范围请新建对话。"
    meta = get_session_metadata(db, user_id, session_id)
    if meta is None:
        raise ReferencePreviewError("SESSION_NOT_FOUND", "会话不存在。")
    if str(meta.persona or "") != PERSONA_EXPLAINER:
        raise ReferencePreviewError(
            "SESSION_PERSONA_MISMATCH",
            "固定来源仅适用于讲解员会话。",
        )

    version = meta.current_source_set_version
    if version is None:
        return {
            "session_id": session_id,
            "version": None,
            "status": None,
            "member_count": 0,
            "stale": False,
            "can_expand": False,
            "members": [],
            "append_only_note": empty_note,
        }

    row = (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == int(user_id),
            AIConversationSourceSet.session_id == session_id,
            AIConversationSourceSet.version == int(version),
        )
        .first()
    )
    status = str(row.status) if row is not None else None
    stale = status == STATUS_STALE
    manifest = get_locked_manifest(db, user_id, session_id, int(version))
    if manifest is None:
        return {
            "session_id": session_id,
            "version": int(version),
            "status": status,
            "member_count": 0,
            "stale": stale,
            "can_expand": False,
            "members": [],
            "append_only_note": empty_note,
        }

    identities = [
        SourceMemberIdentity(
            reference_key=f"{m.source_type}:source:{int(m.source_id)}",
            display_order=int(m.display_order),
        )
        for m in manifest.members
        if str(m.source_type or "") in {"entry", "attachment", "todo", "notion_page"}
    ]
    public = build_public_candidates(db, user_id, identities)
    members: list[Dict[str, Any]] = []
    for idx, item in enumerate(public, start=1):
        raw_key = str(item.get("reference_key") or "")
        parsed = parse_proposal_reference_key(raw_key) if raw_key else None
        entry_id = None
        attachment_id = None
        if parsed is not None:
            st, _kind, oid = parsed
            if st == "entry":
                entry_id = int(oid)
            elif st == "attachment":
                attachment_id = int(oid)
        token = sign_reference_token(
            user_id=user_id,
            session_id=session_id,
            display_index=idx,
            purpose=PURPOSE_ANSWER_REFERENCE,
            entry_id=entry_id,
            attachment_id=attachment_id,
            reference_key=raw_key or None,
        )
        source_type = (
            "todo"
            if parsed is not None and parsed[0] == "todo"
            else "attachment" if attachment_id is not None else "entry"
        )
        members.append(
            {
                "display_index": idx,
                "ref_token": token,
                "source_type": source_type,
                "title": str(item.get("title") or "").strip()[:120],
                "snippet": str(item.get("snippet") or "").strip()[:200],
            }
        )

    can_expand = (not stale) and status == STATUS_LOCKED and len(members) > 0
    return {
        "session_id": session_id,
        "version": int(version),
        "status": status,
        "member_count": len(members),
        "stale": stale,
        "can_expand": can_expand,
        "members": members,
        "append_only_note": empty_note,
    }

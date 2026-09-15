"""
Versioned explainer source set contracts (R11.1 storage + CAS only).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Union

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import desc
from sqlalchemy.orm import Session

from app.models import (
    AIChatSession,
    AIConversation,
    AIConversationSourceSet,
    AttachmentChunk,
    Entry,
    EntryAttachment,
    KnowledgeChunk,
    KnowledgeSource,
    NotionConnection,
    NotionPage,
    Todo,
)
from app.services.ai_session_constants import PERSONA_EXPLAINER, SessionPersonaError
from app.services.entry_family import resolve_root_entry_id
from app.services.reference_identity import parse_proposal_reference_key

SOURCE_SET_SCHEMA_VERSION = 1

STATUS_PROPOSED = "proposed"
STATUS_LOCKED = "locked"
STATUS_SUPERSEDED = "superseded"
STATUS_STALE = "stale"

# Statuses that may be cited by historical messages / delayed confirm.
HISTORICAL_SOURCE_SET_STATUSES = frozenset(
    {STATUS_LOCKED, STATUS_SUPERSEDED, STATUS_STALE}
)


class SourceManifestMember(BaseModel):
    """Persisted member: normalized original evidence only (entry|attachment|todo|notion_page)."""

    model_config = ConfigDict(extra="forbid")

    source_type: str = Field(..., min_length=1, max_length=64)
    source_id: int = Field(..., ge=1)
    display_order: int = Field(..., ge=0)
    entry_id: Optional[int] = Field(default=None, ge=1)
    attachment_id: Optional[int] = Field(default=None, ge=1)
    family_root_entry_id: Optional[int] = Field(default=None, ge=1)
    todo_id: Optional[int] = Field(default=None, ge=1)
    family_root_todo_id: Optional[int] = Field(default=None, ge=1)
    content_hash: Optional[str] = Field(default=None, min_length=64, max_length=64)


class SourceMemberIdentity(BaseModel):
    """
    Proposal identity for public API and service layer.
    Uses unambiguous canonical reference_key only.
    """

    model_config = ConfigDict(extra="forbid")

    reference_key: str = Field(..., min_length=3, max_length=128)
    display_order: int = Field(default=0, ge=0)


class SourceManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SOURCE_SET_SCHEMA_VERSION, ge=1)
    family_root_entry_id: Optional[int] = Field(default=None, ge=1)
    members: List[SourceManifestMember] = Field(default_factory=list)

    @field_validator("members")
    @classmethod
    def _members_sorted(cls, value: List[SourceManifestMember]) -> List[SourceManifestMember]:
        return sorted(value, key=lambda m: (m.display_order, m.source_type, m.source_id))


class SourceSetError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def canonical_manifest_dict(manifest: SourceManifest) -> Dict[str, Any]:
    return json.loads(manifest.model_dump_json())


def fingerprint_manifest(manifest: SourceManifest) -> str:
    payload = canonical_manifest_dict(manifest)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _require_explainer_session(db: Session, user_id: int, session_id: str) -> AIChatSession:
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
            "来源集仅适用于讲解员会话。",
        )
    return row


def _entry_content_hash(entry: Entry) -> str:
    text = (entry.content or "").encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def _member_from_entry(
    db: Session,
    user_id: int,
    entry: Entry,
    display_order: int,
) -> SourceManifestMember:
    root_id = resolve_root_entry_id(db, user_id, int(entry.id))
    return SourceManifestMember(
        source_type="entry",
        source_id=int(entry.id),
        display_order=display_order,
        entry_id=int(entry.id),
        attachment_id=None,
        family_root_entry_id=int(root_id) if root_id is not None else int(entry.id),
        content_hash=_entry_content_hash(entry),
    )


def _member_from_todo(
    db: Session,
    user_id: int,
    todo: Todo,
    display_order: int,
) -> SourceManifestMember:
    from app.services.todo_hierarchy import load_index, family_root_id

    index = load_index(db, int(user_id))
    root_id = family_root_id(int(todo.id), index)
    return SourceManifestMember(
        source_type="todo",
        source_id=int(todo.id),
        display_order=display_order,
        entry_id=None,
        attachment_id=None,
        family_root_entry_id=None,
        todo_id=int(todo.id),
        family_root_todo_id=int(root_id),
        content_hash=None,
    )


def _member_from_notion_page(
    db: Session,
    user_id: int,
    page: NotionPage,
    display_order: int,
) -> SourceManifestMember:
    from app.services.notion_errors import NOTION_SOURCE_SYNC_PENDING, public_message
    from app.services.notion_reference import page_is_searchable

    connection = (
        db.query(NotionConnection)
        .filter(
            NotionConnection.id == int(page.connection_id),
            NotionConnection.user_id == int(user_id),
        )
        .first()
    )
    if connection is None or connection.status == "disconnected":
        raise SourceSetError("SOURCE_NOT_FOUND", "Notion 页面不存在或无权访问。")
    if str(page.sync_status or "") in {"permission_lost", "deleted"}:
        raise SourceSetError("SOURCE_NOT_FOUND", "Notion 页面不存在或无权访问。")
    if str(page.sync_status or "") in {"pending", "processing"} or not page_is_searchable(page):
        raise SourceSetError(NOTION_SOURCE_SYNC_PENDING, public_message(NOTION_SOURCE_SYNC_PENDING))
    return SourceManifestMember(
        source_type="notion_page",
        source_id=int(page.id),
        display_order=display_order,
        entry_id=None,
        attachment_id=None,
        family_root_entry_id=None,
        todo_id=None,
        family_root_todo_id=None,
        content_hash=None,
    )


def _attachment_evidence_content_hash(
    db: Session,
    user_id: int,
    attachment: EntryAttachment,
) -> str:
    """Fingerprint file hash + ordered chunk text/identity (OCR changes must stale)."""
    file_hash = str(attachment.content_hash or "")
    if len(file_hash) != 64:
        file_hash = hashlib.sha256(file_hash.encode("utf-8")).hexdigest()
    chunks = (
        db.query(AttachmentChunk)
        .filter(
            AttachmentChunk.attachment_id == int(attachment.id),
            AttachmentChunk.user_id == user_id,
        )
        .order_by(
            AttachmentChunk.chunk_index.asc(),
            AttachmentChunk.page_no.asc(),
            AttachmentChunk.slide_no.asc(),
            AttachmentChunk.id.asc(),
        )
        .all()
    )
    chunk_payload = []
    for ch in chunks:
        body = (ch.content or "").encode("utf-8")
        chunk_payload.append(
            {
                "chunk_index": int(ch.chunk_index or 0),
                "page_no": ch.page_no,
                "slide_no": ch.slide_no,
                "modality": str(ch.modality or ""),
                "content_sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    payload = {
        "file_hash": file_hash,
        "status": str(attachment.status or ""),
        "chunks": chunk_payload,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _member_from_attachment(
    db: Session,
    user_id: int,
    attachment: EntryAttachment,
    display_order: int,
) -> SourceManifestMember:
    if str(attachment.status or "") != "indexed":
        raise SourceSetError("SOURCE_NOT_FOUND", "来源附件未完成索引或不可用。")
    root_id = resolve_root_entry_id(db, user_id, int(attachment.entry_id))
    return SourceManifestMember(
        source_type="attachment",
        source_id=int(attachment.id),
        display_order=display_order,
        entry_id=int(attachment.entry_id),
        attachment_id=int(attachment.id),
        family_root_entry_id=int(root_id) if root_id is not None else int(attachment.entry_id),
        content_hash=_attachment_evidence_content_hash(db, user_id, attachment),
    )


def _identity_to_reference_key(
    identity: Union[SourceMemberIdentity, SourceManifestMember],
) -> str:
    if isinstance(identity, SourceMemberIdentity):
        return identity.reference_key
    # Re-resolve persisted normalized members.
    st = identity.source_type.strip().lower()
    if st == "entry":
        return f"entry:source:{int(identity.source_id)}"
    if st == "attachment":
        return f"attachment:source:{int(identity.source_id)}"
    if st == "todo":
        return f"todo:source:{int(identity.source_id)}"
    if st == "notion_page":
        return f"notion_page:source:{int(identity.source_id)}"
    raise SourceSetError("SOURCE_TYPE_INVALID", "不支持的来源类型。")


def _resolve_one_member(
    db: Session,
    user_id: int,
    identity: Union[SourceMemberIdentity, SourceManifestMember],
) -> SourceManifestMember:
    """
    Resolve canonical reference_key (or persisted entry/attachment member)
    to a normalized original evidence member. Ownership always rechecked.
    """
    order = int(identity.display_order)
    raw_key = _identity_to_reference_key(identity)
    parsed = parse_proposal_reference_key(raw_key)
    if parsed is None:
        raise SourceSetError("SOURCE_REFERENCE_INVALID", "来源标识无效。")
    source_type, kind, object_id = parsed

    if source_type == "todo" and kind == "source":
        todo = db.query(Todo).filter(Todo.id == object_id, Todo.user_id == user_id).first()
        if todo is None:
            raise SourceSetError("SOURCE_NOT_FOUND", "小要事不存在或无权访问。")
        return _member_from_todo(db, user_id, todo, order)

    if source_type == "notion_page" and kind == "source":
        page = (
            db.query(NotionPage)
            .filter(NotionPage.id == object_id, NotionPage.user_id == user_id)
            .first()
        )
        if page is None:
            raise SourceSetError("SOURCE_NOT_FOUND", "Notion 页面不存在或无权访问。")
        return _member_from_notion_page(db, user_id, page, order)

    if source_type == "entry" and kind == "source":
        entry = (
            db.query(Entry)
            .filter(Entry.id == object_id, Entry.user_id == user_id)
            .first()
        )
        if entry is None:
            raise SourceSetError("SOURCE_NOT_FOUND", "来源记录不存在或无权访问。")
        return _member_from_entry(db, user_id, entry, order)

    if source_type == "attachment" and kind == "source":
        attachment = (
            db.query(EntryAttachment)
            .filter(
                EntryAttachment.id == object_id,
                EntryAttachment.user_id == user_id,
            )
            .first()
        )
        if attachment is None or attachment.status == "failed":
            raise SourceSetError("SOURCE_NOT_FOUND", "来源附件不存在或不可用。")
        return _member_from_attachment(db, user_id, attachment, order)

    if source_type == "attachment_chunk" and kind == "chunk":
        chunk = (
            db.query(AttachmentChunk)
            .filter(
                AttachmentChunk.id == object_id,
                AttachmentChunk.user_id == user_id,
            )
            .first()
        )
        if chunk is None:
            raise SourceSetError("SOURCE_NOT_FOUND", "来源附件分块不存在或无权访问。")
        attachment = (
            db.query(EntryAttachment)
            .filter(
                EntryAttachment.id == int(chunk.attachment_id),
                EntryAttachment.user_id == user_id,
            )
            .first()
        )
        if attachment is None or attachment.status == "failed":
            raise SourceSetError("SOURCE_NOT_FOUND", "来源附件不存在或不可用。")
        return _member_from_attachment(db, user_id, attachment, order)

    if source_type == "knowledge_source" and kind == "chunk":
        kc = (
            db.query(KnowledgeChunk)
            .filter(
                KnowledgeChunk.id == object_id,
                KnowledgeChunk.user_id == user_id,
            )
            .first()
        )
        if kc is None:
            raise SourceSetError("SOURCE_NOT_FOUND", "知识分块不存在或无权访问。")
        origin_type = str(kc.origin_type or "").strip().lower()
        origin_id = int(kc.origin_id)
        if origin_type == "entry":
            entry = (
                db.query(Entry)
                .filter(Entry.id == origin_id, Entry.user_id == user_id)
                .first()
            )
            if entry is None:
                raise SourceSetError("SOURCE_NOT_FOUND", "知识镜像对应记录不存在。")
            return _member_from_entry(db, user_id, entry, order)
        if origin_type == "todo":
            todo = db.query(Todo).filter(Todo.id == origin_id, Todo.user_id == user_id).first()
            if todo is None:
                raise SourceSetError("SOURCE_NOT_FOUND", "知识镜像对应小要事不存在。")
            return _member_from_todo(db, user_id, todo, order)
        if origin_type == "notion_page":
            page = (
                db.query(NotionPage)
                .filter(NotionPage.id == origin_id, NotionPage.user_id == user_id)
                .first()
            )
            if page is None:
                raise SourceSetError("SOURCE_NOT_FOUND", "知识镜像对应 Notion 页面不存在。")
            return _member_from_notion_page(db, user_id, page, order)
        if origin_type == "attachment":
            attachment = (
                db.query(EntryAttachment)
                .filter(
                    EntryAttachment.id == origin_id,
                    EntryAttachment.user_id == user_id,
                )
                .first()
            )
            if attachment is None or attachment.status == "failed":
                raise SourceSetError("SOURCE_NOT_FOUND", "知识镜像对应附件不存在。")
            return _member_from_attachment(db, user_id, attachment, order)
        raise SourceSetError("SOURCE_TYPE_INVALID", "不支持的知识来源类型。")

    raise SourceSetError("SOURCE_REFERENCE_INVALID", "来源标识无效。")


def _resolve_members_from_db(
    db: Session,
    user_id: int,
    members: Sequence[Union[SourceMemberIdentity, SourceManifestMember]],
) -> List[SourceManifestMember]:
    resolved: List[SourceManifestMember] = []
    seen: set[tuple[str, int]] = set()
    for identity in members:
        member = _resolve_one_member(db, user_id, identity)
        key = (member.source_type, int(member.source_id))
        if key in seen:
            continue
        seen.add(key)
        resolved.append(member)
    return [
        m.model_copy(update={"display_order": idx})
        for idx, m in enumerate(resolved)
    ]


def parse_manifest(raw: Any) -> SourceManifest:
    if isinstance(raw, SourceManifest):
        return raw
    return SourceManifest.model_validate(raw)


def _manifest_for_lock(members: Sequence[SourceManifestMember]) -> SourceManifest:
    """Match confirm-time fingerprint shape (members only; family_root None)."""
    return SourceManifest(members=list(members))


def _member_key(member: SourceManifestMember) -> str:
    return f"{member.source_type.strip().lower()}:source:{int(member.source_id)}"


def _find_active_proposal(
    db: Session,
    user_id: int,
    session_id: str,
    *,
    base_version: int,
    fingerprint: str,
) -> Optional[AIConversationSourceSet]:
    rows = (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
            AIConversationSourceSet.status == STATUS_PROPOSED,
            AIConversationSourceSet.base_version == int(base_version),
        )
        .order_by(AIConversationSourceSet.id.desc())
        .all()
    )
    for row in rows:
        man = parse_manifest(row.source_manifest)
        if fingerprint_manifest(_manifest_for_lock(man.members)) == fingerprint:
            return row
    return None


def get_active_proposed(
    db: Session,
    user_id: int,
    session_id: str,
) -> Optional[AIConversationSourceSet]:
    return (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
            AIConversationSourceSet.status == STATUS_PROPOSED,
        )
        .order_by(AIConversationSourceSet.id.desc())
        .first()
    )


def get_proposal_for_session(
    db: Session,
    user_id: int,
    session_id: str,
    proposal_id: int,
) -> Optional[AIConversationSourceSet]:
    """Return proposal only when it belongs to the given user/session (no cross-user leak)."""
    return (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.id == int(proposal_id),
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
        )
        .first()
    )


def resolve_confirmed_proposal_version(
    db: Session,
    user_id: int,
    session_id: str,
    proposal_id: int,
) -> tuple[AIConversationSourceSet, int]:
    """
    Resolve a confirmed proposal to its exact target locked version.
    Unconfirmed / missing / unresolvable cases use stable SourceSetError codes.
    """
    proposal = get_proposal_for_session(db, user_id, session_id, proposal_id)
    if proposal is None:
        raise SourceSetError("SOURCE_SET_NOT_FOUND", "来源 proposal 不存在或无权访问。")
    if proposal.status == STATUS_PROPOSED:
        raise SourceSetError("SOURCE_SET_NOT_CONFIRMED", "请先确认来源后再继续讲解。")
    if proposal.status == STATUS_STALE:
        raise SourceSetError("SOURCE_SET_STALE", "固定来源已失效，请新建讲解员对话。")

    target_manifest, target_version = _rebuild_target_manifest_from_proposal(
        db, user_id, session_id, proposal
    )
    fingerprint = fingerprint_manifest(target_manifest)
    existing = _find_exact_historical_result(
        db,
        user_id,
        session_id,
        target_version=target_version,
        fingerprint=fingerprint,
    )
    if existing is None:
        raise SourceSetError(
            "SOURCE_SET_VERSION_CONFLICT",
            "来源版本已变化，请刷新后重试。",
        )
    return proposal, int(target_version)


def cancel_proposal(
    db: Session,
    user_id: int,
    session_id: str,
    proposal_id: int,
) -> Dict[str, Any]:
    """
    Cancel a proposed source set in one transaction.
    Deletes pending user message(s) bound to proposal_id, then the proposal row.
    Does not change current_source_set_version or delete historical versions.
    Missing proposal for this user/session → idempotent (no leak).
    """
    try:
        session = (
            db.query(AIChatSession)
            .filter(
                AIChatSession.user_id == user_id,
                AIChatSession.session_id == session_id,
            )
            .with_for_update()
            .first()
        )
        if session is None:
            db.rollback()
            return {
                "session_id": session_id,
                "proposal_id": int(proposal_id),
                "cancelled": False,
                "idempotent": True,
            }
        if session.persona != PERSONA_EXPLAINER:
            raise SessionPersonaError(
                "SESSION_PERSONA_MISMATCH",
                "来源集仅适用于讲解员会话。",
            )

        proposal = (
            db.query(AIConversationSourceSet)
            .filter(
                AIConversationSourceSet.id == int(proposal_id),
                AIConversationSourceSet.user_id == user_id,
                AIConversationSourceSet.session_id == session_id,
            )
            .with_for_update()
            .first()
        )
        if proposal is None:
            db.rollback()
            return {
                "session_id": session_id,
                "proposal_id": int(proposal_id),
                "cancelled": False,
                "idempotent": True,
            }

        if proposal.status != STATUS_PROPOSED:
            if proposal.status in {STATUS_LOCKED, STATUS_SUPERSEDED}:
                raise SourceSetError(
                    "SOURCE_SET_ALREADY_CONFIRMED",
                    "来源 proposal 已确认，无法取消。",
                )
            if proposal.status == STATUS_STALE:
                raise SourceSetError(
                    "SOURCE_SET_STALE",
                    "固定来源已失效，请新建讲解员对话。",
                )
            raise SourceSetError(
                "SOURCE_SET_ALREADY_CONFIRMED",
                "来源 proposal 已确认，无法取消。",
            )

        (
            db.query(AIConversation)
            .filter(
                AIConversation.user_id == user_id,
                AIConversation.session_id == session_id,
                AIConversation.proposal_id == int(proposal_id),
                AIConversation.role == "user",
            )
            .delete(synchronize_session=False)
        )
        db.delete(proposal)
        session.last_message_at = datetime.utcnow()
        db.add(session)
        db.commit()
        return {
            "session_id": session_id,
            "proposal_id": int(proposal_id),
            "cancelled": True,
            "idempotent": False,
            "current_source_set_version": session.current_source_set_version,
        }
    except (SourceSetError, SessionPersonaError):
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def create_initial_proposal(
    db: Session,
    user_id: int,
    session_id: str,
    members: Sequence[SourceMemberIdentity],
    *,
    commit: bool = True,
) -> AIConversationSourceSet:
    session = _require_explainer_session(db, user_id, session_id)
    if session.current_source_set_version is not None:
        raise SourceSetError(
            "SOURCE_SET_IMMUTABLE",
            "当前会话已有锁定来源，请使用扩展 proposal。",
        )
    resolved = _resolve_members_from_db(db, user_id, members)
    roots = {m.family_root_entry_id for m in resolved if m.family_root_entry_id}
    manifest = SourceManifest(
        members=resolved,
        family_root_entry_id=next(iter(roots)) if len(roots) == 1 else None,
    )
    fp = fingerprint_manifest(_manifest_for_lock(resolved))
    existing = _find_active_proposal(
        db, user_id, session_id, base_version=0, fingerprint=fp
    )
    if existing is not None:
        return existing
    row = AIConversationSourceSet(
        user_id=user_id,
        session_id=session_id,
        version=None,
        base_version=0,
        status=STATUS_PROPOSED,
        source_manifest=canonical_manifest_dict(manifest),
        content_fingerprint=None,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
        db.refresh(row)
    return row


def create_expansion_proposal(
    db: Session,
    user_id: int,
    session_id: str,
    members: Sequence[SourceMemberIdentity],
    *,
    base_version: int,
    commit: bool = True,
) -> AIConversationSourceSet:
    session = _require_explainer_session(db, user_id, session_id)
    current = session.current_source_set_version or 0
    if current != int(base_version):
        raise SourceSetError(
            "SOURCE_SET_VERSION_CONFLICT",
            "来源版本已变化，请刷新后重试。",
        )
    if current == 0:
        raise SourceSetError(
            "SOURCE_SET_NOT_LOCKED",
            "请先锁定首版来源。",
        )
    resolved = _resolve_members_from_db(db, user_id, members)
    roots = {m.family_root_entry_id for m in resolved if m.family_root_entry_id}
    manifest = SourceManifest(
        members=resolved,
        family_root_entry_id=next(iter(roots)) if len(roots) == 1 else None,
    )
    fp = fingerprint_manifest(_manifest_for_lock(resolved))
    existing = _find_active_proposal(
        db, user_id, session_id, base_version=int(base_version), fingerprint=fp
    )
    if existing is not None:
        return existing
    row = AIConversationSourceSet(
        user_id=user_id,
        session_id=session_id,
        version=None,
        base_version=int(base_version),
        status=STATUS_PROPOSED,
        source_manifest=canonical_manifest_dict(manifest),
        content_fingerprint=None,
    )
    db.add(row)
    if commit:
        db.commit()
        db.refresh(row)
    else:
        db.flush()
        db.refresh(row)
    return row


def filter_manifest_by_selection(
    manifest: SourceManifest,
    selected_reference_keys: Sequence[str],
) -> List[SourceManifestMember]:
    """Keep only selected members / family groups that already exist in the proposal."""
    selected = {str(k).strip() for k in selected_reference_keys if str(k).strip()}
    if not selected:
        raise SourceSetError("SOURCE_SET_EMPTY", "请至少选择一个来源候选组。")
    proposal_keys = {_member_key(m) for m in manifest.members}
    family_roots = {
        f"entry:source:{int(m.family_root_entry_id)}"
        for m in manifest.members
        if m.family_root_entry_id is not None
    }
    for key in selected:
        if key not in proposal_keys and key not in family_roots:
            raise SourceSetError(
                "SOURCE_REFERENCE_INVALID",
                "所选来源不在当前候选 proposal 中。",
            )
    chosen: List[SourceManifestMember] = []
    for member in manifest.members:
        key = _member_key(member)
        family_key = (
            f"entry:source:{int(member.family_root_entry_id)}"
            if member.family_root_entry_id is not None
            else None
        )
        if key in selected or (family_key and family_key in selected):
            chosen.append(member)
    if not chosen:
        raise SourceSetError("SOURCE_SET_EMPTY", "所选来源为空，无法确认。")
    return [
        m.model_copy(update={"display_order": idx})
        for idx, m in enumerate(chosen)
    ]


def _historical_version_row(
    db: Session,
    user_id: int,
    session_id: str,
    version: int,
) -> Optional[AIConversationSourceSet]:
    return (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
            AIConversationSourceSet.version == version,
            AIConversationSourceSet.status.in_(list(HISTORICAL_SOURCE_SET_STATUSES)),
        )
        .first()
    )


def _locked_row(
    db: Session,
    user_id: int,
    session_id: str,
    version: int,
) -> Optional[AIConversationSourceSet]:
    return (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
            AIConversationSourceSet.version == version,
            AIConversationSourceSet.status == STATUS_LOCKED,
        )
        .first()
    )


def _refetch_source_set(db: Session, row_id: int) -> AIConversationSourceSet:
    row = (
        db.query(AIConversationSourceSet)
        .filter(AIConversationSourceSet.id == row_id)
        .first()
    )
    if row is None:
        raise SourceSetError("SOURCE_SET_NOT_FOUND", "来源 proposal 不存在或已处理。")
    return row


def _dedupe_members(
    existing: Sequence[SourceManifestMember],
    additions: Sequence[SourceManifestMember],
) -> List[SourceManifestMember]:
    seen: set[tuple[str, int]] = set()
    merged: List[SourceManifestMember] = []
    order = 0
    for member in list(existing) + list(additions):
        key = (member.source_type.strip().lower(), int(member.source_id))
        if key in seen:
            continue
        seen.add(key)
        merged.append(member.model_copy(update={"display_order": order}))
        order += 1
    return merged


def _rebuild_target_manifest_from_proposal(
    db: Session,
    user_id: int,
    session_id: str,
    proposal: AIConversationSourceSet,
) -> tuple[SourceManifest, int]:
    """Rebuild the exact locked manifest that a proposal historically produced."""
    proposal_base = int(proposal.base_version or 0)
    proposal_manifest = parse_manifest(proposal.source_manifest)
    if proposal_base == 0:
        return _manifest_for_lock(proposal_manifest.members), 1

    base_row = _historical_version_row(db, user_id, session_id, proposal_base)
    if base_row is None:
        raise SourceSetError(
            "SOURCE_SET_NOT_FOUND",
            "来源 proposal 的历史基础版本不可用。",
        )
    base_manifest = parse_manifest(base_row.source_manifest)
    merged_members = _dedupe_members(base_manifest.members, proposal_manifest.members)
    return _manifest_for_lock(merged_members), proposal_base + 1


def _find_exact_historical_result(
    db: Session,
    user_id: int,
    session_id: str,
    *,
    target_version: int,
    fingerprint: str,
) -> Optional[AIConversationSourceSet]:
    return (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
            AIConversationSourceSet.version == target_version,
            AIConversationSourceSet.content_fingerprint == fingerprint,
            AIConversationSourceSet.status.in_(list(HISTORICAL_SOURCE_SET_STATUSES)),
        )
        .first()
    )


def confirm_proposal(
    db: Session,
    user_id: int,
    session_id: str,
    proposal_id: int,
    *,
    base_version: int,
    selected_reference_keys: Optional[Sequence[str]] = None,
) -> AIConversationSourceSet:
    try:
        session = _require_explainer_session(db, user_id, session_id)
        proposal = (
            db.query(AIConversationSourceSet)
            .filter(
                AIConversationSourceSet.id == proposal_id,
                AIConversationSourceSet.user_id == user_id,
                AIConversationSourceSet.session_id == session_id,
            )
            .with_for_update()
            .first()
        )
        if proposal is None:
            raise SourceSetError("SOURCE_SET_NOT_FOUND", "来源 proposal 不存在或已处理。")

        request_base = int(base_version)
        proposal_base = int(proposal.base_version or 0)
        current = int(session.current_source_set_version or 0)

        # Delayed / idempotent re-confirm of an already-applied proposal.
        if proposal.status == STATUS_SUPERSEDED:
            if request_base != proposal_base:
                raise SourceSetError(
                    "SOURCE_SET_VERSION_CONFLICT",
                    "来源版本已变化，请刷新后重试。",
                )
            target_manifest, target_version = _rebuild_target_manifest_from_proposal(
                db, user_id, session_id, proposal
            )
            fingerprint = fingerprint_manifest(target_manifest)
            existing = _find_exact_historical_result(
                db,
                user_id,
                session_id,
                target_version=target_version,
                fingerprint=fingerprint,
            )
            if existing is None:
                raise SourceSetError(
                    "SOURCE_SET_NOT_FOUND",
                    "未找到该 proposal 对应的历史来源版本。",
                )
            eid = int(existing.id)
            db.rollback()
            return _refetch_source_set(db, eid)

        if proposal.status != STATUS_PROPOSED:
            raise SourceSetError("SOURCE_SET_NOT_FOUND", "来源 proposal 不存在或已处理。")

        if not (request_base == proposal_base == current):
            raise SourceSetError(
                "SOURCE_SET_VERSION_CONFLICT",
                "来源版本已变化，请刷新后重试。",
            )

        proposal_manifest = parse_manifest(proposal.source_manifest)
        if not proposal_manifest.members:
            raise SourceSetError("SOURCE_SET_EMPTY", "来源候选为空，无法确认。")

        if selected_reference_keys is not None:
            selected_members = filter_manifest_by_selection(
                proposal_manifest, selected_reference_keys
            )
            # Persist narrowed selection onto proposal so delayed confirm rebuilds exact set.
            proposal.source_manifest = canonical_manifest_dict(
                SourceManifest(members=selected_members)
            )
            db.add(proposal)
            db.flush()
            proposal_manifest = parse_manifest(proposal.source_manifest)

        if proposal_base == 0:
            merged_members = proposal_manifest.members
            next_version = 1
        else:
            locked = _locked_row(db, user_id, session_id, proposal_base)
            if locked is None:
                raise SourceSetError(
                    "SOURCE_SET_VERSION_CONFLICT",
                    "基础来源版本不可用。",
                )
            base_manifest = parse_manifest(locked.source_manifest)
            merged_members = _dedupe_members(base_manifest.members, proposal_manifest.members)
            next_version = proposal_base + 1

        merged = _manifest_for_lock(merged_members)
        fingerprint = fingerprint_manifest(merged)
        now = datetime.utcnow()

        existing_same = _find_exact_historical_result(
            db,
            user_id,
            session_id,
            target_version=next_version,
            fingerprint=fingerprint,
        )
        if existing_same is not None and existing_same.status == STATUS_LOCKED:
            proposal.status = STATUS_SUPERSEDED
            db.add(proposal)
            session.current_source_set_version = next_version
            db.add(session)
            db.commit()
            db.refresh(existing_same)
            return existing_same

        if proposal_base > 0:
            old_locked = _locked_row(db, user_id, session_id, proposal_base)
            if old_locked is not None:
                old_locked.status = STATUS_SUPERSEDED
                db.add(old_locked)

        locked_row = AIConversationSourceSet(
            user_id=user_id,
            session_id=session_id,
            version=next_version,
            base_version=proposal_base,
            status=STATUS_LOCKED,
            source_manifest=canonical_manifest_dict(merged),
            content_fingerprint=fingerprint,
            locked_at=now,
        )
        db.add(locked_row)

        proposal.status = STATUS_SUPERSEDED
        db.add(proposal)

        session.current_source_set_version = next_version
        db.add(session)

        db.commit()
        db.refresh(locked_row)
        return locked_row
    except SourceSetError:
        db.rollback()
        raise
    except SessionPersonaError:
        db.rollback()
        raise
    except Exception:
        db.rollback()
        raise


def validate_source_set_version_for_message(
    db: Session,
    user_id: int,
    session_id: str,
    source_set_version: Optional[int],
) -> Optional[int]:
    """Validate source_set_version before writing ai_conversations."""
    if source_set_version is None:
        return None

    version = int(source_set_version)
    session = (
        db.query(AIChatSession)
        .filter(
            AIChatSession.user_id == user_id,
            AIChatSession.session_id == session_id,
        )
        .first()
    )
    if session is None:
        raise SourceSetError(
            "SOURCE_SET_VERSION_INVALID",
            "仅讲解员会话可绑定来源版本。",
        )
    if session.persona != PERSONA_EXPLAINER:
        raise SessionPersonaError(
            "SESSION_PERSONA_MISMATCH",
            "检索员与整理师消息不得携带 source_set_version。",
        )

    row = (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
            AIConversationSourceSet.version == version,
            AIConversationSourceSet.status.in_(list(HISTORICAL_SOURCE_SET_STATUSES)),
        )
        .first()
    )
    if row is None:
        raise SourceSetError(
            "SOURCE_SET_VERSION_INVALID",
            "来源版本不存在或不可用于历史引用。",
        )
    return version


def mark_source_sets_stale(
    db: Session,
    user_id: int,
    session_id: str,
) -> int:
    rows = (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
            AIConversationSourceSet.status == STATUS_LOCKED,
        )
        .all()
    )
    count = 0
    for row in rows:
        row.status = STATUS_STALE
        db.add(row)
        count += 1
    if count:
        db.commit()
    return count


def list_source_set_versions(
    db: Session,
    user_id: int,
    session_id: str,
) -> List[Dict[str, Any]]:
    rows = (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
        )
        .order_by(desc(AIConversationSourceSet.version), desc(AIConversationSourceSet.created_at))
        .all()
    )
    return [
        {
            "id": row.id,
            "version": row.version,
            "base_version": row.base_version,
            "status": row.status,
            "content_fingerprint": row.content_fingerprint,
            "member_count": len(parse_manifest(row.source_manifest).members),
            "locked_at": row.locked_at.isoformat() if row.locked_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }
        for row in rows
        if row.status != STATUS_PROPOSED
    ]


def get_locked_manifest(
    db: Session,
    user_id: int,
    session_id: str,
    version: int,
) -> Optional[SourceManifest]:
    row = (
        db.query(AIConversationSourceSet)
        .filter(
            AIConversationSourceSet.user_id == user_id,
            AIConversationSourceSet.session_id == session_id,
            AIConversationSourceSet.version == version,
            AIConversationSourceSet.status.in_(list(HISTORICAL_SOURCE_SET_STATUSES)),
        )
        .first()
    )
    if row is None:
        return None
    return parse_manifest(row.source_manifest)


def check_and_mark_stale_if_changed(
    db: Session,
    user_id: int,
    session_id: str,
    version: int,
) -> bool:
    row = _locked_row(db, user_id, session_id, version)
    if row is None:
        return False
    manifest = parse_manifest(row.source_manifest)
    try:
        resolved = _resolve_members_from_db(db, user_id, manifest.members)
    except SourceSetError as exc:
        if getattr(exc, "code", "") == "NOTION_SOURCE_SYNC_PENDING":
            return False
        row.status = STATUS_STALE
        db.add(row)
        db.commit()
        return True
    refreshed = _manifest_for_lock(resolved)
    if fingerprint_manifest(refreshed) != row.content_fingerprint:
        row.status = STATUS_STALE
        db.add(row)
        db.commit()
        return True
    return False

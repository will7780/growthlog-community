"""
Formal entry deletion service (R8.1).

Deletes an owned entry tree with attachment/job/knowledge/embedding/file/vector cleanup.
Only operates on IDs verified to belong to the given user — no broad DELETE.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import List, Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from app.models import (
    AIDerivedContent,
    Embedding,
    Entry,
    EntryAttachment,
)
from app.services.attachment_cleanup import (
    cancel_attachment_jobs,
    delete_attachment_knowledge,
    delete_stored_files_best_effort,
)

logger = logging.getLogger(__name__)


@dataclass
class EntryDeletionResult:
    user_id: int
    root_entry_id: int
    entry_ids: List[int] = field(default_factory=list)
    attachment_ids: List[int] = field(default_factory=list)
    storage_paths: List[str] = field(default_factory=list)


class EntryDeletionError(Exception):
    """Raised when the target entry is missing or not owned (route maps to 404)."""


def collect_owned_subtree_ids(
    db: Session,
    *,
    user_id: int,
    root_entry_id: int,
) -> list[int]:
    """BFS collect root + descendants that belong to user_id."""
    uid = int(user_id)
    root_id = int(root_entry_id)
    root = (
        db.query(Entry.id)
        .filter(Entry.id == root_id, Entry.user_id == uid)
        .first()
    )
    if root is None:
        return []

    collected: list[int] = []
    seen: set[int] = set()
    frontier = [root_id]
    while frontier:
        batch = [eid for eid in frontier if eid not in seen]
        frontier = []
        if not batch:
            break
        owned = (
            db.query(Entry.id)
            .filter(Entry.user_id == uid, Entry.id.in_(batch))
            .all()
        )
        owned_ids = [int(row[0]) for row in owned]
        for eid in owned_ids:
            if eid in seen:
                continue
            seen.add(eid)
            collected.append(eid)
        if not owned_ids:
            continue
        children = (
            db.query(Entry.id)
            .filter(Entry.user_id == uid, Entry.parent_id.in_(owned_ids))
            .all()
        )
        frontier.extend(int(row[0]) for row in children)
    return sorted(collected)


def _delete_entry_knowledge(
    db: Session,
    *,
    user_id: int,
    entry_ids: Sequence[int],
) -> None:
    eids = [int(x) for x in entry_ids]
    if not eids:
        return
    params = {"uid": int(user_id), "eids": eids}
    db.execute(
        text(
            """
            DELETE ke FROM knowledge_embeddings ke
            JOIN knowledge_chunks kc ON kc.id = ke.chunk_id
            JOIN knowledge_sources ks ON ks.id = kc.source_id
            WHERE ks.user_id = :uid
              AND ks.source_type = 'entry'
              AND ks.source_id IN :eids
            """
        ).bindparams(bindparam("eids", expanding=True)),
        params,
    )
    db.execute(
        text(
            """
            DELETE kc FROM knowledge_chunks kc
            JOIN knowledge_sources ks ON ks.id = kc.source_id
            WHERE ks.user_id = :uid
              AND ks.source_type = 'entry'
              AND ks.source_id IN :eids
            """
        ).bindparams(bindparam("eids", expanding=True)),
        params,
    )
    db.execute(
        text(
            """
            DELETE FROM knowledge_sources
            WHERE user_id = :uid
              AND source_type = 'entry'
              AND source_id IN :eids
            """
        ).bindparams(bindparam("eids", expanding=True)),
        params,
    )


def _scrub_ai_derived_contents(
    db: Session,
    *,
    user_id: int,
    entry_ids: Sequence[int],
) -> None:
    """
    ai_derived_contents.source_entry_ids is JSON without FK.
    Scrub deleted IDs for this user only; delete row when sources become empty.
    """
    deleted = {int(x) for x in entry_ids}
    if not deleted:
        return
    rows = (
        db.query(AIDerivedContent)
        .filter(AIDerivedContent.user_id == int(user_id))
        .all()
    )
    for row in rows:
        raw = list(row.source_entry_ids or [])
        try:
            src = [int(x) for x in raw]
        except (TypeError, ValueError):
            continue
        if not any(eid in deleted for eid in src):
            continue
        kept = [eid for eid in src if eid not in deleted]
        if not kept:
            db.delete(row)
        else:
            row.source_entry_ids = kept


def delete_owned_entry_tree(
    db: Session,
    *,
    user_id: int,
    entry_id: int,
) -> EntryDeletionResult:
    """
    Lock + clean + delete owned entry subtree in one transaction.
    Caller must commit/rollback; after successful commit, call
    finalize_entry_deletion_side_effects(result).
    """
    uid = int(user_id)
    root_id = int(entry_id)
    entry_ids = collect_owned_subtree_ids(db, user_id=uid, root_entry_id=root_id)
    if not entry_ids or root_id not in entry_ids:
        raise EntryDeletionError("entry_not_found")

    locked = (
        db.query(Entry)
        .filter(Entry.user_id == uid, Entry.id.in_(entry_ids))
        .order_by(Entry.id.asc())
        .with_for_update()
        .all()
    )
    locked_ids = sorted(int(row.id) for row in locked)
    if locked_ids != sorted(entry_ids):
        raise EntryDeletionError("entry_not_found")

    attachments = (
        db.query(EntryAttachment)
        .filter(
            EntryAttachment.user_id == uid,
            EntryAttachment.entry_id.in_(locked_ids),
        )
        .order_by(EntryAttachment.id.asc())
        .with_for_update()
        .all()
    )
    attachment_ids = [int(a.id) for a in attachments]
    storage_paths = [str(a.storage_path or "") for a in attachments if a.storage_path]

    cancel_attachment_jobs(db, attachment_ids)
    delete_attachment_knowledge(db, user_id=uid, attachment_ids=attachment_ids)
    _delete_entry_knowledge(db, user_id=uid, entry_ids=locked_ids)

    if locked_ids:
        db.query(Embedding).filter(Embedding.entry_id.in_(locked_ids)).delete(
            synchronize_session=False
        )

    _scrub_ai_derived_contents(db, user_id=uid, entry_ids=locked_ids)

    # Delete deepest nodes first so ORM parent/child graph stays consistent.
    by_id = {int(e.id): e for e in locked}
    remaining = set(by_id)
    while remaining:
        progress = False
        for eid in sorted(remaining, reverse=True):
            entry = by_id[eid]
            parent_id = int(entry.parent_id) if entry.parent_id is not None else None
            if parent_id in remaining:
                continue
            db.delete(entry)
            remaining.remove(eid)
            progress = True
        if not progress:
            # Cycle or unexpected graph — fall back to deleting all locked rows.
            for eid in sorted(remaining, reverse=True):
                db.delete(by_id[eid])
            break

    return EntryDeletionResult(
        user_id=uid,
        root_entry_id=root_id,
        entry_ids=locked_ids,
        attachment_ids=attachment_ids,
        storage_paths=storage_paths,
    )


def finalize_entry_deletion_side_effects(result: EntryDeletionResult) -> None:
    """Post-commit: files + FAISS. Never raises to caller for file failures."""
    delete_stored_files_best_effort(result.storage_paths)
    try:
        from app.services.vector_search import remove_entry_from_index

        for eid in result.entry_ids:
            try:
                remove_entry_from_index(result.user_id, eid)
            except Exception:
                logger.warning(
                    "entry_vector_remove_failed user_id=%s entry_id=%s",
                    result.user_id,
                    eid,
                )
    except Exception:
        logger.warning(
            "entry_vector_cleanup_unavailable user_id=%s root_entry_id=%s",
            result.user_id,
            result.root_entry_id,
        )

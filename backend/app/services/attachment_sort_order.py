"""
Stable attachment sort_order allocation for concurrent uploads.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Entry, EntryAttachment


def allocate_attachment_sort_order(db: Session, entry_id: int, user_id: int) -> int:
    """Lock entry row and return the next stable sort_order."""
    entry = (
        db.query(Entry)
        .filter(Entry.id == entry_id, Entry.user_id == user_id)
        .with_for_update()
        .first()
    )
    if entry is None:
        raise ValueError("entry_not_found")

    current_max = (
        db.query(func.max(EntryAttachment.sort_order))
        .filter(EntryAttachment.entry_id == entry_id)
        .scalar()
    )
    if current_max is None:
        return 0
    return int(current_max) + 1


def attachment_sort_key(row: EntryAttachment) -> tuple:
    order = row.sort_order if row.sort_order is not None else 0
    created = row.created_at.isoformat() if row.created_at else ""
    return (order, created, int(row.id))
